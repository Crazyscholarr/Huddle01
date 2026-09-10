"""Dịch phụ đề sang tiếng Việt, giữ NGỮ ĐIỆU và NHẤT QUÁN NHÂN VẬT.

Hai chế độ:
  - provider: gemini   -> gọi REST API (ổn định nhất, nên dùng nếu có API key)
  - provider: browser  -> điều khiển Edge/Chrome vào gemini.google.com, dùng
                          phiên đăng nhập Pro sẵn có, KHÔNG cần API key.

Chiến lược giữ nhất quán:
  - Dịch theo lô (chunk) kèm ngữ cảnh vài dòng đã dịch trước đó.
  - Nếu có nhãn người nói (speaker) thì đưa vào để mỗi nhân vật giữ văn phong riêng.
  - Bắt buộc trả về ĐÚNG số dòng, không thêm chú thích/đánh số.

CHỐNG MẤT CÔNG: mọi lô dịch xong đều được ghi ngay vào file cache cạnh output.
Chạy lại sau khi lỗi/tắt máy sẽ bỏ qua các lô đã dịch, không làm lại từ đầu.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.request
import urllib.error
from contextlib import contextmanager
from typing import Dict, List, Optional, Tuple

from .srt_utils import Segment, normalize_vi_subtitle_text
from .utils import log


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
TOKENROUTER_DEFAULT_BASE_URL = "https://api.tokenrouter.com/v1"
TOKENROUTER_DEFAULT_MODEL = "moonshotai/kimi-k3-free"
TOKENROUTER_GEMINI_DEFAULT_BASE_URL = "https://api.tokenrouter.com/v1beta/models"
TOKENROUTER_GEMINI_DEFAULT_MODEL = "google/gemini-3.6-flash"
INFERX_DEFAULT_BASE_URL = "https://model.inferx.net/endpoints/v1"
INFERX_DEFAULT_MODEL = "deepseek-v4-flash"
# NVIDIA NIM (build.nvidia.com): 1 key nvapi-... dùng chung cho MỌI model trong
# catalog, endpoint chuẩn OpenAI-compatible. MiniMax M3 là mặc định hiện hành;
# vẫn cho phép nhập model khác trong GUI mà không cần sửa mã.
NVIDIA_DEFAULT_BASE_URL = "https://integrate.api.nvidia.com/v1"
NVIDIA_DEFAULT_MODEL = "minimaxai/minimax-m3"
ZENMUX_DEFAULT_BASE_URL = "https://zenmux.ai/api/v1"
ZENMUX_DEFAULT_MODEL = "z-ai/glm-5.3-free"

SYSTEM_INSTRUCTION = (
    "Bạn là chuyên gia lồng tiếng & biên dịch phim chuyên nghiệp. Nhiệm vụ: dịch "
    "phụ đề sang TIẾNG VIỆT thật tự nhiên, đúng ngữ điệu nhân vật, khẩu ngữ đời "
    "thường, dễ hiểu ngay khi nghe (không dịch máy móc, tránh từ Hán Việt khó nếu "
    "có cách nói phổ thông tự nhiên hơn). Giữ nhất quán cách xưng hô của từng nhân "
    "vật xuyên suốt. Dịch thoáng cho hay nhưng phải ĐỦ NGHĨA: không bỏ chủ ngữ, "
    "tân ngữ, đại từ, phủ định, quan hệ nguyên nhân/kết quả; không lược cụm kiểu "
    "'nó', 'bọn nó', 'ngươi/ta' khiến câu cụt hoặc sai ý. TUYỆT ĐỐI không thêm "
    "giải thích, không ghi chú, không đánh số, không dùng dấu ba chấm (... hoặc …) "
    "để nối các mảnh câu; hãy viết câu Việt liền mạch. Kết quả KHÔNG được chứa "
    "chữ Hán/Trung; tên riêng phải phiên âm hoặc Việt hoá bằng chữ Latin. Trả về JSON array chuỗi, "
    "mỗi phần tử là bản dịch của đúng dòng tương ứng, đủ và đúng thứ tự."
)

# Ngân sách độ dài = thời lượng câu × chars_per_sec × MARGIN. Margin từng để
# 1.20 kèm SHORTEN_TRIGGER_RATIO 1.45, nghĩa là câu chỉ bị coi là "quá dài" khi
# vượt 1.74 lần mốc - đo trên 4 video thật thì bản dịch nằm ở 18-22 ký tự/giây
# trong khi mốc là 15, tức lọt hết vào vùng chết và KHÔNG câu nào được rút gọn.
# Hệ quả: TTS phải đọc nhanh 1.6× rồi cắt cụt đuôi, thoại kết thúc sớm hơn hình
# và người xem nghe thành "tiếng chạy trước hình".
TRANSLATION_BUDGET_MARGIN = 1.05
TRANSLATION_MIN_CHARS = 18
SHORTEN_TRIGGER_RATIO = 1.15
SHORTEN_KEEP_RATIO = 0.55
# Câu dài gấp đôi mốc được rút mạnh tay hơn, nhưng vẫn có sàn tuyệt đối để
# không bao giờ biến một câu thành mẩu cụt nghĩa.
SHORTEN_KEEP_RATIO_LONG = 0.40
TRANSLATION_CACHE_VERSION = "vi-natural-sync-v6"
ENABLE_BROWSER_SHORTENING = True
_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


# --------------------------------------------------------------------------- #
#  Bộ nhớ đệm theo lô: chạy lại sau khi lỗi thì không phải dịch lại từ đầu
# --------------------------------------------------------------------------- #
class ChunkCache:
    """Lưu kết quả từng lô ra file JSON. Khoá gắn với NỘI DUNG lô nên khi
    phụ đề gốc thay đổi thì cache tự vô hiệu, không dùng nhầm bản cũ."""

    def __init__(self, path: Optional[str]):
        self.path = path
        self.data: Dict[str, List[str]] = {}
        if path and os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    self.data = json.load(f)
                if self.data:
                    log(f"Tìm thấy {len(self.data)} lô đã dịch trước đó - dùng lại.", "ok")
            except Exception:
                self.data = {}

    @staticmethod
    def key(index: int, texts: List[str]) -> str:
        h = hashlib.md5("\n".join(texts).encode("utf-8")).hexdigest()[:12]
        return f"{TRANSLATION_CACHE_VERSION}-{index}-{len(texts)}-{h}"

    def get(self, key: str) -> Optional[List[str]]:
        v = self.data.get(key)
        return v if isinstance(v, list) else None

    def put(self, key: str, values: List[str]) -> None:
        self.data[key] = values
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            pass          # cache hỏng không được phép làm chết chương trình

    def discard(self, key: str) -> None:
        if key not in self.data:
            return
        self.data.pop(key, None)
        if not self.path:
            return
        try:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False)
            os.replace(tmp, self.path)
        except Exception:
            pass

    def remove(self) -> None:
        self.data = {}
        if self.path and os.path.exists(self.path):
            try:
                os.remove(self.path)
            except OSError:
                pass


class TranslationIncomplete(RuntimeError):
    """Dịch thiếu quá nhiều lô - không nên đem đi lồng tiếng luôn."""

    def __init__(self, failed: List[int], total: int):
        self.failed, self.total = failed, total
        super().__init__(f"thiếu {len(failed)}/{total} lô")


# --------------------------------------------------------------------------- #
#  Chế độ 1: Gemini REST API
# --------------------------------------------------------------------------- #
_GEMINI_FALLBACK_MODELS = [
    "gemini-3.6-flash",
    "gemini-3.5-flash",
    "gemini-3.5-flash-lite",
    "gemini-3.1-flash-lite",
]


def _gemini_model_candidates(model: str) -> List[str]:
    seen, out = set(), []
    for m in [str(model or "").strip(), *_GEMINI_FALLBACK_MODELS]:
        if m and m not in seen:
            seen.add(m)
            out.append(m)
    return out


def _gemini_call(prompt: str, api_key: str, model: str, temperature: float,
                 retries: int = 3) -> str:
    body = {
        "system_instruction": {"parts": [{"text": SYSTEM_INSTRUCTION}]},
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": {"temperature": temperature, "topP": 0.9,
                             "responseMimeType": "application/json"},
    }
    data = json.dumps(body).encode("utf-8")
    last_err = None
    for cand_model in _gemini_model_candidates(model):
        url = GEMINI_URL.format(model=cand_model, key=api_key)
        for attempt in range(retries):
            try:
                req = urllib.request.Request(
                    url, data=data, headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(req, timeout=120) as resp:
                    out = json.loads(resp.read().decode("utf-8"))
                if cand_model != model:
                    log(f"Gemini model '{model}' không dùng được, đã chuyển sang '{cand_model}'.", "warn")
                return out["candidates"][0]["content"]["parts"][0]["text"]
            except urllib.error.HTTPError as e:
                msg = e.read().decode("utf-8", "ignore")[:500]
                last_err = f"{cand_model}: HTTP {e.code}: {msg}"
                if e.code in (404, 410):
                    break
                if e.code in (429, 500, 503):
                    time.sleep(2 * (attempt + 1))  # backoff khi quá tải/hết quota tạm thời
                    continue
                break
            except Exception as e:
                last_err = f"{cand_model}: {e}"
                time.sleep(1.5 * (attempt + 1))
                break
    raise RuntimeError(f"Gemini lỗi: {last_err}")


def _openai_compatible_chat_url(base_url: Optional[str]) -> str:
    base = (base_url or TOKENROUTER_DEFAULT_BASE_URL).strip().rstrip("/")
    if base.endswith("/chat/completions"):
        return base
    if base.endswith("/v1"):
        return base + "/chat/completions"
    return base + "/v1/chat/completions"


def _tokenrouter_gemini_url(base_url: Optional[str], model: str) -> str:
    base = (base_url or TOKENROUTER_GEMINI_DEFAULT_BASE_URL).strip().rstrip("/")
    if base.endswith(":generateContent"):
        return base
    if not base.endswith("/models"):
        base += "/models"
    return f"{base}/{model or TOKENROUTER_GEMINI_DEFAULT_MODEL}:generateContent"


def _message_content_to_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict):
                parts.append(str(item.get("text") or item.get("content") or ""))
            else:
                parts.append(str(item))
        return "".join(parts)
    return "" if content is None else str(content)


def _http_error_summary(provider_label: str, status: int, body: str) -> str:
    msg = body
    try:
        obj = json.loads(body)
        err = obj.get("error") if isinstance(obj, dict) else None
        if isinstance(err, dict):
            msg = err.get("message") or body
    except Exception:
        pass
    msg = str(msg or "").strip()
    if status == 401:
        return f"{provider_label}: key khong hop le hoac da bi tat (HTTP 401: {msg})"
    if status == 403:
        return f"{provider_label}: key khong co quyen dung model nay (HTTP 403: {msg})"
    if status == 429:
        return f"{provider_label}: het quota/rate limit hoac model dang qua tai (HTTP 429: {msg})"
    return f"{provider_label}: HTTP {status}: {msg}"


def _openai_compatible_call(prompt: str, api_key: str, model: str,
                            temperature: float, base_url: Optional[str] = None,
                            retries: int = 3, timeout: int = 420,
                            stream: bool = True,
                            provider_label: str = "TokenRouter",
                            rate_limit_wait: float = 2.0) -> str:
    """Gọi endpoint chuẩn OpenAI /chat/completions (TokenRouter/InferX/NVIDIA...).

    rate_limit_wait: số giây chờ NỀN khi dính HTTP 429, nhân dần theo lần thử.
    Tier free của NVIDIA chặn burst khá gắt (nhánh dịch-lại-từng-câu từng chết
    vì retry 2-4s quá ngắn) nên provider đó truyền mức chờ dài hơn.
    """
    body = {
        "model": model or TOKENROUTER_DEFAULT_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_INSTRUCTION},
            {"role": "user", "content": prompt},
        ],
        "temperature": temperature,
    }
    if str(model or "").strip().lower() == "minimaxai/minimax-m3":
        # Tham số theo API Reference chính thức của NVIDIA cho MiniMax M3.
        # Giữ temperature do từng tác vụ truyền vào để bản dịch/JSON ổn định.
        body["top_p"] = 0.95
        body["max_tokens"] = 8192
    if stream:
        body["stream"] = True
        body["stream_options"] = {"include_usage": True}
    data = json.dumps(body).encode("utf-8")
    url = _openai_compatible_chat_url(base_url)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                    "Accept": "text/event-stream" if stream else "application/json",
                },
            )
            with urllib.request.urlopen(req, timeout=max(60, int(timeout or 420))) as resp:
                if not stream:
                    out = json.loads(resp.read().decode("utf-8"))
                    choices = out.get("choices") or []
                    if not choices:
                        raise RuntimeError("response has no choices")
                    msg = choices[0].get("message") or {}
                    text = _message_content_to_text(
                        msg.get("content", choices[0].get("text", "")))
                    if text:
                        return text
                else:
                    parts = []
                    for raw_line in resp:
                        line = raw_line.decode("utf-8", "ignore").strip()
                        if not line:
                            continue
                        if line.startswith("data:"):
                            line = line[5:].strip()
                        elif line.startswith("event:"):
                            continue
                        if line == "[DONE]":
                            break
                        try:
                            chunk = json.loads(line)
                        except Exception:
                            continue
                        for choice in chunk.get("choices") or []:
                            delta = choice.get("delta") or {}
                            text = _message_content_to_text(delta.get("content"))
                            if text:
                                parts.append(text)
                            elif choice.get("message"):
                                text = _message_content_to_text(
                                    choice["message"].get("content"))
                                if text:
                                    parts.append(text)
                    if parts:
                        return "".join(parts)
            raise RuntimeError("response content is empty")
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "ignore")[:500]
            last_err = _http_error_summary(provider_label, e.code, msg)
            if e.code in (429, 500, 502, 503, 504):
                # Đừng ngủ sau LẦN THỬ CUỐI: đằng nào cũng raise ngay sau đó,
                # ngủ thêm chỉ bắt người dùng chờ không. Mỗi nhịp chờ trần 60s
                # để không bao giờ treo quá lâu một chỗ.
                if attempt + 1 < retries:
                    base_wait = rate_limit_wait if e.code == 429 else 2.0
                    time.sleep(min(60.0, base_wait * (attempt + 1)))
                continue
            break
        except Exception as e:
            last_err = str(e)
            if attempt + 1 < retries:
                time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"{provider_label} loi: {last_err}")


def _tokenrouter_gemini_call(prompt: str, api_key: str, model: str,
                             temperature: float, base_url: Optional[str] = None,
                             retries: int = 3, timeout: int = 420) -> str:
    full_prompt = SYSTEM_INSTRUCTION + "\n\n" + prompt
    body = {
        "contents": [
            {"role": "user", "parts": [{"text": full_prompt}]}
        ],
        "generationConfig": {
            "temperature": temperature,
            "responseMimeType": "application/json",
        },
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    url = _tokenrouter_gemini_url(base_url, model or TOKENROUTER_GEMINI_DEFAULT_MODEL)
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "x-goog-api-key": api_key,
                },
            )
            with urllib.request.urlopen(req, timeout=max(60, int(timeout or 420))) as resp:
                out = json.loads(resp.read().decode("utf-8"))
            candidates = out.get("candidates") or []
            if not candidates:
                raise RuntimeError("response has no candidates")
            parts = (((candidates[0].get("content") or {}).get("parts")) or [])
            text = "".join(str(p.get("text") or "") for p in parts if isinstance(p, dict))
            if text:
                return text
            raise RuntimeError("response content is empty")
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "ignore")[:500]
            last_err = _http_error_summary("TokenRouter Gemini", e.code, msg)
            if e.code in (429, 500, 502, 503, 504):
                time.sleep(2 * (attempt + 1))
                continue
            break
        except Exception as e:
            last_err = str(e)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"TokenRouter Gemini loi: {last_err}")


def _api_call(prompt: str, api_key: str, model: str, temperature: float,
              provider: str = "gemini", api_base_url: Optional[str] = None,
              api_timeout: int = 420,
              api_retries: Optional[int] = None) -> str:
    provider_key = str(provider or "gemini").lower()
    if provider_key == "tokenrouter":
        return _openai_compatible_call(
            prompt, api_key, model or TOKENROUTER_DEFAULT_MODEL,
            temperature, api_base_url or TOKENROUTER_DEFAULT_BASE_URL,
            timeout=api_timeout)
    if provider_key == "inferx":
        return _openai_compatible_call(
            prompt, api_key, model or INFERX_DEFAULT_MODEL,
            temperature, api_base_url or INFERX_DEFAULT_BASE_URL,
            timeout=api_timeout, stream=False)
    if provider_key == "nvidia":
        # Tier free chặn burst từng đợt 1-2 phút và KHÔNG trả header
        # Retry-After (đã soi thật) -> chỉ còn cách kiên nhẫn: 5 lần thử,
        # chờ 15/30/45/60s giữa các lần (tổng ~2.5 phút đủ vượt một đợt chặn).
        return _openai_compatible_call(
            prompt, api_key, model or NVIDIA_DEFAULT_MODEL,
            temperature, api_base_url or NVIDIA_DEFAULT_BASE_URL,
            retries=(5 if api_retries is None else max(1, int(api_retries))),
            timeout=api_timeout, stream=False,
            provider_label="NVIDIA", rate_limit_wait=15.0)
    if provider_key == "zenmux":
        return _openai_compatible_call(
            prompt, api_key, model or ZENMUX_DEFAULT_MODEL,
            temperature, api_base_url or ZENMUX_DEFAULT_BASE_URL,
            retries=4, timeout=api_timeout, stream=False,
            provider_label="ZenMux", rate_limit_wait=5.0)
    if provider_key == "tokenrouter_gemini":
        return _tokenrouter_gemini_call(
            prompt, api_key, model or TOKENROUTER_GEMINI_DEFAULT_MODEL,
            temperature, api_base_url or TOKENROUTER_GEMINI_DEFAULT_BASE_URL,
            timeout=api_timeout)
    return _gemini_call(prompt, api_key, model, temperature)


def api_params_for_provider(tr: dict, provider: str
                            ) -> Tuple[str, str, Optional[str], int]:
    """Lấy (api_key, model, base_url, timeout) đúng theo provider trong config.

    Bảng này từng bị viết hai lần - một bản trong GUI, một bản trong main.py -
    và bản của main.py THIẾU nhánh 'nvidia', nên chạy dòng lệnh với
    `provider: nvidia` lại gửi key/model của Gemini tới endpoint NVIDIA và chết
    với HTTP 404. Giữ một bảng duy nhất ở đây để không lệch nhau nữa.
    """
    tr = tr or {}
    p = str(provider or "gemini").strip().lower()
    if p == "tokenrouter_gemini":
        return (tr.get("tokenrouter_gemini_api_key", ""),
                tr.get("tokenrouter_gemini_model", TOKENROUTER_GEMINI_DEFAULT_MODEL),
                tr.get("tokenrouter_gemini_base_url"),
                int(tr.get("tokenrouter_gemini_timeout", 420) or 420))
    if p == "tokenrouter":
        return (tr.get("tokenrouter_api_key", ""),
                tr.get("tokenrouter_model", TOKENROUTER_DEFAULT_MODEL),
                tr.get("tokenrouter_base_url"),
                int(tr.get("tokenrouter_timeout", 420) or 420))
    if p == "inferx":
        return (tr.get("inferx_api_key", ""),
                tr.get("inferx_model", INFERX_DEFAULT_MODEL),
                tr.get("inferx_base_url"),
                int(tr.get("inferx_timeout", 420) or 420))
    if p == "nvidia":
        return (tr.get("nvidia_api_key", ""),
                tr.get("nvidia_model", NVIDIA_DEFAULT_MODEL),
                tr.get("nvidia_base_url"),
                int(tr.get("nvidia_timeout", 420) or 420))
    if p == "zenmux":
        return (tr.get("zenmux_api_key", ""),
                tr.get("zenmux_model", ZENMUX_DEFAULT_MODEL),
                tr.get("zenmux_base_url"),
                int(tr.get("zenmux_timeout", 420) or 420))
    return (tr.get("gemini_api_key", ""),
            tr.get("gemini_model", "gemini-3.6-flash"), None, 420)


def build_name_hint(male_lead_name: str = "", female_lead_name: str = "") -> str:
    male = str(male_lead_name or "").strip()
    female = str(female_lead_name or "").strip()
    lines = []
    if male:
        lines.append(f"- Nam chinh/nhan vat chinh bat buoc dung ten Viet: {male}.")
    if female:
        lines.append(f"- Nhan vat nu chinh/nu trung tam bat buoc dung ten Viet: {female}.")
    if not lines:
        return ""
    return (
        "Quy uoc ten rieng bat buoc khi dich:\n"
        + "\n".join(lines)
        + "\nNeu nguon co ten Han/Trung, biet danh hoac cach goi cua cac nhan vat nay, "
          "hay quy ve dung ten tren; khong tu y doi sang ten khac."
    )


def _parse_json_lines(raw: str, expected: int) -> Optional[List[str]]:
    raw = raw.strip()
    # cắt bỏ ```json ... ``` nếu có
    if raw.startswith("```"):
        raw = raw.strip("`")
        raw = raw.split("\n", 1)[1] if "\n" in raw else raw
    try:
        arr = json.loads(raw)
        if isinstance(arr, dict):
            arr = list(arr.values())
        if isinstance(arr, list) and len(arr) == expected:
            return [str(x).strip() for x in arr]
    except Exception:
        pass
    return None


def _clean_vi_lines(values: List[str]) -> List[str]:
    return [normalize_vi_subtitle_text("" if v is None else str(v)) for v in values]


def _contains_cjk(text: str) -> bool:
    return bool(_CJK_RE.search(text or ""))


def _cjk_line_numbers(values: List[str]) -> List[int]:
    return [idx + 1 for idx, txt in enumerate(values) if _contains_cjk(txt)]


def _bad_line_summary(lines: List[int], limit: int = 8) -> str:
    if not lines:
        return ""
    head = ", ".join(str(x) for x in lines[:limit])
    more = "" if len(lines) <= limit else f", ... +{len(lines) - limit}"
    return head + more


def translate_segments(
    segments: List[Segment],
    api_key: str,
    model: str = "gemini-3.6-flash",
    provider: str = "gemini",
    api_base_url: Optional[str] = None,
    chunk_size: int = 40,
    temperature: float = 0.35,
    context_lines: int = 3,
    cache_path: Optional[str] = None,
    chars_per_sec: float = 0.0,
    name_hint: str = "",
    api_timeout: int = 420,
    shorten_long_lines_enabled: bool = True,
) -> List[Segment]:
    """Dịch tại chỗ: gán segment.text = bản tiếng Việt. Trả lại chính list đó."""
    provider_key = str(provider or "gemini").lower()
    if not api_key:
        if provider_key.startswith("tokenrouter"):
            name = "TOKENROUTER_API_KEY"
        elif provider_key == "nvidia":
            name = "NVIDIA API key (nvapi-..., tạo free tại build.nvidia.com)"
        elif provider_key == "zenmux":
            name = "ZenMux API key (tạo tại zenmux.ai)"
        else:
            name = "GEMINI_API_KEY"
        raise ValueError(f"Chưa có {name}. Điền key trong GUI/config.yaml hoặc chọn chế độ browser.")
    chunk_size = max(1, int(chunk_size or 40))
    if provider_key == "tokenrouter" and chunk_size > 20:
        log(f"TokenRouter free de timeout voi lo lon; giam chunk_size {chunk_size} -> 20 dong/luot.", "warn")
        chunk_size = 20
    log(f"Dich qua API provider={provider_key}, model='{model}', chunk_size={chunk_size}.", "info")
    name_note = (str(name_hint or "").strip() + "\n\n") if str(name_hint or "").strip() else ""
    escape_note = (
        "QUAN TRONG voi TokenRouter: Tra ve JSON hop le va moi ky tu khong thuoc ASCII "
        "trong ban dich tieng Viet phai viet bang escape JSON \\\\uXXXX "
        "(vi du: \"Xin ch\\\\u00e0o\"). Khong de ky tu co dau truc tiep trong output.\n\n"
        if provider_key == "tokenrouter" else ""
    )

    cache = ChunkCache(cache_path)
    n = len(segments)
    total_chunks = max(1, (n + chunk_size - 1) // chunk_size)
    done = 0
    long_lines = 0                 # số dòng vẫn dài hơn nhịp lồng tiếng sau rút gọn
    prev_context: List[str] = []   # vài dòng dịch trước đó để giữ mạch

    for i in range(0, n, chunk_size):
        chunk = segments[i:i + chunk_size]
        src_lines = []
        for s in chunk:
            spk = f"[{s.speaker}] " if s.speaker else ""
            src_lines.append(f"{spk}{s.text}")

        ckey = ChunkCache.key(i, src_lines)
        vi = cache.get(ckey)
        if vi is not None:
            cleaned_vi = _clean_vi_lines(vi)
            if cleaned_vi != vi:
                cache.put(ckey, cleaned_vi)
            vi = cleaned_vi
            bad = _cjk_line_numbers(vi)
            if bad:
                log(f"Cache lô {i//chunk_size+1}/{total_chunks} còn tiếng Trung "
                    f"ở dòng {_bad_line_summary(bad)} -> bỏ cache và dịch lại.", "warn")
                cache.discard(ckey)
                vi = None

        if vi is None or len(vi) != len(chunk):
            log(f"(api:{provider_key}) lo {i//chunk_size+1}/{total_chunks} - dang goi {model}...", "info")
            ctx = ""
            if prev_context:
                ctx = ("Ngữ cảnh (các câu tiếng Việt VỪA dịch trước đó, để giữ mạch & "
                       "xưng hô nhất quán):\n- " + "\n- ".join(prev_context[-context_lines:]) + "\n\n")

            cps = max(0.0, float(chars_per_sec or 0.0))
            budget_note = ""
            payload = src_lines
            if cps > 0:
                budget_note = (
                    " Moi dong co truong max_chars: day la GIOI HAN THAT ve so "
                    "ky tu, vi giong doc chi co dung bay nhieu thoi gian. Dong "
                    "nao dai hon se bi doc voi roi cat cut giua chung. Hay viet "
                    "gon, khau ngu, bo tu dem va trang tu thua; van giu nguyen "
                    "ten rieng, con so, chu-vi va quan he nhan vat. Chi vuot "
                    "gioi han khi khong con cach nao dien dat du nghia.\n"
                )
                payload = [
                    {"text": line, "max_chars": char_budget(seg, cps)}
                    for line, seg in zip(src_lines, chunk)
                ]

            prompt = (
                ctx +
                name_note +
                escape_note +
                f"Dịch sang tiếng Việt {len(src_lines)} dòng phụ đề dưới đây. "
                f"Trả về JSON array gồm ĐÚNG {len(src_lines)} chuỗi, cùng thứ tự. "
                "Không dùng dấu ba chấm (... hoặc …); nếu câu bị cắt mảnh thì "
                "viết lại thành câu Việt liền mạch.\n\n"
                + budget_note + "\n"
                + json.dumps(payload, ensure_ascii=False)
            )

            raw = _api_call(prompt, api_key, model, temperature, provider,
                            api_base_url, api_timeout)
            vi = _parse_json_lines(raw, len(chunk))
            # Các dòng ĐÃ SẠCH của lô được giữ lại: chỉ 1-2 dòng lẫn tiếng
            # Trung mà vứt cả lô đi dịch lại từng câu (15 dòng = 15 request)
            # thì vừa chậm vừa tự chuốc rate-limit (NVIDIA free dính ngay).
            keep: Dict[int, str] = {}
            if vi is not None:
                vi = _clean_vi_lines(vi)
                bad = _cjk_line_numbers(vi)
                if bad:
                    log(f"Lô {i//chunk_size+1}: API trả còn tiếng Trung ở dòng "
                        f"{_bad_line_summary(bad)} -> giữ dòng sạch, dịch lại "
                        "đúng các dòng đó.", "warn")
                    keep = {k: t for k, t in enumerate(vi)
                            if t and not _contains_cjk(t)}
                    vi = None

            if vi is None:
                # Thử lại từng dòng để không vỡ số lượng
                if not keep:
                    log(f"Lô {i//chunk_size+1}: số dòng không khớp, dịch lại từng câu...", "warn")
                vi = []
                for line_no, s in enumerate(chunk, 1):
                    if line_no - 1 in keep:
                        vi.append(keep[line_no - 1])
                        continue
                    translated = ""
                    last_one = ""
                    for retry in range(2):
                        r = _api_call(
                            name_note +
                            escape_note +
                            "Dịch 1 dòng phụ đề sau sang TIẾNG VIỆT tự nhiên, câu văn "
                            "liền mạch, không dùng dấu ba chấm (... hoặc …). "
                            "BẮT BUỘC trả về JSON array có đúng 1 chuỗi tiếng Việt. "
                            "Kết quả KHÔNG được chứa chữ Hán/Trung; tên riêng phải "
                            "phiên âm hoặc Việt hoá bằng chữ Latin. Nếu nguồn chỉ là "
                            "tiếng đệm/tiếng cười như 嗯, 嘿嘿 thì chuyển thành cách "
                            "nói/ngắt tiếng Việt tự nhiên.\nNguồn: "
                            + json.dumps([s.text], ensure_ascii=False),
                            api_key, model, temperature, provider, api_base_url,
                            api_timeout)
                        one = _parse_json_lines(r, 1)
                        last_one = normalize_vi_subtitle_text(one[0] if one else "")
                        if last_one and not _contains_cjk(last_one):
                            translated = last_one
                            break
                        log(f"Lô {i//chunk_size+1}, dòng {line_no}: bản dịch vẫn còn "
                            "tiếng Trung, thử lại...", "warn")
                    if not translated:
                        sample = (last_one or s.text or "")[:80]
                        raise RuntimeError(
                            f"Lô {i//chunk_size+1}, dòng {line_no} vẫn chưa dịch sang "
                            f"tiếng Việt sạch sau khi thử lại: {sample}")
                    vi.append(translated)

            # Mốc độ dài trong prompt chỉ là gợi ý và model thường vượt. Đo trên
            # 4 video thật: đặt 15 ký tự/giây nhưng bản dịch ra 21-23. Ép lại ở
            # đây, trước khi ghi cache, để lần chạy sau không dùng lại bản dài.
            if shorten_long_lines_enabled and cps > 0 and vi:
                got = {k + 1: t for k, t in enumerate(vi)}
                still = shorten_long_lines(
                    chunk, got, cps,
                    lambda prompt: _api_call(prompt, api_key, model, temperature,
                                             provider, api_base_url, api_timeout),
                    label=f"Lô {i//chunk_size+1}/{total_chunks}: ")
                vi = [got.get(k + 1, vi[k]) for k in range(len(vi))]
                long_lines += still

            cache.put(ckey, vi)

        for s, t in zip(chunk, vi):
            if t:
                s.text = normalize_vi_subtitle_text(t)
        prev_context.extend([t for t in vi if t])
        del prev_context[:-8]        # chỉ dùng vài dòng cuối làm ngữ cảnh
        done += len(chunk)
        log(f"Đã dịch {done}/{n} dòng", "info")

    log("Dịch xong.", "ok")
    if long_lines:
        log(f"Còn {long_lines}/{n} dòng dài hơn thời lượng cho phép - bước lồng "
            "tiếng sẽ tự tăng tốc đọc để bù.", "warn")
    return segments


# --------------------------------------------------------------------------- #
#  Chế độ 2: điều khiển trình duyệt vào Gemini web
# --------------------------------------------------------------------------- #
# Xếp theo thứ tự CỤ THỂ -> CHUNG CHUNG. Không gộp thành 1 chuỗi CSS như bản cũ,
# vì query CSS gộp trả về phần tử ĐẦU TIÊN theo thứ tự DOM (có thể là ô ẩn của
# hộp thoại khác), chứ không phải theo thứ tự ưu tiên mình muốn.
_INPUT_CANDIDATES = [
    "rich-textarea div[contenteditable='true']",
    "div.ql-editor[contenteditable='true']",
    "div[role='textbox'][contenteditable='true']",
    "textarea[aria-label]",
]
_RESP_CANDIDATES = [
    "model-response",
    "message-content.model-response-text",
    ".model-response-text",
    "div.markdown",
]
_SEND_CANDIDATES = [
    "button.send-button",
    "button[aria-label*='Send' i]",
    "button[aria-label*='Gửi' i]",
    "button[mattooltip*='Send' i]",
]
_STOP_CANDIDATES = [
    "button[aria-label*='Stop' i]",
    "button[aria-label*='Dừng' i]",
]


def _launch(p, profile_dir: str, channel: str):
    """Mở trình duyệt bền (nhớ đăng nhập). Ưu tiên Edge -> Chrome -> Chromium."""
    order = [channel] if channel else []
    order += [c for c in ("msedge", "chrome", None) if c not in order]
    last = None
    for ch in order:
        try:
            kw = dict(
                headless=False,
                args=["--start-maximized",
                      # Google hay đổi/khoá giao diện khi thấy cờ tự động hoá
                      "--disable-blink-features=AutomationControlled"],
                ignore_default_args=["--enable-automation"],
                no_viewport=True,
            )
            if ch:
                kw["channel"] = ch
            ctx = p.chromium.launch_persistent_context(profile_dir, **kw)
            log(f"Đã mở trình duyệt: {ch or 'chromium'}", "ok")
            return ctx
        except Exception as e:
            last = e
    raise RuntimeError(
        f"Không mở được trình duyệt nào (Edge/Chrome/Chromium): {last}\n"
        "  - Nếu báo 'profile in use': đóng hết cửa sổ Edge đang mở, hoặc xoá "
        "thư mục browser_profile rồi đăng nhập lại."
    )


def _visible_locator(page, candidates: List[str], timeout: float = 30.0):
    """Trả về Locator ĐANG HIỂN THỊ đầu tiên khớp danh sách selector.

    Dùng Locator (KHÔNG dùng query_selector/ElementHandle) vì Gemini là ứng dụng
    Angular: nó dựng lại DOM sau khi trang tải xong, nên ElementHandle lấy được
    lúc trước sẽ bị 'detached' -> đúng lỗi 'Element is not attached to the DOM'.
    Locator tự tìm lại phần tử ở MỖI thao tác nên miễn nhiễm với việc này.
    """
    deadline = time.time() + timeout
    while True:
        for sel in candidates:
            try:
                loc = page.locator(sel)
                n = loc.count()
            except Exception:
                continue
            # Có thể khớp nhiều phần tử, trong đó vài cái đang ẨN (hộp thoại
            # onboarding của Gemini chẳng hạn) -> lấy cái ĐANG HIỂN THỊ.
            for i in range(min(n, 5)):
                try:
                    item = loc.nth(i)
                    if item.is_visible(timeout=1000):
                        return item
                except Exception:
                    continue
        if time.time() >= deadline:
            return None
        try:
            page.wait_for_timeout(500)
        except Exception:
            return None


_RESP_SEL_CHOSEN: Dict[int, str] = {}


def _resp_locator(page):
    """Locator trỏ tới các khối TRẢ LỜI của model (không tính câu mình gửi).

    Chọn được selector nào rồi thì DÙNG MÃI selector đó: nếu mỗi lần gọi lại
    chọn một selector khác, số khối đếm trước/sau khi gửi sẽ không so sánh được
    với nhau và vòng chờ trả lời sẽ treo cho tới hết giờ.
    """
    sel = _RESP_SEL_CHOSEN.get(id(page))
    if sel:
        return page.locator(sel)
    for s in _RESP_CANDIDATES:
        try:
            if page.locator(s).count() > 0:
                _RESP_SEL_CHOSEN[id(page)] = s
                return page.locator(s)
        except Exception:
            continue
    return page.locator(_RESP_CANDIDATES[0])


def _is_generating(page) -> bool:
    for sel in _STOP_CANDIDATES:
        try:
            if page.locator(sel).first.is_visible(timeout=250):
                return True
        except Exception:
            continue
    return False


def _clear_composer(page, box) -> None:
    box.click(timeout=15000)
    page.keyboard.press("Control+A")
    page.keyboard.press("Delete")
    page.wait_for_timeout(150)


def _put_text(page, box, msg: str) -> bool:
    """Đưa cả khối text vào ô soạn thảo rồi KIỂM CHỨNG là nó đã vào thật."""
    probe = (msg.strip().split("\n")[-1] or "")[:24]

    def landed() -> bool:
        try:
            cur = box.inner_text() or ""
        except Exception:
            return False
        return len(cur) >= len(msg) * 0.5 and (not probe or probe in cur)

    # Cách 1: chèn nguyên khối (nhanh)
    _clear_composer(page, box)
    page.keyboard.insert_text(msg)
    page.wait_for_timeout(400)
    if landed():
        return True

    # Cách 2: gõ từng dòng + Shift+Enter (chậm hơn nhưng chắc với Quill editor)
    _clear_composer(page, box)
    lines = msg.split("\n")
    for k, line in enumerate(lines):
        if line:
            page.keyboard.insert_text(line)
        if k < len(lines) - 1:
            page.keyboard.press("Shift+Enter")
    page.wait_for_timeout(400)
    return landed()


def _submit(page) -> None:
    """Bấm nút gửi; không thấy nút thì mới dùng Enter."""
    btn = _visible_locator(page, _SEND_CANDIDATES, timeout=3.0)
    if btn is not None:
        try:
            if btn.is_enabled(timeout=1000):
                btn.click(timeout=8000)
                return
        except Exception:
            pass
    page.keyboard.press("Enter")


# BẪY LỚN: Gemini render "1. abc" của markdown thành <ol><li>abc</li>...
# Số thứ tự lúc đó là ::marker do CSS sinh ra, KHÔNG nằm trong text - nên
# innerText trả về "abc" trơ trọi, mất sạch số. Phải dựng lại số từ thẻ <ol>.
_JS_EXTRACT = """
el => {
  const out = [];
  el.querySelectorAll('ol').forEach(ol => {
    const start = parseInt(ol.getAttribute('start') || '1', 10) || 1;
    let i = 0;
    ol.querySelectorAll(':scope > li').forEach(li => {
      out.push((start + i) + '. ' + (li.innerText || '').trim().replace(/\\s*\\n\\s*/g, ' '));
      i++;
    });
  });
  return out.length ? out.join('\\n') : (el.innerText || '');
}
"""


def _reply_text(item) -> str:
    """Lấy nội dung câu trả lời, GIỮ ĐƯỢC số thứ tự của danh sách đánh số."""
    # Với câu trả lời JSON, innerText đã giữ đủ dấu ngoặc/khóa. Bộ dựng lại
    # <ol> bên dưới vốn dành cho phụ đề sẽ chỉ trả riêng các mục danh sách nếu
    # Gemini lỡ render một mảng thành <ol>, làm mất toàn bộ phần object JSON.
    try:
        plain = (item.inner_text() or "").strip()
    except Exception:
        plain = ""
    if re.search(r'\{\s*"[^"\n]+"\s*:', plain):
        return plain
    try:
        return (item.evaluate(_JS_EXTRACT) or plain).strip()
    except Exception:
        return plain


def _wait_reply(page, prev_count: int, timeout: float) -> str:
    """Chờ Gemini trả lời XONG: có khối trả lời MỚI, rồi nội dung ngừng dài thêm."""
    deadline = time.time() + timeout
    text, last_change = "", time.time()
    while time.time() < deadline:
        loc, n = None, 0
        try:
            loc = _resp_locator(page)
            n = loc.count()
        except Exception:
            n = 0
        if loc is not None and n > prev_count:
            cur = _reply_text(loc.nth(n - 1)) or text
            if cur != text:
                text, last_change = cur, time.time()
            elif text and (time.time() - last_change) >= 3.0 and not _is_generating(page):
                return text
        page.wait_for_timeout(800)
    return text


# Chấp nhận: [3] abc | (3) abc | 3. abc | 3) abc | 3: abc | 3、abc | 3 - abc
# KHÔNG chấp nhận "3 abc" (số trần + khoảng trắng): thoại thật hoàn toàn có thể
# mở đầu bằng số ("8 个头16个大") - nhận bừa là hỏng nguyên dòng đó.
_NUM_LINE = re.compile(
    r"^[>\-*_`\s]*"
    r"(?:\[\s*(\d{1,4})\s*\]|\(\s*(\d{1,4})\s*\)|(\d{1,4})\s*[\.\)\:：、]|(\d{1,4})\s+[-–—])"
    r"\s*(.+)$")
_MD_EDGE = re.compile(r"^[*_`\s]+|[*_`\s]+$")


def parse_numbered_reply(reply: str, n: int) -> Dict[int, str]:
    """Chỉ nhận các dòng CÓ SỐ THỨ TỰ, ánh xạ theo đúng con số đó.

    Quan trọng: bản cũ nhận cả dòng KHÔNG có số, nên chỉ cần Gemini thêm một câu
    dẫn ("Chắc chắn rồi, đây là bản dịch:") là TOÀN BỘ phụ đề bị lệch đi 1 dòng
    mà không có gì báo. Ánh xạ theo số thì thừa/thiếu dòng dẫn cũng vô hại.
    """
    out: Dict[int, str] = {}
    for raw in (reply or "").split("\n"):
        m = _NUM_LINE.match(raw.strip())
        if not m:
            continue
        num = next((g for g in m.group(1, 2, 3, 4) if g), None)
        if num is None:
            continue
        k = int(num)
        if 1 <= k <= n and k not in out:      # gặp trùng thì giữ lần ĐẦU
            txt = _MD_EDGE.sub("", m.group(5)).strip().strip('"').strip()
            cleaned = normalize_vi_subtitle_text(txt)
            if cleaned:
                out[k] = cleaned
    return out


def match_by_position(reply: str, numbers: List[int]) -> Dict[int, str]:
    """Phao cứu sinh khi câu trả lời KHÔNG còn số nào (Gemini bỏ số, hoặc giao
    diện đổi). Chỉ ghép khi SỐ DÒNG KHỚP CHÍNH XÁC - thà bỏ qua còn hơn ghép
    lệch, vì lệch 1 dòng là hỏng cả đoạn mà không ai biết."""
    lines = [l.strip(" -•\t") for l in (reply or "").split("\n")]
    lines = [l for l in lines if l]
    if not lines:
        return {}
    # CHỈ chấp nhận đúng 2 khả năng: khớp y nguyên, hoặc thừa một dòng dẫn kết
    # thúc bằng dấu ":". Cắt đầu cắt đuôi cho vừa số dòng là ĐOÁN BỪA - lệch 1
    # dòng thì cả đoạn thoại gán sai nhân vật mà không có gì báo.
    variants = [lines]
    if lines[0].endswith(":"):
        variants.append(lines[1:])
    for v in variants:
        if len(v) == len(numbers):
            cleaned = [normalize_vi_subtitle_text(txt) for txt in v]
            if all(cleaned):
                return {k: txt for k, txt in zip(numbers, cleaned)}
    return {}


def char_budget(seg: Segment, chars_per_sec: float) -> int:
    """Mốc ký tự mềm để giọng đọc còn bám được khung thời gian của câu.

    Đây là mấu chốt của lỗi "video chạy trước giọng": câu tiếng Việt dịch từ
    tiếng Trung dài gấp ~2 lần bản gốc, TTS đọc không kịp, câu sau bị đẩy lùi,
    lệch dồn lại thành hàng chục giây. Tăng tốc đọc chỉ chữa được một phần -
    gốc rễ là bản dịch phải gọn lại. Nhưng đây chỉ là MỐC MỀM: nếu ép quá cứng,
    câu ngắn dễ bị cụt nghĩa ("kẻ nói dối" thành "kẻ dối"), nghe còn tệ hơn.
    """
    cps = max(0.0, float(chars_per_sec or 0.0))
    if cps <= 0:
        return 0
    dur = max(0.6, float(seg.end) - float(seg.start))
    return max(TRANSLATION_MIN_CHARS, int(dur * cps * TRANSLATION_BUDGET_MARGIN))


def _too_long_for_tts(seg: Segment, text: str, chars_per_sec: float) -> bool:
    return len(text or "") > char_budget(seg, chars_per_sec) * SHORTEN_TRIGGER_RATIO


def reading_pressure(segments: List[Segment],
                     chars_per_sec: float = 15.0) -> Dict:
    """Đo xem bản dịch có đọc kịp khung thời gian của video không.

    Trả về tổng số phút cần để đọc tự nhiên so với số phút ô trống thực có, và
    số dòng buộc phải đọc nhanh. Đây là chỉ số dự báo sớm cho lỗi "tiếng chạy
    trước hình": khi cần nhiều thời gian hơn số có, TTS phải nén và cắt câu,
    thoại kết thúc sớm hơn hình dù mốc bắt đầu vẫn đúng.
    """
    cps = max(1.0, float(chars_per_sec or 15.0))
    total_chars = 0
    total_slot = 0.0
    over = 0
    hopeless = 0
    for seg in segments:
        text = (seg.text or "").strip()
        if not text:
            continue
        slot = max(0.2, float(seg.end) - float(seg.start))
        total_chars += len(text)
        total_slot += slot
        need = len(text) / slot
        if need > cps:
            over += 1
        if need > cps * 1.6:
            hopeless += 1
    counted = sum(1 for s in segments if (s.text or "").strip())
    need_seconds = total_chars / cps
    return {
        "lines": counted,
        "chars": total_chars,
        "slot_seconds": total_slot,
        "need_seconds": need_seconds,
        "ratio": (need_seconds / total_slot) if total_slot > 0 else 0.0,
        "over_lines": over,
        "hopeless_lines": hopeless,
    }


def log_reading_pressure(segments: List[Segment],
                         chars_per_sec: float = 15.0) -> Dict:
    """In chẩn đoán áp lực đọc trước khi tổng hợp giọng."""
    st = reading_pressure(segments, chars_per_sec)
    if not st["lines"]:
        return st
    ratio = st["ratio"]
    msg = (f"Ap luc doc: ban dich can {st['need_seconds']/60:.1f} phut de doc "
           f"tu nhien, khung thoi gian co {st['slot_seconds']/60:.1f} phut "
           f"({ratio*100:.0f}%).")
    if ratio <= 1.05:
        log(msg + " Giong se bam sat hinh.", "ok")
        return st
    log(msg, "warn")
    log(f"  {st['over_lines']}/{st['lines']} dong phai doc nhanh hon binh thuong; "
        f"{st['hopeless_lines']} dong vuot ca tran 1.6x nen se bi cat cut.", "warn")
    log("  Giong doc se ket thuc som hon hinh -> nghe nhu 'tieng chay truoc "
        "hinh'. Muon het han: giam translation.chars_per_sec (vd 13) roi XOA "
        "file .vi.srt de dich lai.", "warn")
    return st


_MEANING_PHRASE_RULES = (
    ("nói dối", ("nói dối", "dối trá", "lừa dối")),
)


def _norm_vi(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _keeps_core_meaning(original: str, candidate: str) -> bool:
    old = _norm_vi(original)
    new = _norm_vi(candidate)
    for trigger, allowed in _MEANING_PHRASE_RULES:
        if trigger in old and not any(a in new for a in allowed):
            return False
    return True


# Token viết hoa KHÔNG đứng đầu câu gần như luôn là tên riêng/danh xưng. Rút gọn
# mà đánh rơi chúng là hỏng mạch truyện, nên chặn thẳng.
_PROPER_NOUN_RE = re.compile(r"(?<![.!?…]\s)(?<!^)\b([A-ZĐ][\wÀ-ỹ]{1,})\b")
_NUMBER_RE = re.compile(r"\d+")


def _entities(text: str) -> Tuple[set, set]:
    body = (text or "").strip()
    names = {m.group(1).lower() for m in _PROPER_NOUN_RE.finditer(body)}
    numbers = set(_NUMBER_RE.findall(body))
    return names, numbers


def _keeps_entities(original: str, candidate: str) -> bool:
    """Bản rút gọn phải giữ đủ tên riêng và con số của bản gốc."""
    old_names, old_numbers = _entities(original)
    new_names, new_numbers = _entities(candidate)
    if old_names - new_names:
        return False
    if old_numbers - new_numbers:
        return False
    return True


def _accept_shortened(original: str, candidate: str, seg: Segment,
                     chars_per_sec: float) -> bool:
    old = (original or "").strip()
    new = (candidate or "").strip()
    if not new or len(new) >= len(old):
        return False
    if not _keeps_core_meaning(old, new):
        return False
    if not _keeps_entities(old, new):
        return False
    # Những câu chỉ hơi dài không được phép bị bóp xuống còn nửa ý. Câu quá dài
    # thật sự được rút mạnh hơn để cứu nhịp lồng tiếng, nhưng vẫn có sàn cứng -
    # trước đây nhánh đó KHÔNG có sàn nào nên mới sinh ra "mà bọn nó nói" ->
    # "mà bọn nói".
    target = max(1, char_budget(seg, chars_per_sec))
    floor_ratio = (SHORTEN_KEEP_RATIO if len(old) <= target * 2
                   else SHORTEN_KEEP_RATIO_LONG)
    if len(new) < max(8, int(len(old) * floor_ratio)):
        return False
    return True


def _build_prompt(chunk: List[Segment], numbers: List[int],
                  context: List[str], again: bool = False,
                  proofread: bool = False,
                  chars_per_sec: float = 0.0,
                  name_hint: str = "") -> str:
    """`numbers` là SỐ THỨ TỰ GỐC trong lô (1..len(chunk)). Khi hỏi bổ sung các
    dòng còn thiếu, ta giữ nguyên con số cũ để ghép lại không bao giờ lệch."""
    # Dùng [số] thay vì "số." : "1." bị markdown biến thành danh sách <ol>, mà
    # số của <ol> là do CSS vẽ ra nên đọc text về là MẤT SỐ. "[1]" thì giữ nguyên.
    def _line(k: int) -> str:
        seg = chunk[k - 1]
        spk = f"({seg.speaker}) " if seg.speaker else ""
        return f"[{k}] {spk}{seg.text}"

    src = "\n".join(_line(k) for k in numbers)
    limit_note = ""
    budget_block = ""
    if chars_per_sec > 0:
        limit_note = (
            " Số trong ngoặc đơn (\u226425) là GIỚI HẠN ĐỘ DÀI của từng dòng, "
            "tính bằng ký tự. Đây là ràng buộc THẬT: giọng đọc chỉ có đúng bấy "
            "nhiêu thời gian, dòng nào dài hơn sẽ bị đọc vội rồi cắt cụt giữa "
            "chừng. Hãy viết gọn, khẩu ngữ, dễ hiểu ngay khi nghe; bỏ từ đệm, "
            "bỏ trạng từ thừa, gộp mệnh đề rườm rà. Vẫn phải giữ nguyên tên "
            "riêng, con số, chủ-vị và quan hệ nhân vật/sự việc; tránh từ Hán "
            "Việt khó nếu có cách nói phổ thông hơn. Chỉ được vượt giới hạn khi "
            "không còn cách nào diễn đạt đủ nghĩa, và vượt càng ít càng tốt.")
        limit_note += (
            " Cac moc do dai chi la metadata: KHONG chep so, dau <=, dau ngoac "
            "hay chu max_chars vao ban dich.")
        budget_block = (
            "\nGioi han do dai cho tung dong (KHONG chep vao ban dich):\n"
            + "\n".join(
                f"[{k}] <= {char_budget(chunk[k - 1], chars_per_sec)} ky tu"
                for k in numbers)
            + "\n")
    ctx = ""
    if context and not again:
        ctx = ("Ngữ cảnh (câu tiếng Việt vừa dịch trước đó, để giữ mạch và xưng "
               "hô nhất quán):\n" + "\n".join(f"- {c}" for c in context[-3:]) + "\n\n")
    name_note = (str(name_hint or "").strip() + "\n\n") if str(name_hint or "").strip() else ""
    if proofread:
        head = ("Bạn còn thiếu mấy dòng. Sửa NỐT đúng những dòng sau, GIỮ NGUYÊN số."
                if again else
                "Đây là phụ đề TIẾNG VIỆT do máy nghe tự động từ video nên có nhiều "
                "chỗ NGHE NHẦM (sai danh xưng, sai từ, câu vô nghĩa). Hãy sửa lại "
                "cho đúng tiếng Việt tự nhiên và hợp ngữ cảnh câu chuyện. KHÔNG "
                "dịch, KHÔNG diễn giải thêm, giữ nguyên ý và độ dài tương đương. "
                "Dòng nào đã đúng thì chép lại y nguyên.")
    else:
        head = ("Bạn còn thiếu mấy dòng. Dịch NỐT đúng những dòng sau, GIỮ NGUYÊN số."
                if again else
                "Bạn là biên dịch phim chuyên nghiệp. Dịch các dòng sau sang TIẾNG VIỆT "
                "thật tự nhiên, đúng ngữ điệu, giữ xưng hô nhân vật nhất quán. "
                "Dịch đủ chủ-vị-tân ngữ; không lược đại từ hoặc danh xưng làm câu cụt.")
    return (
        ctx + name_note + head + limit_note +
        f" Trả về ĐÚNG {len(numbers)} dòng theo mẫu:  "
        + ("[số] câu đã sửa\n" if proofread else "[số] bản dịch\n") +
        "Mỗi dòng nguồn gắn với một mốc thời gian cố định trên video, nên dòng "
        "[k] của bạn phải nói ĐÚNG nội dung của dòng nguồn [k]: không kéo ý của "
        "dòng trước xuống, không đẩy ý của dòng này sang dòng sau, không đảo "
        "thứ tự. Sai điều này thì lời thoại sẽ phát lệch khỏi hình hàng chục "
        "giây, dù từng câu dịch vẫn hay. Nguồn do máy nghe tự động nên nhiều "
        "dòng bị cắt ngang giữa câu: gặp dòng cụt thì cứ dịch đúng phần chữ có "
        "trong dòng đó thành một câu tiếng Việt đọc được, KHÔNG sắp xếp lại nội "
        "dung giữa các dòng cho xuôi tai. "
        "Câu Việt phải liền mạch; KHÔNG dùng dấu ba chấm (... hoặc …) để nối "
        "các mảnh câu. "
        "Giữ nguyên con số trong ngoặc vuông của từng dòng. KHÔNG dùng danh "
        "sách đánh số của markdown, KHÔNG in đậm, không lời dẫn, không giải "
        "thích, không gộp dòng.\n" + budget_block + "\nCac dong nguon:\n" + src
    )


def _build_shorten_prompt(chunk: List[Segment], over: List[int],
                          got: Dict[int, str], chars_per_sec: float) -> str:
    lines = "\n".join(
        f"[{k}] Gốc: {chunk[k-1].text} | Việt: {got[k]} "
        f"| target <= {char_budget(chunk[k-1], chars_per_sec)}, current {len(got[k])}"
        for k in over)
    return (
        "Các dòng dưới đây hơi dài so với nhịp lồng tiếng. Hãy RÚT GỌN VỪA ĐỦ, "
        "không rút quá tay. Mục tiêu là bám gần số ký tự trong ngoặc, nhưng bản "
        "rút gọn vẫn phải rõ nghĩa ngay khi nghe.\n"
        "- Giữ tên riêng, danh xưng, chủ-vị, quan hệ nguyên nhân/kết quả.\n"
        "- Không biến cụm đủ nghĩa thành cụm cụt nghĩa, ví dụ KHÔNG đổi "
        "\"kẻ nói dối\" thành \"kẻ dối\".\n"
        "- Không dùng dấu ba chấm (... hoặc …); câu Việt phải liền mạch.\n"
        "- Ưu tiên từ phổ thông, tránh Hán Việt khó hiểu nếu có cách nói tự nhiên hơn.\n"
        "- Nếu không thể ngắn hơn mà vẫn rõ nghĩa, được vượt mốc một chút.\n"
        "Trả về ĐÚNG mẫu:  [số] câu đã rút gọn\n"
        "Không lời dẫn, không giải thích.\n\n" + lines)


def shorten_long_lines(chunk: List[Segment], got: Dict[int, str],
                       chars_per_sec: float, ask, label: str = "") -> int:
    """Rút gọn tại chỗ các dòng dịch dài quá nhịp lồng tiếng.

    `got` ánh xạ số thứ tự trong lô (1-based) sang bản dịch, được cập nhật tại
    chỗ. `ask` nhận prompt và trả về nguyên văn câu trả lời của model, nhờ vậy
    dùng chung được cho cả luồng trình duyệt lẫn luồng API.

    Trả về số dòng VẪN còn vượt mốc sau khi rút gọn, để bước sau biết còn bao
    nhiêu câu sẽ phải nhờ TTS đọc nhanh bù.
    """
    cps = max(0.0, float(chars_per_sec or 0.0))
    if cps <= 0 or not got:
        return 0
    over = [k for k in sorted(got)
            if got[k] and _too_long_for_tts(chunk[k - 1], got[k], cps)]
    if not over:
        return 0

    log(f"{label}{len(over)}/{len(chunk)} dòng dài hơn nhịp lồng tiếng, "
        "nhờ rút gọn vừa đủ...", "warn")
    try:
        short = parse_numbered_reply(
            ask(_build_shorten_prompt(chunk, over, got, cps)), len(chunk))
    except Exception as e:
        log(f"  rút gọn không thành ({e}) - giữ bản dài.", "warn")
        return len(over)

    applied = 0
    for k, v in short.items():
        if k in over and _accept_shortened(got[k], v, chunk[k - 1], cps):
            got[k] = normalize_vi_subtitle_text(v)
            applied += 1
    still = sum(1 for k in over if _too_long_for_tts(chunk[k - 1], got[k], cps))
    log(f"  rút gọn được {applied}/{len(over)} dòng, còn {still} dòng vượt mốc.",
        "info")
    return still


def _dump_debug(path: Optional[str], chunk_no: int, attempt: int, reply: str,
                expected: int, missing: int) -> None:
    """Ghi lại NGUYÊN VĂN câu Gemini trả lời khi ghép không khớp.

    Khi giao diện Gemini đổi lần nữa, file này cho biết ngay nó trả về cái gì -
    khỏi phải đoán mò."""
    if not path:
        return
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"\n{'='*70}\nLÔ {chunk_no} - lượt {attempt} - cần {expected} "
                    f"dòng, còn thiếu {missing}\n{'-'*70}\n{reply[:4000]}\n")
    except Exception:
        pass


def _ask_once(page, msg: str, wait_reply: int) -> str:
    """Gửi 1 tin nhắn và chờ trả lời. Tự thử lại khi giao diện dựng lại DOM."""
    last_err: Optional[Exception] = None
    for attempt in range(4):
        try:
            box = _visible_locator(page, _INPUT_CANDIDATES, timeout=30.0)
            if box is None:
                raise RuntimeError("không thấy ô nhập của Gemini")
            try:
                prev = _resp_locator(page).count()
            except Exception:
                prev = 0
            if not _put_text(page, box, msg):
                raise RuntimeError("chèn nội dung vào ô nhập không thành công")
            _submit(page)
            reply = _wait_reply(page, prev, wait_reply)
            if reply:
                return reply
            raise RuntimeError("hết thời gian chờ mà chưa thấy trả lời")
        except Exception as e:
            last_err = e
            # (str(e).splitlines() or [""]): exception có message RỖNG thì
            # splitlines() trả [] -> [0] gây IndexError ngay trong khối except
            # (nơi lẽ ra phải bền nhất), làm sập cả quá trình dịch.
            log(f"lượt gửi {attempt + 1}/4 hỏng ({type(e).__name__}: "
                f"{(str(e).splitlines() or [''])[0][:80]})", "warn")
            if attempt == 3:
                break
            try:
                page.wait_for_timeout(1500 * (attempt + 1))
            except Exception:
                break
    raise RuntimeError(f"không gửi được sau 4 lần: {last_err}")


@contextmanager
def phien_gemini_trinh_duyet(profile_dir: str, channel: str = "msedge",
                             url: str = "https://gemini.google.com/app",
                             wait_reply: int = 240):
    """Mở Gemini trong trình duyệt rồi trả về một hàm `hoi(prompt) -> str`.

    Dùng chung cho mọi việc cần Gemini mà không có API key: dịch phụ đề, và
    (mới) viết kịch bản truyện audio. Toàn bộ phần khó - né cờ automation, chờ
    trả lời xong, gõ lại khi DOM dựng lại - nằm ở các hàm đã có sẵn bên dưới.

        with phien_gemini_trinh_duyet(profile) as hoi:
            tra_loi = hoi("Xin chào")
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        raise RuntimeError("Chưa cài Playwright. Chạy trong venv: pip install playwright")

    os.makedirs(profile_dir, exist_ok=True)
    log("Mở trình duyệt điều khiển Gemini (dùng phiên đăng nhập của bạn)...", "step")
    with sync_playwright() as p:
        ctx = _launch(p, profile_dir, channel)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(60000)
        try:
            page.goto(url, wait_until="domcontentloaded")
            if _visible_locator(page, _INPUT_CANDIDATES, timeout=25.0) is None:
                log("Chưa vào được ô chat Gemini (có thể chưa đăng nhập). Hãy "
                    "xử lý trong cửa sổ vừa mở...", "warn")
                try:
                    input("   → Xong rồi thì quay lại đây bấm ENTER để tiếp tục...")
                except EOFError:
                    page.wait_for_timeout(30000)
                if _visible_locator(page, _INPUT_CANDIDATES, timeout=180.0) is None:
                    raise RuntimeError("Vẫn không tìm thấy ô nhập của Gemini.")

            def hoi(prompt: str) -> str:
                return _ask_once(page, prompt, wait_reply)

            yield hoi
        finally:
            try:
                ctx.close()
            except Exception:
                pass


def translate_via_browser(
    segments: List[Segment],
    profile_dir: str,
    channel: str = "msedge",
    url: str = "https://gemini.google.com/app",
    chunk_size: int = 25,
    wait_reply: int = 120,
    cache_path: Optional[str] = None,
    reset_every: int = 10,
    source_lang: Optional[str] = None,
    chars_per_sec: float = 0.0,
    name_hint: str = "",
    shorten_long_lines_enabled: bool = True,
) -> List[Segment]:
    """Điều khiển Edge (hoặc Chrome) vào Gemini web để dịch - dùng tài khoản Pro
    đã đăng nhập, KHÔNG cần API key.

    Lần đầu: cửa sổ Edge mở ra -> tự đăng nhập Google/Gemini 1 lần. Profile được
    nhớ trong thư mục 'browser_profile' nên lần sau khỏi đăng nhập lại.

    Lô nào dịch hỏng thì GIỮ NGUYÊN câu gốc và đi tiếp, cuối cùng báo rõ số lô
    hỏng - thà thiếu vài câu còn hơn mất trắng cả tiếng đồng hồ đã chạy.
    """
    try:
        from playwright.sync_api import sync_playwright
    except Exception:
        raise RuntimeError("Chưa cài Playwright. Chạy trong venv: pip install playwright")

    os.makedirs(profile_dir, exist_ok=True)
    cache = ChunkCache(cache_path)
    debug_path = (os.path.join(os.path.dirname(cache_path), "_gemini_tra_loi.txt")
                  if cache_path else None)
    if debug_path and os.path.exists(debug_path):
        try:
            os.remove(debug_path)          # chỉ giữ log của lần chạy này
        except OSError:
            pass
    log("Mở trình duyệt điều khiển Gemini (dùng phiên đăng nhập Pro của bạn)...", "step")

    n = len(segments)
    total_chunks = max(1, (n + chunk_size - 1) // chunk_size)
    failed: List[int] = []
    context: List[str] = []
    # Audio vốn ĐÃ là tiếng Việt -> không dịch nữa mà nhờ Gemini SỬA chỗ nghe
    # nhầm. Đây là cách duy nhất chữa được lỗi nghe nhầm cả cụm ("sao chân
    # phải ta không bằng cảm giác" -> "sao chân ta không còn cảm giác gì cả").
    proofread = str(source_lang or "").lower().startswith("vi")
    if proofread:
        log("Nguồn đã là tiếng Việt -> chuyển sang chế độ SỬA LỖI NGHE NHẦM "
            "(không dịch).", "info")
    cps = max(0.0, float(chars_per_sec))
    if cps > 0:
        log(f"Canh độ dài bản dịch theo thời lượng từng câu (~{cps:.0f} ký tự/giây, "
            "có biên độ giữ nghĩa) để giọng đọc bám hình.", "info")
    long_lines = 0
    missing_lines = 0          # tổng số dòng thiếu ở các lô dịch DỞ (không phải lô hỏng hẳn)

    with sync_playwright() as p:
        ctx = _launch(p, profile_dir, channel)
        page = ctx.pages[0] if ctx.pages else ctx.new_page()
        page.set_default_timeout(60000)
        try:
            page.goto(url, wait_until="domcontentloaded")

            # Chờ ô soạn thảo. Chưa đăng nhập -> nhắc người dùng làm thủ công.
            if _visible_locator(page, _INPUT_CANDIDATES, timeout=25.0) is None:
                log("Chưa vào được ô chat Gemini (có thể chưa đăng nhập, hoặc "
                    "đang hiện hộp thoại giới thiệu). Hãy xử lý trong cửa sổ "
                    "vừa mở...", "warn")
                try:
                    input("   → Xong rồi thì quay lại đây bấm ENTER để tiếp tục...")
                except EOFError:
                    page.wait_for_timeout(30000)
                if _visible_locator(page, _INPUT_CANDIDATES, timeout=180.0) is None:
                    raise RuntimeError("Vẫn không tìm thấy ô nhập của Gemini.")

            sent_since_reset = 0
            for ci, i in enumerate(range(0, n, chunk_size), 1):
                chunk = segments[i:i + chunk_size]
                src_texts = [s.text for s in chunk]
                ckey = ChunkCache.key(i, src_texts)

                cached = cache.get(ckey)
                # CHỈ bỏ qua lô khi nó đã dịch ĐẦY ĐỦ (không còn dòng rỗng). Bản
                # cũ bỏ qua cả khi cache còn chứa dòng rỗng (lô dịch DỞ) -> các
                # dòng thiếu bị KẸT tiếng gốc VĨNH VIỄN dù chạy lại, trái với lời
                # hứa "chạy lại sẽ dịch tiếp phần còn thiếu". Lô dở: nạp sẵn phần
                # đã có rồi chỉ hỏi bổ sung đúng những dòng còn thiếu.
                seed: Dict[int, str] = {}
                if cached and len(cached) == len(chunk):
                    cleaned_cached = _clean_vi_lines(cached)
                    if cleaned_cached != cached:
                        cache.put(ckey, cleaned_cached)
                    cached = cleaned_cached
                    bad = _cjk_line_numbers(cached)
                    if bad:
                        log(f"Cache lô {ci}/{total_chunks} còn tiếng Trung ở dòng "
                            f"{_bad_line_summary(bad)} -> bỏ cache và dịch lại.", "warn")
                        cache.discard(ckey)
                        cached = None
                if cached and len(cached) == len(chunk):
                    if all(cached):
                        for s, t in zip(chunk, cached):
                            if t:
                                s.text = normalize_vi_subtitle_text(t)
                        context.extend([t for t in cached if t])
                        del context[:-8]
                        log(f"(cache) lô {ci}/{total_chunks} - bỏ qua, đã dịch trước đó", "info")
                        continue
                    seed = {k: cached[k - 1] for k in range(1, len(chunk) + 1) if cached[k - 1]}
                    log(f"(cache) lô {ci}/{total_chunks} - đã có {len(seed)}/{len(chunk)} "
                        "dòng từ lần trước, dịch tiếp phần còn thiếu.", "info")

                # Chat quá dài làm Gemini chậm dần và dễ lạc đề -> định kỳ mở chat mới
                if reset_every and sent_since_reset >= reset_every:
                    try:
                        page.goto(url, wait_until="domcontentloaded")
                        _visible_locator(page, _INPUT_CANDIDATES, timeout=30.0)
                        sent_since_reset = 0
                        log("Đã mở đoạn chat mới cho nhẹ ngữ cảnh.", "info")
                    except Exception:
                        pass

                # Hỏi lần đầu cả lô; thiếu dòng nào thì hỏi BỔ SUNG đúng dòng đó
                # (giữ nguyên số thứ tự) thay vì hỏi lại cả lô -> nhanh và hội tụ.
                got: Dict[int, str] = dict(seed)
                need = [k for k in range(1, len(chunk) + 1) if k not in got]
                for attempt in range(3):
                    try:
                        reply = _ask_once(
                            page, _build_prompt(chunk, need, context,
                                                again=attempt > 0,
                                                proofread=proofread,
                                                chars_per_sec=cps,
                                                name_hint=name_hint),
                            wait_reply)
                    except Exception as e:
                        log(f"Lô {ci}/{total_chunks} lỗi: {e}", "warn")
                        break
                    sent_since_reset += 1
                    new = parse_numbered_reply(reply, len(chunk))
                    if not new:
                        # Không thấy số nào -> ghép theo VỊ TRÍ, và chỉ khi số
                        # dòng khớp đúng khít.
                        new = match_by_position(reply, need)
                        if new:
                            log(f"Lô {ci}/{total_chunks}: trả lời không có số thứ tự, "
                                f"ghép theo vị trí ({len(new)} dòng khớp).", "warn")
                    for k, v in new.items():
                        if k in need:
                            clean_v = normalize_vi_subtitle_text(v)
                            if _contains_cjk(clean_v):
                                log(f"Lô {ci}/{total_chunks}, dòng {k}: phản hồi còn "
                                    "tiếng Trung, hỏi bổ sung lại.", "warn")
                                continue
                            got[k] = clean_v
                    need = [k for k in range(1, len(chunk) + 1) if k not in got]
                    if not need:
                        break
                    _dump_debug(debug_path, ci, attempt + 1, reply, len(chunk), len(need))
                    log(f"Lô {ci}/{total_chunks}: còn thiếu {len(need)}/{len(chunk)} "
                        "dòng, hỏi bổ sung...", "warn")
                    page.wait_for_timeout(1200)

                if not got:
                    failed.append(ci)
                    log(f"Lô {ci}/{total_chunks} BỎ QUA - giữ nguyên câu gốc.", "err")
                    continue

                # Rút gọn lần hai từng bị tắt vì làm hỏng nghĩa ("mà bọn nó nói"
                # -> "mà bọn nói"). Nay bật lại kèm bộ chặn ở _accept_shortened:
                # phải giữ tên riêng, con số, và không được rút dưới sàn tỉ lệ.
                if (ENABLE_BROWSER_SHORTENING and shorten_long_lines_enabled
                        and cps > 0 and got):
                    def _ask(prompt: str) -> str:
                        nonlocal sent_since_reset
                        sent_since_reset += 1
                        return _ask_once(page, prompt, wait_reply)

                    long_lines += shorten_long_lines(
                        chunk, got, cps, _ask,
                        label=f"Lô {ci}/{total_chunks}: ")

                vi = _clean_vi_lines([got.get(k + 1, "") for k in range(len(chunk))])
                for s, t in zip(chunk, vi):
                    if t:
                        s.text = normalize_vi_subtitle_text(t)
                cache.put(ckey, vi)
                context.extend([t for t in vi if t])
                del context[:-8]     # chỉ dùng vài dòng cuối làm ngữ cảnh
                miss = sum(1 for t in vi if not t)
                missing_lines += miss
                log(f"(browser) lô {ci}/{total_chunks} - {min(i + chunk_size, n)}/{n} dòng"
                    + (f" (thiếu {miss} dòng, giữ bản gốc)" if miss else ""),
                    "warn" if miss else "info")
        finally:
            try:
                ctx.close()
            except Exception:
                pass

    if failed:
        log(f"Có {len(failed)}/{total_chunks} lô KHÔNG dịch được (lô "
            f"{', '.join(map(str, failed[:10]))}{'...' if len(failed) > 10 else ''}). "
            "Các dòng đó giữ nguyên tiếng gốc. Chạy lại chương trình sẽ chỉ dịch "
            "phần còn thiếu (đã có cache).", "err")
        if debug_path and os.path.exists(debug_path):
            log(f"Nguyên văn câu Gemini trả lời đã lưu ở: {debug_path}", "info")
        # Thiếu 1 lô thì bỏ qua cho xong việc; thiếu nhiều thì dừng, đừng lồng
        # tiếng một bản dịch lỗ chỗ rồi phải render lại từ đầu.
        if len(failed) > max(1.0, total_chunks * 0.1):
            raise TranslationIncomplete(failed, total_chunks)
    elif missing_lines:
        # Không lô nào hỏng HẲN nhưng vẫn có dòng lẻ chưa dịch được (nằm rải rác
        # trong các lô dở) -> đừng in "Dịch xong toàn bộ" gây hiểu nhầm là sạch.
        log(f"Dịch xong nhưng còn {missing_lines}/{n} dòng lẻ chưa dịch được, "
            "đang giữ tiếng gốc. Chạy lại chương trình sẽ dịch tiếp các dòng này "
            "(đã có cache).", "warn")
    else:
        log("Dịch xong toàn bộ.", "ok")
    if cps > 0 and long_lines:
        log(f"Còn {long_lines}/{n} dòng dài hơn thời lượng cho phép - bước lồng "
            "tiếng sẽ tự tăng tốc đọc để bù.", "warn")
    return segments
