"""Tiện ích chung: log, chạy lệnh, ffprobe, kiểm tra công cụ."""
from __future__ import annotations

import json
import math
import os
import glob
import logging
import queue
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable, List, Optional


def _configure_stdio() -> None:
    os.environ.setdefault("PYTHONUTF8", "1")
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_stdio()


class C:
    """Màu terminal (ANSI)."""
    G = "\033[92m"; Y = "\033[93m"; R = "\033[91m"; B = "\033[94m"; DIM = "\033[2m"; E = "\033[0m"


_LOG_FILE: Optional[str] = None
_LOG_LOCK = threading.RLock()
_PY_LOG_HANDLER: Optional[logging.Handler] = None
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def _plain(s: str) -> str:
    return _ANSI_RE.sub("", str(s))


def _safe_print(line: str) -> None:
    try:
        print(line, flush=True)
    except UnicodeEncodeError:
        enc = getattr(sys.stdout, "encoding", None) or "utf-8"
        safe = line.encode(enc, errors="replace").decode(enc, errors="replace")
        print(safe, flush=True)


def start_file_log(path: str, append: bool = False) -> bool:
    """Bật ghi log ra file riêng, mỗi dòng có timestamp.

    Dùng được cho cả CLI lẫn GUI. Từ lúc bật, mọi lời gọi `log()` sẽ được ghi
    song song ra file này; đồng thời gắn thêm handler cho module `logging` để
    bắt cả log của thư viện như FunASR/modelscope.
    """
    global _LOG_FILE, _PY_LOG_HANDLER
    try:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        mode = "a" if append else "w"
        with open(path, mode, encoding="utf-8") as f:
            f.write("\n" if append else "")
            f.write("=" * 80 + "\n")
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + "  AutoDubVN process log\n")
            f.write("=" * 80 + "\n")
        with _LOG_LOCK:
            _LOG_FILE = path

        root = logging.getLogger()
        if _PY_LOG_HANDLER is not None:
            try:
                root.removeHandler(_PY_LOG_HANDLER)
                _PY_LOG_HANDLER.close()
            except Exception:
                pass
        handler = logging.FileHandler(path, mode="a", encoding="utf-8")
        handler.setFormatter(logging.Formatter(
            "%(asctime)s [PY-%(levelname)s] %(name)s: %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S"))
        root.addHandler(handler)
        if root.level > logging.INFO:
            root.setLevel(logging.INFO)
        _PY_LOG_HANDLER = handler
        return True
    except Exception:
        return False


def get_file_log_path() -> Optional[str]:
    return _LOG_FILE


def stop_file_log() -> None:
    """Tắt ghi log file hiện tại (chủ yếu dùng trong test)."""
    global _LOG_FILE, _PY_LOG_HANDLER
    with _LOG_LOCK:
        _LOG_FILE = None
    if _PY_LOG_HANDLER is not None:
        root = logging.getLogger()
        try:
            root.removeHandler(_PY_LOG_HANDLER)
            _PY_LOG_HANDLER.close()
        except Exception:
            pass
        _PY_LOG_HANDLER = None


def log(msg: str, kind: str = "info") -> None:
    tags = {
        "info": ("[i]", f"{C.B}[i]{C.E}"),
        "ok": ("[OK]", f"{C.G}[OK]{C.E}"),
        "warn": ("[WARN]", f"{C.Y}[!]{C.E}"),
        "err": ("[ERR]", f"{C.R}[x]{C.E}"),
        "step": ("[STEP]", f"{C.B}==>{C.E}"),
        "dim": ("[TIME]", f"{C.DIM}[t]{C.E}"),
    }
    plain_tag, color_tag = tags.get(kind, ("[i]", "[i]"))
    # Console trước đây không có thời điểm nên khi một bước dài (đặc biệt tải
    # video) người dùng không biết tiến trình còn chạy hay đã treo. File log đã
    # có timestamp đầy đủ; console dùng HH:MM:SS để ngắn và dễ theo dõi.
    console_ts = time.strftime("%H:%M:%S")
    _safe_print(f"[{console_ts}] {color_tag} {msg}")

    path = _LOG_FILE
    if path:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        try:
            with _LOG_LOCK, open(path, "a", encoding="utf-8") as f:
                for line in str(msg).splitlines() or [""]:
                    f.write(f"{ts} {plain_tag} {_plain(line)}\n")
        except Exception:
            pass


# Trên Windows, mỗi lần gọi ffmpeg từ app cửa sổ sẽ nháy lên một console đen.
# Cờ này chạy tiến trình con ẩn hẳn đi.
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0) if sys.platform == "win32" else 0
_RUN_LOCK = threading.RLock()
_RUNNING_PROCS = {}
_CANCEL_EVENT = None
_CANCEL_EVENT_PROVIDER = None


def _creationflags_for(cmd: List[str]) -> int:
    flags = _NO_WINDOW
    if sys.platform == "win32" and cmd:
        exe = os.path.basename(str(cmd[0])).lower()
        if exe in {"ffmpeg", "ffmpeg.exe"}:
            flags |= getattr(subprocess, "BELOW_NORMAL_PRIORITY_CLASS", 0)
    return flags


def set_cancel_event(event) -> None:
    global _CANCEL_EVENT
    _CANCEL_EVENT = event


def set_cancel_event_provider(provider) -> None:
    """Register a job-aware event provider without coupling utils to server."""
    global _CANCEL_EVENT_PROVIDER
    _CANCEL_EVENT_PROVIDER = provider


def active_cancel_event(resource: str = ""):
    if _CANCEL_EVENT_PROVIDER is not None:
        try:
            event = _CANCEL_EVENT_PROVIDER(resource)
            if event is not None:
                return event
        except TypeError:
            try:
                event = _CANCEL_EVENT_PROVIDER()
                if event is not None:
                    return event
            except Exception:
                pass
        except Exception:
            pass
    return _CANCEL_EVENT


def register_running_process(proc, cancel_event=None, resource: str = ""):
    owner = cancel_event if cancel_event is not None else active_cancel_event(resource)
    with _RUN_LOCK:
        _RUNNING_PROCS[proc] = owner
    return owner


def unregister_running_process(proc) -> None:
    with _RUN_LOCK:
        _RUNNING_PROCS.pop(proc, None)


def cancel_running_processes(cancel_event=None) -> None:
    """Terminate subprocesses owned by one job, or every process if omitted."""
    with _RUN_LOCK:
        procs = [proc for proc, owner in _RUNNING_PROCS.items()
                 if cancel_event is None or owner is cancel_event]
    for proc in procs:
        try:
            if proc.poll() is None:
                proc.terminate()
        except Exception:
            pass
    deadline = time.time() + 2.0
    for proc in procs:
        try:
            while proc.poll() is None and time.time() < deadline:
                time.sleep(0.05)
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass


def run(cmd: List[str], check: bool = True, quiet: bool = True,
        timeout: Optional[float] = 3600,
        line_callback: Optional[Callable[[str], None]] = None,
        heartbeat_callback: Optional[Callable[[float], None]] = None,
        heartbeat_interval: float = 15.0,
        ) -> subprocess.CompletedProcess:
    """Chạy lệnh ngoài (ffmpeg/ffprobe...) một cách AN TOÀN cho app cửa sổ.

    Ba điều quan trọng, thiếu cái nào cũng có thể làm treo cả chương trình:
      - stdin=DEVNULL : ffmpeg ĐỌC STDIN theo mặc định. Trong app không có cửa
        sổ dòng lệnh, nó sẽ chờ stdin mãi mãi -> luồng đứng, cửa sổ "not responding".
      - CREATE_NO_WINDOW : không nháy console đen mỗi lần gọi.
      - timeout : lệnh treo thì bỏ sau ngần này giây thay vì kẹt vĩnh viễn.
    """
    streaming = line_callback is not None
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if (quiet or streaming) else None,
        # Khi stream, gộp stderr vào stdout để giữ đúng thứ tự log yt-dlp và
        # tránh deadlock do hai pipe đầy độc lập trên Windows.
        stderr=subprocess.STDOUT if streaming else (
            subprocess.PIPE if quiet else None),
        text=True,
        encoding='utf-8',
        errors='replace',
        creationflags=_creationflags_for(cmd),
    )
    executable = os.path.basename(str(cmd[0])).lower() if cmd else ""
    resource_hint = ("ffmpeg" if executable in {
        "ffmpeg", "ffmpeg.exe", "ffprobe", "ffprobe.exe"
    } else "network" if executable in {
        "yt-dlp", "yt-dlp.exe", "aria2c", "aria2c.exe"
    } else "")
    owner_cancel_event = register_running_process(proc, resource=resource_hint)
    started = time.monotonic()
    last_activity = started
    try:
        if streaming:
            line_queue: queue.Queue = queue.Queue()
            stream_done = object()

            def _read_stream() -> None:
                try:
                    if proc.stdout is not None:
                        for line in proc.stdout:
                            line_queue.put(line)
                finally:
                    line_queue.put(stream_done)

            reader = threading.Thread(target=_read_stream, daemon=True)
            reader.start()
            chunks = []
            reader_finished = False
            while not reader_finished:
                if owner_cancel_event is not None and owner_cancel_event.is_set():
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    raise InterruptedError("Da huy lenh dang chay")
                if timeout is not None and time.monotonic() - started > timeout:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise RuntimeError(
                        f"Lenh chay qua {timeout:.0f}s nen bi dung: "
                        f"{' '.join(cmd[:4])}...")
                try:
                    item = line_queue.get(timeout=0.25)
                except queue.Empty:
                    now = time.monotonic()
                    if (heartbeat_callback and
                            now - last_activity >= max(1.0, float(heartbeat_interval))):
                        try:
                            heartbeat_callback(now - started)
                        except Exception:
                            pass
                        last_activity = now
                    continue
                if item is stream_done:
                    reader_finished = True
                    continue
                chunks.append(item)
                last_activity = time.monotonic()
                try:
                    line_callback(str(item).rstrip("\r\n"))
                except Exception:
                    # Callback chỉ phục vụ hiển thị. Không để lỗi giao diện
                    # làm hỏng chính tiến trình tải/render.
                    pass
            proc.wait()
            stdout, stderr = "".join(chunks), ""
        else:
            while True:
                if owner_cancel_event is not None and owner_cancel_event.is_set():
                    try:
                        proc.terminate()
                        proc.wait(timeout=2)
                    except Exception:
                        try:
                            proc.kill()
                        except Exception:
                            pass
                    raise InterruptedError("Da huy lenh dang chay")
                if timeout is not None and time.monotonic() - started > timeout:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    raise RuntimeError(f"Lenh chay qua {timeout:.0f}s nen bi dung: {' '.join(cmd[:4])}...")
                try:
                    stdout, stderr = proc.communicate(timeout=0.25)
                    break
                except subprocess.TimeoutExpired:
                    now = time.monotonic()
                    if (heartbeat_callback and
                            now - last_activity >= max(
                                1.0, float(heartbeat_interval))):
                        try:
                            heartbeat_callback(now - started)
                        except Exception:
                            pass
                        last_activity = now
                    continue
        res = subprocess.CompletedProcess(cmd, proc.returncode, stdout, stderr)
    finally:
        unregister_running_process(proc)
    if owner_cancel_event is not None and owner_cancel_event.is_set():
        raise InterruptedError("Da huy lenh dang chay")
    if check and res.returncode != 0:
        err = (res.stderr or res.stdout or "")[-4000:]
        raise RuntimeError(f"Lệnh lỗi ({res.returncode}): {' '.join(cmd[:4])}...\n{err}")
    return res


def which(name: str) -> Optional[str]:
    found = shutil.which(name)
    if found:
        return found

    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    raw = str(name or "").strip()
    base, ext = os.path.splitext(raw)
    names = [raw]
    if sys.platform == "win32" and not ext:
        names.append(raw + ".exe")
    lowered = {n.lower() for n in names}

    candidates = []
    if "mp4box" in lowered or "mp4box.exe" in lowered:
        candidates += [
            os.path.join(root, "tools", "gpac", "mp4box.exe"),
            os.path.join(root, "tools", "gpac", "MP4Box.exe"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                         "GPAC", "mp4box.exe"),
        ]
    if "gst-launch-1.0" in lowered or "gst-launch-1.0.exe" in lowered:
        candidates += [
            os.path.join(root, "tools", "gstreamer", "bin", "gst-launch-1.0.exe"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                         "gstreamer", "1.0", "msvc_x86_64", "bin",
                         "gst-launch-1.0.exe"),
        ]
    if "gst-inspect-1.0" in lowered or "gst-inspect-1.0.exe" in lowered:
        candidates += [
            os.path.join(root, "tools", "gstreamer", "bin", "gst-inspect-1.0.exe"),
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                         "gstreamer", "1.0", "msvc_x86_64", "bin",
                         "gst-inspect-1.0.exe"),
        ]
    if "aria2c" in lowered or "aria2c.exe" in lowered:
        local = os.environ.get("LOCALAPPDATA", "")
        candidates += [
            os.path.join(root, "tools", "aria2", "aria2c.exe"),
            *glob.glob(os.path.join(
                local, "Microsoft", "WinGet", "Packages", "aria2.aria2_*",
                "aria2-*", "aria2c.exe")),
        ]

    for p in candidates:
        if p and os.path.exists(p):
            return p
    return None


def _tool_runs(path: str, timeout: float = 10) -> bool:
    try:
        res = subprocess.run(
            [path, "-version"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            creationflags=_creationflags_for([path]),
        )
        return res.returncode == 0
    except Exception:
        return False


def ffmpeg_dir_to_path(fdir: str) -> bool:
    """Put a configured ffmpeg folder in PATH only when ffmpeg/ffprobe run."""
    raw = str(fdir or "").strip().strip('"').strip("'")
    if not raw:
        return False
    if raw.lower().endswith(".exe"):
        raw = os.path.dirname(raw)
    if os.path.isdir(raw) and not os.path.exists(os.path.join(raw, "ffmpeg.exe")):
        cand = os.path.join(raw, "bin")
        if os.path.exists(os.path.join(cand, "ffmpeg.exe")):
            raw = cand
    if not os.path.isdir(raw):
        log(f"ffmpeg_dir không tồn tại: {raw!r} — bỏ qua, dùng PATH hệ thống.", "warn")
        return False

    exe = ".exe" if sys.platform == "win32" else ""
    ffmpeg = os.path.join(raw, "ffmpeg" + exe)
    ffprobe = os.path.join(raw, "ffprobe" + exe)
    if not os.path.exists(ffmpeg) or not os.path.exists(ffprobe):
        log(f"ffmpeg_dir thiếu ffmpeg/ffprobe: {raw!r} — bỏ qua, dùng PATH hệ thống.", "warn")
        return False
    if not _tool_runs(ffmpeg) or not _tool_runs(ffprobe):
        log(f"ffmpeg_dir có file nhưng không chạy được (có thể bị Windows chặn): {raw!r} — bỏ qua, dùng PATH hệ thống.", "warn")
        return False

    os.environ["PATH"] = raw + os.pathsep + os.environ.get("PATH", "")
    return True


def require(name: str, hint: str = "") -> None:
    path = which(name)
    if not path:
        log(f"Thiếu công cụ '{name}'. {hint}", "err")
        raise SystemExit(1)
    if os.path.basename(path).lower() in {"ffmpeg.exe", "ffmpeg", "ffprobe.exe", "ffprobe"}:
        if not _tool_runs(path):
            log(f"Công cụ '{name}' có tồn tại nhưng không chạy được: {path}", "err")
            log("Nếu dùng Windows Application Control/WDAC, hãy để ffmpeg_dir: '' để dùng bản ffmpeg trong PATH, hoặc trỏ tới bản không bị chặn.", "err")
            raise SystemExit(1)


def ffprobe_duration(path: str) -> float:
    """Độ dài file media (giây). File hỏng/không có -> 0.0 chứ KHÔNG ném lỗi.

    check=False là cố ý: các hàm dò thông tin phải luôn trả được giá trị an
    toàn, nếu không thì một file bị xoá giữa chừng sẽ làm sập cả pipeline.
    """
    try:
        res = run([
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "json", path,
        ], check=False, timeout=60)
        return float(json.loads(res.stdout or "{}")["format"]["duration"])
    except Exception:
        return 0.0


def ffprobe_video_size(path: str) -> tuple[int, int]:
    """(width, height) của luồng video. Không có -> (0, 0), không ném lỗi."""
    try:
        res = run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", path,
        ], check=False, timeout=60)
        st = json.loads(res.stdout or "{}")["streams"][0]
        return int(st["width"]), int(st["height"])
    except Exception:
        return 0, 0


def ffprobe_video_codec(path: str) -> tuple[str, str]:
    """(codec_name, pix_fmt) của luồng hình. Trả ("", "") nếu không có luồng hình.

    Dùng để phát hiện video HEVC/AV1 hoặc 10-bit — Windows Photos không mở được,
    nên khi xuất phải chuyển sang H.264 8-bit.
    """
    try:
        res = run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=codec_name,pix_fmt", "-of", "json", path,
        ], check=False, timeout=60)
        st = json.loads(res.stdout or "{}").get("streams") or []
        if not st:
            return "", ""
        return str(st[0].get("codec_name") or ""), str(st[0].get("pix_fmt") or "")
    except Exception:
        return "", ""


def ffprobe_fps(path: str) -> float:
    """FPS của luồng hình. 0 nếu không đọc được."""
    try:
        res = run([
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=avg_frame_rate,r_frame_rate",
            "-of", "json", path,
        ], check=False, timeout=60)
        st = (json.loads(res.stdout or "{}").get("streams") or [{}])[0]
        for key in ("avg_frame_rate", "r_frame_rate"):
            raw = str(st.get(key) or "")
            if "/" in raw:
                a, b = raw.split("/", 1)
                den = float(b)
                if den > 0:
                    val = float(a) / den
                    if val > 1:
                        return val
            elif raw:
                val = float(raw)
                if val > 1:
                    return val
    except Exception:
        pass
    return 0.0


def nvenc_gop_frames(fps: Optional[float] = None,
                     keyint_seconds: float = 2.0) -> int:
    """Số khung mỗi GOP (~2 giây) để tua/cắt copy được."""
    f = float(fps or 0.0)
    if f <= 1.0:
        f = 24.0
    return max(24, int(round(f * float(keyint_seconds))))


def nvenc_maxrate_bps(width: Optional[int] = None,
                      height: Optional[int] = None) -> Optional[int]:
    """Trần bitrate: 8 Mbps tại 1080p, tỉ lệ theo số pixel.

    CQ không trần dễ ra 10–12 Mbps cho hoạt hình (gấp ~10 lần nguồn AV1),
    trong khi YouTube 1080p chỉ khuyến nghị khoảng 8 Mbps.
    """
    w, h = int(width or 0), int(height or 0)
    if w < 16 or h < 16:
        return None
    return max(3_000_000, int(8_000_000 * (w * h) / (1920 * 1080)))


def nvenc_encode_args(crf, *, pix_fmt: Optional[str] = "yuv420p",
                      fps: Optional[float] = None,
                      width: Optional[int] = None,
                      height: Optional[int] = None,
                      extra: Optional[List[str]] = None) -> List[str]:
    """Cờ h264_nvenc cho file xuất: nén tử tế, tua được.

    Không dùng -preset p1 -tune ll: đó là chế độ livestream (GOP vô hạn,
    không B-frame) nên file to và không tua/cắt bằng copy được.
    """
    args = [
        "-c:v", "h264_nvenc",
        "-preset", "p4",
        "-tune", "hq",
        "-rc", "vbr",
        "-cq", str(crf),
        "-b:v", "0",
        "-profile:v", "high",
        "-g", str(nvenc_gop_frames(fps)),
        "-bf", "3",
        "-rc-lookahead", "20",
        "-forced-idr", "1",
    ]
    maxrate = nvenc_maxrate_bps(width, height)
    if maxrate:
        args += ["-maxrate", str(maxrate), "-bufsize", str(maxrate * 2)]
    if pix_fmt:
        args += ["-pix_fmt", pix_fmt]
    if extra:
        args += list(extra)
    return args


def ffprobe_has_stream(path: str, kind: str = "v") -> bool:
    """File có luồng loại này không? kind: "v" = hình, "a" = tiếng."""
    k = "a" if str(kind).lower().startswith("a") else "v"
    try:
        res = run([
            "ffprobe", "-v", "error", "-select_streams", k,
            "-show_entries", "stream=codec_type", "-of", "json", path,
        ], check=False, timeout=60)
        return bool(json.loads(res.stdout or "{}").get("streams"))
    except Exception:
        return False


def ffprobe_is_blank_video(path: str, samples: int = 6,
                           luma_threshold: int = 18) -> bool:
    """Hình có TOÀN MÀU ĐEN không (placeholder giả)?

    Bilibili đôi khi trả về luồng hình đen kịt kèm tiếng thật khi độ phân giải
    bị khoá sau đăng nhập/VIP — kích thước khai báo vẫn đúng nên không thể phát
    hiện bằng width/height.

    Cách làm: lấy vài khung hình rải đều, thu nhỏ về ảnh xám 64x36, đọc thô rồi
    tính độ sáng. Không cần numpy, và chỉ tua vài lần nên rất nhanh kể cả với
    phim dài. Trả True chỉ khi MỌI khung mẫu đều tối, để không báo nhầm cảnh tối.
    """
    dur = ffprobe_duration(path)
    if dur <= 0:
        return False
    W, H = 64, 36
    checked = 0
    for i in range(max(1, samples)):
        t = dur * (0.08 + 0.84 * (i / max(1, samples - 1))) if samples > 1 else dur / 2
        try:
            res = subprocess.run(
                ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t:.3f}",
                 "-i", path, "-frames:v", "1",
                 "-vf", f"scale={W}:{H},format=gray", "-f", "rawvideo", "-"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, timeout=30, creationflags=_NO_WINDOW)
        except Exception:
            continue
        buf = res.stdout or b""
        if len(buf) < W * H:
            continue
        checked += 1
        if max(buf[:W * H]) > luma_threshold:
            return False        # có khung sáng -> hình thật
    return checked > 0          # lấy được mẫu và mẫu nào cũng tối


def resolve_keep_original_db(video_cfg: dict) -> Optional[float]:
    """Trả mức âm nền gốc theo dB cho FFmpeg.

    Ưu tiên:
      1. keep_original_muted=true -> tắt hẳn
      2. keep_original_db (dB) nếu người dùng đặt trực tiếp
      3. keep_original_volume (0-10) chỉ để tương thích project cũ
      4. None nếu cả hai đều không đặt

    GUI mới dùng trực tiếp khoảng -60..0 dB vì mức 1/10 của thang cũ vẫn là
    -20 dB và có thể còn khá rõ với nguồn thoại lớn.
    """
    if bool(video_cfg.get("keep_original_muted", False)):
        return None
    db = video_cfg.get("keep_original_db")
    if db is not None:
        return min(0.0, max(-60.0, float(db)))
    # Tương thích project/config cũ dùng thang 0-10.
    vol = video_cfg.get("keep_original_volume")
    if vol is None:
        return None
    vol = float(vol)
    if vol <= 0:
        return None                         # 0 = tắt hẳn nền gốc
    vol = min(10.0, max(0.01, vol))         # clamp tránh log(0)
    return round(20.0 * math.log10(vol / 10.0), 1)


_NVENC_OK: Optional[bool] = None
_H264_MF_OK: Optional[bool] = None
_CUDA_DEC_OK: Optional[bool] = None


def has_nvenc() -> bool:
    """Kiem tra NVENC co encode duoc thuc te khong (khong chi liet ke encoder)."""
    global _NVENC_OK
    if _NVENC_OK is not None:
        return _NVENC_OK
    try:
        res = run(
            ["ffmpeg", "-hide_banner",
             "-f", "lavfi", "-i", "color=c=black:s=256x256:r=1",
             "-frames:v", "1",
             "-c:v", "h264_nvenc", "-preset", "p1", "-pix_fmt", "yuv420p",
             "-f", "null", "-"],
            check=False, timeout=15,
        )
        _NVENC_OK = res.returncode == 0
    except Exception:
        _NVENC_OK = False
    if not _NVENC_OK:
        log("NVENC test: khong encode duoc (driver cu hoac GPU ban). "
            "Se dung CPU x264 on dinh thay.", "warn")
    return _NVENC_OK


def has_cuda_decode() -> bool:
    """Kiem tra GPU co GIAI MA duoc H.264 khong (-hwaccel cuda).

    Giai ma va ma hoa la hai khoi phan cung KHAC NHAU: card chay NVENC ngon
    van co the khong nhan -hwaccel cuda neu ffmpeg duoc build thieu, nen phai
    thu that. Cach thu: tu ma hoa mot doan h264 ti hon ra file tam roi giai
    ma lai bang cuda. Chi chay mot lan cho ca phien.
    """
    global _CUDA_DEC_OK
    if _CUDA_DEC_OK is not None:
        return _CUDA_DEC_OK
    tmp = ""
    try:
        res = run(["ffmpeg", "-hide_banner", "-hwaccels"], check=False, timeout=10)
        if "cuda" not in (res.stdout or "").lower():
            _CUDA_DEC_OK = False
            return False
        fd, tmp = tempfile.mkstemp(suffix=".mp4", prefix="cudatest_")
        os.close(fd)
        mk = run(
            ["ffmpeg", "-y", "-hide_banner",
             "-f", "lavfi", "-i", "color=c=black:s=320x240:r=5:d=0.4",
             "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
             tmp],
            check=False, timeout=20,
        )
        if mk.returncode != 0 or not os.path.getsize(tmp):
            _CUDA_DEC_OK = False
            return False
        dec = run(
            ["ffmpeg", "-hide_banner", "-hwaccel", "cuda", "-i", tmp,
             "-f", "null", "-"],
            check=False, timeout=20,
        )
        _CUDA_DEC_OK = dec.returncode == 0
    except Exception:
        _CUDA_DEC_OK = False
    finally:
        if tmp:
            try:
                os.remove(tmp)
            except OSError:
                pass
    return _CUDA_DEC_OK


def has_h264_mf() -> bool:
    """Kiem tra Windows MediaFoundation H.264 (h264_mf) co encode duoc khong."""
    global _H264_MF_OK
    if _H264_MF_OK is not None:
        return _H264_MF_OK
    if sys.platform != "win32":
        _H264_MF_OK = False
        return False
    try:
        res = run(
            ["ffmpeg", "-hide_banner",
             "-f", "lavfi", "-i", "color=c=black:s=128x128:r=1",
             "-frames:v", "1",
             "-c:v", "h264_mf", "-f", "null", "-"],
            check=False, timeout=10,
        )
        _H264_MF_OK = res.returncode == 0
    except Exception:
        _H264_MF_OK = False
    return _H264_MF_OK


class Timer:
    def __init__(self, label: str):
        self.label = label
    def __enter__(self):
        self.t = time.time(); return self
    def __exit__(self, *a):
        log(f"{self.label}: {time.time() - self.t:.1f}s", "dim") if False else \
            print(f"{C.DIM}    ⏱ {self.label}: {time.time() - self.t:.1f}s{C.E}", flush=True)
