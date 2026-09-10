"""Bounded background jobs shared by every GUI API.

The worker threads are daemon threads on purpose: the desktop app can still
close if a third-party network library ignores cancellation.  ``shutdown``
first signals every job and joins workers for a bounded amount of time, so the
normal path is graceful without bringing back the old collection of ad-hoc
daemon threads.
"""
from __future__ import annotations

import json
import os
import queue
import threading
import time
import uuid
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, Optional


RESOURCE_LIMITS = {
    "ffmpeg": 1,
    "network": 3,
    "ai": 3,
    "sqlite": 1,
    "default": 2,
}


class JobQueueFull(RuntimeError):
    pass


class _BoundedExecutor:
    """Small Executor-compatible pool with daemon workers and bounded queue."""

    _STOP = object()

    def __init__(self, max_workers: int, name: str, max_queue: int = 256):
        self.max_workers = max(1, int(max_workers))
        self.name = str(name)
        self._queue: queue.Queue = queue.Queue(maxsize=max(1, int(max_queue)))
        self._lock = threading.RLock()
        self._threads: list[threading.Thread] = []
        self._shutdown = False

    def _ensure_workers(self) -> None:
        with self._lock:
            if self._threads:
                return
            for index in range(self.max_workers):
                worker = threading.Thread(
                    target=self._worker,
                    name=f"autodub-{self.name}-{index + 1}",
                    daemon=True,
                )
                self._threads.append(worker)
                worker.start()

    def submit(self, fn: Callable, /, *args, **kwargs) -> Future:
        with self._lock:
            if self._shutdown:
                raise RuntimeError(f"Executor {self.name} đã đóng.")
            self._ensure_workers()
            future = Future()
            try:
                self._queue.put_nowait((future, fn, args, kwargs))
            except queue.Full as exc:
                raise JobQueueFull(
                    f"Hàng đợi {self.name} đã đầy; hãy chờ tác vụ hiện tại xong."
                ) from exc
            return future

    def _worker(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is self._STOP:
                    return
                future, fn, args, kwargs = item
                if not future.set_running_or_notify_cancel():
                    continue
                try:
                    result = fn(*args, **kwargs)
                except BaseException as exc:
                    future.set_exception(exc)
                else:
                    future.set_result(result)
            finally:
                self._queue.task_done()

    def shutdown(self, wait: bool = True, cancel_futures: bool = True,
                 timeout: float = 10.0) -> bool:
        with self._lock:
            if not self._shutdown:
                self._shutdown = True
                if cancel_futures:
                    while True:
                        try:
                            item = self._queue.get_nowait()
                        except queue.Empty:
                            break
                        try:
                            if item is not self._STOP:
                                item[0].cancel()
                        finally:
                            self._queue.task_done()
                for _ in self._threads:
                    try:
                        self._queue.put_nowait(self._STOP)
                    except queue.Full:
                        self._queue.put(self._STOP)
            threads = list(self._threads)
        if not wait:
            return not any(thread.is_alive() for thread in threads)
        deadline = time.monotonic() + max(0.0, float(timeout))
        for thread in threads:
            remaining = max(0.0, deadline - time.monotonic())
            thread.join(remaining)
        return not any(thread.is_alive() for thread in threads)


@dataclass
class _Job:
    id: str
    name: str
    resource: str
    foreground: bool
    metadata: Dict[str, Any]
    cancel_event: threading.Event = field(default_factory=threading.Event)
    status: str = "queued"
    submitted_at: float = field(default_factory=time.time)
    started_at: float = 0.0
    finished_at: float = 0.0
    error: str = ""
    future: Optional[Future] = None

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "resource": self.resource,
            "foreground": self.foreground,
            "metadata": dict(self.metadata),
            "status": self.status,
            "submitted_at": self.submitted_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "error": self.error,
        }


_LOCAL = threading.local()
_ACTIVE_LOCK = threading.RLock()
_ACTIVE_BY_RESOURCE: Dict[str, set[threading.Event]] = {}


def current_job_id() -> str:
    return str(getattr(_LOCAL, "job_id", "") or "")


def current_cancel_event(default=None, resource: str = ""):
    local = getattr(_LOCAL, "cancel_event", None)
    if local is not None:
        return local
    if resource:
        with _ACTIVE_LOCK:
            active = list(_ACTIVE_BY_RESOURCE.get(resource) or ())
        # Nested ThreadPoolExecutor workers do not inherit thread-local state.
        # A resource-limited pool (especially ffmpeg=1) still has an
        # unambiguous owner, so subprocess cancellation remains job-scoped.
        if len(active) == 1:
            return active[0]
    return default


class JobManager:
    def __init__(self, persist_path: str,
                 resource_limits: Optional[Dict[str, int]] = None,
                 history_limit: int = 200):
        self.persist_path = os.path.abspath(persist_path)
        self.resource_limits = dict(RESOURCE_LIMITS)
        self.resource_limits.update(resource_limits or {})
        self.history_limit = max(20, int(history_limit))
        self._lock = threading.RLock()
        self._jobs: Dict[str, _Job] = {}
        self._restored: list[Dict[str, Any]] = []
        self._executors = {
            name: _BoundedExecutor(limit, name)
            for name, limit in self.resource_limits.items()
        }
        self._shutdown = False
        self._restore_history()

    def _restore_history(self) -> None:
        try:
            with open(self.persist_path, "r", encoding="utf-8") as handle:
                payload = json.load(handle)
        except (OSError, ValueError, TypeError):
            return
        rows = payload.get("jobs") if isinstance(payload, dict) else []
        if not isinstance(rows, list):
            return
        now = time.time()
        for raw in rows[-self.history_limit:]:
            if not isinstance(raw, dict):
                continue
            item = dict(raw)
            if item.get("status") in {"queued", "running", "cancelling"}:
                item["status"] = "interrupted"
                item["finished_at"] = now
                item["error"] = "Ứng dụng đã đóng trước khi tác vụ hoàn tất."
            self._restored.append(item)
        self._persist()

    def _history_rows(self) -> list[Dict[str, Any]]:
        with self._lock:
            current = [job.public() for job in self._jobs.values()]
            rows = (self._restored + current)[-self.history_limit:]
        return rows

    def _persist(self) -> None:
        rows = self._history_rows()
        target = self.persist_path
        temp = target + ".tmp"
        try:
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(temp, "w", encoding="utf-8") as handle:
                json.dump({"version": 1, "jobs": rows}, handle,
                          ensure_ascii=False, indent=2)
            os.replace(temp, target)
        except OSError:
            try:
                if os.path.exists(temp):
                    os.remove(temp)
            except OSError:
                pass

    def submit(self, target: Callable, *, name: str,
               resource: str = "default", foreground: bool = True,
               metadata: Optional[Dict[str, Any]] = None,
               args: Iterable = (), kwargs: Optional[Dict[str, Any]] = None) -> str:
        resource = resource if resource in self._executors else "default"
        with self._lock:
            if self._shutdown:
                raise RuntimeError("Trình quản lý tác vụ đang đóng.")
            job_id = uuid.uuid4().hex[:16]
            job = _Job(
                id=job_id,
                name=str(name or getattr(target, "__name__", "background job")),
                resource=resource,
                foreground=bool(foreground),
                metadata=dict(metadata or {}),
            )
            self._jobs[job_id] = job

        def run_job():
            _LOCAL.job_id = job.id
            _LOCAL.cancel_event = job.cancel_event
            with _ACTIVE_LOCK:
                _ACTIVE_BY_RESOURCE.setdefault(job.resource, set()).add(
                    job.cancel_event)
            with self._lock:
                if job.cancel_event.is_set():
                    job.status = "cancelled"
                    job.finished_at = time.time()
                    self._persist()
                    _LOCAL.job_id = ""
                    _LOCAL.cancel_event = None
                    with _ACTIVE_LOCK:
                        active = _ACTIVE_BY_RESOURCE.get(job.resource)
                        if active is not None:
                            active.discard(job.cancel_event)
                            if not active:
                                _ACTIVE_BY_RESOURCE.pop(job.resource, None)
                    return None
                job.status = "running"
                job.started_at = time.time()
                self._persist()
            try:
                result = target(*(tuple(args)), **dict(kwargs or {}))
                with self._lock:
                    job.status = "cancelled" if job.cancel_event.is_set() else "completed"
                return result
            except InterruptedError:
                with self._lock:
                    job.status = "cancelled"
                return None
            except BaseException as exc:
                with self._lock:
                    job.status = "failed"
                    job.error = str(exc)[:500]
                raise
            finally:
                with self._lock:
                    job.finished_at = time.time()
                    self._persist()
                _LOCAL.job_id = ""
                _LOCAL.cancel_event = None
                with _ACTIVE_LOCK:
                    active = _ACTIVE_BY_RESOURCE.get(job.resource)
                    if active is not None:
                        active.discard(job.cancel_event)
                        if not active:
                            _ACTIVE_BY_RESOURCE.pop(job.resource, None)

        try:
            future = self._executors[resource].submit(run_job)
        except Exception:
            with self._lock:
                self._jobs.pop(job_id, None)
                self._persist()
            raise
        job.future = future
        self._persist()
        return job_id

    def get_cancel_event(self, job_id: str):
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
            return job.cancel_event if job else None

    def cancel(self, job_id: str) -> bool:
        with self._lock:
            job = self._jobs.get(str(job_id or ""))
            if not job or job.status not in {"queued", "running", "cancelling"}:
                return False
            job.cancel_event.set()
            job.status = "cancelling" if job.status == "running" else "cancelled"
            if job.future is not None and job.future.cancel():
                job.status = "cancelled"
                job.finished_at = time.time()
            self._persist()
            return True

    def cancel_foreground(self, metadata_key: str = "", metadata_value: Any = None
                          ) -> list[tuple[str, threading.Event]]:
        with self._lock:
            candidates = [
                job for job in self._jobs.values()
                if job.foreground and job.status in {"queued", "running", "cancelling"}
            ]
            if metadata_key:
                exact = [job for job in candidates
                         if job.metadata.get(metadata_key) == metadata_value]
                if exact:
                    candidates = exact
            candidates.sort(key=lambda item: item.submitted_at, reverse=True)
            # Without a specific queue item, preserve the old Cancel button's
            # meaning: cancel the most recently started foreground operation.
            candidates = candidates[:1]
        cancelled = []
        for job in candidates:
            if self.cancel(job.id):
                cancelled.append((job.id, job.cancel_event))
        return cancelled

    def snapshot(self, active_only: bool = False, limit: int = 30) -> list[Dict[str, Any]]:
        rows = self._history_rows()
        if active_only:
            rows = [row for row in rows
                    if row.get("status") in {"queued", "running", "cancelling"}]
        return rows[-max(1, int(limit)):]

    def shutdown(self, wait: bool = True, timeout: float = 12.0) -> bool:
        with self._lock:
            if self._shutdown:
                return True
            self._shutdown = True
            active = [job for job in self._jobs.values()
                      if job.status in {"queued", "running", "cancelling"}]
        for job in active:
            job.cancel_event.set()
            if job.future is not None:
                job.future.cancel()
        deadline = time.monotonic() + max(0.0, float(timeout))
        ok = True
        for executor in self._executors.values():
            remaining = max(0.0, deadline - time.monotonic())
            ok = executor.shutdown(wait=wait, cancel_futures=True,
                                   timeout=remaining) and ok
        with self._lock:
            now = time.time()
            for job in active:
                if job.status in {"queued", "running", "cancelling"}:
                    job.status = "cancelled" if job.cancel_event.is_set() else "interrupted"
                    job.finished_at = now
            self._persist()
        return ok
