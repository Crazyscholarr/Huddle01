"""API cho hai công cụ phụ trợ độc lập: tải video Audio và cắt hàng loạt.

Module này không sửa ``STATE['manual']`` hay project lồng tiếng. Giao diện chỉ
đưa file kết quả vào AI Story khi người dùng bấm nút nhập rõ ràng.
"""
from __future__ import annotations

import os
import re
import time
from typing import Dict, List, Tuple

from .config_api import _load_cfg
from .state import (HERE, STATE, _LOCK, current_cancel_event, submit_job,
                    _log, _progress)


JsonResult = Tuple[Dict, int]


def _paths(value) -> List[str]:
    if isinstance(value, str):
        value = re.split(r"[\r\n]+", value)
    if not isinstance(value, list):
        return []
    out, seen = [], set()
    for item in value:
        raw = item.get("path") if isinstance(item, dict) else item
        path = str(raw or "").strip().strip('"')
        if path and path not in seen:
            seen.add(path)
            out.append(path)
    return out


def _links(value) -> List[str]:
    if isinstance(value, str):
        value = re.split(r"[\r\n]+", value)
    if not isinstance(value, list):
        return []
    from ..downloader import extract_url
    out, seen = [], set()
    for item in value:
        raw = item.get("url") if isinstance(item, dict) else item
        url = extract_url(str(raw or ""))
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


def _output_dir(raw, group: str) -> str:
    value = str(raw or "").strip().strip('"')
    if value:
        path = os.path.abspath(os.path.expandvars(value))
    else:
        stamp = time.strftime("%Y%m%d_%H%M%S")
        path = os.path.join(HERE, "downloads", group, stamp)
    os.makedirs(path, exist_ok=True)
    return path


def _update(**values) -> None:
    with _LOCK:
        state = STATE["video_tools"]
        state.update(values)
        state["rev"] = int(state.get("rev", 0)) + 1


def _begin(kind: str, total: int, output_dir: str) -> JsonResult | None:
    with _LOCK:
        tools = STATE["video_tools"]
        if STATE["running"] or STATE["busy"] or tools.get("working"):
            return {"error": "Đang bận: " + (STATE["busy"] or "đang xử lý")}, 409
        STATE["cancel"] = False
        STATE["running"] = True
        STATE["busy"] = ("Đang tải video nền cho Audio…" if kind == "download"
                         else "Đang cắt video hàng loạt…")
        tools.update({"working": True, "active": kind, "error": ""})
        if kind == "download":
            tools.update({"download_status": "Đang chuẩn bị tải…",
                          "download_pct": 0.0, "download_done": 0,
                          "download_total": total,
                          "download_output_dir": output_dir})
        else:
            tools.update({"cut_status": "Đang chuẩn bị cắt…",
                          "cut_pct": 0.0, "cut_done": 0,
                          "cut_total": total, "cut_output_dir": output_dir})
        tools["rev"] = int(tools.get("rev", 0)) + 1
    return None


def _end() -> None:
    with _LOCK:
        tools = STATE["video_tools"]
        tools.update({"working": False, "active": ""})
        tools["rev"] = int(tools.get("rev", 0)) + 1
        STATE["running"] = False
        STATE["busy"] = ""


def api_search_videos(b: Dict) -> JsonResult:
    """Tìm video nền trong khu phụ trợ, không ghi kết quả sang AI Story."""
    keyword = str(b.get("keyword") or "").strip()
    if not keyword:
        return {"error": "Hãy nhập từ khóa tìm video nền."}, 400
    try:
        limit = max(1, min(50, int(b.get("limit", 10) or 10)))
    except (TypeError, ValueError):
        limit = 10
    provider = str(b.get("provider") or "all").strip().lower()
    if provider not in {"all", "bilibili", "youtube"}:
        provider = "all"
    with _LOCK:
        if STATE["video_tools"].get("working"):
            return {"error": "Công cụ Video đang xử lý tác vụ khác."}, 409
    _update(search_keyword=keyword, search_provider=provider, error="",
            search_status="Đang tìm video nền…", search_results=[])
    try:
        from .. import story_sources
        cfg = _load_cfg().get("download", {}) or {}
        rows = story_sources.search(
            keyword, limit=limit, provider=provider,
            cookies_from_browser=cfg.get("cookies_from_browser"),
            cookies_file=cfg.get("cookies_file"))
        _update(search_results=rows,
                search_status="Tìm thấy %d video" % len(rows))
        return {"ok": True, "keyword": keyword, "provider": provider,
                "results": rows}, 200
    except Exception as exc:
        message = "Tìm video nền thất bại: %s" % exc
        _update(search_status=message, error=str(exc)[:300])
        return {"error": message}, 500


def api_download_videos(b: Dict) -> JsonResult:
    """Tải nhiều link vào kho video nền Audio, không chạm AI Story."""
    links = _links(b.get("links", b.get("urls", [])))
    if not links:
        return {"error": "Hãy dán ít nhất một link video hợp lệ."}, 400
    out_dir = _output_dir(b.get("output_dir"), "audio_background")
    blocked = _begin("download", len(links), out_dir)
    if blocked:
        return blocked
    _update(download_files=[], download_status="Đang kết nối nguồn video…")

    def _work(urls=list(links), target=out_dir, payload=dict(b)):
        try:
            from .. import story_sources
            cfg = _load_cfg().get("download", {}) or {}
            quality = str(payload.get("quality") or cfg.get("quality") or "best")

            def progress(done, total, message):
                pct = done * 100.0 / max(1, total)
                _update(download_done=done, download_total=total,
                        download_pct=pct,
                        download_status=f"{done}/{total}: {message}")
                _progress(pct=pct, step="Tải video nền Audio", detail=message)

            def live_progress(pct, detail):
                _update(download_pct=float(pct),
                        download_status=str(detail)[:280])
                _progress(pct=float(pct), step="Tải video nền Audio",
                          detail=str(detail)[:180])

            rows = story_sources.download_many(
                urls, target, quality=quality,
                cookies_from_browser=cfg.get("cookies_from_browser"),
                cookies_file=cfg.get("cookies_file"),
                concurrent_fragments=int(cfg.get("concurrent_fragments", 8) or 8),
                external_downloader=cfg.get("external_downloader", "auto"),
                progress=progress, live_progress=live_progress,
                max_workers=max(1, min(3, int(payload.get("workers", 3) or 3))))
            files = [os.path.abspath(row["path"]) for row in rows
                     if row.get("path") and os.path.isfile(row["path"])]
            if not files:
                raise RuntimeError(
                    "Không tải được video nào; xem log thời gian để biết lỗi từng link.")
            _update(download_files=files, download_done=len(files),
                    download_total=len(urls), download_pct=100.0,
                    download_status=f"Đã tải {len(files)}/{len(urls)} video")
            _progress(pct=100, step="Tải video nền Audio xong",
                      detail=f"{len(files)} file sẵn sàng")
            _log(f"Công cụ tải video Audio hoàn tất: {len(files)} file.", "ok")
        except Exception as exc:
            cancelled = current_cancel_event().is_set()
            message = ("Đã dừng tải; file hoàn tất vẫn được giữ lại."
                       if cancelled else f"Tải video Audio lỗi: {exc}")
            _update(download_status=message, error="" if cancelled else str(exc)[:300])
            _log(message, "warn" if cancelled else "err")
        finally:
            _end()

    submit_job(_work, name="Tải video cho công cụ Audio", resource="network",
               metadata={"kind": "video_tools_download"})
    return {"ok": True, "async": True, "total": len(links),
            "output_dir": out_dir}, 200


def api_cut_videos(b: Dict) -> JsonResult:
    """Cắt đồng thời một danh sách nhiều video thành các đoạn nhỏ."""
    raw_paths = _paths(b.get("paths", b.get("video_sources", [])))
    if not raw_paths:
        return {"error": "Hãy chọn một hoặc nhiều video cần cắt."}, 400
    from .. import story_sources
    sources = story_sources._video_files(raw_paths)
    if not sources:
        return {"error": "Danh sách không có file video hợp lệ."}, 400
    try:
        lo = max(2.0, float(b.get("min_seconds", 300) or 300))
        hi = max(lo, float(b.get("max_seconds", 600) or 600))
    except (TypeError, ValueError):
        return {"error": "Thời lượng đoạn cắt không hợp lệ."}, 400
    out_dir = _output_dir(b.get("output_dir"), "video_segments")
    blocked = _begin("cut", len(sources), out_dir)
    if blocked:
        return blocked
    _update(cut_sources=sources, cut_files=[],
            cut_status=f"Đã nhận {len(sources)} video; đang chuẩn bị FFmpeg…")

    def _work(source_paths=list(sources), target=out_dir,
              min_s=lo, max_s=hi):
        try:
            prefix = time.strftime("%Y%m%d_%H%M%S_")

            def progress(done, total, message):
                pct = done * 100.0 / max(1, total)
                _update(cut_done=done, cut_total=total, cut_pct=pct,
                        cut_status=f"{done}/{total}: {message}")
                _progress(pct=pct, step="Cắt video hàng loạt", detail=message)

            clips = story_sources.cut_video_segments(
                source_paths, target, min_seconds=min_s, max_seconds=max_s,
                progress=progress, filename_prefix=prefix)
            clips = [os.path.abspath(path) for path in clips if os.path.isfile(path)]
            _update(cut_files=clips, cut_done=len(source_paths),
                    cut_total=len(source_paths), cut_pct=100.0,
                    cut_status=f"Đã cắt xong {len(clips)} đoạn từ {len(source_paths)} video")
            _progress(pct=100, step="Cắt video hàng loạt xong",
                      detail=f"{len(clips)} clip sẵn sàng")
            _log(f"Công cụ cắt hàng loạt hoàn tất: {len(clips)} clip.", "ok")
        except Exception as exc:
            cancelled = current_cancel_event().is_set()
            partial = story_sources._video_files([target])
            message = (f"Đã dừng cắt; giữ lại {len(partial)} clip đã tạo."
                       if cancelled else f"Cắt video hàng loạt lỗi: {exc}")
            _update(cut_files=partial, cut_status=message,
                    error="" if cancelled else str(exc)[:300])
            _log(message, "warn" if cancelled else "err")
        finally:
            _end()

    submit_job(_work, name="Cắt video hàng loạt", resource="ffmpeg",
               metadata={"kind": "video_tools_cut"})
    return {"ok": True, "async": True, "total": len(sources),
            "output_dir": out_dir}, 200
