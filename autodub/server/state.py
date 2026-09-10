"""Trạng thái dùng chung của backend GUI: hàng đợi, tiến độ, khoá luồng.

Tách riêng để mọi module khác (pipeline, render, HTTP handler...) cùng nhìn
vào MỘT bộ trạng thái duy nhất mà không import vòng lẫn nhau. Các dict ở đây
được sửa TẠI CHỖ (mutate), không bao giờ gán lại, nên `from .state import
STATE` ở đâu cũng trỏ về đúng một đối tượng.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Dict, Optional

from ..utils import log, set_cancel_event, set_cancel_event_provider
from .job_manager import JobManager, current_cancel_event as _job_cancel_event

# autodub/server/state.py -> autodub/server -> autodub -> thư mục gốc dự án
HERE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
UI_DIR = os.path.join(HERE, "ui")
CONFIG_PATH = os.path.join(HERE, "config.yaml")

_LOCK = threading.RLock()
_NEXT_ID = [1]
_CANCEL_EVENT = threading.Event()
_DOWNLOAD_SEM = threading.Semaphore(3)  # giới hạn tải đồng thời
set_cancel_event(_CANCEL_EVENT)
JOB_MANAGER = JobManager(os.path.join(HERE, "data", "background_jobs.json"))


def current_cancel_event(resource: str = ""):
    """Cancellation token of this managed job, with legacy fallback."""
    return _job_cancel_event(_CANCEL_EVENT, resource=resource)


set_cancel_event_provider(current_cancel_event)


def submit_job(target, *, name: str, resource: str = "default",
               foreground: bool = True, metadata: Optional[Dict] = None,
               args=(), kwargs=None) -> str:
    # A legacy direct thread may have left this fallback event set. Managed
    # jobs use their own token, so clearing it cannot un-cancel another job.
    _CANCEL_EVENT.clear()
    return JOB_MANAGER.submit(
        target, name=name, resource=resource, foreground=foreground,
        metadata=metadata, args=args, kwargs=kwargs)


def shutdown_background_jobs(wait: bool = True, timeout: float = 12.0) -> bool:
    return JOB_MANAGER.shutdown(wait=wait, timeout=timeout)

STATE: Dict = {
    "queue": [],          # [{id,name,path,status,progress,note}]
    "selected": None,
    "running": False,
    "cancel": False,
    "busy": "",           # việc nền đang chạy (vd "Đang dò vùng sub cứng…")
    "progress": {"pct": 0, "step": "", "detail": "", "sub": 0, "total": 6},
    "log": [],
    "nvenc": None,
    "manual": {
        "rev": 0, "working": False, "status": "Sẵn sàng",
        "audio_path": "", "audio_duration": 0.0,
        "output_path": "", "error": "",
        "script_path": "", "script_title": "", "script_words": 0,
        "recommended_voice": "", "voice_analysis": {},
        "voice_recommendations": [],
        "voice_cast": [], "voice_assignment_coverage": 0.0,
        "voice_cast_path": "",
         "image_pack_path": "", "image_prompt_path": "",
         "image_provider_url": "", "image_scene_count": 0,
         "image_ready_count": 0, "image_prompt_ready": False,
         "image_generation_status": "",
        "source_keyword": "", "source_results": [], "source_catalog": [],
        "source_links": [], "source_videos": [], "source_clips": [],
        "source_status": "", "source_done": 0, "source_total": 0,
        "content_idea_id": "", "content_outline": "", "rewrite_brief": "",
        "youtube_description": "", "youtube_tags": [], "metadata_path": "",
        "calendar_path": "",
        "reference_keyword": "", "reference_results": [],
        "reference_status": "", "cut_status": "", "cut_done": 0, "cut_total": 0,
    },
    "downloads": [],          # [{id, url, status, progress, name, error}]
    "download_active": 0,     # số lượng thread đang tải
    "download_max": 3,        # giới hạn đồng thời (configurable)
    # Công cụ phụ trợ tách hẳn khỏi project lồng tiếng/AI Story. Kết quả chỉ
    # được đưa vào story khi người dùng chủ động bấm nút nhập ở giao diện.
    "video_tools": {
        "rev": 0, "working": False, "active": "", "error": "",
        "download_status": "", "download_pct": 0.0,
        "download_done": 0, "download_total": 0,
        "download_files": [], "download_output_dir": "",
        "search_keyword": "", "search_provider": "all",
        "search_status": "", "search_results": [],
        "cut_status": "", "cut_pct": 0.0,
        "cut_done": 0, "cut_total": 0,
        "cut_sources": [], "cut_files": [], "cut_output_dir": "",
    },
    "content_pipeline": {
        "rev": 0, "working": False, "active": "", "status": "Sẵn sàng",
        "progress": 0.0, "done": 0, "total": 0, "error": "", "warning": "",
        "activity": [], "current_stage": "", "current_item": "",
        "started_at": 0.0, "provider_model": "", "provider_offline": False,
        "ai_success": 0, "ai_failed": 0,
        "input_path": "", "count": 0, "selected_count": 0,
        "provider": "auto", "provider_configured": False,
        "export_xlsx": "", "export_json": "", "output_dir": "",
        "search_keyword": "", "search_source_keys": [],
        "search_results": [], "download_success": 0, "download_failed": 0,
        "download_errors": [], "calendar_path": "",
        "reload_success": 0, "reload_failed": 0, "reload_errors": [],
        "deleted_count": 0, "protected_count": 0,
    },
}

PROJECTS: Dict[int, Dict] = {}
# Số hiệu phiên bản của từng dự án. Giao diện chỉ tải lại DỰ ÁN (có thể nặng vài
# MB với phim dài) khi số này đổi, thay vì kéo về mỗi 1.2 giây -> hết treo.
REV: Dict[int, int] = {}


def bump_rev(job_id: int):
    with _LOCK:
        REV[job_id] = REV.get(job_id, 0) + 1


def _log(msg: str, kind: str = "info"):
    with _LOCK:
        STATE["log"].append({"t": time.time(), "kind": kind, "msg": str(msg)})
        del STATE["log"][:-300]
    log(msg, kind)


def _progress(pct: float = None, step: str = None, detail: str = None,
              sub: int = None):
    with _LOCK:
        p = STATE["progress"]
        if pct is not None:
            p["pct"] = max(0, min(100, round(float(pct), 1)))
        if step is not None:
            p["step"] = step
        if detail is not None:
            p["detail"] = detail
        if sub is not None:
            p["sub"] = sub


def _find(job_id: int) -> Optional[Dict]:
    for j in STATE["queue"]:
        if j["id"] == job_id:
            return j
    return None
