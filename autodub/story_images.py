"""Gói prompt/ảnh bền vững cho luồng video kể chuyện.

Danh sách ảnh trước đây chỉ sống trong JavaScript nên mất khi đóng giao diện.
Module này lưu toàn bộ thứ tự cảnh, prompt và file ảnh vào một manifest JSON.
Người dùng có thể mang ``PROMPTS_AI_STUDIO.txt`` sang Gemini/AI Studio, tải
ảnh về rồi gắn lại; bước dựng video luôn đọc ảnh theo đúng thứ tự trong manifest.
"""
from __future__ import annotations

import base64
import json
import os
import re
import shutil
import time
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence


GEMINI_WEB_URL = "https://gemini.google.com/app"
GEMINI_INTERACTIONS_URL = "https://generativelanguage.googleapis.com/v1beta/interactions"
DEFAULT_IMAGE_MODEL = "gemini-3.1-flash-lite-image"
DEFAULT_BROWSER_IMAGE_MODEL = "Nano Banana 2 (Gemini web)"
IMAGE_EXTENSIONS = {
    ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff"
}
PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PACK_ROOT = PROJECT_ROOT / "output" / "story_image_packs"


def gemini_browser_settings(cfg: Dict) -> Dict:
    """Lấy cấu hình Gemini web, mặc định dùng chung profile với phần dịch."""
    tr = cfg.get("translation") if isinstance(cfg.get("translation"), dict) else {}
    image_cfg = cfg.get("tao_anh") if isinstance(cfg.get("tao_anh"), dict) else {}
    raw_profile = str(image_cfg.get("browser_profile") or
                      tr.get("browser_profile") or "browser_profile").strip()
    profile = Path(raw_profile).expanduser()
    if not profile.is_absolute():
        profile = PROJECT_ROOT / profile
    return {
        "profile_dir": str(profile.resolve()),
        "channel": str(image_cfg.get("browser_channel") or
                       tr.get("browser_channel") or "msedge").strip(),
        "url": str(image_cfg.get("browser_url") or GEMINI_WEB_URL).strip(),
        "timeout": max(45.0, float(image_cfg.get(
            "wait_image_seconds", 90) or 90)),
        "wait_reply": max(30, int(image_cfg.get(
            "wait_prompt_seconds", tr.get("wait_reply", 180)) or 180)),
        "retries": max(1, int(image_cfg.get(
            "browser_retries", 2) or 2)),
        "request_gap": max(0.0, float(image_cfg.get("request_gap_seconds", 1.5) or 0)),
        "session_restarts": max(0, int(image_cfg.get(
            "browser_session_restarts", 5) or 0)),
        "fresh_chat_every": max(0, int(image_cfg.get(
            "browser_fresh_chat_every", 2) or 0)),
        "restart_cooldown": max(0.0, float(image_cfg.get(
            "browser_restart_cooldown_seconds", 10) or 0)),
    }


def _slug(text: str, fallback: str = "truyen") -> str:
    plain = unicodedata.normalize("NFKD", str(text or ""))
    plain = "".join(ch for ch in plain if not unicodedata.combining(ch))
    plain = plain.replace("đ", "d").replace("Đ", "D")
    plain = re.sub(r"[^A-Za-z0-9]+", "_", plain).strip("_").lower()
    return (plain or fallback)[:64]


def _manifest_path(path: str | os.PathLike) -> Path:
    p = Path(path).expanduser().resolve()
    return p / "manifest.json" if p.is_dir() else p


def _write_json(path: Path, payload: Dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _expand_images(paths: Iterable[str]) -> List[str]:
    out: List[str] = []
    for raw in paths or []:
        p = Path(str(raw or "").strip().strip('"')).expanduser()
        if p.is_dir():
            files = sorted(
                (x for x in p.iterdir()
                 if x.is_file() and x.suffix.lower() in IMAGE_EXTENSIONS),
                key=lambda x: [int(t) if t.isdigit() else t.lower()
                               for t in re.split(r"(\d+)", x.name)],
            )
            out.extend(str(x.resolve()) for x in files)
        elif p.is_file() and p.suffix.lower() in IMAGE_EXTENSIONS:
            out.append(str(p.resolve()))
    # Bỏ trùng nhưng giữ nguyên thứ tự người dùng đã chọn.
    seen = set()
    unique = []
    for p in out:
        key = os.path.normcase(os.path.abspath(p))
        if key not in seen:
            seen.add(key)
            unique.append(os.path.abspath(p))
    return unique


def parse_scene_prompts(text: str) -> List[str]:
    """Đọc prompt đánh số, kể cả Markdown ``**1.** ...`` của Gemini."""
    matches = list(re.finditer(
        r"(?ms)^\s*(?:\*\*)?(\d{1,3})[.)](?:\*\*)?\s+(.+?)"
        r"(?=^\s*(?:\*\*)?\d{1,3}[.)](?:\*\*)?\s+|^\s*NEGATIVE\s*:|\Z)",
        text or "",
    ))
    return [re.sub(r"\s+", " ", m.group(2)).strip() for m in matches
            if len(re.sub(r"\s+", " ", m.group(2)).strip()) > 20]


def generate_scene_prompts(master_prompt: str, cfg: Dict,
                           expected_count: int = 14,
                           logger: Optional[Callable] = None) -> List[str]:
    """Dùng provider văn bản đang cấu hình để rút prompt từng cảnh thật.

    Chỉ gọi Gemini API khi phần tạo ảnh được cấu hình rõ ``provider: api``.
    Ở chế độ browser, key cũ còn sót trong cấu hình dịch không được dùng nhầm;
    hàm dùng Gemini web với profile đã đăng nhập khi provider văn bản cũng là browser.
    """
    logger = logger or (lambda _msg, _kind="info": None)
    tr = dict(cfg.get("translation")
              if isinstance(cfg.get("translation"), dict) else {})
    provider = str(tr.get("provider") or "browser").strip().lower()
    image_cfg = cfg.get("tao_anh") if isinstance(cfg.get("tao_anh"), dict) else {}
    image_provider = str(image_cfg.get("provider") or "browser").strip().lower()
    # Browser ảnh vẫn có thể dùng provider văn bản khác (NVIDIA/TokenRouter)
    # để rút prompt cảnh. Nếu phần dịch cũng là browser/Gemini thì hỏi trực
    # tiếp Gemini web bằng đúng profile đăng nhập, tuyệt đối không mượn key cũ.
    browser_images = image_provider not in {"api", "gemini"}
    if browser_images and provider in {"browser", "gemini"}:
        settings = gemini_browser_settings(cfg)
        try:
            return generate_scene_prompts_gemini_browser(
                master_prompt, expected_count=expected_count,
                profile_dir=settings["profile_dir"], channel=settings["channel"],
                url=settings["url"], wait_reply=settings["wait_reply"],
                logger=logger)
        except Exception as exc:
            logger("Chưa tự rút được prompt bằng Gemini web: %s" %
                   str(exc)[:180], "warn")
            return []
    if provider == "browser" and image_provider in {"api", "gemini"}:
        image_key = str(image_cfg.get("gemini_api_key") or
                        tr.get("gemini_api_key") or "").strip()
        if image_key:
            tr["gemini_api_key"] = image_key
            provider = "gemini"
    try:
        from . import translate
        api_key, model, base_url, timeout = translate.api_params_for_provider(
            tr, provider)
        if provider == "browser" or not str(api_key or "").strip():
            return []
        logger("Đang rút prompt hình ảnh bám theo 6 chương…", "step")
        raw = translate._api_call(
            master_prompt, api_key, model, 0.35, provider=provider,
            api_base_url=base_url, api_timeout=timeout)
        prompts = parse_scene_prompts(raw)
        if len(prompts) < expected_count:
            logger("AI chỉ trả %d/%d prompt cảnh; chuyển sang Gemini web."
                   % (len(prompts), expected_count), "warn")
            if browser_images:
                settings = gemini_browser_settings(cfg)
                try:
                    return generate_scene_prompts_gemini_browser(
                        master_prompt, expected_count=expected_count,
                        profile_dir=settings["profile_dir"],
                        channel=settings["channel"], url=settings["url"],
                        wait_reply=settings["wait_reply"], logger=logger)
                except Exception as browser_exc:
                    logger("Gemini web cũng chưa rút được prompt: %s" %
                           str(browser_exc)[:180], "warn")
            return []
        return prompts[:expected_count]
    except Exception as exc:
        logger("Chưa tự rút được prompt từng cảnh: %s" % str(exc)[:180], "warn")
        if browser_images:
            settings = gemini_browser_settings(cfg)
            try:
                return generate_scene_prompts_gemini_browser(
                    master_prompt, expected_count=expected_count,
                    profile_dir=settings["profile_dir"], channel=settings["channel"],
                    url=settings["url"], wait_reply=settings["wait_reply"],
                    logger=logger)
            except Exception as browser_exc:
                logger("Gemini web cũng chưa rút được prompt: %s" %
                       str(browser_exc)[:180], "warn")
        return []


class GeminiImageError(RuntimeError):
    def __init__(self, message: str, status: int = 0, retry_after: float = 0.0):
        super().__init__(message)
        self.status = int(status or 0)
        self.retry_after = max(0.0, float(retry_after or 0.0))


class GeminiBrowserError(RuntimeError):
    """Lỗi có thể đọc được của luồng điều khiển Gemini web."""


def generate_scene_prompts_gemini_browser(
        master_prompt: str, expected_count: int, profile_dir: str,
        channel: str = "msedge", url: str = GEMINI_WEB_URL,
        wait_reply: int = 180, logger: Optional[Callable] = None) -> List[str]:
    """Nhờ Gemini web lập prompt cảnh bằng profile đã đăng nhập."""
    logger = logger or (lambda _msg, _kind="info": None)
    try:
        from . import translate
        logger("Đang rút prompt cảnh bằng Gemini đã đăng nhập…", "step")
        with translate.phien_gemini_trinh_duyet(
                profile_dir, channel=channel, url=url,
                wait_reply=wait_reply) as ask:
            raw = ask(master_prompt)
        prompts = parse_scene_prompts(raw)
        if len(prompts) < expected_count:
            raise GeminiBrowserError(
                "Gemini chỉ trả %d/%d prompt cảnh." %
                (len(prompts), expected_count))
        logger("Đã nhận đủ %d prompt cảnh từ Gemini web." % expected_count, "ok")
        return prompts[:expected_count]
    except GeminiBrowserError:
        raise
    except Exception as exc:
        raise GeminiBrowserError(
            "Không rút được prompt bằng Gemini web: %s" % str(exc)[:220]) from exc


def _visible_role(page, role: str, name_pattern, timeout: float = 30.0):
    """Tìm phần tử role đang hiện; Gemini thường giữ vài bản DOM đã ẩn."""
    deadline = time.monotonic() + max(0.1, float(timeout))
    while time.monotonic() < deadline:
        try:
            loc = page.get_by_role(role, name=name_pattern)
            for idx in range(min(loc.count(), 8)):
                item = loc.nth(idx)
                if item.is_visible(timeout=500):
                    return item
        except Exception:
            pass
        page.wait_for_timeout(350)
    return None


class _GeminiWebImageSession:
    """Một cửa sổ Gemini web tạo nhiều cảnh liên tiếp trong cùng cuộc chat."""

    _INPUT_NAME = re.compile(
        r"Nhập câu lệnh cho Gemini|Mô tả hình ảnh|Enter a prompt|Ask Gemini|Describe your image",
        re.IGNORECASE)
    _TOOLS_NAME = re.compile(
        r"Nội dung tải lên và công cụ|Upload files|Add files|files and tools",
        re.IGNORECASE)
    _IMAGE_MODE_NAME = re.compile(
        r"Tạo hình ảnh|Create images?|Generate images?", re.IGNORECASE)
    _IMAGE_MODE_ACTIVE = re.compile(
        r"Bỏ chọn Hình ảnh|Remove Image|Deselect Image", re.IGNORECASE)
    _SEND_NAME = re.compile(r"Gửi tin nhắn|Send message|Submit", re.IGNORECASE)
    _STOP_NAME = re.compile(
        r"Ngừng tạo câu trả lời|Stop response|Stop generating", re.IGNORECASE)
    _DOWNLOAD_SELECTOR = ", ".join((
        "button[aria-label*='Tải hình ảnh có kích thước đầy đủ' i]",
        "button[aria-label*='Download full size image' i]",
        "button[aria-label*='Download image' i]",
    ))
    _FAILURE_TEXT = (
        "i don't seem to have access to that content",
        "failed to generate",
        "something went wrong",
        "đã xảy ra lỗi",
        "không thể tạo",
        "yêu cầu tạo ảnh bị từ chối",
    )

    def __init__(self, profile_dir: str, channel: str, url: str,
                 timeout: float, logger: Callable, cancel_event=None):
        self.profile_dir = os.path.abspath(profile_dir)
        self.channel = str(channel or "msedge").strip()
        self.url = str(url or GEMINI_WEB_URL).strip()
        self.timeout = max(45.0, float(timeout or 240))
        self.logger = logger
        self.cancel_event = cancel_event
        self._pw = None
        self.context = None
        self.page = None

    def _cancelled(self) -> None:
        if self.cancel_event is not None and self.cancel_event.is_set():
            raise InterruptedError("Đã huỷ tạo ảnh.")

    def _sleep(self, seconds: float) -> None:
        end = time.monotonic() + max(0.0, float(seconds))
        while time.monotonic() < end:
            self._cancelled()
            time.sleep(min(0.25, end - time.monotonic()))

    def __enter__(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as exc:
            raise GeminiBrowserError(
                "Chưa cài Playwright; hãy chạy install.bat trước.") from exc
        Path(self.profile_dir).mkdir(parents=True, exist_ok=True)
        self.logger("Mở Gemini bằng hồ sơ: %s" % self.profile_dir, "step")
        self._pw = sync_playwright().start()
        order = [self.channel] if self.channel else []
        order += [x for x in ("msedge", "chrome", None) if x not in order]
        last_error = None
        for browser_channel in order:
            for attempt in range(1, 3):
                self._cancelled()
                try:
                    kwargs = dict(
                        user_data_dir=self.profile_dir,
                        headless=False,
                        args=["--start-maximized",
                              "--disable-blink-features=AutomationControlled"],
                        ignore_default_args=["--enable-automation"],
                        no_viewport=True,
                        accept_downloads=True,
                        locale="vi-VN",
                    )
                    if browser_channel:
                        kwargs["channel"] = browser_channel
                    self.context = self._pw.chromium.launch_persistent_context(**kwargs)
                    break
                except Exception as exc:
                    last_error = exc
                    if attempt < 2:
                        self._sleep(3)
            if self.context is not None:
                break
        if self.context is None:
            self.close()
            brief = (str(last_error).strip().splitlines() or
                     [type(last_error).__name__ if last_error else "không rõ"])[0]
            raise GeminiBrowserError(
                "Không mở được hồ sơ Gemini. Hãy đóng cửa sổ đã mở bởi "
                "login_gemini.bat rồi chạy lại. (%s)" % brief[:180])

        self.page = self.context.pages[0] if self.context.pages else self.context.new_page()
        self.page.set_default_timeout(30000)
        try:
            self.page.goto(self.url, wait_until="domcontentloaded", timeout=90000)
        except Exception:
            # Gemini là SPA; dù wait_until lỗi, ô nhập thường vẫn dựng được.
            pass
        box = _visible_role(self.page, "textbox", self._INPUT_NAME, timeout=45)
        if box is None:
            current = str(self.page.url or "")
            self.close()
            if "accounts.google.com" in current:
                detail = "hồ sơ chưa đăng nhập Google"
            else:
                detail = "không thấy ô nhập Gemini"
            raise GeminiBrowserError(
                "%s. Hãy chạy login_gemini.bat, đăng nhập xong rồi đóng "
                "toàn bộ cửa sổ đó trước khi chạy AutoDubVN." % detail.capitalize())
        self._enable_image_mode()
        return self

    def close(self) -> None:
        for closer in (getattr(self.context, "close", None),
                       getattr(self._pw, "stop", None)):
            try:
                if closer:
                    closer()
            except Exception:
                pass
        self.context = None
        self.page = None
        self._pw = None

    def __exit__(self, _exc_type, _exc, _tb):
        self.close()
        return False

    def _enable_image_mode(self) -> None:
        active = _visible_role(
            self.page, "button", self._IMAGE_MODE_ACTIVE, timeout=2)
        if active is not None:
            return
        tools = _visible_role(self.page, "button", self._TOOLS_NAME, timeout=15)
        if tools is None:
            raise GeminiBrowserError("Không tìm thấy nút Công cụ của Gemini.")
        tools.click(timeout=10000)
        image_mode = _visible_role(
            self.page, "menuitemcheckbox", self._IMAGE_MODE_NAME, timeout=10)
        if image_mode is None:
            raise GeminiBrowserError(
                "Tài khoản Gemini này chưa hiện mục 'Tạo hình ảnh'.")
        image_mode.click(timeout=10000)
        if _visible_role(self.page, "button", self._IMAGE_MODE_ACTIVE,
                         timeout=10) is None:
            raise GeminiBrowserError("Gemini không bật được chế độ Tạo hình ảnh.")

    def _is_generating(self) -> bool:
        return _visible_role(self.page, "button", self._STOP_NAME, timeout=0.2) is not None

    def _failure_detail(self) -> str:
        try:
            text = (self.page.locator("main").inner_text(timeout=2000) or "").lower()
        except Exception:
            return ""
        for marker in self._FAILURE_TEXT:
            if marker in text:
                return marker
        return ""

    def open_clean_chat(self) -> None:
        """Dừng lượt cũ và mở chat sạch nhưng giữ nguyên phiên đăng nhập."""
        self._cancelled()
        try:
            stop = _visible_role(self.page, "button", self._STOP_NAME, timeout=2)
            if stop is not None:
                stop.click(timeout=5000)
        except Exception:
            pass
        try:
            self.page.goto(self.url, wait_until="domcontentloaded", timeout=60000)
        except Exception:
            pass
        box = _visible_role(self.page, "textbox", self._INPUT_NAME, timeout=35)
        if box is None:
            raise GeminiBrowserError(
                "Gemini không phục hồi được ô nhập sau khi mở chat sạch.")
        self._enable_image_mode()

    def recover_after_failure(self, scene_index: int, reason: Exception) -> None:
        """Dừng lượt Gemini bị kẹt và mở chat sạch trước khi thử lại."""
        self.logger(
            "Khôi phục Gemini sau lỗi cảnh %03d; mở cuộc trò chuyện mới trước khi thử lại."
            % int(scene_index), "info")
        self.open_clean_chat()

    def generate_scene(self, scene_index: int, prompt: str,
                       images_dir: Path, aspect: str,
                       heartbeat: Optional[Callable[[str], None]] = None) -> Path:
        """Gửi một prompt, chờ ảnh mới và tải file kích thước đầy đủ."""
        self._cancelled()
        self._enable_image_mode()
        box = _visible_role(self.page, "textbox", self._INPUT_NAME, timeout=20)
        if box is None:
            raise GeminiBrowserError("Không còn thấy ô nhập Gemini.")
        before = self.page.locator(self._DOWNLOAD_SELECTOR).count()
        request = (
            "Create exactly one image for this story scene. Preserve the same "
            "characters, clothing, locations, cinematic color palette and visual "
            "style. Treat all character details in the scene prompt as canonical, "
            "including in a fresh chat. Use aspect ratio %s. No text, "
            "caption, logo, watermark or collage.\n\nSCENE %03d:\n%s"
            % ("9:16" if str(aspect) == "9:16" else "16:9",
               int(scene_index), str(prompt or "").strip()))
        box.fill(request, timeout=20000)
        send = _visible_role(self.page, "button", self._SEND_NAME, timeout=10)
        if send is not None:
            send.click(timeout=10000)
        else:
            box.press("Enter", timeout=10000)

        sent_at = time.monotonic()
        deadline = sent_at + self.timeout
        startup_limit = min(self.timeout, max(35.0, self.timeout * 0.5))
        started = False
        finished_at = None
        next_heartbeat = sent_at
        next_log = sent_at + 30.0
        while time.monotonic() < deadline:
            self._cancelled()
            now = time.monotonic()
            elapsed = int(now - sent_at)
            if now >= next_heartbeat:
                message = ("Đang chờ Gemini tạo cảnh %03d · %d/%d giây"
                           % (scene_index, elapsed, int(self.timeout)))
                if heartbeat:
                    heartbeat(message)
                next_heartbeat = now + 8.0
            if now >= next_log:
                self.logger("Cảnh %03d vẫn đang xử lý (%d/%d giây)…" %
                            (scene_index, elapsed, int(self.timeout)), "info")
                next_log = now + 30.0
            buttons = self.page.locator(self._DOWNLOAD_SELECTOR)
            if buttons.count() > before:
                button = buttons.nth(buttons.count() - 1)
                images_dir.mkdir(parents=True, exist_ok=True)
                with self.page.expect_download(timeout=45000) as download_info:
                    button.click(timeout=15000)
                download = download_info.value
                suffix = Path(str(download.suggested_filename or "")).suffix.lower()
                if suffix not in IMAGE_EXTENSIONS:
                    suffix = ".png"
                dest = images_dir / ("scene_%03d%s" % (scene_index, suffix))
                temp = images_dir / ("._scene_%03d%s.part" % (scene_index, suffix))
                download.save_as(str(temp))
                if not temp.is_file() or temp.stat().st_size < 1024:
                    try:
                        temp.unlink()
                    except OSError:
                        pass
                    raise GeminiBrowserError(
                        "Gemini tải về file ảnh rỗng ở cảnh %03d." % scene_index)
                os.replace(temp, dest)
                return dest

            generating = self._is_generating()
            if generating:
                started = True
                finished_at = None
            elif started:
                finished_at = finished_at or time.monotonic()
                if time.monotonic() - finished_at >= 3.0:
                    detail = self._failure_detail()
                    raise GeminiBrowserError(
                        "Gemini kết thúc nhưng không trả ảnh ở cảnh %03d%s."
                        % (scene_index, " (%s)" % detail if detail else ""))
            elif not started and now - sent_at >= startup_limit:
                detail = self._failure_detail()
                raise GeminiBrowserError(
                    "Gemini không bắt đầu tạo ảnh cảnh %03d sau %d giây%s."
                    % (scene_index, int(startup_limit),
                       " (%s)" % detail if detail else ""))
            self._sleep(0.8)
        raise GeminiBrowserError(
            "Hết %d giây chờ ảnh cảnh %03d." % (int(self.timeout), scene_index))


def _gemini_image_request(prompt: str, api_key: str, model: str,
                          aspect: str, timeout: float = 180.0) -> tuple[bytes, str]:
    """Gọi Interactions API chính thức và lấy ảnh inline đầu tiên."""
    payload = {
        "model": str(model or DEFAULT_IMAGE_MODEL),
        "input": str(prompt or "").strip(),
        "response_format": {
            "type": "image", "mime_type": "image/jpeg",
            "aspect_ratio": "9:16" if str(aspect) == "9:16" else "16:9",
            "image_size": "1K",
        },
    }
    req = urllib.request.Request(
        GEMINI_INTERACTIONS_URL,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 "x-goog-api-key": str(api_key or "").strip()},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=max(30.0, float(timeout))) as resp:
            data = json.loads(resp.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as exc:
        try:
            detail = exc.read().decode("utf-8", "replace")[:500]
        except Exception:
            detail = str(exc)
        try:
            retry_after = float(exc.headers.get("Retry-After") or 0)
        except (TypeError, ValueError, AttributeError):
            retry_after = 0.0
        raise GeminiImageError(
            "Gemini Image HTTP %d: %s" % (exc.code, detail),
            status=exc.code, retry_after=retry_after) from exc
    except Exception as exc:
        raise GeminiImageError("Không gọi được Gemini Image: %s" % exc) from exc

    for step in data.get("steps") or []:
        if str(step.get("type") or "") != "model_output":
            continue
        for block in step.get("content") or []:
            if str(block.get("type") or "") != "image" or not block.get("data"):
                continue
            try:
                raw = base64.b64decode(str(block["data"]), validate=True)
            except Exception as exc:
                raise GeminiImageError("Gemini trả dữ liệu ảnh base64 bị hỏng.") from exc
            if len(raw) < 1024:
                raise GeminiImageError("Gemini trả ảnh rỗng hoặc quá nhỏ.")
            return raw, str(block.get("mime_type") or "image/jpeg")
    raise GeminiImageError("Gemini hoàn tất nhưng không trả ảnh.")


def _save_generated_scene(path: str | os.PathLike, scene_index: int,
                          image_bytes: bytes, mime_type: str) -> Dict:
    """Lưu ngay từng ảnh và cập nhật manifest để lỗi giữa chừng không mất công."""
    manifest_path = _manifest_path(path)
    manifest = load_pack(manifest_path)
    scenes = list(manifest.get("scenes") or [])
    if scene_index < 1 or scene_index > len(scenes):
        raise IndexError("Chỉ số cảnh nằm ngoài manifest.")
    ext = ".png" if "png" in str(mime_type).lower() else ".jpg"
    image_dir = manifest_path.parent / "images"
    image_dir.mkdir(parents=True, exist_ok=True)
    dest = image_dir / f"scene_{scene_index:03d}{ext}"
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(image_bytes)
    os.replace(tmp, dest)
    scenes[scene_index - 1].update({
        "expected_file": str(dest.relative_to(manifest_path.parent)).replace("\\", "/"),
        "image_path": str(dest.resolve()), "status": "ready",
    })
    manifest["scenes"] = scenes
    manifest["ready_count"] = sum(1 for scene in scenes if scene.get("status") == "ready")
    manifest["status"] = ("ready" if manifest["ready_count"] == len(scenes)
                          else "generating_images")
    manifest["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    manifest.pop("manifest_path", None)
    manifest.pop("pack_dir", None)
    _write_json(manifest_path, manifest)
    return load_pack(manifest_path)


def _register_generated_scene_file(path: str | os.PathLike, scene_index: int,
                                   image_path: str | os.PathLike) -> Dict:
    """Gắn file Gemini web vừa tải vào đúng cảnh và cập nhật manifest."""
    manifest_path = _manifest_path(path)
    manifest = load_pack(manifest_path)
    scenes = list(manifest.get("scenes") or [])
    if scene_index < 1 or scene_index > len(scenes):
        raise IndexError("Chỉ số cảnh nằm ngoài manifest.")
    source = Path(image_path).resolve()
    if not source.is_file() or source.suffix.lower() not in IMAGE_EXTENSIONS:
        raise GeminiBrowserError("File ảnh tải về không hợp lệ: %s" % source)
    scenes[scene_index - 1].update({
        "expected_file": str(source.relative_to(manifest_path.parent)).replace("\\", "/"),
        "image_path": str(source),
        "status": "ready",
    })
    manifest["scenes"] = scenes
    manifest["ready_count"] = sum(1 for item in scenes
                                  if item.get("status") == "ready")
    manifest["status"] = ("ready" if manifest["ready_count"] == len(scenes)
                          else "generating_images")
    manifest["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    manifest.pop("manifest_path", None)
    manifest.pop("pack_dir", None)
    _write_json(manifest_path, manifest)
    return load_pack(manifest_path)


def generate_images_gemini_browser(
        path: str | os.PathLike, profile_dir: str,
        channel: str = "msedge", url: str = GEMINI_WEB_URL,
        timeout: float = 240.0, max_retries: int = 3,
        request_gap: float = 1.5, max_session_restarts: int = 5,
        fresh_chat_every: int = 2, restart_cooldown: float = 10.0,
        logger: Optional[Callable] = None,
        progress: Optional[Callable] = None, cancel_event=None) -> Dict:
    """Tạo ảnh bằng Gemini web và tự phục hồi trình duyệt khi phiên bị chai.

    Mỗi ảnh được ghi manifest ngay. Sau vài ảnh app chủ động mở chat sạch; nếu
    một cảnh vẫn lỗi hết số lượt thử, toàn bộ phiên Playwright được đóng/mở lại
    rồi tiếp tục chính cảnh đó thay vì trả lỗi bắt người dùng khởi động app.
    """
    logger = logger or (lambda _msg, _kind="info": None)
    progress = progress or (lambda _done, _total, _message="": None)
    retries = max(1, int(max_retries or 1))
    allowed_restarts = max(0, int(max_session_restarts or 0))
    fresh_every = max(0, int(fresh_chat_every or 0))
    restart_count = 0

    def cancelable_wait(seconds: float) -> None:
        deadline = time.monotonic() + max(0.0, float(seconds))
        while time.monotonic() < deadline:
            if cancel_event is not None and cancel_event.is_set():
                raise InterruptedError("Đã huỷ tạo ảnh.")
            time.sleep(min(.25, deadline - time.monotonic()))

    while True:
        manifest = load_pack(path)
        scenes = list(manifest.get("scenes") or [])
        missing = [scene for scene in scenes
                   if scene.get("status") != "ready" and
                   str(scene.get("prompt") or "").strip()]
        if not missing:
            if scenes and int(manifest.get("ready_count", 0) or 0) == len(scenes):
                return manifest
            raise GeminiBrowserError("Gói ảnh chưa có prompt từng cảnh để tự sinh.")
        total_scenes = len(scenes)
        try:
            with _GeminiWebImageSession(
                    profile_dir, channel, url, timeout, logger,
                    cancel_event=cancel_event) as session:
                made_in_chat = 0
                for scene in missing:
                    index = int(scene.get("index") or 1)
                    if fresh_every and made_in_chat >= fresh_every:
                        refresh = getattr(session, "open_clean_chat", None)
                        if callable(refresh):
                            logger("Đã tạo %d ảnh; chủ động mở chat Gemini sạch để tránh phiên bị chai."
                                   % made_in_chat, "info")
                            refresh()
                        made_in_chat = 0
                    last_error = None
                    for attempt in range(1, retries + 1):
                        try:
                            ready_now = int(manifest.get("ready_count", 0) or 0)
                            downloaded = session.generate_scene(
                                index, str(scene.get("prompt") or ""),
                                Path(manifest["pack_dir"]) / "images",
                                str(manifest.get("aspect") or "16:9"),
                                heartbeat=lambda message, completed=ready_now: progress(
                                    completed, total_scenes, message))
                            manifest = _register_generated_scene_file(
                                path, index, downloaded)
                            ready_now = int(manifest.get("ready_count", 0) or 0)
                            message = ("Đã tải cảnh %03d · tổng %d/%d"
                                       % (index, ready_now, total_scenes))
                            progress(ready_now, total_scenes, message)
                            logger("Đã tạo và tải ảnh cảnh %03d bằng Gemini web." % index,
                                   "ok")
                            last_error = None
                            made_in_chat += 1
                            break
                        except InterruptedError:
                            raise
                        except Exception as exc:
                            last_error = exc
                            logger("Cảnh %03d lỗi lượt %d/%d: %s" %
                                   (index, attempt, retries, str(exc)[:180]), "warn")
                            if attempt < retries:
                                recover = getattr(session, "recover_after_failure", None)
                                if callable(recover):
                                    recover(index, exc)
                                session._sleep(min(12.0, 2.5 * attempt))
                    if last_error is not None:
                        raise GeminiBrowserError(str(last_error)) from last_error
                    if request_gap > 0:
                        session._sleep(request_gap)
            return load_pack(path)
        except InterruptedError:
            raise
        except Exception as exc:
            if restart_count >= allowed_restarts:
                raise GeminiBrowserError(
                    "Gemini vẫn lỗi sau %d lần tự khởi động lại: %s" %
                    (restart_count, str(exc))) from exc
            restart_count += 1
            delay = min(45.0, max(0.0, float(restart_cooldown)) * restart_count)
            current = load_pack(path)
            ready_now = int(current.get("ready_count", 0) or 0)
            message = ("Gemini bị kẹt; app tự mở lại phiên %d/%d sau %.0f giây "
                       "và tiếp tục từ %d/%d ảnh…" %
                       (restart_count, allowed_restarts, delay,
                        ready_now, total_scenes))
            logger(message, "warn")
            progress(ready_now, total_scenes, message)
            cancelable_wait(delay)


def generate_images_gemini(path: str | os.PathLike, api_key: str,
                           model: str = DEFAULT_IMAGE_MODEL,
                           timeout: float = 180.0, max_retries: int = 3,
                           request_gap: float = 1.5,
                           logger: Optional[Callable] = None,
                           progress: Optional[Callable] = None,
                           cancel_event=None) -> Dict:
    """Sinh các cảnh còn thiếu bằng Gemini, tuần tự để tránh rate-limit."""
    if not str(api_key or "").strip():
        raise GeminiImageError("Chưa cấu hình Gemini API key để tự tạo ảnh.")
    logger = logger or (lambda _msg, _kind="info": None)
    progress = progress or (lambda _done, _total, _message="": None)
    manifest = load_pack(path)
    scenes = list(manifest.get("scenes") or [])
    missing = [scene for scene in scenes
               if scene.get("status") != "ready" and str(scene.get("prompt") or "").strip()]
    if not missing:
        if int(manifest.get("ready_count", 0) or 0) == len(scenes) and scenes:
            return manifest
        raise GeminiImageError("Gói ảnh chưa có prompt từng cảnh để tự sinh.")
    retries = max(1, int(max_retries or 1))
    for done, scene in enumerate(missing, 1):
        if cancel_event is not None and cancel_event.is_set():
            raise InterruptedError("Đã huỷ tạo ảnh.")
        index = int(scene.get("index") or done)
        last_error: Optional[Exception] = None
        for attempt in range(1, retries + 1):
            try:
                raw, mime = _gemini_image_request(
                    str(scene.get("prompt") or ""), api_key, model,
                    str(manifest.get("aspect") or "16:9"), timeout=timeout)
                manifest = _save_generated_scene(path, index, raw, mime)
                progress(done, len(missing), "Đã tạo cảnh %03d/%03d" % (done, len(missing)))
                logger("Đã tạo ảnh cảnh %03d bằng Gemini." % index, "ok")
                last_error = None
                break
            except GeminiImageError as exc:
                last_error = exc
                if attempt >= retries or exc.status not in {0, 429, 500, 502, 503, 504}:
                    break
                wait = exc.retry_after or min(30.0, 2.0 ** attempt)
                logger("Cảnh %03d bị giới hạn; thử lại sau %.0f giây."
                       % (index, wait), "warn")
                time.sleep(wait)
        if last_error is not None:
            raise last_error
        if done < len(missing) and request_gap > 0:
            time.sleep(float(request_gap))
    return load_pack(path)


def _prompt_document(manifest: Dict, master_prompt: str) -> str:
    lines = [
        "GÓI PROMPT ẢNH AUTODUBVN",
        "",
        f"Gemini: {manifest['provider']['url']}",
        f"Model: {manifest['provider']['model']}",
        f"Khổ ảnh: {manifest['aspect']}",
        "",
        "QUY ƯỚC LƯU ẢNH",
        "- Tạo lần lượt từ cảnh 001 đến hết.",
        "- Tải ảnh về đúng tên gợi ý scene_001.png, scene_002.png, ...",
        "- Không đổi thứ tự. AutoDubVN sẽ ghi nhớ thứ tự bằng manifest.json.",
        "- Giữ cùng ngoại hình nhân vật, trang phục, bảng màu và phong cách giữa các cảnh.",
        "",
    ]
    if master_prompt.strip():
        lines.extend(["PROMPT TỔNG ĐỂ GEMINI LẬP DANH SÁCH CẢNH", "", master_prompt.strip(), ""])
    prompts = [s for s in manifest.get("scenes", []) if s.get("prompt")]
    if prompts:
        lines.extend(["PROMPT TỪNG CẢNH", ""])
        for scene in prompts:
            lines.extend([
                f"CẢNH {scene['index']:03d} -> {scene['expected_file']}",
                scene["prompt"].strip(),
                "",
            ])
    return "\n".join(lines).rstrip() + "\n"


def load_pack(path: str | os.PathLike) -> Dict:
    manifest_path = _manifest_path(path)
    with manifest_path.open("r", encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or int(payload.get("schema_version", 0)) != 1:
        raise ValueError("Manifest gói ảnh không hợp lệ.")
    payload["manifest_path"] = str(manifest_path)
    payload["pack_dir"] = str(manifest_path.parent)
    return payload


def attach_images(path: str | os.PathLike, image_paths: Sequence[str]) -> Dict:
    """Chép ảnh vào gói và gắn lần lượt với các cảnh; không phụ thuộc file gốc."""
    manifest_path = _manifest_path(path)
    manifest = load_pack(manifest_path)
    images = _expand_images(image_paths)
    image_dir = manifest_path.parent / "images"
    image_dir.mkdir(parents=True, exist_ok=True)

    # Chụp một bản trung gian trước khi ghi đích. Nếu người dùng đảo thứ tự các
    # ảnh vốn đã nằm trong pack, copy thẳng scene_002 -> scene_001 sẽ phá mất
    # nguồn của lượt kế tiếp.
    staged = []
    for idx, source in enumerate(images, 1):
        src = Path(source)
        ext = src.suffix.lower() if src.suffix.lower() in IMAGE_EXTENSIONS else ".png"
        temp = image_dir / f"._incoming_{idx:03d}{ext}"
        shutil.copy2(src, temp)
        staged.append(temp)

    scenes = list(manifest.get("scenes") or [])
    while len(scenes) < len(images):
        i = len(scenes) + 1
        scenes.append({
            "index": i, "chapter": None, "prompt": "",
            "expected_file": f"images/scene_{i:03d}.png",
            "image_path": "", "status": "missing",
        })

    for idx, src in enumerate(staged, 1):
        ext = src.suffix.lower()
        dest = image_dir / f"scene_{idx:03d}{ext}"
        os.replace(src, dest)
        scene = scenes[idx - 1]
        scene.update({
            "index": idx,
            "expected_file": str(dest.relative_to(manifest_path.parent)).replace("\\", "/"),
            "image_path": str(dest.resolve()),
            "status": "ready",
        })

    # Ảnh bị bỏ khỏi danh sách không được lén dùng lại khi dựng.
    for scene in scenes[len(images):]:
        scene["image_path"] = ""
        scene["status"] = "missing"

    manifest["scenes"] = scenes
    manifest["scene_count"] = len(scenes)
    manifest["ready_count"] = len(images)
    manifest["status"] = "ready" if images and len(images) == len(scenes) else "waiting_images"
    manifest["updated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    manifest.pop("manifest_path", None)
    manifest.pop("pack_dir", None)
    _write_json(manifest_path, manifest)
    return load_pack(manifest_path)


def create_pack(title: str, design_text: str = "", master_prompt: str = "",
                scene_prompts: Optional[Sequence[str]] = None,
                image_paths: Optional[Sequence[str]] = None,
                aspect: str = "16:9", scene_count: int = 14,
                script_path: str = "", design_path: str = "",
                root: str | os.PathLike | None = None,
                pack_dir: str | os.PathLike | None = None) -> Dict:
    """Tạo manifest, tài liệu prompt và thư mục ảnh độc lập có thể phục hồi."""
    prompts = [str(x or "").strip() for x in (scene_prompts or [])]
    count = max(1, int(scene_count or 14), len(prompts), len(image_paths or []))
    if pack_dir:
        folder = Path(pack_dir).expanduser().resolve()
    else:
        base = Path(root).expanduser().resolve() if root else DEFAULT_PACK_ROOT
        stamp = time.strftime("%Y%m%d_%H%M%S")
        folder = base / f"{_slug(title)}_{stamp}"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "images").mkdir(exist_ok=True)

    scenes = []
    for i in range(1, count + 1):
        scenes.append({
            "index": i,
            # Chia đều toàn bộ danh sách qua 6 chương, kể cả khi số cảnh
            # không chia hết (14 cảnh -> 3/2/3/2/2/2, không bỏ chương 6).
            "chapter": min(6, ((i - 1) * 6) // count + 1),
            "prompt": prompts[i - 1] if i <= len(prompts) else "",
            "expected_file": f"images/scene_{i:03d}.png",
            "image_path": "",
            "status": "missing",
        })
    now = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    manifest = {
        "schema_version": 1,
        "id": folder.name,
        "title": str(title or "Truyện").strip(),
        "created_at": now,
        "updated_at": now,
        "provider": {
            "name": "Gemini web",
            "model": DEFAULT_BROWSER_IMAGE_MODEL,
            "url": GEMINI_WEB_URL,
        },
        "aspect": "9:16" if str(aspect) == "9:16" else "16:9",
        "scene_count": count,
        "ready_count": 0,
        "status": "waiting_images",
        "script_path": os.path.abspath(script_path) if script_path else "",
        "design_path": os.path.abspath(design_path) if design_path else "",
        "prompt_file": str((folder / "PROMPTS_AI_STUDIO.txt").resolve()),
        "images_dir": str((folder / "images").resolve()),
        "scenes": scenes,
    }
    manifest_path = folder / "manifest.json"
    _write_json(manifest_path, manifest)
    (folder / "PROMPTS_AI_STUDIO.txt").write_text(
        _prompt_document(manifest, master_prompt), encoding="utf-8")
    if image_paths:
        return attach_images(manifest_path, list(image_paths))
    return load_pack(manifest_path)


def resolve_images(path: str | os.PathLike) -> List[str]:
    """Trả ảnh tồn tại theo index trong manifest; cảnh thiếu được bỏ qua rõ ràng."""
    manifest = load_pack(path)
    out = []
    for scene in sorted(manifest.get("scenes") or [], key=lambda x: int(x.get("index", 0))):
        # ``attach_images(..., [])`` dùng trạng thái missing để biểu thị người
        # dùng đã bấm Xoá hết. Không được thấy file scene_*.png còn trên đĩa
        # rồi tự ý nạp lại vào giao diện.
        if str(scene.get("status") or "missing") != "ready":
            continue
        raw = str(scene.get("image_path") or "")
        if not raw and scene.get("expected_file"):
            raw = str(Path(manifest["pack_dir"]) / str(scene["expected_file"]))
        if raw and Path(raw).is_file() and Path(raw).suffix.lower() in IMAGE_EXTENSIONS:
            out.append(str(Path(raw).resolve()))
    return out


def chapter_weights_from_script(script_path: str | os.PathLike) -> List[int]:
    """Lấy độ dài sáu chương cạnh KICH_BAN_DOC để rải ảnh đúng mạch truyện."""
    path = Path(script_path).expanduser().resolve()
    folder = path.parent
    chapter_files = sorted(folder.glob("chuong_*.txt"))
    weights = []
    for chapter in chapter_files[:6]:
        try:
            text = chapter.read_text(encoding="utf-8")
        except OSError:
            continue
        weights.append(max(1, len(re.findall(r"\w+", text, flags=re.UNICODE))))
    return weights if len(weights) >= 2 else []


def expand_for_chapters(path: str | os.PathLike, chapter_weights: Sequence[int],
                        total_duration: float, max_seconds: float = 25.0) -> List[str]:
    """Lặp ảnh theo nhóm chương để cảnh không bị quay vòng sai nội dung.

    Slideshow chia đều thời lượng mỗi phần. Ta phân bổ số phần theo số từ của
    từng chương, rồi chỉ quay vòng những ảnh thuộc chính chương đó. Sai số mốc
    chương tối đa xấp xỉ ``max_seconds`` thay vì ảnh chương 1 lặp tới cuối phim.
    """
    manifest = load_pack(path)
    ready = []
    for scene in sorted(manifest.get("scenes") or [], key=lambda x: int(x.get("index", 0))):
        if str(scene.get("status") or "missing") != "ready":
            continue
        raw = str(scene.get("image_path") or "")
        if not raw and scene.get("expected_file"):
            raw = str(Path(manifest["pack_dir"]) / str(scene["expected_file"]))
        if raw and Path(raw).is_file():
            ready.append((max(1, int(scene.get("chapter") or 1)), str(Path(raw).resolve())))
    if not ready:
        return []
    weights = [max(0, int(x or 0)) for x in chapter_weights]
    if not weights or sum(weights) <= 0:
        return [p for _c, p in ready]
    groups: Dict[int, List[str]] = {}
    for chapter, image in ready:
        groups.setdefault(chapter, []).append(image)
    total_parts = max(len(ready), int((max(0.1, float(total_duration)) + max_seconds - 1)
                                      // max_seconds))
    raw_counts = [total_parts * w / sum(weights) for w in weights]
    counts = [max(1 if groups.get(i + 1) else 0, int(x))
              for i, x in enumerate(raw_counts)]
    while sum(counts) < total_parts:
        candidates = sorted(range(len(weights)),
                            key=lambda i: (raw_counts[i] - int(raw_counts[i]), weights[i]),
                            reverse=True)
        counts[candidates[(sum(counts) - sum(int(x) for x in raw_counts)) % len(candidates)]] += 1
    while sum(counts) > total_parts:
        candidates = sorted(range(len(weights)), key=lambda i: counts[i], reverse=True)
        changed = False
        for i in candidates:
            minimum = 1 if groups.get(i + 1) else 0
            if counts[i] > minimum:
                counts[i] -= 1
                changed = True
                break
        if not changed:
            break

    all_images = [p for _c, p in ready]
    sequence = []
    for i, count in enumerate(counts, 1):
        choices = groups.get(i) or all_images
        sequence.extend(choices[j % len(choices)] for j in range(count))
    return sequence


def latest_pack(root: str | os.PathLike | None = None) -> Optional[Dict]:
    base = Path(root).expanduser().resolve() if root else DEFAULT_PACK_ROOT
    if not base.is_dir():
        return None
    files = list(base.glob("*/manifest.json"))
    if not files:
        return None
    return load_pack(max(files, key=lambda p: p.stat().st_mtime))


def public_summary(manifest: Dict, include_prompt: bool = False) -> Dict:
    """Dữ liệu gọn cho UI; tùy chọn kèm prompt dự phòng để sao chép sang Gemini."""
    result = {
        "ok": True,
        "title": str(manifest.get("title") or ""),
        "manifest_path": manifest.get("manifest_path", ""),
        "pack_dir": manifest.get("pack_dir", ""),
        "prompt_file": manifest.get("prompt_file", ""),
        "provider_url": (manifest.get("provider") or {}).get("url", GEMINI_WEB_URL),
        "status": manifest.get("status", "waiting_images"),
        "scene_count": int(manifest.get("scene_count", 0) or 0),
        "ready_count": int(manifest.get("ready_count", 0) or 0),
        "script_path": str(manifest.get("script_path") or ""),
        "images": resolve_images(manifest.get("manifest_path") or manifest.get("pack_dir")),
    }
    if include_prompt:
        prompts = [s for s in manifest.get("scenes") or []
                   if str(s.get("prompt") or "").strip()]
        if prompts:
            lines = [
                "Bạn đang ở chế độ tạo ảnh. Hãy tạo một bộ ảnh minh họa độc lập "
                "theo đúng thứ tự dưới đây.",
                "Không trả lại danh sách prompt, không ghép collage, không chèn "
                "chữ vào ảnh. Giữ nguyên ngoại hình nhân vật, trang phục, bối "
                "cảnh và bảng màu giữa tất cả các cảnh.",
                "Mỗi ảnh khổ %s. Hãy bắt đầu tạo từ CẢNH 001 và tiếp tục lần "
                "lượt; nếu hệ thống giới hạn số ảnh mỗi lượt thì tạo tối đa có "
                "thể rồi chờ tôi nhắn 'tiếp tục'." %
                str(manifest.get("aspect") or "16:9"),
                "",
            ]
            for scene in prompts:
                lines.extend([
                    "CẢNH %03d" % int(scene.get("index") or 0),
                    str(scene.get("prompt") or "").strip(),
                    "",
                ])
            result["prompt_text"] = "\n".join(lines).strip()
        else:
            prompt_file = str(manifest.get("prompt_file") or "")
            result["prompt_text"] = (
                Path(prompt_file).read_text(encoding="utf-8")
                if prompt_file and Path(prompt_file).is_file() else "")
    return result
