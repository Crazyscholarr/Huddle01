"""HTTP server nội bộ + routing cho giao diện.

Handler chỉ làm việc nhận request / trả JSON; logic thật nằm ở các module
khác (pipeline, render, projects, manual_api...). Endpoint nào chạy lâu đều
được đẩy sang thread nền để giao diện không bị đơ.
"""
from __future__ import annotations

import json
import copy
import mimetypes
import os
import re
import shutil
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict, Optional

from .. import detect
from ..utils import log, has_nvenc, cancel_running_processes
from .state import (HERE, UI_DIR, STATE, PROJECTS, REV,
                    _LOCK, _NEXT_ID, _CANCEL_EVENT, _DOWNLOAD_SEM, JOB_MANAGER,
                    submit_job, shutdown_background_jobs,
                    bump_rev, _log, _progress, _find)
from .helpers import _cleanup_temp_files
from .config_api import (_load_cfg, _translation_cfg_for_gui,
                         _tts_cfg_for_gui, _save_translation_cfg,
                         _test_translation_api)
from .projects import get_project, _save_project_state
from .render import render_preview
from .pipeline import run_pipeline
from . import manual_api
from . import video_tools_api
from . import content_api


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def handle_one_request(self):
        """Ngắt kết nối giữa chừng là chuyện thường với video - đừng nổ traceback."""
        try:
            super().handle_one_request()
        except (ConnectionAbortedError, ConnectionResetError,
                BrokenPipeError, TimeoutError):
            self.close_connection = True

    # ---------------- helpers ----------------
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self) -> Dict:
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except Exception:
            return {}

    def _file(self, path: str, ctype: Optional[str] = None):
        if not os.path.exists(path):
            self._json({"error": "not found"}, 404)
            return
        ctype = ctype or (mimetypes.guess_type(path)[0] or "application/octet-stream")
        size = os.path.getsize(path)
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            start = int(m.group(1)) if m and m.group(1) else 0
            end = int(m.group(2)) if m and m.group(2) else size - 1
            end = min(end, size - 1)
            start = max(0, min(start, end))
            length = end - start + 1
            self.send_response(206)
            self.send_header("Content-Type", ctype)
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            self.send_header("Content-Length", str(length))
            self.end_headers()
            with open(path, "rb") as f:
                f.seek(start)
                remain = length
                while remain > 0:
                    chunk = f.read(min(262144, remain))
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                    except (BrokenPipeError, ConnectionResetError,
                            ConnectionAbortedError, OSError):
                        return   # trình duyệt huỷ khi tua - chuyện bình thường
                    remain -= len(chunk)
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(size))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        try:
            with open(path, "rb") as f:
                shutil.copyfileobj(f, self.wfile)
        except (BrokenPipeError, ConnectionResetError,
                ConnectionAbortedError, OSError):
            return

    # ---------------- GET ----------------
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        q = urllib.parse.parse_qs(u.query)
        p = u.path

        if p in ("/", "/index.html"):
            return self._file(os.path.join(UI_DIR, "index.html"), "text/html; charset=utf-8")

        # File tĩnh của giao diện (style.css, app.js...) - chỉ cho phép tên
        # file phẳng nằm NGAY TRONG thư mục ui/, chặn mọi kiểu ../ lách ra ngoài.
        if re.fullmatch(r"/[A-Za-z0-9_\-.]+\.(css|js|svg|png|woff2?)", p):
            return self._file(os.path.join(UI_DIR, p.lstrip("/")))

        if p == "/api/local_image":
            # Thumbnail cho kho ảnh chế độ Kể chuyện: webview không đọc được
            # file:// nên phải phát qua server. Chỉ phục vụ đúng file ảnh.
            raw = urllib.parse.unquote(q.get("path", [""])[0] or "")
            ext = os.path.splitext(raw)[1].lower()
            if ext not in {".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif",
                           ".tif", ".tiff"}:
                return self._json({"error": "chỉ phục vụ file ảnh"}, 400)
            if not os.path.isfile(raw):
                return self._json({"error": "không thấy ảnh"}, 404)
            return self._file(raw)

        if p == "/api/local_video":
            # Xem trước video người dùng vừa chọn trong khung Kể chuyện.
            raw = urllib.parse.unquote(q.get("path", [""])[0] or "")
            ext = os.path.splitext(raw)[1].lower()
            if ext not in {".mp4", ".mkv", ".webm", ".mov", ".avi", ".flv",
                           ".ts", ".m4v"}:
                return self._json({"error": "chỉ phục vụ file video"}, 400)
            if not os.path.isfile(raw):
                return self._json({"error": "không thấy video"}, 404)
            return self._file(raw)

        if p == "/api/state":
            # Phải THẬT NHẸ: giao diện gọi mỗi 1.2 giây. Không trả nội dung dự án
            # ở đây, chỉ trả số hiệu phiên bản để bên kia biết có cần tải lại không.
            with _LOCK:
                if STATE["nvenc"] is None:
                    STATE["nvenc"] = has_nvenc()
                sel = STATE["selected"]
                pr = PROJECTS.get(sel) if sel else None
                return self._json({
                    "queue": STATE["queue"], "selected": sel,
                    "running": STATE["running"], "busy": STATE["busy"],
                    "progress": STATE["progress"], "nvenc": STATE["nvenc"],
                    "rev": REV.get(sel, 0) if sel else 0,
                    "seg_count": len(pr.get("segments", [])) if pr else 0,
                    "log": STATE["log"][-8:],
                    "manual": dict(STATE.get("manual") or {}),
                    "video_tools": copy.deepcopy(STATE.get("video_tools") or {}),
                    "content_pipeline": copy.deepcopy(
                        STATE.get("content_pipeline") or {}),
                    "background_jobs": JOB_MANAGER.snapshot(active_only=True),
                })

        if p == "/api/content/list":
            return self._json(*content_api.api_content_list(q))

        if p == "/api/content/item":
            return self._json(*content_api.api_content_item(q))

        if p == "/api/content/catalog":
            return self._json(*content_api.api_content_catalog(q))

        if p == "/api/story/generated_script":
            return self._json(*manual_api.api_story_generated_script())

        if p == "/api/story/image_pack/latest":
            return self._json(*manual_api.api_story_image_pack_latest())

        if p == "/api/config":
            try:
                return self._json({"translation": _translation_cfg_for_gui(),
                                   "tts": _tts_cfg_for_gui()})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if p == "/api/project":
            jid = int(q.get("id", [0])[0] or 0)
            pr = get_project(jid)
            return self._json(pr or {"error": "no project"},
                              200 if pr else 404)

        if p == "/api/video":
            jid = int(q.get("id", [0])[0] or 0)
            j = _find(jid)
            if not j:
                return self._json({"error": "no job"}, 404)
            return self._file(j["path"])

        if p == "/api/preview":
            jid = int(q.get("id", [0])[0] or 0)
            t = float(q.get("t", ["0"])[0] or 0)
            try:
                out = os.path.join(HERE, "output", "_preview", f"p{jid}.png")
                os.makedirs(os.path.dirname(out), exist_ok=True)
                render_preview(jid, t, out)
                return self._file(out, "image/png")
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if p == "/api/voices":
            eng = (q.get("engine", ["edge"])[0] or "edge").lower()
            try:
                from .. import tts as tts_mod
                return self._json({"engine": eng,
                                   "voices": tts_mod.list_voices(eng)})
            except Exception as e:
                return self._json({"engine": eng, "voices": [],
                                   "error": str(e)[:200]})

        if p == "/api/nghe_thu":
            try:
                info = manual_api.tao_ban_nghe_thu(
                    engine=q.get("engine", [""])[0],
                    voice=q.get("voice", [""])[0],
                    pitch=q.get("pitch", [""])[0],
                    rate=q.get("rate", [""])[0],
                    text=q.get("text", [""])[0])
                return self._file(info["path"], "audio/mpeg")
            except manual_api.DangBan as e:
                return self._json({"error": str(e)}, 409)
            except Exception as e:
                log(f"Nghe thử giọng lỗi: {e}", "warn")
                return self._json({"error": str(e)[:300]}, 500)

        if p == "/api/manual/audio":
            with _LOCK:
                path = str((STATE.get("manual") or {}).get("audio_path") or "")
            if not path or not os.path.isfile(path):
                return self._json({"error": "Chưa có file âm thanh."}, 404)
            return self._file(path)

        if p == "/api/manual/output":
            with _LOCK:
                path = str((STATE.get("manual") or {}).get("output_path") or "")
            if not path or not os.path.isfile(path):
                return self._json({"error": "Chưa có video kết quả."}, 404)
            return self._file(path)

        if p == "/api/nhac_nen":
            try:
                from .. import nhac_nen as nn
                return self._json({"thu_muc": nn.thu_muc_nhac(),
                                   "bai": nn.liet_ke_nhac_co_san()})
            except Exception as e:
                return self._json({"error": str(e)[:200]}, 500)

        if p == "/api/browse":
            # liệt kê video trong thư mục để chọn nhanh
            d = q.get("dir", [""])[0]
            try:
                items = []
                if d and os.path.isdir(d):
                    for name in sorted(os.listdir(d)):
                        fp = os.path.join(d, name)
                        if os.path.isdir(fp):
                            items.append({"name": name, "path": fp, "dir": True})
                        elif name.lower().endswith(
                                (".mp4", ".mkv", ".mov", ".avi", ".ts", ".webm", ".flv")):
                            items.append({"name": name, "path": fp, "dir": False,
                                          "size": os.path.getsize(fp)})
                return self._json({"dir": d, "items": items})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        return self._json({"error": "unknown endpoint"}, 404)

    # ---------------- POST ----------------
    def do_POST(self):
        p = urllib.parse.urlparse(self.path).path
        b = self._body()

        if p == "/api/test_translation":
            try:
                tr = b.get("translation") if isinstance(b, dict) else {}
                return self._json(_test_translation_api(
                    tr if isinstance(tr, dict) else {}))
            except Exception as e:
                return self._json({"ok": False, "error": str(e)}, 400)

        if p == "/api/content/import":
            return self._json(*content_api.api_content_import(b))

        if p == "/api/content/sample":
            return self._json(*content_api.api_content_sample(b))

        if p == "/api/content/search":
            return self._json(*content_api.api_content_search(b))

        if p == "/api/content/download":
            return self._json(*content_api.api_content_download(b))

        if p == "/api/content/analyze":
            return self._json(*content_api.api_content_analyze(b))

        if p == "/api/content/update":
            return self._json(*content_api.api_content_update(b))

        if p == "/api/content/select":
            return self._json(*content_api.api_content_select(b))

        if p == "/api/content/delete":
            return self._json(*content_api.api_content_delete(b))

        if p == "/api/content/reload":
            return self._json(*content_api.api_content_reload(b))

        if p == "/api/content/export":
            return self._json(*content_api.api_content_export(b))

        if p == "/api/content/use_story":
            return self._json(*content_api.api_content_use_story(b))

        if p == "/api/config":
            try:
                tr = b.get("translation") if isinstance(b, dict) else {}
                if not isinstance(tr, dict):
                    return self._json({"error": "translation must be an object"}, 400)
                return self._json({"ok": True,
                                   "translation": _save_translation_cfg(tr)})
            except Exception as e:
                return self._json({"error": str(e)}, 400)

        if p == "/api/queue/add":
            path = (b.get("path") or "").strip().strip('"')
            raw_url = (b.get("url") or "").strip()
            if raw_url:
                from ..downloader import extract_url
                url = extract_url(raw_url)
                if not url:
                    return self._json({"error": "Không thấy URL hợp lệ. Hãy dán riêng link bắt đầu bằng http:// hoặc https://."}, 400)
            else:
                url = ""
            try:
                if url:
                    with _LOCK:
                        jid = _NEXT_ID[0]
                        _NEXT_ID[0] += 1
                        job = {"id": jid, "name": url[:60] + "…",
                               "path": "", "status": "chờ tải",
                               "note": "Đang chờ slot tải…",
                               "progress": 0, "source_url": url}
                        STATE["queue"].append(job)
                        STATE["selected"] = jid

                    def _download_worker(job_id=jid, src_url=url):
                        # Cập nhật trạng thái khi bắt đầu chờ semaphore
                        with _LOCK:
                            j = _find(job_id)
                            if j:
                                j.update({"status": "chờ tải",
                                          "note": "Đang chờ slot tải…"})
                        _DOWNLOAD_SEM.acquire()
                        try:
                            with _LOCK:
                                j = _find(job_id)
                                if not j:
                                    return
                                j.update({"status": "đang tải",
                                          "note": "Đang chuẩn bị bộ tải video",
                                          "progress": 5})
                                STATE["download_active"] = STATE.get("download_active", 0) + 1
                            _log(f"Đang tải: {src_url}", "step")
                            from ..downloader import download_video
                            cfg = _load_cfg().get("download", {})

                            def _download_progress(info):
                                pct = info.get("percent")
                                note = str(info.get("text") or "Đang tải video…")
                                with _LOCK:
                                    active_job = _find(job_id)
                                    if not active_job:
                                        return
                                    if pct is not None:
                                        # DASH tải hình rồi tới tiếng nên % của
                                        # luồng sau có thể về 0; giữ thanh tổng
                                        # thể không chạy lùi, chi tiết vẫn ghi
                                        # đúng luồng/%/tốc độ/ETA hiện tại.
                                        active_job["progress"] = max(
                                            float(active_job.get("progress") or 5),
                                            min(99.0, float(pct)))
                                    active_job["note"] = note[:220]

                            downloaded = download_video(
                                src_url, os.path.join(HERE, "downloads"),
                                quality=cfg.get("quality", "best"),
                                cookies_from_browser=cfg.get("cookies_from_browser"),
                                cookies_file=cfg.get("cookies_file"),
                                concurrent_fragments=cfg.get("concurrent_fragments", 8),
                                external_downloader=cfg.get("external_downloader", "auto"),
                                progress_callback=_download_progress)
                            if not downloaded or not os.path.isfile(downloaded):
                                raise RuntimeError(f"Không thấy file sau khi tải: {downloaded}")
                            with _LOCK:
                                j = _find(job_id)
                                if j:
                                    j.update({"name": os.path.basename(downloaded),
                                              "path": downloaded,
                                              "status": "chờ",
                                              "note": "Tải xong, sẵn sàng xử lý",
                                              "progress": 100})
                                    STATE["selected"] = job_id
                            get_project(job_id)
                            bump_rev(job_id)
                            _log(f"Đã tải xong: {os.path.basename(downloaded)}", "ok")
                        except Exception as e:
                            with _LOCK:
                                j = _find(job_id)
                                if j:
                                    j.update({"status": "lỗi", "note": str(e)[:200],
                                              "progress": 100})
                            _log(f"Tải video lỗi: {e}", "err")
                        finally:
                            with _LOCK:
                                STATE["download_active"] = max(0, STATE.get("download_active", 0) - 1)
                            _DOWNLOAD_SEM.release()

                    background_job_id = submit_job(
                        _download_worker, name="Tải video vào hàng đợi",
                        resource="network", metadata={"kind": "queue_download",
                                                        "queue_id": jid})
                    with _LOCK:
                        queued = _find(jid)
                        if queued:
                            queued["background_job_id"] = background_job_id
                    return self._json({"ok": True, "id": jid, "async": True})
                if not path or not os.path.isfile(path):
                    return self._json({"error": f"Không thấy file: {path}"}, 400)
                with _LOCK:
                    jid = _NEXT_ID[0]
                    _NEXT_ID[0] += 1
                    job = {"id": jid, "name": os.path.basename(path),
                           "path": path, "status": "chờ", "note": ""}
                    STATE["queue"].append(job)
                    STATE["selected"] = jid
                get_project(jid)
                return self._json({"ok": True, "id": jid})
            except Exception as e:
                return self._json({"error": str(e)}, 500)

        if p == "/api/queue/add_batch":
            urls = b.get("urls", [])
            if isinstance(urls, str):
                urls = [u.strip() for u in urls.split("\n") if u.strip()]
            from ..downloader import extract_url
            results = []
            for raw in urls:
                url = extract_url(raw)
                if not url:
                    results.append({"url": raw, "error": "URL không hợp lệ"})
                    continue
                try:
                    with _LOCK:
                        jid = _NEXT_ID[0]
                        _NEXT_ID[0] += 1
                        job = {"id": jid, "name": url[:60] + "…",
                               "path": "", "status": "chờ tải",
                               "note": "Đang chờ slot tải…",
                               "progress": 0, "source_url": url}
                        STATE["queue"].append(job)

                    def _batch_dl(job_id=jid, src_url=url):
                        with _LOCK:
                            j = _find(job_id)
                            if j:
                                j.update({"status": "chờ tải", "note": "Đang chờ slot tải…"})
                        _DOWNLOAD_SEM.acquire()
                        try:
                            with _LOCK:
                                j = _find(job_id)
                                if not j:
                                    return
                                j.update({"status": "đang tải",
                                          "note": "Đang chuẩn bị bộ tải video",
                                          "progress": 5})
                                STATE["download_active"] = STATE.get("download_active", 0) + 1
                            from ..downloader import download_video
                            cfg = _load_cfg().get("download", {})

                            def _download_progress(info):
                                pct = info.get("percent")
                                note = str(info.get("text") or "Đang tải video…")
                                with _LOCK:
                                    active_job = _find(job_id)
                                    if not active_job:
                                        return
                                    if pct is not None:
                                        active_job["progress"] = max(
                                            float(active_job.get("progress") or 5),
                                            min(99.0, float(pct)))
                                    active_job["note"] = note[:220]

                            downloaded = download_video(
                                src_url, os.path.join(HERE, "downloads"),
                                quality=cfg.get("quality", "best"),
                                cookies_from_browser=cfg.get("cookies_from_browser"),
                                cookies_file=cfg.get("cookies_file"),
                                concurrent_fragments=cfg.get("concurrent_fragments", 8),
                                external_downloader=cfg.get("external_downloader", "auto"),
                                progress_callback=_download_progress)
                            if not downloaded or not os.path.isfile(downloaded):
                                raise RuntimeError(f"Không thấy file sau khi tải: {downloaded}")
                            with _LOCK:
                                j = _find(job_id)
                                if j:
                                    j.update({"name": os.path.basename(downloaded),
                                              "path": downloaded, "status": "chờ",
                                              "note": "Tải xong", "progress": 100})
                            get_project(job_id)
                            bump_rev(job_id)
                            _log(f"Đã tải xong: {os.path.basename(downloaded)}", "ok")
                        except Exception as e:
                            with _LOCK:
                                j = _find(job_id)
                                if j:
                                    j.update({"status": "lỗi", "note": str(e)[:200], "progress": 100})
                            _log(f"Tải video lỗi: {e}", "err")
                        finally:
                            with _LOCK:
                                STATE["download_active"] = max(0, STATE.get("download_active", 0) - 1)
                            _DOWNLOAD_SEM.release()

                    background_job_id = submit_job(
                        _batch_dl, name="Tải video hàng loạt",
                        resource="network", metadata={"kind": "queue_download",
                                                        "queue_id": jid})
                    with _LOCK:
                        queued = _find(jid)
                        if queued:
                            queued["background_job_id"] = background_job_id
                    results.append({"url": url, "id": jid, "ok": True})
                except Exception as e:
                    results.append({"url": raw, "error": str(e)})
            return self._json({"ok": True, "results": results})

        if p == "/api/queue/remove":
            background_job_id = ""
            with _LOCK:
                jid = int(b.get("id", 0))
                existing = _find(jid)
                if existing:
                    background_job_id = str(existing.get("background_job_id") or "")
                STATE["queue"] = [j for j in STATE["queue"] if j["id"] != jid]
                PROJECTS.pop(jid, None)
                if STATE["selected"] == jid:
                    STATE["selected"] = STATE["queue"][0]["id"] if STATE["queue"] else None
            if background_job_id and JOB_MANAGER.cancel(background_job_id):
                event = JOB_MANAGER.get_cancel_event(background_job_id)
                cancel_running_processes(event)
            return self._json({"ok": True})

        if p == "/api/queue/select":
            with _LOCK:
                STATE["selected"] = int(b.get("id", 0)) or None
            return self._json({"ok": True})

        if p == "/api/project":
            jid = int(b.get("id", 0))
            pr = get_project(jid)
            if not pr:
                return self._json({"error": "no project"}, 404)
            with _LOCK:
                for k in ("regions", "logo", "sub_style", "segments", "options"):
                    if k in b:
                        pr[k] = b[k]
                _save_project_state(pr)
            # KHÔNG tăng rev ở đây: thay đổi này do CHÍNH giao diện gửi lên,
            # tăng rev sẽ khiến nó tự tải lại và ghi đè thứ người dùng đang gõ.
            return self._json({"ok": True, "rev": REV.get(jid, 0)})

        if p == "/api/detect_sub":
            # Dò sub cứng chạy ~24 tiến trình ffmpeg, mất 30-60s. Chạy NỀN để
            # cửa sổ không đơ (Windows sẽ báo "not responding" nếu chặn ở đây).
            jid = int(b.get("id", 0))
            j = _find(jid)
            if not j:
                return self._json({"error": "no job"}, 404)
            with _LOCK:
                if STATE["busy"]:
                    return self._json({"error": "Đang bận: " + STATE["busy"]}, 409)
                STATE["busy"] = "Đang dò vùng sub cứng…"

            def _work(job_id=jid, path=j["path"]):
                try:
                    r = detect.detect_hardsub_region(path)
                    pr = get_project(job_id)
                    with _LOCK:
                        pr.setdefault("regions", []).append(r)
                        _save_project_state(pr)
                    bump_rev(job_id)
                    _log(f"Đã dò vùng sub: {r['w']}×{r['h']} tại ({r['x']},{r['y']})", "ok")
                except InterruptedError:
                    _log("Đã dừng dò vùng phụ đề cứng.", "warn")
                except Exception as e:
                    _log(f"Dò sub lỗi: {e}", "err")
                finally:
                    with _LOCK:
                        STATE["busy"] = ""

            submit_job(_work, name="Dò vùng phụ đề cứng", resource="ffmpeg",
                       metadata={"kind": "detect_hardsub"})
            return self._json({"ok": True, "async": True})

        if p == "/api/manual/use_audio":
            return self._json(*manual_api.api_manual_use_audio(b))

        if p == "/api/manual/tts":
            return self._json(*manual_api.api_manual_tts(b))

        if p == "/api/nhac_nen/tai":
            return self._json(*manual_api.api_nhac_nen_tai(b))

        if p == "/api/manual/nhac_nen":
            return self._json(*manual_api.api_manual_nhac_nen(b))

        if p == "/api/manual/slideshow":
            return self._json(*manual_api.api_manual_slideshow(b))

        if p == "/api/manual/run_all":
            return self._json(*manual_api.api_manual_run_all(b))

        if p == "/api/story/generate_and_run":
            return self._json(*manual_api.api_story_generate_and_run(b))

        if p == "/api/story/resume_images":
            return self._json(*manual_api.api_story_resume_images(b))

        if p == "/api/story/image_pack":
            return self._json(*manual_api.api_story_image_pack(b))

        if p == "/api/story/voice_recommendations":
            return self._json(*manual_api.api_story_voice_recommendations(b))

        if p == "/api/story/search_sources":
            return self._json(*manual_api.api_story_search_sources(b))

        if p == "/api/story/reference_catalog":
            return self._json(*manual_api.api_story_reference_catalog(b))

        if p == "/api/story/search_references":
            return self._json(*manual_api.api_story_search_references(b))

        if p == "/api/story/cut_sources":
            return self._json(*manual_api.api_story_cut_sources(b))

        if p == "/api/story/download_sources":
            return self._json(*manual_api.api_story_download_sources(b))

        if p == "/api/story/video_info":
            return self._json(*manual_api.api_story_video_info(b))

        if p == "/api/tools/search_videos":
            return self._json(*video_tools_api.api_search_videos(b))

        if p == "/api/tools/download_videos":
            return self._json(*video_tools_api.api_download_videos(b))

        if p == "/api/tools/cut_videos":
            return self._json(*video_tools_api.api_cut_videos(b))

        if p == "/api/manual/mux":
            return self._json(*manual_api.api_manual_mux(b))

        if p == "/api/run":
            with _LOCK:
                if STATE["running"]:
                    return self._json({"error": "Đang chạy việc khác"}, 409)
            jid = int(b.get("id", 0))
            steps = b.get("steps") or ["asr", "translate", "tts", "render"]
            if not _find(jid):
                return self._json({"error": "no job"}, 404)
            background_job_id = submit_job(
                run_pipeline, name="Pipeline lồng tiếng", resource="ffmpeg",
                metadata={"kind": "dub_pipeline", "queue_id": jid},
                args=(jid, steps))
            with _LOCK:
                queued = _find(jid)
                if queued:
                    queued["background_job_id"] = background_job_id
            return self._json({"ok": True})

        if p == "/api/prefetch":
            with _LOCK:
                if STATE["busy"]:
                    return self._json({"error": "Đang bận: " + STATE["busy"]}, 409)
                STATE["busy"] = "Đang tải model về máy…"

            def _dl():
                try:
                    from .. import tts as tts_mod
                    def _p(msg):
                        with _LOCK:
                            STATE["busy"] = "Tải model: " + str(msg)[:70]
                    r = tts_mod.prefetch_models(progress=_p)
                    if r.get("loi"):
                        _log(f"Tải model còn thiếu: {r['loi']}", "warn")
                    else:
                        _log("Đã tải đủ model giọng nói.", "ok")
                except Exception as e:
                    _log(f"Tải model lỗi: {e}", "err")
                finally:
                    with _LOCK:
                        STATE["busy"] = ""

            submit_job(_dl, name="Tải model giọng nói", resource="ai",
                       metadata={"kind": "model_prefetch"})
            return self._json({"ok": True, "async": True})

        if p == "/api/cleanup_temp":
            with _LOCK:
                if STATE["running"]:
                    return self._json({"error": "Dang chay pipeline, hay huy/cho xong roi don file tam."}, 409)
                if STATE["busy"]:
                    return self._json({"error": "Dang ban: " + STATE["busy"]}, 409)
                STATE["busy"] = "Dang don file tam..."
            try:
                result = _cleanup_temp_files()
                gb = result.get("bytes", 0) / (1024 ** 3)
                free_gb = result.get("free", 0) / (1024 ** 3)
                _log(
                    f"Da don {result.get('files', 0)} file tam, giai phong {gb:.2f} GB. "
                    f"Con trong o dia: {free_gb:.2f} GB.",
                    "ok",
                )
                if result.get("failed"):
                    _log(f"Khong xoa duoc {len(result['failed'])} file tam dang bi khoa.", "warn")
                return self._json({"ok": True, **result})
            except Exception as e:
                return self._json({"error": str(e)}, 500)
            finally:
                with _LOCK:
                    STATE["busy"] = ""

        if p == "/api/cancel":
            requested_job_id = str(b.get("job_id") or "")
            selected_id = 0
            with _LOCK:
                was_running = bool(STATE["running"] or STATE["busy"] or
                                   (STATE.get("manual") or {}).get("working"))
                STATE["cancel"] = True
                selected_id = int(STATE.get("selected") or 0)
                manual = STATE.get("manual") or {}
                if manual.get("working"):
                    manual.update({
                        "status": "Đang dừng tác vụ…",
                        "error": "",
                        "rev": int(manual.get("rev", 0)) + 1,
                    })
            cancelled = []
            if requested_job_id:
                if JOB_MANAGER.cancel(requested_job_id):
                    event = JOB_MANAGER.get_cancel_event(requested_job_id)
                    cancelled = [(requested_job_id, event)]
            else:
                cancelled = JOB_MANAGER.cancel_foreground(
                    "queue_id", selected_id) if selected_id else []
                if not cancelled:
                    cancelled = JOB_MANAGER.cancel_foreground()
            if cancelled:
                for _job_id, event in cancelled:
                    cancel_running_processes(event)
            else:
                # Compatibility for work started outside JobManager.
                _CANCEL_EVENT.set()
                cancel_running_processes()
            return self._json({"ok": True, "active": was_running or bool(cancelled),
                               "cancelled_jobs": [item[0] for item in cancelled]})

        return self._json({"error": "unknown endpoint"}, 404)


class QuietServer(ThreadingHTTPServer):
    """Máy chủ không spam traceback khi trình duyệt tự ngắt kết nối.

    WebView2 mở/đóng liên tục các kết nối khi tua video, mỗi lần như vậy
    socketserver in ra một traceback dài (WinError 10053 / 10054). Chúng vô
    hại nhưng làm ngập cửa sổ log và che mất lỗi thật.
    """
    daemon_threads = True
    request_queue_size = 128

    def handle_error(self, request, client_address):
        import sys as _sys
        exc = _sys.exc_info()[1]
        if isinstance(exc, (ConnectionAbortedError, ConnectionResetError,
                            BrokenPipeError, TimeoutError)):
            return                      # bỏ qua, không in gì
        super().handle_error(request, client_address)


def serve(port: int = 8760, open_browser: bool = True):
    os.makedirs(UI_DIR, exist_ok=True)
    httpd = QuietServer(("127.0.0.1", port), Handler)
    url = f"http://127.0.0.1:{port}/"
    print()
    log(f"Giao diện AutoDubVN: {url}", "ok")
    print("   (Đóng cửa sổ này để tắt chương trình)\n")
    if open_browser:
        try:
            import webbrowser
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nĐã dừng.")
    finally:
        httpd.server_close()
        shutdown_background_jobs(wait=True, timeout=12.0)
