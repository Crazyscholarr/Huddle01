"""Đọc/ghi phần cấu hình mà giao diện được phép chỉnh (mục translation, tts).

Ghi config.yaml theo kiểu SỬA TỪNG DÒNG (giữ nguyên comment và thứ tự) chứ
không dump lại cả file - dump sẽ xoá sạch chú thích hướng dẫn của người dùng.
"""
from __future__ import annotations

import json
import os
import re
from typing import Dict, Optional, Tuple

from .state import CONFIG_PATH


def _load_cfg() -> Dict:
    import yaml
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


_TRANSLATION_GUI_KEYS = (
    "provider",
    "gemini_api_key",
    "gemini_model",
    "tokenrouter_api_key",
    "tokenrouter_base_url",
    "tokenrouter_model",
    "tokenrouter_timeout",
    "tokenrouter_gemini_api_key",
    "tokenrouter_gemini_base_url",
    "tokenrouter_gemini_model",
    "tokenrouter_gemini_timeout",
    "inferx_api_key",
    "inferx_base_url",
    "inferx_model",
    "inferx_timeout",
    "nvidia_api_key",
    "nvidia_base_url",
    "nvidia_model",
    "nvidia_timeout",
    "zenmux_api_key",
    "zenmux_base_url",
    "zenmux_model",
    "zenmux_timeout",
    "male_lead_name",
    "female_lead_name",
    "chunk_size",
    "chars_per_sec",
)


def _translation_cfg_for_gui() -> Dict:
    tr = (_load_cfg().get("translation") or {})
    return {
        "provider": tr.get("provider", "browser"),
        "gemini_api_key": tr.get("gemini_api_key", ""),
        "gemini_model": tr.get("gemini_model", "gemini-3.6-flash"),
        "tokenrouter_api_key": tr.get("tokenrouter_api_key", ""),
        "tokenrouter_base_url": tr.get("tokenrouter_base_url",
                                       "https://api.tokenrouter.com/v1"),
        "tokenrouter_model": tr.get("tokenrouter_model",
                                    "moonshotai/kimi-k3-free"),
        "tokenrouter_timeout": tr.get("tokenrouter_timeout", 420),
        "tokenrouter_gemini_api_key": tr.get("tokenrouter_gemini_api_key", ""),
        "tokenrouter_gemini_base_url": tr.get(
            "tokenrouter_gemini_base_url",
            "https://api.tokenrouter.com/v1beta/models"),
        "tokenrouter_gemini_model": tr.get(
            "tokenrouter_gemini_model", "google/gemini-3.6-flash"),
        "tokenrouter_gemini_timeout": tr.get("tokenrouter_gemini_timeout", 420),
        "inferx_api_key": tr.get("inferx_api_key", ""),
        "inferx_base_url": tr.get(
            "inferx_base_url", "https://model.inferx.net/endpoints/v1"),
        "inferx_model": tr.get("inferx_model", "deepseek-v4-flash"),
        "inferx_timeout": tr.get("inferx_timeout", 420),
        "nvidia_api_key": tr.get("nvidia_api_key", ""),
        "nvidia_base_url": tr.get("nvidia_base_url",
                                  "https://integrate.api.nvidia.com/v1"),
        "nvidia_model": tr.get("nvidia_model", "minimaxai/minimax-m3"),
        "nvidia_timeout": tr.get("nvidia_timeout", 420),
        "zenmux_api_key": tr.get("zenmux_api_key", ""),
        "zenmux_base_url": tr.get("zenmux_base_url",
                                  "https://zenmux.ai/api/v1"),
        "zenmux_model": tr.get("zenmux_model", "z-ai/glm-5.3-free"),
        "zenmux_timeout": tr.get("zenmux_timeout", 420),
        "male_lead_name": tr.get("male_lead_name", ""),
        "female_lead_name": tr.get("female_lead_name", ""),
        "chunk_size": tr.get("chunk_size", 80),
        "chars_per_sec": tr.get("chars_per_sec", 14),
    }


def _tts_cfg_for_gui() -> Dict:
    tc = (_load_cfg().get("tts") or {})
    engine = str(tc.get("engine", "edge") or "edge").lower()
    voice = (tc.get("vieneu_voice") if engine == "vieneu"
             else tc.get("capcut_voice") if engine == "capcut"
             else tc.get("narrator_voice"))
    return {
        "engine": engine,
        "voice": voice or "vi-VN-NamMinhNeural",
        "pitch": tc.get("narrator_pitch", "+0Hz"),
        "rate": tc.get("base_rate", "+0%"),
    }


def _yaml_scalar(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None or value == "":
        return '""'
    if isinstance(value, (int, float)):
        return str(value)
    return json.dumps(str(value), ensure_ascii=False)


def _save_translation_cfg(updates: Dict) -> Dict:
    clean = {k: updates[k] for k in _TRANSLATION_GUI_KEYS if k in updates}
    provider = str(clean.get("provider", "") or "").lower()
    if provider and provider not in {"browser", "gemini", "tokenrouter",
                                     "tokenrouter_gemini", "inferx", "nvidia",
                                     "zenmux"}:
        raise ValueError("provider must be browser, gemini, tokenrouter, "
                         "tokenrouter_gemini, inferx, nvidia, or zenmux")
    if "chunk_size" in clean:
        clean["chunk_size"] = max(1, int(float(clean["chunk_size"] or 80)))
    if "chars_per_sec" in clean:
        clean["chars_per_sec"] = max(0, float(clean["chars_per_sec"] or 0))
    if "tokenrouter_timeout" in clean:
        clean["tokenrouter_timeout"] = max(60, int(float(clean["tokenrouter_timeout"] or 420)))
    if "tokenrouter_gemini_timeout" in clean:
        clean["tokenrouter_gemini_timeout"] = max(
            60, int(float(clean["tokenrouter_gemini_timeout"] or 420)))
    if "inferx_timeout" in clean:
        clean["inferx_timeout"] = max(60, int(float(clean["inferx_timeout"] or 420)))
    if "nvidia_timeout" in clean:
        clean["nvidia_timeout"] = max(60, int(float(clean["nvidia_timeout"] or 420)))
    if "zenmux_timeout" in clean:
        clean["zenmux_timeout"] = max(60, int(float(clean["zenmux_timeout"] or 420)))

    with open(CONFIG_PATH, "r", encoding="utf-8", newline="") as f:
        lines = f.readlines()

    start = next((i for i, line in enumerate(lines)
                  if re.match(r"^translation\s*:", line)), None)
    if start is None:
        lines.append("\ntranslation:\n")
        start = len(lines) - 1
        end = len(lines)
    else:
        end = len(lines)
        for i in range(start + 1, len(lines)):
            if re.match(r"^\S", lines[i]) and not lines[i].lstrip().startswith("#"):
                end = i
                break

    seen = set()
    for i in range(start + 1, end):
        m = re.match(r"^(\s*)([A-Za-z0-9_]+)(\s*:\s*)(.*?)(\s+#.*)?(\r?\n)?$",
                     lines[i])
        if not m:
            continue
        key = m.group(2)
        if key not in clean:
            continue
        newline = m.group(6) or "\n"
        comment = m.group(5) or ""
        lines[i] = f"{m.group(1)}{key}{m.group(3)}{_yaml_scalar(clean[key])}{comment}{newline}"
        seen.add(key)

    missing = [k for k in _TRANSLATION_GUI_KEYS if k in clean and k not in seen]
    if missing:
        insert = [f"  {k}: {_yaml_scalar(clean[k])}\n" for k in missing]
        lines[end:end] = insert

    tmp = CONFIG_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.writelines(lines)
    os.replace(tmp, CONFIG_PATH)
    return _translation_cfg_for_gui()


def _translation_api_params(tr: Dict, provider: str) -> Tuple[str, str, Optional[str], int]:
    """Cùng một bảng với CLI (autodub/translate.py) để hai đường không lệch nhau."""
    from .. import translate as tr_mod
    return tr_mod.api_params_for_provider(tr, provider)


def _test_translation_api(overrides: Optional[Dict] = None) -> Dict:
    from .. import translate as tr_mod

    cfg_tr = _translation_cfg_for_gui()
    if isinstance(overrides, dict):
        cfg_tr.update({k: v for k, v in overrides.items()
                       if k in _TRANSLATION_GUI_KEYS})
    provider = str(cfg_tr.get("provider", "browser") or "browser").lower()
    if provider == "browser":
        return {"ok": True, "provider": provider,
                "message": "browser mode khong dung API key"}
    if provider not in {"gemini", "tokenrouter", "tokenrouter_gemini",
                        "inferx", "nvidia", "zenmux"}:
        raise ValueError("provider khong hop le")

    api_key, model, base_url, timeout = _translation_api_params(cfg_tr, provider)
    if not api_key:
        if provider == "nvidia":
            raise ValueError(
                "Chưa lưu NVIDIA API key. Mở Cài đặt > API, dán key nvapi-... "
                "mới rồi bấm Kiểm tra kết nối API.")
        raise ValueError("Chưa điền API key cho provider đang chọn")
    raw = tr_mod._api_call(
        'Return exactly this JSON array and nothing else: ["ok"]',
        api_key, model, 0.1, provider, base_url, min(timeout, 180))
    parsed = tr_mod._parse_json_lines(raw, 1)
    if parsed != ["ok"]:
        return {"ok": True, "provider": provider, "model": model,
                "message": "API tra loi duoc nhung khong dung JSON test",
                "raw": str(raw)[:200]}
    return {"ok": True, "provider": provider, "model": model,
            "message": "API key/model hoat dong"}
