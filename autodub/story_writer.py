"""Cầu nối tới công cụ ``Tạo kịch bản`` đặt cạnh AutoDubVN.

Công cụ viết truyện vẫn chạy độc lập trong một tiến trình riêng. AutoDub nhận
kết quả qua JSON để luôn lấy đúng ``KICH_BAN_DOC.txt`` thay vì đoán từ log hoặc
vô tình đưa cả bản thiết kế/kiểm tra vào TTS.
"""
from __future__ import annotations

import json
import os
import queue
import re
import subprocess
import sys
import threading
import time
import uuid
from typing import Callable, Dict, Optional

from .utils import register_running_process, unregister_running_process

from .server.state import HERE


class StoryWriterError(RuntimeError):
    pass


_PROGRESS_RE = re.compile(r"\((\d+)\s*/\s*(\d+)\)\s*$")


def _resolve_path(value: str, base: str) -> str:
    path = os.path.expandvars(os.path.expanduser(str(value or "").strip().strip('"')))
    if not os.path.isabs(path):
        path = os.path.join(base, path)
    return os.path.abspath(path)


def resolve_settings(cfg: Dict) -> Dict:
    section = cfg.get("tao_kich_ban") if isinstance(cfg.get("tao_kich_ban"), dict) else {}
    default_dir = os.path.abspath(os.path.join(HERE, os.pardir, "Tạo kịch bản"))
    tool_dir = _resolve_path(section.get("tool_dir") or default_dir, HERE)
    python_value = str(section.get("python") or "").strip()
    python_exe = _resolve_path(python_value, tool_dir) if python_value else sys.executable
    timeout_minutes = max(10, int(section.get("timeout_minutes", 180) or 180))
    return {
        "tool_dir": tool_dir,
        "python": python_exe,
        "timeout_seconds": timeout_minutes * 60,
    }


def _validate(settings: Dict) -> str:
    tool_dir = settings["tool_dir"]
    entry = os.path.join(tool_dir, "run.py")
    if not os.path.isfile(entry):
        raise StoryWriterError(
            "Không thấy công cụ Tạo kịch bản tại %s. Kiểm tra tao_kich_ban.tool_dir "
            "trong config.yaml." % tool_dir)
    if not os.path.isfile(settings["python"]):
        raise StoryWriterError("Không thấy Python dùng để tạo kịch bản: %s" % settings["python"])
    return entry


def _append_cta_args(cmd: list, cta: Optional[Dict]) -> None:
    """Chuyển cấu hình lời nhắc kênh của AutoDub sang CLI Tạo kịch bản."""
    if not isinstance(cta, dict):
        return
    if not bool(cta.get("enabled", True)):
        cmd.append("--no-channel-cta")
        return
    positions = cta.get("positions")
    if not isinstance(positions, (list, tuple)):
        positions = [12, 55]
    cleaned = []
    for value in positions:
        try:
            pct = max(5, min(90, int(float(value))))
        except (TypeError, ValueError):
            continue
        if pct not in cleaned:
            cleaned.append(pct)
    for fallback in (12, 55):
        if len(cleaned) >= 2:
            break
        if fallback not in cleaned:
            cleaned.append(fallback)
    cmd.extend(["--cta-positions", ",".join(map(str, cleaned))])
    try:
        speed = max(1.0, min(2.0, float(cta.get("speed", 2.0) or 2.0)))
    except (TypeError, ValueError):
        speed = 2.0
    cmd.extend(["--cta-speed", str(speed)])
    cta_text = str(cta.get("text") or "").strip()
    if cta_text:
        cmd.extend(["--cta-text", cta_text])


def generate(title: str, cfg: Dict, log: Optional[Callable] = None,
             progress: Optional[Callable] = None, cancel_event=None,
             cta: Optional[Dict] = None, rewrite_brief: str = "") -> Dict:
    """Chạy công cụ viết truyện và trả đường dẫn bản chỉ dành cho giọng đọc."""
    title = str(title or "").strip()
    if not title:
        raise StoryWriterError("Hãy nhập tiêu đề truyện.")
    settings = resolve_settings(cfg)
    entry = _validate(settings)
    log = log or (lambda _msg, _kind="info": None)
    progress = progress or (lambda _done, _total, _msg="": None)

    result_dir = os.path.join(HERE, "output", "_tmp", "tao_kich_ban")
    os.makedirs(result_dir, exist_ok=True)
    result_json = os.path.join(result_dir, "result_%s.json" % uuid.uuid4().hex)
    cmd = [settings["python"], "-u", entry, "-t", title,
           "--result-json", result_json]
    brief_path = ""
    brief = str(rewrite_brief or "").strip()
    if brief:
        brief_path = os.path.join(result_dir, "brief_%s.txt" % uuid.uuid4().hex)
        with open(brief_path, "w", encoding="utf-8") as handle:
            # Hồ sơ này do bước phân tích tạo, không chứa toàn văn nguồn. Giới hạn
            # thêm để không vô tình biến CLI thành đường truyền cả truyện gốc.
            handle.write(brief[:16000])
        cmd.extend(["--source-brief-file", brief_path])
    _append_cta_args(cmd, cta)
    creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0) if os.name == "nt" else 0
    try:
        proc = subprocess.Popen(
            cmd, cwd=settings["tool_dir"], stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, text=True, encoding="utf-8", errors="replace",
            bufsize=1, creationflags=creationflags)
    except OSError as exc:
        if brief_path and os.path.exists(brief_path):
            os.remove(brief_path)
        raise StoryWriterError("Không khởi động được công cụ tạo kịch bản: %s" % exc) from exc
    register_running_process(proc, cancel_event=cancel_event, resource="ai")

    lines = queue.Queue()

    def _read_stdout():
        try:
            for line in proc.stdout or ():
                lines.put(line.rstrip())
        finally:
            lines.put(None)

    threading.Thread(target=_read_stdout, daemon=True).start()
    deadline = time.monotonic() + settings["timeout_seconds"]
    reader_done = False
    try:
        while proc.poll() is None or not reader_done:
            if cancel_event is not None and cancel_event.is_set():
                proc.terminate()
                raise StoryWriterError("Đã huỷ tạo kịch bản.")
            if time.monotonic() >= deadline:
                proc.terminate()
                raise StoryWriterError("Tạo kịch bản quá thời gian cho phép.")
            try:
                line = lines.get(timeout=0.25)
            except queue.Empty:
                continue
            if line is None:
                reader_done = True
                continue
            if line:
                kind = "error" if line.startswith("[LOI]") else (
                    "warn" if line.startswith("[!]") else "info")
                log("[Tạo kịch bản] " + line, kind)
                match = _PROGRESS_RE.search(line)
                if match:
                    progress(int(match.group(1)), int(match.group(2)), line)
        code = proc.wait()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait(timeout=5)
        if proc.stdout is not None:
            proc.stdout.close()
        if brief_path and os.path.exists(brief_path):
            os.remove(brief_path)
        unregister_running_process(proc)

    if code != 0:
        raise StoryWriterError("Công cụ tạo kịch bản kết thúc với mã lỗi %d." % code)
    if not os.path.isfile(result_json):
        raise StoryWriterError("Công cụ đã chạy xong nhưng không trả file kết quả JSON.")
    try:
        try:
            with open(result_json, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except (OSError, ValueError) as exc:
            raise StoryWriterError("Không đọc được kết quả tạo kịch bản: %s" % exc) from exc
    
        results = payload.get("results") if isinstance(payload, dict) else None
        item = results[0] if isinstance(results, list) and results else None
        if not isinstance(item, dict):
            raise StoryWriterError("Kết quả tạo kịch bản không đúng định dạng.")
        if item.get("error"):
            raise StoryWriterError(str(item["error"]))
        folder = os.path.abspath(str(item.get("folder") or ""))
        script_path = os.path.join(folder, "KICH_BAN_DOC.txt")
        if not os.path.isfile(script_path):
            raise StoryWriterError("Không thấy bản dành cho giọng đọc: %s" % script_path)
        return {
            "title": str(item.get("title") or title),
            "folder": folder,
            "script_path": script_path,
            "design_path": (os.path.join(folder, "00_ban_thiet_ke.txt")
                            if os.path.isfile(os.path.join(folder, "00_ban_thiet_ke.txt")) else ""),
            "words": int(item.get("words") or 0),
            "meta": item.get("meta") if isinstance(item.get("meta"), dict) else {},
            "result_json": result_json,
        }
    finally:
        if os.path.exists(result_json):
            os.remove(result_json)
