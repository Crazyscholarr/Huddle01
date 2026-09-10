#!/usr/bin/env python3
"""AutoDubVN - APP DESKTOP (cửa sổ riêng, không phải tab trình duyệt).

Dùng pywebview: mở một cửa sổ Windows thật, bên trong render giao diện bằng
WebView2 (đã có sẵn trên Windows 10/11). Cách này KHÔNG cài thêm .exe nào nên
không bị Device Guard/WDAC chặn như các bộ GUI nặng (Qt/Electron).

Cửa sổ ở chế độ KHÔNG VIỀN (frameless) và tự vẽ thanh tiêu đề trong HTML, nên
giao diện trông đúng như bản thiết kế.

Chạy: nhấp đúp run_gui.bat  (hoặc: python gui/app.py)
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

VIDEO_TYPES = ("Video (*.mp4;*.mkv;*.mov;*.avi;*.ts;*.webm;*.flv;*.m4v)",
               "Tất cả file (*.*)")
IMAGE_TYPES = ("Ảnh (*.png;*.jpg;*.jpeg;*.webp)", "Tất cả file (*.*)")
AUDIO_TYPES = ("Âm thanh (*.mp3;*.wav;*.m4a;*.aac;*.flac;*.ogg;*.opus)",
               "Tất cả file (*.*)")
DATA_TYPES = ("Kho truyện (*.json;*.sqlite;*.sqlite3;*.db)",
              "Tất cả file (*.*)")


TEXT_TYPES = ("Text (*.txt;*.md)", "All files (*.*)")

_TEXT_EXTENSIONS = {".txt", ".md"}
_MAX_TEXT_FILE_BYTES = 10 * 1024 * 1024


def _read_text_file(path: str) -> dict:
    """Read a selected narration file, including UTF-8 and UTF-16 text."""
    path = os.path.abspath(str(path or ""))
    if not os.path.isfile(path):
        raise ValueError("Kh\u00f4ng t\u00ecm th\u1ea5y file v\u0103n b\u1ea3n.")
    if os.path.splitext(path)[1].lower() not in _TEXT_EXTENSIONS:
        raise ValueError("Ch\u1ec9 h\u1ed7 tr\u1ee3 file TXT ho\u1eb7c MD.")
    size = os.path.getsize(path)
    if size > _MAX_TEXT_FILE_BYTES:
        raise ValueError("File v\u0103n b\u1ea3n qu\u00e1 l\u1edbn (t\u1ed1i \u0111a 10 MB).")

    with open(path, "rb") as f:
        raw = f.read()
    if not raw:
        raise ValueError("File v\u0103n b\u1ea3n \u0111ang tr\u1ed1ng.")

    encodings = (("utf-16", "utf-8-sig")
                 if raw.startswith((b"\xff\xfe", b"\xfe\xff"))
                 else ("utf-8-sig", "cp1258"))
    text = None
    for encoding in encodings:
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    if text is None or not text.strip():
        raise ValueError("Kh\u00f4ng \u0111\u1ecdc \u0111\u01b0\u1ee3c n\u1ed9i dung file (h\u00e3y l\u01b0u d\u1ea1ng UTF-8).")

    return {
        "path": path,
        "name": os.path.splitext(os.path.basename(path))[0],
        "filename": os.path.basename(path),
        "text": text,
        "chars": len(text),
    }


def _free_port(preferred: int = 8760) -> int:
    for p in (preferred, preferred + 1, preferred + 2, preferred + 3):
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", p))
                return p
            except OSError:
                continue
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _prepare_ffmpeg() -> bool:
    """Đưa ffmpeg do người dùng chỉ định trong config.yaml vào PATH."""
    from autodub.utils import ffmpeg_dir_to_path, require, log
    try:
        import yaml
    except ModuleNotFoundError:
        print("[x] Thiếu thư viện. Chạy trong venv:\n"
              "    python -m pip install pyyaml edge-tts yt-dlp playwright")
        return False
    try:
        with open(os.path.join(ROOT, "config.yaml"), "r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
    except Exception as e:
        print(f"[x] Lỗi đọc config.yaml: {e}")
        return False

    fdir = (cfg.get("ffmpeg_dir") or "").strip().strip('"').strip("'")
    if fdir:
        ffmpeg_dir_to_path(fdir)

    try:
        require("ffmpeg", "Tải bản STATIC (ffmpeg-release-essentials.zip) ở https://www.gyan.dev/ffmpeg/builds/ rồi điền ffmpeg_dir trong config.yaml.")
        require("ffprobe", "ffprobe đi kèm ffmpeg (cùng thư mục bin).")
    except SystemExit:
        return False
    return True


# QUAN TRỌNG: KHÔNG được giữ đối tượng cửa sổ làm THUỘC TÍNH của lớp Api.
# pywebview duyệt đệ quy mọi thuộc tính của js_api để phơi sang JavaScript; gặp
# đối tượng .NET của WinForms nó sẽ đi mãi
# (window.native.AccessibilityObject.Bounds.Empty.Empty.Empty...) rồi ném
# "maximum recursion depth exceeded" và hàng loạt lỗi COM
# "can only be accessed from the UI thread". Giữ ở biến mức module thì không bị.
_WIN = {"w": None}


def _open_dialog(kind: str, multiple: bool, types):
    """Mở hộp thoại chọn file, dùng API mới FileDialog.* và tự lùi về bản cũ."""
    import webview
    w = _WIN["w"]
    if w is None:
        return []
    fd = getattr(webview, "FileDialog", None)
    if fd is not None:                       # pywebview >= 5.4
        mode = fd.FOLDER if kind == "folder" else fd.OPEN
    else:                                    # bản cũ
        mode = (webview.FOLDER_DIALOG if kind == "folder"
                else webview.OPEN_DIALOG)
    kw = {"allow_multiple": multiple}
    if kind != "folder":
        kw["file_types"] = types
    r = w.create_file_dialog(mode, **kw)
    return list(r) if r else []


def _paste_clipboard_into_gemini(delay_seconds: float = 5.5) -> bool:
    """Đưa focus vào ô prompt Gemini/AI Studio trên Edge rồi gửi Ctrl+V."""
    if os.name != "nt":
        return False
    try:
        import ctypes
        from ctypes import wintypes

        time.sleep(max(1.0, float(delay_seconds)))
        user32 = ctypes.windll.user32
        handles = []
        enum_proc_type = ctypes.WINFUNCTYPE(
            wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        user32.IsWindowVisible.argtypes = [wintypes.HWND]
        user32.IsWindowVisible.restype = wintypes.BOOL
        user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
        user32.GetWindowTextLengthW.restype = ctypes.c_int
        user32.GetWindowTextW.argtypes = [
            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        user32.GetWindowTextW.restype = ctypes.c_int
        user32.IsIconic.argtypes = [wintypes.HWND]
        user32.SetForegroundWindow.argtypes = [wintypes.HWND]
        user32.GetWindowRect.argtypes = [
            wintypes.HWND, ctypes.POINTER(wintypes.RECT)]

        def _collect(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n <= 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            title = buf.value.casefold()
            if ("gemini" in title or "ai studio" in title or
                    "aistudio.google.com" in title):
                handles.append((hwnd, title))
            return True

        # Trang có thể tải chậm; tìm cửa sổ thêm vài giây thay vì dán vào app cũ.
        deadline = time.monotonic() + 12.0
        callback = enum_proc_type(_collect)
        while not handles and time.monotonic() < deadline:
            user32.EnumWindows(callback, 0)
            if not handles:
                time.sleep(0.8)
        if not handles:
            return False

        hwnd, window_title = handles[0]  # Tab vừa mở thường ở đầu thứ tự Z.
        if user32.IsIconic(hwnd):
            user32.ShowWindow(hwnd, 9)  # SW_RESTORE
        # Một lần bấm Alt giúp SetForegroundWindow được Windows chấp nhận ổn định.
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 0x0002, 0)
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.25)

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        width, height = rect.right - rect.left, rect.bottom - rect.top
        if width < 500 or height < 500:
            return False
        # Gemini chat mới đặt ô nhập giữa màn hình; AI Studio đặt sát đáy.
        if "gemini" in window_title and "ai studio" not in window_title:
            x = rect.left + int(width * 0.58)
            y = rect.top + int(height * 0.53)
        else:
            x = rect.left + int(width * 0.47)
            y = rect.bottom - max(50, min(90, int(height * 0.045)))
        user32.SetCursorPos(x, y)
        user32.mouse_event(0x0002, 0, 0, 0, 0)  # LEFTDOWN
        user32.mouse_event(0x0004, 0, 0, 0, 0)  # LEFTUP
        time.sleep(0.35)
        user32.keybd_event(0x11, 0, 0, 0)       # CTRL down
        user32.keybd_event(0x56, 0, 0, 0)       # V down
        user32.keybd_event(0x56, 0, 0x0002, 0)  # V up
        user32.keybd_event(0x11, 0, 0x0002, 0)  # CTRL up
        return True
    except Exception:
        return False


class Api:
    """Cầu nối cho JavaScript gọi xuống Python (hộp thoại file, điều khiển cửa sổ).

    Lớp này CHỈ chứa phương thức, tuyệt đối không giữ tham chiếu tới cửa sổ hay
    bất kỳ đối tượng .NET nào (xem ghi chú ở biến _WIN phía trên).
    """

    # ---------------- hộp thoại chọn file THẬT của Windows ----------------
    def pick_video(self):
        try:
            return _open_dialog("file", True, VIDEO_TYPES)
        except Exception as e:
            return {"error": str(e)}

    def pick_image(self):
        try:
            r = _open_dialog("file", False, IMAGE_TYPES)
            return r[0] if r else ""
        except Exception as e:
            return {"error": str(e)}

    def pick_images(self):
        """Chọn NHIỀU ảnh một lúc cho kho ảnh của chế độ Kể chuyện."""
        try:
            return _open_dialog("file", True, IMAGE_TYPES)
        except Exception as e:
            return {"error": str(e)}

    def pick_audio(self):
        try:
            r = _open_dialog("file", False, AUDIO_TYPES)
            return r[0] if r else ""
        except Exception as e:
            return {"error": str(e)}

    def pick_text(self):
        try:
            r = _open_dialog("file", False, TEXT_TYPES)
            return _read_text_file(r[0]) if r else ""
        except Exception as e:
            return {"error": str(e)}

    def pick_story_data(self):
        """Chọn kho truyện JSON/SQLite; chỉ trả đường dẫn, không nạp qua JS."""
        try:
            r = _open_dialog("file", False, DATA_TYPES)
            return r[0] if r else ""
        except Exception as e:
            return {"error": str(e)}

    def pick_folder(self):
        try:
            r = _open_dialog("folder", False, None)
            return r[0] if r else ""
        except Exception:
            return ""

    def open_folder(self, path: str):
        """Mở thư mục chứa file kết quả trong Explorer."""
        try:
            p = path if os.path.isdir(path) else os.path.dirname(path)
            if os.name == "nt":
                os.startfile(p)  # noqa: S606
            return True
        except Exception:
            return False

    def open_url(self, url: str):
        """Mở trang ngoài (ví dụ AI Studio) bằng trình duyệt mặc định."""
        try:
            import webbrowser
            target = str(url or "").strip()
            if not target.startswith(("https://", "http://")):
                return False
            return bool(webbrowser.open(target))
        except Exception:
            return False

    def open_url_and_paste(self, url: str, text: str):
        """Mở Gemini/AI Studio bằng browser đã đăng nhập rồi tự dán prompt."""
        target = str(url or "").strip()
        if not target.startswith(("https://gemini.google.com/",
                                  "https://aistudio.google.com/")):
            return {"opened": False, "copied": False, "paste_scheduled": False}
        copied = self.copy_text(text)
        opened = self.open_url(target)
        # webbrowser.open đôi khi trả False dù Edge vẫn đã nhận lệnh mở tab.
        scheduled = bool(copied and os.name == "nt")
        if scheduled:
            threading.Thread(
                target=_paste_clipboard_into_gemini,
                kwargs={"delay_seconds": 5.5}, daemon=True).start()
        return {"opened": bool(opened), "copied": bool(copied),
                "paste_scheduled": scheduled}

    def copy_text(self, text: str):
        """Sao chép chắc chắn trong app desktop khi Clipboard API bị WebView chặn."""
        try:
            import tkinter
            root = tkinter.Tk()
            root.withdraw()
            root.clipboard_clear()
            root.clipboard_append(str(text or ""))
            root.update()  # giữ dữ liệu sau khi cửa sổ tạm bị huỷ
            root.destroy()
            return True
        except Exception:
            return False

    # ---------------- điều khiển cửa sổ (thanh tiêu đề tự vẽ) ----------------
    def win_minimize(self):
        try:
            _WIN["w"].minimize()
        except Exception:
            pass
        return True

    def win_maximize(self):
        try:
            if _WIN.get("maxed"):
                _WIN["w"].restore()
                _WIN["maxed"] = False
            else:
                _WIN["w"].maximize()
                _WIN["maxed"] = True
        except Exception:
            pass
        return bool(_WIN.get("maxed"))

    def win_close(self):
        try:
            _WIN["w"].destroy()
        except Exception:
            os._exit(0)
        return True

    def is_desktop(self):
        return True


def main() -> int:
    if not _prepare_ffmpeg():
        input("\nBấm ENTER để đóng...")
        return 1

    try:
        import webview
    except ImportError:
        print("[x] Thiếu pywebview. Cài bằng:\n"
              "    python -m pip install -r gui\\requirements-gui.txt")
        input("\nBấm ENTER để đóng...")
        return 1

    from autodub import server
    from autodub.utils import log

    # Hiển thị nguồn chạy ngay khi khởi động để không nhầm shortcut/bản copy
    # cũ (đặc biệt khi đã có nhiều thư mục AutoDubVN trên máy).
    log(f"Build content-planner 2026-08-21.6 · nguồn: {ROOT}", "dim")
    log(f"Python runtime: {sys.executable}", "dim")

    port = _free_port()
    url = f"http://127.0.0.1:{port}/"

    # Máy chủ chạy nền, chỉ nghe trên localhost
    t = threading.Thread(target=server.serve,
                         kwargs={"port": port, "open_browser": False},
                         daemon=True)
    t.start()

    # chờ máy chủ sẵn sàng
    for _ in range(60):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.4):
                break
        except OSError:
            time.sleep(0.15)
    else:
        log("Máy chủ nội bộ không khởi động được.", "err")
        return 1

    api = Api()
    window = webview.create_window(
        "AutoDubVN · By Crazyscholar",
        url,
        js_api=api,
        width=1500,
        height=940,
        min_size=(1180, 720),
        frameless=True,          # tự vẽ thanh tiêu đề trong HTML
        easy_drag=False,         # chỉ kéo được ở vùng .pywebview-drag-region
        background_color="#0b1016",
        text_select=False,
    )
    _WIN["w"] = window
    _WIN["maxed"] = False

    log(f"Đang mở cửa sổ AutoDubVN (nội bộ: {url})", "ok")
    try:
        webview.start()          # chặn ở đây tới khi đóng cửa sổ
    except Exception as e:
        log(f"Không mở được cửa sổ ({e}).", "err")
        log("Máy có thể thiếu 'Microsoft Edge WebView2 Runtime'. Tải tại: "
            "https://developer.microsoft.com/microsoft-edge/webview2/", "warn")
        input("\nBấm ENTER để đóng...")
        return 1
    finally:
        # Báo hủy từng job và chờ có giới hạn. Worker còn kẹt trong thư viện
        # bên thứ ba là daemon nên không giữ tiến trình sống vô hạn.
        server.shutdown_background_jobs(wait=True, timeout=12.0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
