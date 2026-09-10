"""Các endpoint của luồng "kể chuyện" (tab Tạo audio trên giao diện).

Mỗi hàm nhận body JSON đã parse và trả về (obj, http_code) để Handler gọi
`self._json(*api_xxx(b))`. Việc nặng đều chạy trong thread nền, kết quả cập
nhật vào STATE["manual"] cho giao diện poll.
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
from typing import Dict, Optional, Tuple

from .. import overlays, srt_utils
from ..srt_utils import Segment
from ..utils import ffprobe_duration
from .state import (HERE, STATE, _LOCK, current_cancel_event, submit_job,
                    _log, _progress, _find)
from .helpers import _safe_path_stem, _doc_file_van_ban
from .config_api import _load_cfg
from .projects import (get_project, _active_media_span, _run_stem_for_project,
                       _project_rows_for_span, _segments_from_rows,
                       _render_project_for_span)
from .render import render_with_layers, render_with_layers_chunked

JsonResult = Tuple[Dict, int]


def _raise_if_cancelled() -> None:
    if current_cancel_event().is_set():
        raise InterruptedError("Đã dừng tác vụ theo yêu cầu.")


def _mark_manual_cancelled(detail: str = "Các phần đã làm xong vẫn được giữ lại.") -> None:
    """Đưa tác vụ kể chuyện về trạng thái dừng, không báo nhầm là lỗi."""
    with _LOCK:
        manual = STATE["manual"]
        manual.update({
            "working": False,
            "status": "Đã dừng tác vụ",
            "error": "",
            "rev": int(manual.get("rev", 0)) + 1,
        })
    _progress(step="Đã dừng", detail=detail)
    _log("Đã dừng tác vụ theo yêu cầu; dữ liệu hoàn tất vẫn được giữ lại.", "warn")


def _story_output_dir(title: str) -> str:
    """Thư mục sản phẩm video kể chuyện, đặt theo đúng tiêu đề dễ nhận biết."""
    safe_title = _safe_path_stem(title, fallback="video_ke_chuyen", limit=70)
    return os.path.join(HERE, "output", safe_title)


def _save_youtube_metadata(video_path: str, payload: Dict) -> str:
    """Ghi sidecar để tiêu đề/mô tả/tag đi cùng file render cuối."""
    description = str(payload.get("youtube_description") or "").strip()
    outline = str(payload.get("content_outline") or "").strip()
    idea_id = str(payload.get("content_idea_id") or "").strip()
    raw_tags = payload.get("youtube_tags") or []
    if isinstance(raw_tags, str):
        tags = [x.strip() for x in re.split(r"[,;\n]+", raw_tags) if x.strip()]
    elif isinstance(raw_tags, (list, tuple)):
        tags = [str(x).strip() for x in raw_tags if str(x).strip()]
    else:
        tags = []
    if not any((description, outline, idea_id, tags)):
        return ""
    metadata = {
        "video_title": str(payload.get("name") or "").strip(),
        "youtube_description": description,
        "youtube_tags": tags,
        "content_outline": outline,
        "content_idea_id": idea_id,
        "content_calendar_path": str(payload.get("content_calendar_path") or ""),
        "video_path": os.path.abspath(video_path),
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
    }
    target = os.path.splitext(os.path.abspath(video_path))[0] + ".youtube.json"
    with open(target, "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False, indent=2)
    return target


def _save_story_deliverables(video_path: str, payload: Dict) -> Tuple[str, str]:
    """Lưu sidecar YouTube và cập nhật Excel lịch nội dung sau render."""
    calendar_path = ""
    try:
        from ..content_pipeline import ContentStore, append_content_calendar

        if not os.path.isfile(video_path):
            raise FileNotFoundError("video kết quả chưa tồn tại để ghi lịch")

        cfg = _load_cfg()
        cp = cfg.get("content_pipeline") if isinstance(
            cfg.get("content_pipeline"), dict) else {}

        def resolve(value: str, default: str) -> str:
            raw = os.path.expandvars(str(value or default).strip().strip('"'))
            return os.path.abspath(raw if os.path.isabs(raw) else os.path.join(HERE, raw))

        database = resolve(cp.get("database", ""), "data/content_ideas.sqlite")
        output_dir = resolve(cp.get("output_dir", ""), "output/content_plans")
        calendar_target = resolve(
            cp.get("calendar_file", ""),
            os.path.join(output_dir, "lich_noi_dung_goc_mit.xlsx"))
        idea_id = str(payload.get("content_idea_id") or "").strip()
        store = ContentStore(database)
        record = store.get(idea_id) if idea_id else None
        record = dict(record or {
            "id": idea_id,
            "source": "Nhập trực tiếp",
            "primary_genre": "Chuyện gia đình và tuổi già",
        })
        calendar_path = append_content_calendar(
            record, calendar_target,
            final_title=str(payload.get("name") or ""), video_path=video_path,
            target_duration=str(cp.get("target_duration") or "1h00 - 1h30"),
            status="Đã xuất video")
        if idea_id and store.get(idea_id):
            store.patch(idea_id, {"status": "rendered"})
        _log(f"[Kể chuyện] Đã cập nhật Excel lịch nội dung: {calendar_path}", "ok")
    except Exception as exc:
        # Video đã render thành công không được báo lỗi chỉ vì Excel đang mở.
        _log(f"[Kể chuyện] Chưa cập nhật được Excel lịch nội dung: {exc}", "warn")
    metadata_payload = dict(payload)
    metadata_payload["content_calendar_path"] = calendar_path
    return _save_youtube_metadata(video_path, metadata_payload), calendar_path


def _story_tts_workdir(out_dir: str, title: str, stamp: str,
                       engine: str) -> Tuple[str, int]:
    """Tiếp tục các clip CapCut của lượt hỏng gần nhất nếu còn trên đĩa.

    Tên mỗi clip CapCut chứa hash của nội dung + voice + tốc độ nên dùng lại
    thư mục cũ vẫn an toàn khi người dùng sửa truyện hoặc đổi giọng: chỉ file có
    đúng hash mới được tái sử dụng, phần khác sẽ được tạo mới.
    """
    tmp_root = os.path.join(out_dir, "_tmp")
    fresh = os.path.join(tmp_root, f"{title}_{stamp}")
    if str(engine or "").lower() != "capcut" or not os.path.isdir(tmp_root):
        return fresh, 0
    candidates = []
    try:
        for entry in os.scandir(tmp_root):
            if not entry.is_dir() or not entry.name.startswith(title + "_"):
                continue
            try:
                clips = sum(1 for item in os.scandir(entry.path)
                            if item.is_file() and item.name.startswith("capcut_")
                            and item.name.lower().endswith(".mp3")
                            and item.stat().st_size >= 512)
                if clips:
                    candidates.append((entry.stat().st_mtime, clips, entry.path))
            except OSError:
                continue
    except OSError:
        return fresh, 0
    if not candidates:
        return fresh, 0
    _mtime, clips, path = max(candidates, key=lambda item: item[0])
    return path, clips


def _lay_van_ban_tu_body(b: Dict) -> Tuple[str, Optional[JsonResult]]:
    """Lấy nội dung truyện từ body (text trực tiếp hoặc đường dẫn file txt)."""
    text = str(b.get("text") or "").strip()
    txt_path = str(b.get("txt_path") or "").strip().strip('"')
    if not text and txt_path:
        if not os.path.isfile(txt_path):
            return "", ({"error": f"Không thấy file: {txt_path}"}, 400)
        try:
            text = _doc_file_van_ban(txt_path)
        except Exception as e:
            return "", ({"error": f"Không đọc được file: {e}"}, 400)
        if not text.strip():
            return "", ({"error": "File văn bản trống."}, 400)
        if not b.get("name"):
            b["name"] = os.path.splitext(os.path.basename(txt_path))[0]
    if not text:
        return "", ({"error": "Hãy nhập văn bản cần đọc."}, 400)
    return text, None


def _cta_tts_options(payload: Dict) -> Dict:
    """Lấy tốc độ/nội dung CTA; tắt CTA thì tuyệt đối không tăng tốc câu nào."""
    cta = payload.get("cta") if isinstance(payload.get("cta"), dict) else {}
    if not bool(cta.get("enabled", True)):
        return {"channel_cta_speed": 1.0, "channel_cta_text": ""}
    try:
        speed = max(1.0, min(2.0, float(cta.get("speed", 2.0) or 2.0)))
    except (TypeError, ValueError):
        speed = 2.0
    return {"channel_cta_speed": speed,
            "channel_cta_text": str(cta.get("text") or "").strip()}


def _ensure_story_ctas(text: str, payload: Dict) -> str:
    """Bổ sung CTA cho cả truyện dán tay; truyện do writer tạo sẵn không bị lặp."""
    cta_cfg = payload.get("cta") if isinstance(payload.get("cta"), dict) else None
    body = str(text or "").strip()
    if not cta_cfg or not bool(cta_cfg.get("enabled", True)) or not body:
        return body
    template = str(cta_cfg.get("text") or "").strip() or (
        "Bạn đang nghe chuyện tại gốc mít kể chuyện . Nếu thấy câu chuyện này ý nghĩa, "
        "cô chú, anh chị nhớ đăng ký kênh, bật chuông và để lại một lời bình luận để "
        "tiếp tục đồng hành cùng Gốc Mít nghen. Mọi nội dung đều hư cấu xin mọi người "
        "không làm theo bất cứ dưới hình thức nào hoặc tung tin đồn , chúng tôi không "
        "chịu trách nhiệm .")
    cta = template.replace("{channel}", "Gốc Mít Kể Chuyện").strip()
    raw_positions = cta_cfg.get("positions")
    if not isinstance(raw_positions, (list, tuple)):
        raw_positions = [12, 55]
    positions = []
    for value in raw_positions:
        try:
            pct = max(5, min(90, int(float(value))))
        except (TypeError, ValueError):
            continue
        if pct not in positions:
            positions.append(pct)
    for fallback in (12, 55):
        if len(positions) >= 2:
            break
        if fallback not in positions:
            positions.append(fallback)
    already = body.count(cta)
    needed = max(0, len(positions) - already)
    if not needed:
        return body
    words = list(re.finditer(r"\S+", body))
    if len(words) < 2:
        return (body + "\n\n" + "\n\n".join([cta] * needed)).strip()
    cuts = []
    for pct in sorted(positions[-needed:]):
        target = max(1, min(len(words) - 1, int(len(words) * pct / 100.0)))
        start = words[target - 1].end()
        window = body[start:start + 1200]
        paragraph = re.search(r"\n\s*\n", window)
        sentence = re.search(r"[.!?][\"'”’)]*\s+", window)
        candidates = [start + m.end() for m in (paragraph, sentence) if m]
        cut = min(candidates) if candidates else start
        if cut in cuts:
            cut = start
        if cut not in cuts:
            cuts.append(cut)
    for cut in sorted(cuts, reverse=True):
        body = body[:cut].rstrip() + "\n\n" + cta + "\n\n" + body[cut:].lstrip()
    return body


def _story_design_text(payload: Dict) -> str:
    """Tìm hồ sơ nhân vật cạnh KICH_BAN_DOC.txt; truyện dán tay vẫn dùng dò tên."""
    explicit = str(payload.get("character_context_path") or "").strip().strip('"')
    txt_path = str(payload.get("txt_path") or "").strip().strip('"')
    candidates = [explicit]
    if txt_path:
        candidates.append(os.path.join(os.path.dirname(os.path.abspath(txt_path)),
                                       "00_ban_thiet_ke.txt"))
    for path in candidates:
        if path and os.path.isfile(path):
            try:
                return _doc_file_van_ban(path)
            except Exception:
                continue
    return ""


def _story_image_inputs(payload: Dict) -> list:
    """Lấy ảnh trực tiếp hoặc khôi phục đúng thứ tự từ manifest đã lưu."""
    anh = payload.get("anh")
    if isinstance(anh, str):
        anh = [x for x in re.split(r"[\r\n]+", anh) if x.strip()]
    images = list(anh) if isinstance(anh, list) else []
    if images:
        return images
    pack = str(payload.get("image_pack") or "").strip().strip('"')
    if not pack:
        return []
    try:
        from .. import story_images
        return story_images.resolve_images(pack)
    except Exception as exc:
        _log("Không khôi phục được gói ảnh: %s" % exc, "warn")
        return []


def _story_title_key(value: str) -> str:
    """Khoá so sánh tiêu đề, bỏ khác biệt hoa/thường và dấu câu."""
    return re.sub(r"[^\w]+", " ", str(value or "").casefold(),
                  flags=re.UNICODE).strip()


def _story_video_inputs(payload: Dict) -> list:
    raw = payload.get("video_sources", payload.get("source_videos", []))
    if isinstance(raw, str):
        raw = re.split(r"[\r\n]+", raw)
    return [str(x.get("path") if isinstance(x, dict) else x).strip()
            for x in (raw if isinstance(raw, list) else [])
            if str(x.get("path") if isinstance(x, dict) else x).strip()]


def _generated_story_image_inputs(payload: Dict, title: str) -> Tuple[Dict, list, str]:
    """Không cho quy trình tự tạo ảnh mượn nhầm gói của truyện trước.

    Cùng tiêu đề thì giữ gói để tiếp tục các cảnh còn thiếu. Tiêu đề khác và
    đang bật tự tạo ảnh thì bỏ cả ``anh`` lẫn ``image_pack`` cũ, buộc Gemini
    lập prompt và tạo một bộ ảnh độc lập.
    """
    clean_payload = dict(payload)
    images = _story_image_inputs(clean_payload)
    if not bool(clean_payload.get("auto_images", True)):
        return clean_payload, images, ""
    pack_path = str(clean_payload.get("image_pack") or "").strip().strip('"')
    if not pack_path:
        return clean_payload, images, ""
    try:
        from .. import story_images
        manifest = story_images.load_pack(pack_path)
        pack_title = str(manifest.get("title") or "").strip()
    except Exception:
        manifest = {}
        pack_title = ""
    if pack_title and _story_title_key(pack_title) == _story_title_key(title):
        scene_count = len(manifest.get("scenes") or [])
        ready_count = int(manifest.get("ready_count", 0) or 0)
        if scene_count and ready_count < scene_count:
            # Giữ manifest để lượt sau sinh tiếp đúng những cảnh còn thiếu,
            # nhưng không đưa bộ ảnh dở dang sang bước dựng video.
            clean_payload["anh"] = []
            return clean_payload, [], ""
        return clean_payload, images, ""
    clean_payload["anh"] = []
    clean_payload["image_pack"] = ""
    return clean_payload, [], pack_title or "gói ảnh không xác định"


def _story_slideshow_images(payload: Dict, fallback: list,
                            total_duration: float) -> Tuple[list, bool]:
    """Lập lịch ảnh theo chương từ manifest; trả (ảnh, có_lặp_chủ_ý)."""
    pack = str(payload.get("image_pack") or "").strip().strip('"')
    script_path = str(payload.get("txt_path") or "").strip().strip('"')
    if not pack or not script_path:
        return list(fallback), False
    try:
        from .. import story_images
        weights = story_images.chapter_weights_from_script(script_path)
        planned = story_images.expand_for_chapters(
            pack, weights, total_duration=total_duration)
        if planned:
            _log("Gói ảnh: xếp %d cảnh theo %d chương, giữ đúng thứ tự manifest."
                 % (len(planned), len(weights) or 1), "ok")
            return planned, True
    except Exception as exc:
        _log("Không lập được lịch ảnh theo chương; dùng thứ tự ảnh thường: %s" % exc,
             "warn")
    return list(fallback), False


def api_story_image_pack(b: Dict) -> JsonResult:
    """Tạo/cập nhật gói prompt Gemini và ảnh đã tải về theo thứ tự cảnh."""
    try:
        from .. import kich_ban, story_images
        manifest_path = str(b.get("manifest_path") or b.get("image_pack") or "").strip()
        images = b.get("images", b.get("anh", []))
        if isinstance(images, str):
            images = [x for x in re.split(r"[\r\n]+", images) if x.strip()]
        if manifest_path:
            manifest = story_images.load_pack(manifest_path)
            expected_title = str(b.get("expected_title") or "").strip()
            pack_title = str(manifest.get("title") or "").strip()
            if (expected_title and
                    _story_title_key(expected_title) != _story_title_key(pack_title)):
                owner = pack_title or "gói ảnh không xác định"
                return {"error": ("Gói prompt này thuộc truyện '%s', không phải '%s'. "
                                  "Hãy tạo kịch bản cho tiêu đề mới trước."
                                  % (owner, expected_title))}, 409
            if isinstance(images, list) and (images or b.get("replace_images")):
                manifest = story_images.attach_images(manifest_path, images)
            return story_images.public_summary(
                manifest, include_prompt=bool(b.get("include_prompt", True))), 200

        design = str(b.get("design_text") or "").strip() or _story_design_text(b)
        design_path = str(b.get("character_context_path") or "").strip().strip('"')
        txt_path = str(b.get("txt_path") or "").strip().strip('"')
        if not design_path and txt_path:
            candidate = os.path.join(os.path.dirname(os.path.abspath(txt_path)),
                                     "00_ban_thiet_ke.txt")
            if os.path.isfile(candidate):
                design_path = candidate
        try:
            count = max(1, min(60, int(b.get("scene_count", 14) or 14)))
        except (TypeError, ValueError):
            count = 14
        aspect = "9:16" if str(b.get("aspect") or "") == "9:16" else "16:9"
        prompts = b.get("scene_prompts")
        if not isinstance(prompts, list):
            prompts = story_images.parse_scene_prompts(str(b.get("prompts_text") or ""))
        master = str(b.get("master_prompt") or "").strip()
        if not master:
            if design:
                master = kich_ban.prompt_anh(design, so_canh=count, kho=aspect)
            else:
                master = ("Hãy lập %d prompt ảnh %s theo đúng thứ tự cho truyện: %s. "
                          "Giữ nhân vật và phong cách nhất quán; không chữ, logo hay watermark."
                          % (count, aspect, str(b.get("title") or b.get("name") or "").strip()))
        manifest = story_images.create_pack(
            title=str(b.get("title") or b.get("name") or "Truyện").strip(),
            design_text=design, master_prompt=master, scene_prompts=prompts,
            image_paths=images if isinstance(images, list) else [],
            aspect=aspect, scene_count=count, script_path=txt_path,
            design_path=design_path)
        return story_images.public_summary(manifest, include_prompt=True), 200
    except Exception as exc:
        return {"error": "Không tạo/cập nhật được gói ảnh: %s" % exc}, 500


def api_story_image_pack_latest() -> JsonResult:
    try:
        from .. import story_images
        manifest = story_images.latest_pack()
        if not manifest:
            return {"ok": True, "manifest_path": "", "images": []}, 200
        return story_images.public_summary(manifest, include_prompt=False), 200
    except Exception as exc:
        return {"error": "Không đọc được gói ảnh gần nhất: %s" % exc}, 500


def _story_image_generation_config(cfg: Dict) -> Tuple[Dict, str, str, str]:
    """Tách cấu hình ảnh; browser tuyệt đối không mượn nhầm key dịch cũ."""
    from .. import story_images

    image_cfg = cfg.get("tao_anh") if isinstance(cfg.get("tao_anh"), dict) else {}
    provider = str(image_cfg.get("provider") or "browser").strip().lower()
    if provider not in {"api", "gemini"}:
        return image_cfg, "browser", "", str(
            image_cfg.get("gemini_image_model") or story_images.DEFAULT_IMAGE_MODEL)
    tr_cfg = cfg.get("translation") if isinstance(cfg.get("translation"), dict) else {}
    api_key = str(image_cfg.get("gemini_api_key") or
                  tr_cfg.get("gemini_api_key") or "").strip()
    model = str(image_cfg.get("gemini_image_model") or
                story_images.DEFAULT_IMAGE_MODEL)
    return image_cfg, "api", api_key, model


def _prepare_generated_story_images(result: Dict, payload: Dict,
                                    cfg: Dict) -> Tuple[list, str]:
    """Từ kịch bản vừa viết: rút prompt rồi tự sinh ảnh bằng API hoặc Gemini web."""
    from .. import kich_ban, story_images

    image_cfg, image_provider, api_key, model = _story_image_generation_config(cfg)
    manifest = None
    existing_pack = str(payload.get("image_pack") or "").strip().strip('"')
    if existing_pack:
        try:
            candidate = story_images.load_pack(existing_pack)
            wanted_title = str(result.get("title") or payload.get("story_title") or "")
            same_title = (_story_title_key(candidate.get("title")) ==
                          _story_title_key(wanted_title))
            scenes = list(candidate.get("scenes") or [])
            if same_title and scenes and any(str(x.get("prompt") or "").strip()
                                             for x in scenes):
                manifest = candidate
                _log("Tiếp tục gói ảnh đang dở: %d/%d cảnh đã có; chỉ tạo phần còn thiếu."
                     % (int(candidate.get("ready_count", 0) or 0), len(scenes)), "ok")
        except Exception as exc:
            _log("Không tiếp tục được gói ảnh cũ; sẽ lập gói mới: %s" %
                 str(exc)[:160], "warn")

    if manifest is None:
        try:
            count = max(1, min(30, int(payload.get(
                "scene_count", image_cfg.get("scene_count", 14)) or 14)))
        except (TypeError, ValueError):
            count = 14
        aspect = "9:16" if str(payload.get("aspect") or "") == "9:16" else "16:9"
        design_path = str(result.get("design_path") or "")
        design = (_doc_file_van_ban(design_path)
                  if design_path and os.path.isfile(design_path) else "")
        master = kich_ban.prompt_anh(design, so_canh=count, kho=aspect)
        _progress(pct=36, step="Chuẩn bị hình ảnh",
                  detail="Rút prompt từng cảnh từ kịch bản")
        prompts = story_images.generate_scene_prompts(
            master, cfg, expected_count=count, logger=_log)
        manifest = story_images.create_pack(
            title=str(result.get("title") or payload.get("story_title") or "Truyện"),
            design_text=design, master_prompt=master, scene_prompts=prompts,
            aspect=aspect, scene_count=count,
            script_path=str(result.get("script_path") or ""),
            design_path=design_path)
    else:
        scenes = list(manifest.get("scenes") or [])
        count = len(scenes)
        aspect = str(manifest.get("aspect") or "16:9")
        prompts = [str(scene.get("prompt") or "") for scene in scenes]
    pack_path = str(manifest.get("manifest_path") or "")
    prompt_path = str(manifest.get("prompt_file") or "")
    ready_at_start = int(manifest.get("ready_count", 0) or 0)
    with _LOCK:
        manual = STATE["manual"]
        manual.update({
            "image_pack_path": pack_path,
            "image_prompt_path": prompt_path,
            "image_provider_url": story_images.GEMINI_WEB_URL,
            "image_scene_count": count,
            "image_ready_count": ready_at_start,
            "image_prompt_ready": False,
            "image_generation_status": (
                "Tiếp tục từ %d/%d ảnh…" % (ready_at_start, count)
                if ready_at_start else
                "Đã tạo prompt; chuẩn bị sinh ảnh…" if image_provider == "api"
                else "Đã tạo prompt; chuẩn bị tự tạo ảnh trên Gemini…"),
            "status": "Đã viết truyện; đang chuẩn bị hình ảnh…",
            "rev": int(manual.get("rev", 0)) + 1,
        })

    auto_images = bool(payload.get(
        "auto_images", image_cfg.get("auto_generate", True)))
    automation_error = ""

    def _image_progress(done, total, message=""):
        pct = 38 + 10 * float(done) / max(1, int(total))
        _progress(pct=pct, step="Tự tạo ảnh Gemini", detail=message)
        with _LOCK:
            manual = STATE["manual"]
            manual.update({
                "image_ready_count": int(done),
                "image_generation_status": message,
                "status": "Đang tự tạo ảnh %d/%d…" % (done, total),
                "rev": int(manual.get("rev", 0)) + 1,
            })

    if auto_images and image_provider == "api" and prompts and api_key:
        try:
            manifest = story_images.generate_images_gemini(
                pack_path, api_key, model=model,
                timeout=float(image_cfg.get("timeout_seconds", 180) or 180),
                max_retries=int(image_cfg.get("max_retries", 3) or 3),
                request_gap=float(image_cfg.get("request_gap_seconds", 1.5) or 0),
                logger=_log, progress=_image_progress,
                cancel_event=current_cancel_event())
        except Exception as exc:
            _log("Tự tạo ảnh Gemini chưa hoàn tất: %s" % str(exc)[:220], "warn")
            automation_error = str(exc)
            manifest = story_images.load_pack(pack_path)
    elif auto_images and image_provider == "browser" and prompts:
        settings = story_images.gemini_browser_settings(cfg)
        _log("Dùng Gemini Pro đã đăng nhập; app sẽ tự gửi từng prompt và tải "
             "ảnh, không dùng API key.", "info")
        try:
            manifest = story_images.generate_images_gemini_browser(
                pack_path, profile_dir=settings["profile_dir"],
                channel=settings["channel"], url=settings["url"],
                timeout=settings["timeout"], max_retries=settings["retries"],
                request_gap=settings["request_gap"],
                max_session_restarts=settings["session_restarts"],
                fresh_chat_every=settings["fresh_chat_every"],
                restart_cooldown=settings["restart_cooldown"], logger=_log,
                progress=_image_progress, cancel_event=current_cancel_event())
        except Exception as exc:
            automation_error = str(exc)
            _log("Tự tạo ảnh qua Gemini web chưa hoàn tất: %s" %
                 automation_error[:220], "warn")
            manifest = story_images.load_pack(pack_path)
    elif auto_images and not prompts:
        automation_error = "Chưa rút được danh sách prompt từng cảnh."
        _log("Chưa có danh sách prompt từng cảnh; giữ prompt tổng để xử lý lại.",
             "warn")
    elif auto_images and not api_key:
        automation_error = "Chưa có Gemini API key cho ảnh."
        _log("Chưa có Gemini API key cho ảnh; giữ gói prompt để xử lý lại.",
             "warn")

    images = story_images.resolve_images(pack_path)
    complete = bool(images) and len(images) >= count
    with _LOCK:
        manual = STATE["manual"]
        manual.update({
            "image_ready_count": len(images),
            "image_prompt_ready": not complete,
            "image_generation_status": (
                "Đã tạo đủ %d ảnh" % len(images) if complete else
                "Tự động Gemini lỗi: %s" % automation_error[:180]
                if automation_error else
                "Đang chờ ảnh: đã có %d/%d" % (len(images), count)),
            "rev": int(manual.get("rev", 0)) + 1,
        })
    return images if complete else [], pack_path


def _story_voice_plan(source_text: str, payload: Dict, cfg: Dict, engine: str,
                      voice: str, pitch: str) -> Optional[Dict]:
    """Lập dàn giọng khi bật tự chọn giọng hoặc đa giọng nhân vật."""
    auto_narrator = bool(payload.get("voice_auto", False))
    multi_voice = bool(payload.get("multi_voice", False))
    if not auto_narrator and not multi_voice:
        return None
    from .. import story_voice, tts as tts_mod
    catalog = tts_mod.list_voices(engine)
    if not catalog:
        _log("Không có catalog giọng để lập dàn nhân vật; dùng giọng đã chọn.", "warn")
        return None
    narrator_voice = voice
    if engine == "edge":
        normalized = tts_mod.normalize_edge_narrator({"voice": voice, "pitch": pitch})
        narrator_voice = "%s|%s" % (normalized["voice"], normalized["pitch"])
    plan = story_voice.plan_story_voices(
        source_text, catalog, engine=engine, narrator_voice=narrator_voice,
        design_text=_story_design_text(payload), auto_narrator=auto_narrator,
        max_characters=max(1, min(12, int(payload.get("max_character_voices", 8) or 8))))
    if not multi_voice:
        plan["cast"] = []
        plan["utterances"] = None
    return plan


def _save_voice_cast(path: str, plan: Optional[Dict]) -> str:
    if not plan:
        return ""
    payload = {k: v for k, v in plan.items() if k not in {"utterances", "characters"}}
    payload["characters"] = [
        {k: v for k, v in item.items() if k != "aliases"}
        for item in (plan.get("characters") or [])
    ]
    try:
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        return os.path.abspath(path)
    except OSError as exc:
        _log("Không lưu được sơ đồ dàn giọng: %s" % exc, "warn")
        return ""


def _segments_tu_timeline(timeline) -> list:
    return [Segment(i + 1, float(t["start"]), float(t["end"]), str(t["text"]))
            for i, t in enumerate(timeline or [])]


def _ass_tu_srt(srt_path: str, workdir: str, w: int, h: int,
                style: Optional[Dict]) -> Optional[str]:
    """Dựng file .ass phụ đề kể chuyện từ SRT khớp giọng đọc.

    Mặc định hợp video kể chuyện: chữ dưới đáy, cho xuống 2 dòng (khác chế độ
    lồng tiếng vốn ép 1 dòng), cỡ chữ theo cạnh ngắn nên khổ dọc 9:16 vẫn cân.
    """
    if not srt_path or not os.path.isfile(srt_path):
        return None
    segs = srt_utils.load_srt_file(srt_path)
    if not segs:
        return None
    st = dict(overlays.DEFAULT_SUB_STYLE)
    st.update({
        "single_line": False,
        "align": "bottom-center",
        "margin_v": max(40, int(h * 0.06)),
        "size": max(28, int(min(w, h) * 0.045)),
        "box": None,
    })
    if isinstance(style, dict):
        st.update({k: v for k, v in style.items() if v is not None})
    ass_path = os.path.join(workdir, "phu_de_ke_chuyen.ass")
    overlays.save_ass(ass_path, segs, w, h, st)
    return ass_path


# --------------------------------------------------------------------------- #
#  NGHE THỬ GIỌNG
#
#  Một truyện 12.000 từ đọc mất vài phút mới ra file, nên trước đây muốn biết
#  giọng nào hợp thì phải tạo cả file rồi nghe, không hợp lại tạo lại. Hàm dưới
#  đọc đúng một câu bằng chính giọng/tốc độ đang chọn để so giọng trong vài giây.
# --------------------------------------------------------------------------- #
# Câu mẫu chọn theo ngách kể chuyện: có đủ dấu thanh, tên gọi Nam Bộ và vật thể
# đời thường, nên nghe là biết ngay giọng có mộc mạc hay bị đọc như đọc báo.
CAU_NGHE_THU = (
    "Đêm đó xóm Cồn Gió có gió thật. Bà Bảy ngồi ở bậc cửa, tay còn cầm chai "
    "dầu gió xanh, nghe tiếng ghe ngoài sông mà hổng nói câu nào."
)
NGHE_THU_MAX_CHARS = 320
_NGHE_THU_LOCK = threading.Lock()


class DangBan(RuntimeError):
    """Máy đang chạy việc dài - không chen bản nghe thử vào giữa."""


def _cat_cau_nghe_thu(text: str, max_chars: int = NGHE_THU_MAX_CHARS) -> str:
    """Lấy một đoạn ngắn, cắt ở dấu kết câu để nghe không bị cụt giữa câu."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    if not raw:
        return CAU_NGHE_THU
    if len(raw) <= max_chars:
        return raw
    cat = raw[:max_chars]
    for dau in (". ", "! ", "? ", "; ", ", "):
        vi_tri = cat.rfind(dau)
        if vi_tri >= max_chars // 3:
            return cat[:vi_tri + 1].strip()
    return cat.rsplit(" ", 1)[0].strip() or cat


def tao_ban_nghe_thu(engine: str = "", voice: str = "", pitch: str = "",
                     rate: str = "", text: str = "") -> Dict:
    """Đọc thử một câu ngắn bằng giọng đang chọn. Trả về {path, text, ...}.

    Kết quả được nhớ theo (engine, giọng, cao độ, tốc độ, câu) nên bấm lại đúng
    cấu hình cũ là phát ngay, không gọi TTS lần nữa.
    """
    import hashlib

    from .. import tts as tts_mod

    with _LOCK:
        dang_chay = bool(STATE.get("running")) or bool(STATE.get("busy"))
        ghi_chu = str(STATE.get("busy") or "đang xử lý")
    if dang_chay:
        raise DangBan(f"Đang bận: {ghi_chu}. Nghe thử được ngay sau khi xong.")

    cfg = _load_cfg()
    tc = cfg.get("tts", {}) or {}
    eng = str(engine or tc.get("engine") or "edge").strip().lower()
    if eng not in {"edge", "vieneu", "capcut"}:
        raise ValueError(f"Engine TTS không hỗ trợ: {eng}")
    mac_dinh = (tc.get("vieneu_voice") if eng == "vieneu"
                else tc.get("capcut_voice") if eng == "capcut"
                else tc.get("narrator_voice"))
    giong = str(voice or mac_dinh or "vi-VN-NamMinhNeural")
    cao_do = str(pitch or tc.get("narrator_pitch") or "+0Hz")
    toc_do = str(rate or tc.get("base_rate") or "+0%")
    narrator = {"voice": giong, "pitch": cao_do}
    if eng == "edge":
        # Danh sách giọng edge trả id kiểu "vi-VN-NamMinhNeural|+8Hz" (giọng +
        # cao độ), phải tách ra trước khi dựng SSML.
        narrator = tts_mod.normalize_edge_narrator(narrator)
    cau = _cat_cau_nghe_thu(text)

    khoa = hashlib.sha1(
        "|".join([eng, narrator["voice"], str(narrator.get("pitch") or ""),
                  toc_do, cau]).encode("utf-8")).hexdigest()[:16]
    out_dir = os.path.join(HERE, "output", "_nghe_thu")
    out_path = os.path.join(out_dir, f"{eng}_{khoa}.mp3")
    ket_qua = {"path": out_path, "text": cau, "engine": eng,
               "voice": narrator["voice"], "rate": toc_do, "cached": True}
    if os.path.isfile(out_path) and os.path.getsize(out_path) > 1024:
        if eng == "capcut":
            tts_mod._record_capcut_voice_status(narrator["voice"], True)
        return ket_qua

    with _NGHE_THU_LOCK:          # hai lần bấm liên tiếp không chồng lên nhau
        if os.path.isfile(out_path) and os.path.getsize(out_path) > 1024:
            if eng == "capcut":
                tts_mod._record_capcut_voice_status(narrator["voice"], True)
            return ket_qua
        _log(f"Nghe thử giọng: engine={eng}, voice={narrator['voice']}, "
             f"rate={toc_do}", "step")
        tts_mod.synthesize_text_audio(
            cau, os.path.join(out_dir, "_tmp", khoa), out_path,
            engine=eng, narrator=narrator, base_rate=toc_do,
            concurrency=2, max_retries=int(tc.get("max_retries", 3) or 3),
            retry_base_delay=float(tc.get("retry_delay", 1.2) or 1.2),
            vieneu_options=tc.get("vieneu_options"),
            capcut_options=tc.get("capcut_options"),
            max_chunk_chars=NGHE_THU_MAX_CHARS)
    ket_qua["cached"] = False
    return ket_qua


def api_manual_use_audio(b: Dict) -> JsonResult:
    path = str(b.get("path") or "").strip().strip('"')
    if not path or not os.path.isfile(path):
        return {"error": f"Không thấy file âm thanh: {path}"}, 400
    if os.path.splitext(path)[1].lower() not in {
            ".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg", ".opus"}:
        return {"error": "Định dạng âm thanh chưa hỗ trợ."}, 400
    duration = ffprobe_duration(path)
    if duration <= 0:
        return {"error": "Không đọc được file âm thanh."}, 400
    with _LOCK:
        manual = STATE["manual"]
        manual.update({"audio_path": os.path.abspath(path),
                       "audio_duration": duration,
                       "output_path": "", "error": "",
                       "working": False,
                       "status": "Đã chọn audio có sẵn",
                       "rev": int(manual.get("rev", 0)) + 1})
    return {"ok": True, "path": os.path.abspath(path),
            "duration": duration}, 200


def api_manual_tts(b: Dict) -> JsonResult:
    text, err = _lay_van_ban_tu_body(b)
    if err:
        return err
    with _LOCK:
        if STATE["running"] or STATE["busy"]:
            return {"error": "Đang bận: " +
                    (STATE["busy"] or "đang xử lý")}, 409
        STATE["cancel"] = False
        STATE["running"] = True
        STATE["busy"] = "Đang tạo audio từ văn bản…"
        manual = STATE["manual"]
        manual.update({"working": True, "status": "Đang tổng hợp giọng…",
                       "error": "", "output_path": "",
                       "voice_cast": [], "voice_assignment_coverage": 0.0,
                       "voice_cast_path": "",
                       "rev": int(manual.get("rev", 0)) + 1})
    _progress(pct=5, step="Tạo audio", detail="Đang chuẩn bị văn bản")

    def _manual_tts_work(payload=dict(b), source_text=text):
        try:
            from .. import tts as tts_mod
            source_text = _ensure_story_ctas(source_text, payload)
            cfg = _load_cfg()
            tc = cfg.get("tts", {}) or {}
            engine = str(payload.get("engine") or tc.get("engine") or "edge").lower()
            default_voice = (tc.get("vieneu_voice") if engine == "vieneu"
                             else tc.get("capcut_voice") if engine == "capcut"
                             else tc.get("narrator_voice"))
            voice = str(payload.get("voice") or default_voice or
                        "vi-VN-NamMinhNeural")
            pitch = str(payload.get("pitch") or tc.get("narrator_pitch") or "+0Hz")
            rate = str(payload.get("rate") or tc.get("base_rate") or "+0%")
            title = _safe_path_stem(
                payload.get("name") or source_text[:50],
                fallback="audio_ke_chuyen", limit=70)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            out_dir = os.path.join(HERE, "output", "manual_audio")
            workdir = os.path.join(out_dir, "_tmp", f"{title}_{stamp}")
            out_path = os.path.join(out_dir, f"{title}_{stamp}.mp3")
            os.makedirs(workdir, exist_ok=True)
            voice_plan = _story_voice_plan(
                source_text, payload, cfg, engine, voice, pitch)
            if voice_plan:
                voice = str((voice_plan.get("narrator") or {}).get("id") or voice)
                if voice_plan.get("cast"):
                    _log("Dàn giọng: người kể + %d nhân vật; nhận diện chắc %.1f%% lượt thoại."
                         % (len(voice_plan["cast"]),
                            float(voice_plan.get("assignment_coverage") or 0)), "ok")
            _log(f"Tạo audio thủ công: engine={engine}, voice={voice}, rate={rate}", "step")
            _progress(pct=15, step="Tạo audio", detail=f"Đang đọc bằng {engine}")
            result = tts_mod.synthesize_text_audio(
                source_text, workdir, out_path,
                engine=engine,
                narrator={"voice": voice, "pitch": pitch},
                base_rate=rate,
                concurrency=int(tc.get("concurrency", 8) or 8),
                max_retries=int(tc.get("max_retries", 3) or 3),
                retry_base_delay=float(tc.get("retry_delay", 1.2) or 1.2),
                vieneu_options=tc.get("vieneu_options"),
                capcut_options=tc.get("capcut_options"),
                max_chunk_chars=240,   # đoạn ngắn -> mốc phụ đề mịn hơn
                utterances=(voice_plan or {}).get("utterances"),
                **_cta_tts_options(payload),
                cancel_event=current_cancel_event(),
            )
            cast_path = _save_voice_cast(os.path.splitext(out_path)[0] + ".giong.json",
                                         voice_plan)
            # Lưu phụ đề khớp giọng đọc để bước dựng video ghi cứng lên hình.
            srt_path = os.path.splitext(out_path)[0] + ".srt"
            try:
                srt_utils.save_srt_file(
                    srt_path, _segments_tu_timeline(result.get("segments")))
            except Exception as e:
                srt_path = ""
                _log(f"Không lưu được phụ đề giọng đọc: {e}", "warn")
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"working": False, "status": "Tạo audio hoàn tất",
                               "audio_path": result["path"],
                               "audio_duration": result["duration"],
                               "srt_path": srt_path,
                               "voice_cast": (voice_plan or {}).get("cast") or [],
                               "voice_assignment_coverage": float(
                                   (voice_plan or {}).get("assignment_coverage") or 0),
                               "voice_cast_path": cast_path,
                               "error": "",
                               "rev": int(manual.get("rev", 0)) + 1})
            _progress(pct=100, step="Tạo audio",
                      detail=os.path.basename(result["path"]))
        except InterruptedError:
            _mark_manual_cancelled("Đã giữ lại các đoạn giọng tạo xong.")
        except Exception as e:
            _log(f"Tạo audio từ văn bản lỗi: {e}", "err")
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"working": False, "status": "Tạo audio lỗi",
                               "error": str(e)[:300],
                               "rev": int(manual.get("rev", 0)) + 1})
            _progress(pct=100, step="Tạo audio lỗi", detail=str(e)[:160])
        finally:
            with _LOCK:
                STATE["running"] = False
                STATE["busy"] = ""

    submit_job(_manual_tts_work, name="Tạo audio từ văn bản", resource="ai",
               metadata={"kind": "manual_tts"})
    return {"ok": True, "async": True}, 200


def api_nhac_nen_tai(b: Dict) -> JsonResult:
    so_bai = max(1, min(20, int(b.get("so_bai") or 3)))
    cats = b.get("danh_muc") if isinstance(b.get("danh_muc"), list) else None
    with _LOCK:
        if STATE["busy"]:
            return {"error": "Đang bận: " + STATE["busy"]}, 409
        STATE["busy"] = "Đang tải nhạc nền…"

    def _tai_nhac_work():
        try:
            from .. import nhac_nen as nn
            _progress(pct=10, step="Tải nhạc nền", detail="Đang tìm bài phù hợp")
            có = nn.dam_bao_co_nhac(so_bai, categories=cats)
            _log(f"Kho nhạc nền hiện có {len(có)} bài.", "ok")
            _progress(pct=100, step="Tải nhạc nền xong",
                      detail=f"{len(có)} bài trong máy")
        except Exception as e:
            _log(f"Tải nhạc nền lỗi: {e}", "err")
            _progress(pct=100, step="Tải nhạc nền lỗi", detail=str(e)[:160])
        finally:
            with _LOCK:
                STATE["busy"] = ""

    submit_job(_tai_nhac_work, name="Tải nhạc nền", resource="network",
               metadata={"kind": "music_download"})
    return {"ok": True, "async": True}, 200


def api_manual_nhac_nen(b: Dict) -> JsonResult:
    with _LOCK:
        current = str((STATE.get("manual") or {}).get("audio_path") or "")
    audio_path = str(b.get("audio_path") or current).strip().strip('"')
    if not audio_path or not os.path.isfile(audio_path):
        return {"error": "Hãy tạo hoặc chọn giọng đọc trước."}, 400
    with _LOCK:
        if STATE["running"] or STATE["busy"]:
            return {"error": "Đang bận: " +
                    (STATE["busy"] or "đang xử lý")}, 409
        STATE["cancel"] = False
        STATE["running"] = True
        STATE["busy"] = "Đang trộn nhạc nền…"
        manual = STATE["manual"]
        manual.update({"working": True, "status": "Đang trộn nhạc nền…",
                       "error": "",
                       "rev": int(manual.get("rev", 0)) + 1})

    def _nhac_work(payload=dict(b), voice=os.path.abspath(audio_path)):
        try:
            from .. import nhac_nen as nn
            cfg = _load_cfg()
            nc = cfg.get("nhac_nen", {}) or {}
            muc_db = float(payload.get("muc_db", nc.get("muc_db", -38)))
            duck = bool(payload.get("duck", nc.get("duck", True)))
            ratio = float(payload.get("duck_ratio", nc.get("duck_ratio", 8)))
            fade = float(payload.get("fade", nc.get("fade", 2.0)))
            bai = str(payload.get("bai") or "")
            if not bai and nc.get("tu_dong_tai", True):
                cats = nc.get("danh_muc")
                nn.dam_bao_co_nhac(
                    int(nc.get("so_bai_tai", 3) or 3),
                    categories=cats if isinstance(cats, list) else None)
            stem = os.path.splitext(os.path.basename(voice))[0]
            out_path = os.path.join(os.path.dirname(voice),
                                    f"{stem}_co_nhac.m4a")
            _progress(pct=20, step="Trộn nhạc nền", detail="Đang cân mức âm")
            result = nn.tron_nhac_nen(voice, out_path, music_path=bai,
                                      muc_db=muc_db, duck=duck,
                                      duck_ratio=ratio, fade=fade)
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"working": False,
                               "status": "Đã trộn nhạc nền",
                               "audio_path": result["path"],
                               "audio_duration": result["duration"],
                               "nhac_nen": os.path.basename(result.get("music") or ""),
                               "error": "",
                               "rev": int(manual.get("rev", 0)) + 1})
            _progress(pct=100, step="Trộn nhạc nền xong",
                      detail=os.path.basename(result["path"]))
        except InterruptedError:
            _mark_manual_cancelled()
        except Exception as e:
            _log(f"Trộn nhạc nền lỗi: {e}", "err")
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"working": False, "status": "Trộn nhạc nền lỗi",
                               "error": str(e)[:300],
                               "rev": int(manual.get("rev", 0)) + 1})
            _progress(pct=100, step="Trộn nhạc nền lỗi", detail=str(e)[:160])
        finally:
            with _LOCK:
                STATE["running"] = False
                STATE["busy"] = ""

    submit_job(_nhac_work, name="Trộn nhạc nền", resource="ffmpeg",
               metadata={"kind": "music_mix"})
    return {"ok": True, "async": True}, 200


def api_manual_slideshow(b: Dict) -> JsonResult:
    with _LOCK:
        current = str((STATE.get("manual") or {}).get("audio_path") or "")
    audio_path = str(b.get("audio_path") or current).strip().strip('"')
    anh = _story_image_inputs(b)
    video_sources = _story_video_inputs(b)
    if not anh and not video_sources:
        return {"error": "Hãy chọn ảnh, thư mục ảnh hoặc video nguồn."}, 400
    if not audio_path or not os.path.isfile(audio_path):
        return {"error": "Hãy tạo giọng đọc trước khi dựng video."}, 400
    with _LOCK:
        if STATE["running"] or STATE["busy"]:
            return {"error": "Đang bận: " +
                    (STATE["busy"] or "đang xử lý")}, 409
        STATE["cancel"] = False
        STATE["running"] = True
        render_kind = "video nguồn" if video_sources else "ảnh"
        STATE["busy"] = f"Đang dựng video từ {render_kind}…"
        manual = STATE["manual"]
        manual.update({"working": True, "status": f"Đang dựng video từ {render_kind}…",
                       "error": "", "output_path": "",
                       "source_videos": video_sources,
                       "rev": int(manual.get("rev", 0)) + 1})

    def _slideshow_work(payload=dict(b), imgs=list(anh),
                        source_videos=list(video_sources),
                        voice=os.path.abspath(audio_path)):
        try:
            from .. import slideshow as ss
            cfg = _load_cfg()
            sc = cfg.get("slideshow", {}) or {}
            w = int(payload.get("w") or sc.get("w", 1920))
            h = int(payload.get("h") or sc.get("h", 1080))
            fps = int(payload.get("fps") or sc.get("fps", 30))
            kieu = str(payload.get("kieu") or sc.get("kieu", "chuyen_dong"))
            title = _safe_path_stem(
                payload.get("name") or os.path.splitext(
                    os.path.basename(voice))[0],
                fallback="video_ke_chuyen", limit=70)
            out_dir = _story_output_dir(title)
            workdir = os.path.join(out_dir, "_tmp", title)
            out_path = os.path.join(out_dir, f"{title}.mp4")
            os.makedirs(workdir, exist_ok=True)
            # Phụ đề cứng (tuỳ chọn): dùng SRT sinh ra lúc tạo giọng đọc.
            ass_path = None
            sub_cfg = payload.get("sub") if isinstance(payload.get("sub"), dict) else {}
            if sub_cfg.get("enabled"):
                with _LOCK:
                    srt_path = str((STATE.get("manual") or {}).get("srt_path") or "")
                ass_path = _ass_tu_srt(srt_path, workdir, w, h, sub_cfg.get("style"))
                if not ass_path:
                    _log("Chưa có phụ đề khớp giọng đọc (hãy tạo audio bằng app "
                         "trước) - dựng video không phụ đề.", "warn")
            if source_videos:
                clip_min = float(payload.get("source_clip_min_seconds", 300) or 300)
                clip_max = float(payload.get(
                    "source_clip_max_seconds",
                    payload.get("source_clip_seconds", 600)) or 600)
                random_pick = bool(payload.get("source_random", True))
                _log(
                    "[Kể chuyện] Dùng lại audio có sẵn; bắt đầu %s %d mục "
                    "video nguồn (mỗi đoạn %.1f–%.1f phút)."
                    % ("random" if random_pick else "xếp tuần tự",
                       len(source_videos), clip_min / 60.0, clip_max / 60.0),
                    "step")

                def _video_progress(pct, detail):
                    _progress(pct=pct, step="Dựng video từ video nguồn",
                              detail=detail)
                    if (float(pct) <= 3.0 or
                            str(detail).startswith("FFmpeg vẫn đang ghép")):
                        _log("[Kể chuyện] " + str(detail), "step")

                result = ss.tao_video_tu_video(
                    source_videos, voice, out_path, workdir=workdir,
                    w=w, h=h, fps=fps,
                    hieu_ung=str(payload.get("source_effect") or "tinh"),
                    ass_path=ass_path,
                    logo=(payload.get("logo") if isinstance(payload.get("logo"), dict)
                          else None),
                    character=(payload.get("character")
                               if isinstance(payload.get("character"), dict)
                               else None),
                    source_cover=str(payload.get("source_cover") or "none"),
                    min_seconds=clip_min, max_seconds=clip_max,
                    random_pick=random_pick,
                    random_seed=int(payload.get("source_random_seed", 0) or 0) or None,
                    transform=(payload.get("source_transform")
                               if isinstance(payload.get("source_transform"), dict)
                               else None),
                    blur_regions=(payload.get("regions")
                                  if isinstance(payload.get("regions"), list)
                                  else None),
                    blur_bottom_ratio=float(payload.get("blur_bottom_ratio", 0) or 0),
                    progress=_video_progress)
            else:
                render_imgs, keep_repeats = _story_slideshow_images(
                    payload, imgs, ffprobe_duration(voice))
                result = ss.tao_video_tu_anh(
                    render_imgs, voice, out_path, workdir=workdir,
                    w=w, h=h, fps=fps, kieu=kieu, ass_path=ass_path,
                    logo=(payload.get("logo") if isinstance(payload.get("logo"), dict)
                          else None),
                    character=(payload.get("character")
                               if isinstance(payload.get("character"), dict)
                               else None),
                    giu_canh_lap=keep_repeats,
                    progress=lambda pct, detail: _progress(
                        pct=pct, step="Dựng video từ ảnh", detail=detail))
            metadata_path, calendar_path = _save_story_deliverables(
                result["path"], payload)
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"working": False,
                               "status": "Dựng video hoàn tất",
                               "output_path": result["path"], "error": "",
                               "metadata_path": metadata_path,
                               "calendar_path": calendar_path,
                               "rev": int(manual.get("rev", 0)) + 1})
            _progress(pct=100, step="Dựng video xong",
                      detail=os.path.basename(result["path"]))
            if source_videos:
                _log("Đã random %d đoạn từ %d video nguồn: %s"
                     % (result["so_doan"], result["so_video"], result["path"]), "ok")
            else:
                _log(f"Đã dựng video từ {result['so_anh']} ảnh: {result['path']}", "ok")
        except InterruptedError:
            _mark_manual_cancelled(
                "Đã dừng dựng video; audio và nguồn hình vẫn được giữ lại.")
        except Exception as e:
            _log(f"Dựng video từ nguồn hình lỗi: {e}", "err")
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"working": False, "status": "Dựng video lỗi",
                               "error": str(e)[:300],
                               "rev": int(manual.get("rev", 0)) + 1})
            _progress(pct=100, step="Dựng video lỗi", detail=str(e)[:160])
        finally:
            with _LOCK:
                STATE["running"] = False
                STATE["busy"] = ""

    submit_job(_slideshow_work, name="Dựng video kể chuyện", resource="ffmpeg",
               metadata={"kind": "story_render"})
    return {"ok": True, "async": True}, 200


def api_story_voice_recommendations(b: Dict) -> JsonResult:
    """Phân tích nội dung, nhân vật và đề xuất cả dàn giọng từ catalog đang có."""
    text, err = _lay_van_ban_tu_body(b)
    if err:
        return err
    engine = str(b.get("engine") or "capcut").strip().lower()
    if engine not in {"edge", "vieneu", "capcut"}:
        return {"error": "Bộ giọng không hỗ trợ: %s" % engine}, 400
    try:
        from .. import story_voice, tts as tts_mod
        voices = tts_mod.list_voices(engine)
        if not voices:
            return {"error": "Không lấy được danh sách giọng %s." % engine}, 503
        result = story_voice.plan_story_voices(
            text, voices, engine=engine,
            narrator_voice=str(b.get("voice") or ""),
            design_text=_story_design_text(b), auto_narrator=True,
            max_characters=max(1, min(12, int(b.get("max_character_voices", 8) or 8))))
        result.pop("utterances", None)  # không trả hàng nghìn lượt thoại qua HTTP
        for character in result.get("characters") or []:
            character.pop("aliases", None)
        result["engine"] = engine
        result["catalog_count"] = len(voices)
        return result, 200
    except Exception as exc:
        return {"error": "Không phân tích được giọng phù hợp: %s" % exc}, 500


def api_story_search_sources(b: Dict) -> JsonResult:
    """Tìm video Bilibili/YouTube theo từ khóa cho tab Kể chuyện AI."""
    keyword = str(b.get("keyword") or "").strip()
    try:
        limit = max(1, min(50, int(b.get("limit", 10) or 10)))
    except (TypeError, ValueError):
        limit = 10
    if not keyword:
        return {"error": "Hãy nhập từ khóa tìm video Bilibili."}, 400
    try:
        from .. import story_sources
        cfg = _load_cfg().get("download", {}) or {}
        provider = str(b.get("provider") or "bilibili").strip().lower()
        rows = story_sources.search(
            keyword, limit=limit, provider=provider,
            cookies_from_browser=cfg.get("cookies_from_browser"),
            cookies_file=cfg.get("cookies_file"))
        with _LOCK:
            manual = STATE["manual"]
            manual.update({"source_keyword": keyword, "source_results": rows,
                           "source_status": "Tìm thấy %d video" % len(rows),
                           "rev": int(manual.get("rev", 0)) + 1})
        return {"ok": True, "keyword": keyword, "provider": provider,
                "results": rows}, 200
    except Exception as exc:
        return {"error": "Tìm nguồn video thất bại: %s" % exc}, 500


def api_story_reference_catalog(b: Dict = None) -> JsonResult:
    """Danh mục nguồn tham khảo và bộ từ khóa tiếng Trung cho truyện mới."""
    try:
        from .. import story_sources
        return {"ok": True, "sources": story_sources.reference_catalog(),
                "chinese_keywords": story_sources.chinese_keyword_catalog()}, 200
    except Exception as exc:
        return {"error": "Không đọc được danh mục nguồn tham khảo: %s" % exc}, 500


def api_story_search_references(b: Dict) -> JsonResult:
    """Tìm tiêu đề/snippet tham khảo từ các website đã chọn, không sao chép bài."""
    keyword = str(b.get("keyword") or "").strip()
    if not keyword:
        return {"error": "Hãy nhập từ khóa tham khảo tiếng Trung hoặc tiếng Việt."}, 400
    try:
        limit = max(1, min(50, int(b.get("limit", 20) or 20)))
    except (TypeError, ValueError):
        limit = 20
    source_keys = b.get("source_keys") or []
    if isinstance(source_keys, str):
        source_keys = re.split(r"[,\s]+", source_keys)
    try:
        from .. import story_sources
        rows = story_sources.search_web_references(keyword, source_keys, limit)
        with _LOCK:
            manual = STATE["manual"]
            manual.update({"reference_keyword": keyword, "reference_results": rows,
                           "reference_status": "Tìm thấy %d bài tham khảo" % len(rows),
                           "rev": int(manual.get("rev", 0)) + 1})
        return {"ok": True, "keyword": keyword, "results": rows}, 200
    except Exception as exc:
        return {"error": "Tìm bài tham khảo thất bại: %s" % exc}, 500


def api_story_cut_sources(b: Dict) -> JsonResult:
    """Cắt video nền thành các clip ngắn để random-pick theo audio."""
    raw = b.get("video_sources", b.get("paths", []))
    if isinstance(raw, str):
        raw = re.split(r"[\r\n]+", raw)
    paths = [str(x.get("path") if isinstance(x, dict) else x).strip()
             for x in (raw if isinstance(raw, list) else []) if str(x).strip()]
    if not paths:
        return {"error": "Hãy chọn video nền trước khi cắt."}, 400
    try:
        min_seconds = max(2.0, float(b.get("min_seconds", 300) or 300))
        max_seconds = max(min_seconds, float(b.get("max_seconds", 600) or 600))
    except (TypeError, ValueError):
        min_seconds, max_seconds = 300.0, 600.0
    with _LOCK:
        if STATE["running"] or STATE["busy"]:
            return {"error": "Đang bận: " + (STATE["busy"] or "đang xử lý")}, 409
        STATE["cancel"] = False
        STATE["running"] = True
        STATE["busy"] = "Đang cắt video nền thành đoạn nhỏ"
        manual = STATE["manual"]
        manual.update({"cut_status": "Đang chuẩn bị cắt video…", "cut_done": 0,
                       "cut_total": len(paths), "error": "",
                       "rev": int(manual.get("rev", 0)) + 1})

    def _work(source_paths=list(paths), lo=min_seconds, hi=max_seconds,
              payload=dict(b)):
        try:
            from .. import story_sources
            title = _safe_path_stem(str(payload.get("name") or "story_clips"),
                                    fallback="story_clips", limit=60)
            out_dir = os.path.join(HERE, "downloads", "story_clips", title)

            def progress(done, total, message):
                with _LOCK:
                    manual = STATE["manual"]
                    manual.update({"cut_status": "%d/%d: %s" % (done, total, message),
                                   "cut_done": done, "cut_total": total,
                                   "rev": int(manual.get("rev", 0)) + 1})
                _progress(pct=done * 100.0 / max(1, total),
                          step="Cắt video nền", detail=message)

            clips = story_sources.cut_video_segments(
                source_paths, out_dir, min_seconds=lo, max_seconds=hi,
                progress=progress)
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"source_videos": clips, "source_clips": clips,
                               "cut_status": "Đã cắt %d clip" % len(clips),
                               "cut_done": len(source_paths), "cut_total": len(source_paths),
                               "error": "", "rev": int(manual.get("rev", 0)) + 1})
            _progress(pct=100, step="Cắt video xong", detail="%d clip sẵn sàng" % len(clips))
        except InterruptedError:
            _mark_manual_cancelled("Đã dừng cắt video; các clip đã tạo vẫn được giữ lại.")
        except Exception as exc:
            _log("Cắt video nền lỗi: %s" % exc, "err")
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"cut_status": "Cắt lỗi", "error": str(exc)[:300],
                               "rev": int(manual.get("rev", 0)) + 1})
            _progress(pct=100, step="Cắt video lỗi", detail=str(exc)[:160])
        finally:
            with _LOCK:
                STATE["running"] = False
                STATE["busy"] = ""

    submit_job(_work, name="Cắt video nguồn", resource="ffmpeg",
               metadata={"kind": "story_cut_sources"})
    return {"ok": True, "async": True, "total": len(paths)}, 200


def api_story_download_sources(b: Dict) -> JsonResult:
    """Tải hàng loạt link đã chọn, cập nhật tiến độ qua /api/state."""
    raw = b.get("links", b.get("urls", []))
    if isinstance(raw, str):
        raw = re.split(r"[\r\n]+", raw)
    links = [str(x.get("url") if isinstance(x, dict) else x).strip()
             for x in (raw if isinstance(raw, list) else [])]
    links = [x for x in links if x]
    if not links:
        return {"error": "Hãy chọn video tìm được hoặc dán danh sách link."}, 400
    with _LOCK:
        if STATE["running"] or STATE["busy"]:
            return {"error": "Đang bận: " + (STATE["busy"] or "đang xử lý")}, 409
        STATE["cancel"] = False
        STATE["running"] = True
        STATE["busy"] = "Đang tải video nền…"
        manual = STATE["manual"]
        manual.update({"working": True, "source_links": links,
                       "source_videos": [], "source_done": 0,
                       "source_total": len(links),
                       "source_status": "Đang chuẩn bị tải %d video…" % len(links),
                       "error": "", "rev": int(manual.get("rev", 0)) + 1})

    def _work(payload=dict(b), urls=list(links)):
        try:
            from .. import story_sources
            cfg = _load_cfg().get("download", {}) or {}
            title = _safe_path_stem(str(payload.get("name") or "story_sources"),
                                    fallback="story_sources", limit=60)
            out_dir = os.path.join(HERE, "downloads", "story_sources", title)

            def progress(done, total, message):
                with _LOCK:
                    manual = STATE["manual"]
                    manual.update({"source_done": done, "source_total": total,
                                   "source_status": "%d/%d: %s" %
                                   (done, total, message),
                                   "rev": int(manual.get("rev", 0)) + 1})
                _progress(pct=done * 100.0 / max(1, total),
                          step="Tải video nền", detail=message)

            def live_progress(pct, detail):
                with _LOCK:
                    manual = STATE["manual"]
                    manual.update({"source_status": str(detail)[:260],
                                   "rev": int(manual.get("rev", 0)) + 1})
                _progress(pct=float(pct), step="Tải video nền",
                          detail=str(detail)[:260])

            result = story_sources.download_many(
                urls, out_dir, quality=str(cfg.get("quality") or "best"),
                cookies_from_browser=cfg.get("cookies_from_browser"),
                cookies_file=cfg.get("cookies_file"),
                concurrent_fragments=int(cfg.get("concurrent_fragments", 8) or 8),
                external_downloader=cfg.get("external_downloader", "auto"),
                progress=progress, live_progress=live_progress)
            paths = [x["path"] for x in result]
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"working": False, "source_videos": paths,
                               "source_done": len(paths), "source_total": len(urls),
                               "source_status": "Đã tải %d/%d video" %
                               (len(paths), len(urls)), "error": "",
                               "rev": int(manual.get("rev", 0)) + 1})
            _progress(pct=100, step="Tải video nền xong",
                      detail="%d file sẵn sàng" % len(paths))
        except InterruptedError:
            _mark_manual_cancelled("Đã dừng tải video; các file đã tải vẫn được giữ lại.")
        except Exception as exc:
            _log("Tải video nền lỗi: %s" % exc, "err")
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"working": False, "source_status": "Tải lỗi",
                               "error": str(exc)[:300],
                               "rev": int(manual.get("rev", 0)) + 1})
            _progress(pct=100, step="Tải video nền lỗi", detail=str(exc)[:160])
        finally:
            with _LOCK:
                STATE["running"] = False
                STATE["busy"] = ""

    submit_job(_work, name="Tải video nguồn", resource="network",
               metadata={"kind": "story_download_sources"})
    return {"ok": True, "async": True, "total": len(links)}, 200


def api_story_generated_script() -> JsonResult:
    """Trả bản đọc vừa tạo cho giao diện; không nhét văn bản dài vào /api/state."""
    with _LOCK:
        manual = STATE.get("manual") or {}
        path = str(manual.get("script_path") or "")
        title = str(manual.get("script_title") or "")
        words = int(manual.get("script_words") or 0)
    if not path or not os.path.isfile(path):
        return {"error": "Chưa có kịch bản vừa tạo."}, 404
    try:
        text = _doc_file_van_ban(path)
    except Exception as exc:
        return {"error": "Không đọc được kịch bản vừa tạo: %s" % exc}, 500
    return {"text": text, "path": path, "title": title, "words": words}, 200


def api_story_resume_images(b: Dict) -> JsonResult:
    """Tiếp tục đúng các cảnh thiếu trong manifest rồi bàn giao thẳng sang video."""
    pack_path = str(b.get("image_pack") or "").strip().strip('"')
    if not pack_path:
        return {"error": "Chưa có gói ảnh để tiếp tục."}, 400
    try:
        from .. import story_images
        manifest = story_images.load_pack(pack_path)
    except Exception as exc:
        return {"error": "Không đọc được gói ảnh: %s" % exc}, 400
    scenes = list(manifest.get("scenes") or [])
    if not scenes:
        return {"error": "Gói ảnh không có danh sách cảnh."}, 400
    requested_title = str(b.get("story_title") or "").strip()
    pack_title = str(manifest.get("title") or "").strip()
    if (requested_title and pack_title and
            _story_title_key(requested_title) != _story_title_key(pack_title)):
        return {"error": ("Không thể tiếp tục ảnh của truyện '%s' cho tiêu đề mới '%s'."
                          % (pack_title, requested_title))}, 409
    script_path = str(b.get("txt_path") or manifest.get("script_path") or "")
    if not script_path or not os.path.isfile(script_path):
        return {"error": "Không thấy KICH_BAN_DOC.txt của gói ảnh."}, 400

    with _LOCK:
        if STATE["running"] or STATE["busy"]:
            return {"error": "Đang bận: " +
                    (STATE["busy"] or "đang xử lý")}, 409
        STATE["cancel"] = False
        STATE["running"] = True
        STATE["busy"] = "Đang tiếp tục tạo các ảnh còn thiếu…"
        manual = STATE["manual"]
        manual.update({
            "working": True,
            "status": "Tiếp tục tạo ảnh từ cảnh còn thiếu…",
            "error": "", "output_path": "",
            "script_path": os.path.abspath(script_path),
            "script_title": str(manifest.get("title") or b.get("name") or ""),
            "image_pack_path": str(manifest.get("manifest_path") or pack_path),
            "image_scene_count": len(scenes),
            "image_ready_count": int(manifest.get("ready_count", 0) or 0),
            "image_prompt_ready": False,
            "rev": int(manual.get("rev", 0)) + 1,
        })
    _progress(pct=36, step="Tiếp tục tạo ảnh",
              detail="Giữ ảnh đã có, bắt đầu từ cảnh còn thiếu")

    def _work(payload=dict(b), current=manifest,
              source_script=os.path.abspath(script_path)):
        handed_off = False
        try:
            cfg = _load_cfg()
            payload["auto_images"] = True
            payload["image_pack"] = str(current.get("manifest_path") or pack_path)
            payload["anh"] = []
            result = {
                "title": str(current.get("title") or payload.get("name") or "Truyện"),
                "script_path": source_script,
                "design_path": str(current.get("design_path") or ""),
            }
            images, resumed_pack = _prepare_generated_story_images(
                result, payload, cfg)
            _raise_if_cancelled()
            if not images:
                with _LOCK:
                    manual = STATE["manual"]
                    ready = int(manual.get("image_ready_count", 0) or 0)
                    total = int(manual.get("image_scene_count", 0) or 0)
                    manual.update({
                        "working": False,
                        "status": "Tạo ảnh tạm dừng ở %d/%d; bấm Tiếp tục để thử lại"
                                  % (ready, total),
                        "error": "",
                        "rev": int(manual.get("rev", 0)) + 1,
                    })
                _progress(pct=100, step="Tạo ảnh tạm dừng",
                          detail="Ảnh đã tạo được giữ nguyên")
                return

            with _LOCK:
                STATE["running"] = False
                STATE["busy"] = ""
            video_payload = dict(payload)
            video_payload.pop("text", None)
            video_payload["txt_path"] = source_script
            video_payload["name"] = str(payload.get("name") or result["title"])
            video_payload["anh"] = images
            video_payload["image_pack"] = resumed_pack
            video_payload["character_context_path"] = result.get("design_path") or ""
            video_payload["_progress_start"] = 48
            video_payload["_handoff"] = True
            response, code = api_manual_run_all(video_payload)
            if code != 200:
                raise RuntimeError(response.get("error") or
                                   "Không khởi động được bước dựng video.")
            handed_off = True
            _log("Đã tạo đủ ảnh còn thiếu; tiếp tục giọng đọc và dựng video.", "ok")
        except InterruptedError:
            _mark_manual_cancelled("Ảnh đã tạo xong vẫn được giữ để tiếp tục sau.")
        except Exception as exc:
            _log("Tiếp tục tạo ảnh lỗi: %s" % exc, "err")
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"working": False, "status": "Tiếp tục tạo ảnh lỗi",
                               "error": str(exc)[:300],
                               "rev": int(manual.get("rev", 0)) + 1})
            _progress(pct=100, step="Tiếp tục tạo ảnh lỗi",
                      detail=str(exc)[:160])
        finally:
            if not handed_off:
                with _LOCK:
                    STATE["running"] = False
                    STATE["busy"] = ""

    submit_job(_work, name="Tiếp tục tạo ảnh", resource="ai",
               metadata={"kind": "story_resume_images"})
    return {"ok": True, "async": True,
            "ready_count": int(manifest.get("ready_count", 0) or 0),
            "scene_count": len(scenes)}, 200


def api_story_generate_and_run(b: Dict) -> JsonResult:
    """Một nút: tiêu đề -> KICH_BAN_DOC.txt -> TTS/nhạc/sub/video."""
    title = str(b.get("story_title") or b.get("title") or "").strip()
    if not title:
        return {"error": "Hãy nhập tiêu đề truyện cần viết."}, 400
    payload, anh, stale_pack_title = _generated_story_image_inputs(b, title)
    if stale_pack_title:
        _log("Tiêu đề mới '%s': không dùng lại ảnh của '%s'; sẽ tạo gói ảnh mới."
             % (title, stale_pack_title), "info")
    with _LOCK:
        if STATE["running"] or STATE["busy"]:
            return {"error": "Đang bận: " + (STATE["busy"] or "đang xử lý")}, 409
        STATE["cancel"] = False
        STATE["running"] = True
        STATE["busy"] = "Đang tạo kịch bản từ tiêu đề…"
        manual = STATE["manual"]
        manual.update({
            "working": True, "status": "Đang lập và viết kịch bản…",
            "error": "", "output_path": "", "script_path": "",
            "script_title": title, "script_words": 0,
            "recommended_voice": "", "voice_analysis": {},
            "voice_recommendations": [], "voice_cast": [],
            "voice_assignment_coverage": 0.0, "voice_cast_path": "",
            "image_pack_path": "", "image_prompt_path": "",
            "image_provider_url": "", "image_scene_count": 0,
            "image_ready_count": 0, "image_prompt_ready": False,
            "image_generation_status": "",
            "content_idea_id": str(payload.get("content_idea_id") or
                                   manual.get("content_idea_id") or ""),
            "rewrite_brief": str(payload.get("rewrite_brief") or
                                 manual.get("rewrite_brief") or ""),
            "rev": int(manual.get("rev", 0)) + 1,
        })
    _progress(pct=2, step="Tạo kịch bản", detail="Mở công cụ viết truyện")

    def _work(payload=dict(payload), imgs=list(anh), source_title=title):
        handed_off = False
        try:
            from .. import story_writer
            cfg = _load_cfg()

            def _writer_progress(done, total, message=""):
                ratio = float(done) / max(1, int(total))
                _progress(pct=3 + ratio * 32, step="Tạo kịch bản",
                          detail=message[:160] or ("Đã xong %d/%d bước" % (done, total)))

            result = story_writer.generate(
                source_title, cfg, log=_log, progress=_writer_progress,
                cancel_event=current_cancel_event(),
                rewrite_brief=str(payload.get("rewrite_brief") or ""),
                cta=(payload.get("cta") if isinstance(payload.get("cta"), dict)
                     else None))
            _raise_if_cancelled()
            with _LOCK:
                manual = STATE["manual"]
                manual.update({
                    "script_path": result["script_path"],
                    "script_title": result["title"],
                    "script_words": result["words"],
                    "status": "Đã tạo kịch bản; đang kiểm tra chất lượng…",
                    "rev": int(manual.get("rev", 0)) + 1,
                })
            writer_cfg = cfg.get("tao_kich_ban") \
                if isinstance(cfg.get("tao_kich_ban"), dict) else {}
            measured = result.get("meta", {}).get("kiem_tra_tu_dong")
            quality_issues = []
            if isinstance(measured, dict):
                # Mốc 12.000 là mục tiêu biên tập, không nên làm mất cả lượt
                # chạy dài khi file TTS cuối chỉ thiếu vài phần trăm. Dùng số
                # từ của KICH_BAN_DOC (đã gồm CTA) để quyết định bàn giao;
                # vẫn chặn bản thiếu đáng kể hoặc vượt trần 16.000 từ.
                target_min = max(1, int(writer_cfg.get(
                    "quality_target_min_words", 12000) or 12000))
                target_max = max(target_min, int(writer_cfg.get(
                    "quality_target_max_words", 16000) or 16000))
                min_ratio = max(.80, min(1.0, float(writer_cfg.get(
                    "quality_min_word_ratio", .95) or .95)))
                final_words = int(result.get("words") or
                                  measured.get("total_words") or 0)
                minimum_handoff = int(round(target_min * min_ratio))
                word_handoff_ok = minimum_handoff <= final_words <= target_max
                if not word_handoff_ok:
                    quality_issues.append("tổng số từ ngoài mục tiêu")
                elif not measured.get("within_target", False):
                    _log(("Kịch bản đạt %.1f%% mục tiêu từ (%s/%s); sai số nhỏ "
                          "nên vẫn tiếp tục tạo video.") %
                         (100.0 * final_words / target_min,
                          format(final_words, ","), format(target_min, ",")), "warn")
                if not measured.get("dialogue_target", False):
                    quality_issues.append("tỉ lệ đoạn đối thoại dưới 45%")
                if measured.get("banned_terms"):
                    quality_issues.append("còn từ cấm")
                if measured.get("repeated_paragraphs"):
                    _log("Kịch bản có %s đoạn lặp nguyên văn; nên xem 98_kiem_tra_tu_dong.txt."
                         % measured["repeated_paragraphs"], "warn")
            if quality_issues and writer_cfg.get("require_quality_pass", True):
                raise RuntimeError(
                    "Kịch bản đã được lưu nhưng chưa dựng video để tránh tốn TTS: %s. "
                    "Xem 98_kiem_tra_tu_dong.txt trong %s."
                    % (", ".join(quality_issues), result["folder"]))
            source_videos = _story_video_inputs(payload)
            auto_img = bool(payload.get("auto_images", True))
            if not imgs and not source_videos:
                if auto_img:
                    imgs, image_pack = _prepare_generated_story_images(
                        result, payload, cfg)
                    _raise_if_cancelled()
                    payload["image_pack"] = image_pack
                    if not imgs:
                        ready = int((STATE.get("manual") or {}).get(
                            "image_ready_count", 0) or 0)
                        total = int((STATE.get("manual") or {}).get(
                            "image_scene_count", 0) or 0)
                        with _LOCK:
                            manual = STATE["manual"]
                            manual.update({
                                "working": False,
                                "status": ("Tạo ảnh tạm dừng ở %d/%d; chờ bổ sung ảnh "
                                           "hoặc bấm Tiếp tục"
                                           % (ready, total)),
                                "error": "",
                                "rev": int(manual.get("rev", 0)) + 1,
                            })
                        _progress(pct=100, step="Đang chờ ảnh",
                                  detail="Ảnh đã lưu; bấm Tiếp tục để tạo các cảnh còn thiếu")
                        _log("Tạo ảnh đã tạm dừng ở %d/%d. Ảnh đã có vẫn được giữ; "
                             "bấm Tiếp tục để chạy từ cảnh còn thiếu." % (ready, total), "warn")
                        return
                else:
                    _log("Đã tắt tạo ảnh AI: bỏ qua bước tạo ảnh, chuyển sang tạo giọng đọc.", "info")
            auto_voice_id = ""
            voice_result = None
            if bool(payload.get("voice_auto", False)) or bool(payload.get("multi_voice", False)):
                script_text = _doc_file_van_ban(result["script_path"])
                engine = str(payload.get("engine") or
                             (cfg.get("tts") or {}).get("engine") or "capcut").lower()
                plan_payload = dict(payload)
                plan_payload["character_context_path"] = result.get("design_path") or ""
                voice_result = _story_voice_plan(
                    script_text, plan_payload, cfg, engine,
                    str(payload.get("voice") or ""), str(payload.get("pitch") or "+0Hz"))
                if voice_result:
                    auto_voice_id = str((voice_result.get("narrator") or {}).get("id") or "")
                    narrator = voice_result.get("narrator") or {}
                    _log("Giọng kể tự chọn: %s. Đã gán %d nhân vật, phủ %.1f%% lượt thoại."
                         % (narrator.get("name") or auto_voice_id,
                            len(voice_result.get("cast") or []),
                            float(voice_result.get("assignment_coverage") or 0)), "ok")
            with _LOCK:
                manual = STATE["manual"]
                manual.update({
                    "status": "Đã tạo kịch bản; đang tự nạp vào dựng video…",
                    "recommended_voice": auto_voice_id,
                    "voice_analysis": (voice_result or {}).get("analysis") or {},
                    "voice_recommendations": (voice_result or {}).get("recommendations") or [],
                    "voice_cast": (voice_result or {}).get("cast") or [],
                    "voice_assignment_coverage": float(
                        (voice_result or {}).get("assignment_coverage") or 0),
                    "rev": int(manual.get("rev", 0)) + 1,
                })
                # Bàn giao khoá cho api_manual_run_all ngay trong thread này.
                STATE["running"] = False
                STATE["busy"] = ""
            _progress(pct=48, step="Kịch bản và hình ảnh hoàn tất",
                      detail="%s từ · đang tạo giọng đọc" % result["words"])
            video_payload = dict(payload)
            video_payload.pop("text", None)
            video_payload["txt_path"] = result["script_path"]
            video_payload["name"] = str(payload.get("name") or result["title"])
            video_payload["anh"] = imgs
            video_payload["video_sources"] = source_videos
            video_payload["character_context_path"] = result.get("design_path") or ""
            if auto_voice_id:
                video_payload["voice"] = auto_voice_id
            video_payload["_progress_start"] = 48
            video_payload["_handoff"] = True
            _raise_if_cancelled()
            response, code = api_manual_run_all(video_payload)
            if code != 200:
                raise RuntimeError(response.get("error") or "Không khởi động được bước dựng video.")
            handed_off = True
            _log("Đã tự nạp KICH_BAN_DOC.txt vào pipeline video kể chuyện.", "ok")
        except InterruptedError:
            _mark_manual_cancelled(
                "Đã giữ lại kịch bản, prompt, ảnh và các phần đã hoàn thành.")
        except Exception as exc:
            _log("Tạo kịch bản và video lỗi: %s" % exc, "err")
            with _LOCK:
                manual = STATE["manual"]
                manual.update({
                    "working": False, "status": "Quy trình tự động bị lỗi",
                    "error": str(exc)[:300],
                    "rev": int(manual.get("rev", 0)) + 1,
                })
            _progress(pct=100, step="Tạo kịch bản lỗi", detail=str(exc)[:160])
        finally:
            if not handed_off:
                with _LOCK:
                    STATE["running"] = False
                    STATE["busy"] = ""

    submit_job(_work, name="Tạo kịch bản kể chuyện", resource="ai",
               metadata={"kind": "story_generate"})
    return {"ok": True, "async": True}, 200


def api_manual_run_all(b: Dict) -> JsonResult:
    """CHẠY TẤT CẢ cho chế độ Kể chuyện: text -> giọng -> nhạc -> phụ đề -> video.

    Body: {text|txt_path, name, engine, voice, pitch, rate,
           anh: [file/thư mục...], w, h, fps, kieu,
           nhac: {enabled, bai, muc_db, duck, duck_ratio, fade},
           sub: {enabled, style: {size, color, align, ...}}}
    """
    text, err = _lay_van_ban_tu_body(b)
    if err:
        return err
    anh = _story_image_inputs(b)
    video_sources = _story_video_inputs(b)
    audio_only = not anh and not video_sources
    try:
        progress_start = max(0.0, min(95.0, float(b.get("_progress_start", 0) or 0)))
    except (TypeError, ValueError):
        progress_start = 0.0

    def _story_progress(pct, step, detail):
        mapped = progress_start + (100.0 - progress_start) * float(pct) / 100.0
        _progress(pct=mapped, step=step, detail=detail)

    with _LOCK:
        if STATE["running"] or STATE["busy"]:
            return {"error": "Đang bận: " +
                    (STATE["busy"] or "đang xử lý")}, 409
        if b.get("_handoff") and current_cancel_event().is_set():
            return {"error": "Đã dừng tác vụ theo yêu cầu."}, 409
        STATE["cancel"] = False
        STATE["running"] = True
        STATE["busy"] = "Đang làm video kể chuyện…"
        manual = STATE["manual"]
        manual.update({"working": True,
                       "status": "Bước 1/4: tổng hợp giọng đọc…",
                       "error": "", "output_path": "",
                       "image_pack_path": str(b.get("image_pack") or ""),
                       "image_ready_count": len(anh),
                       "image_prompt_ready": False,
                       "image_generation_status": (
                           "Đã nhận video nguồn; bắt đầu dựng video"
                           if video_sources else "Đã nhận ảnh; bắt đầu dựng video" if anh else "Đang tạo giọng đọc"),
                       "source_videos": video_sources,
                       "voice_cast": [], "voice_assignment_coverage": 0.0,
                       "voice_cast_path": "",
                       "rev": int(manual.get("rev", 0)) + 1})
    _story_progress(pct=3, step="Video kể chuyện", detail="Chuẩn bị văn bản")

    def _work(payload=dict(b), source_text=text, imgs=list(anh),
              source_videos=list(video_sources)):
        try:
            from .. import tts as tts_mod, nhac_nen as nn, slideshow as ss
            source_text = _ensure_story_ctas(source_text, payload)
            cfg = _load_cfg()
            tc = cfg.get("tts", {}) or {}
            nc = cfg.get("nhac_nen", {}) or {}
            sc = cfg.get("slideshow", {}) or {}

            engine = str(payload.get("engine") or tc.get("engine") or "edge").lower()
            default_voice = (tc.get("vieneu_voice") if engine == "vieneu"
                             else tc.get("capcut_voice") if engine == "capcut"
                             else tc.get("narrator_voice"))
            voice = str(payload.get("voice") or default_voice or
                        "vi-VN-NamMinhNeural")
            pitch = str(payload.get("pitch") or tc.get("narrator_pitch") or "+0Hz")
            rate = str(payload.get("rate") or tc.get("base_rate") or "+0%")
            w = int(payload.get("w") or sc.get("w", 1920))
            h = int(payload.get("h") or sc.get("h", 1080))
            fps = int(payload.get("fps") or sc.get("fps", 30))
            kieu = str(payload.get("kieu") or sc.get("kieu", "chuyen_dong"))
            nhac = payload.get("nhac") if isinstance(payload.get("nhac"), dict) else {}
            sub_cfg = payload.get("sub") if isinstance(payload.get("sub"), dict) else {}

            title = _safe_path_stem(payload.get("name") or source_text[:50],
                                    fallback="video_ke_chuyen", limit=70)
            stamp = time.strftime("%Y%m%d_%H%M%S")
            out_dir = _story_output_dir(title)
            workdir, reusable_clips = _story_tts_workdir(
                out_dir, title, stamp, engine)
            os.makedirs(workdir, exist_ok=True)
            if reusable_clips:
                _log("[Kể chuyện] Tiếp tục từ %d đoạn CapCut đã tạo ở lượt trước."
                     % reusable_clips, "ok")

            voice_plan = _story_voice_plan(
                source_text, payload, cfg, engine, voice, pitch)
            if voice_plan:
                voice = str((voice_plan.get("narrator") or {}).get("id") or voice)
                cast_count = len(voice_plan.get("cast") or [])
                if cast_count:
                    _log("[Kể chuyện] Dàn giọng: 1 người kể + %d nhân vật; "
                         "nhận diện chắc %.1f%% lượt thoại."
                         % (cast_count, float(voice_plan.get("assignment_coverage") or 0)), "ok")

            # ---- 1/4: giọng đọc ----
            _log(f"[Kể chuyện] 1/4 giọng đọc: engine={engine}, voice={voice}, "
                 f"khung {w}x{h}", "step")
            _story_progress(pct=8, step="Video kể chuyện", detail=f"1/4 Đọc bằng {engine}")
            tts_res = tts_mod.synthesize_text_audio(
                source_text, workdir, os.path.join(workdir, "giong_doc.mp3"),
                engine=engine, narrator={"voice": voice, "pitch": pitch},
                base_rate=rate,
                concurrency=int(tc.get("concurrency", 8) or 8),
                max_retries=int(tc.get("max_retries", 3) or 3),
                retry_base_delay=float(tc.get("retry_delay", 1.2) or 1.2),
                vieneu_options=tc.get("vieneu_options"),
                capcut_options=tc.get("capcut_options"),
                max_chunk_chars=240,   # đoạn ngắn -> mốc phụ đề mịn
                utterances=(voice_plan or {}).get("utterances"),
                **_cta_tts_options(payload),
                cancel_event=current_cancel_event())
            _raise_if_cancelled()
            srt_path = os.path.join(out_dir, f"{title}_{stamp}.srt")
            srt_utils.save_srt_file(
                srt_path, _segments_tu_timeline(tts_res.get("segments")))
            audio = tts_res["path"]
            cast_path = _save_voice_cast(
                os.path.join(out_dir, f"{title}_{stamp}.giong.json"), voice_plan)
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"audio_path": audio,
                               "audio_duration": tts_res["duration"],
                               "srt_path": srt_path,
                               "voice_cast": (voice_plan or {}).get("cast") or [],
                               "voice_assignment_coverage": float(
                                   (voice_plan or {}).get("assignment_coverage") or 0),
                               "voice_cast_path": cast_path,
                               "status": "Bước 2/4: nhạc nền…",
                               "rev": int(manual.get("rev", 0)) + 1})

            # ---- 2/4: nhạc nền (tuỳ chọn) ----
            if nhac.get("enabled", True):
                _raise_if_cancelled()
                _story_progress(pct=35, step="Video kể chuyện", detail="2/4 Trộn nhạc nền")
                if not str(nhac.get("bai") or "") and nc.get("tu_dong_tai", True):
                    cats = nc.get("danh_muc")
                    nn.dam_bao_co_nhac(
                        int(nc.get("so_bai_tai", 3) or 3),
                        categories=cats if isinstance(cats, list) else None)
                mix = nn.tron_nhac_nen(
                    audio, os.path.join(workdir, "giong_co_nhac.m4a"),
                    music_path=str(nhac.get("bai") or ""),
                    muc_db=float(nhac.get("muc_db", nc.get("muc_db", -38))),
                    duck=bool(nhac.get("duck", nc.get("duck", True))),
                    duck_ratio=float(nhac.get("duck_ratio", nc.get("duck_ratio", 8))),
                    fade=float(nhac.get("fade", nc.get("fade", 2.0))))
                audio = mix["path"]
                # Luôn công bố track cuối (đã có nhạc) cho nút dựng lại video.
                # Nếu giữ đường dẫn giong_doc.mp3 ở STATE, lượt "random video +
                # xuất MP4" sau đó sẽ vô tình làm mất nhạc nền.
                with _LOCK:
                    manual = STATE["manual"]
                    manual.update({"audio_path": audio,
                                   "audio_duration": float(
                                       mix.get("duration") or tts_res["duration"]),
                                   "rev": int(manual.get("rev", 0)) + 1})

            # ---- 3/4: phụ đề cứng (tuỳ chọn) ----
            ass_path = None
            _raise_if_cancelled()
            if sub_cfg.get("enabled", True):
                _story_progress(pct=45, step="Video kể chuyện", detail="3/4 Dựng phụ đề")
                ass_path = _ass_tu_srt(srt_path, workdir, w, h, sub_cfg.get("style"))

            # ---- Nếu chưa có ảnh/video: kết thúc ở audio để người dùng tải lên sau ----
            if not imgs and not source_videos:
                dur_str = "%dp%02ds" % (int(tts_res["duration"] // 60), int(tts_res["duration"] % 60))
                with _LOCK:
                    manual = STATE["manual"]
                    manual.update({"working": False,
                                   "status": f"Đã tạo xong audio ({dur_str})! Hãy thêm video nguồn (cột trái) rồi bấm Xuất MP4.",
                                   "audio_path": audio,
                                   "audio_duration": tts_res["duration"],
                                   "srt_path": srt_path,
                                   "error": "",
                                   "rev": int(manual.get("rev", 0)) + 1})
                _story_progress(pct=100, step="Audio sẵn sàng",
                                detail=f"Thời lượng audio: {dur_str} — Hãy chọn video nguồn để ghép")
                _log(f"[Kể chuyện] Đã tạo xong giọng đọc ({dur_str}) và phụ đề. Thêm video nguồn và bấm Xuất MP4 để hoàn thành.", "ok")
                return

            # ---- 4/4: dựng video từ ảnh hoặc video nguồn ----
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"status": ("Bước 4/4: dựng video từ video nguồn…"
                                            if source_videos else
                                            "Bước 4/4: dựng video từ ảnh…"),
                               "rev": int(manual.get("rev", 0)) + 1})
            out_path = os.path.join(out_dir, f"{title}_{stamp}.mp4")
            if source_videos:
                clip_min = float(payload.get("source_clip_min_seconds", 300) or 300)
                clip_max = float(payload.get(
                    "source_clip_max_seconds",
                    payload.get("source_clip_seconds", 600)) or 600)
                random_pick = bool(payload.get("source_random", True))
                _log(
                    "[Kể chuyện] 4/4 %s %d mục video nguồn cho đủ %.1f phút "
                    "audio (mỗi đoạn %.1f–%.1f phút)."
                    % ("random" if random_pick else "xếp tuần tự",
                       len(source_videos), float(tts_res["duration"]) / 60.0,
                       clip_min / 60.0, clip_max / 60.0),
                    "step")

                def _video_progress(pct, detail):
                    _story_progress(pct=50 + pct * 0.5,
                                    step="Video kể chuyện",
                                    detail="4/4 " + str(detail))
                    if (float(pct) <= 3.0 or
                            str(detail).startswith("FFmpeg vẫn đang ghép")):
                        _log("[Kể chuyện] " + str(detail), "step")

                result = ss.tao_video_tu_video(
                    source_videos, audio, out_path, workdir=workdir, w=w, h=h,
                    fps=fps, hieu_ung=str(payload.get("source_effect") or "tinh"),
                    ass_path=ass_path,
                    logo=(payload.get("logo") if isinstance(payload.get("logo"), dict)
                          else None),
                    character=(payload.get("character") if isinstance(payload.get("character"), dict)
                               else None),
                    source_cover=str(payload.get("source_cover") or "none"),
                    min_seconds=clip_min,
                    max_seconds=clip_max,
                    random_pick=random_pick,
                    random_seed=int(payload.get("source_random_seed", 0) or 0) or None,
                    transform=(payload.get("source_transform")
                               if isinstance(payload.get("source_transform"), dict)
                               else None),
                    blur_regions=(payload.get("regions")
                                  if isinstance(payload.get("regions"), list)
                                  else None),
                    blur_bottom_ratio=float(payload.get("blur_bottom_ratio", 0) or 0),
                    progress=_video_progress)
            else:
                render_imgs, keep_repeats = _story_slideshow_images(
                    payload, imgs, ffprobe_duration(audio))
                result = ss.tao_video_tu_anh(
                    render_imgs, audio, out_path, workdir=workdir, w=w, h=h, fps=fps,
                    kieu=kieu, ass_path=ass_path,
                    logo=(payload.get("logo") if isinstance(payload.get("logo"), dict)
                          else None), giu_canh_lap=keep_repeats,
                    character=(payload.get("character") if isinstance(payload.get("character"), dict)
                               else None),
                    progress=lambda pct, detail: _story_progress(
                        pct=50 + pct * 0.5, step="Video kể chuyện",
                        detail="4/4 " + str(detail)))
            metadata_path, calendar_path = _save_story_deliverables(
                result["path"], payload)
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"working": False,
                               "status": "Video kể chuyện hoàn tất",
                               "output_path": result["path"], "error": "",
                               "metadata_path": metadata_path,
                               "calendar_path": calendar_path,
                               "rev": int(manual.get("rev", 0)) + 1})
            _story_progress(pct=100, step="Video kể chuyện xong",
                            detail=os.path.basename(result["path"]))
            _log(f"[Kể chuyện] Xong: {result['path']}", "ok")
        except InterruptedError:
            _mark_manual_cancelled(
                "Đã giữ lại các đoạn giọng, audio, ảnh và phụ đề đã hoàn thành.")
        except Exception as e:
            _log(f"Làm video kể chuyện lỗi: {e}", "err")
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"working": False, "status": "Làm video lỗi",
                               "error": str(e)[:300],
                               "rev": int(manual.get("rev", 0)) + 1})
            _story_progress(pct=100, step="Video kể chuyện lỗi", detail=str(e)[:160])
        finally:
            with _LOCK:
                STATE["running"] = False
                STATE["busy"] = ""

    submit_job(_work, name="Làm video kể chuyện tự động", resource="ffmpeg",
               metadata={"kind": "story_run_all"})
    return {"ok": True, "async": True}, 200


def api_story_video_info(b: Dict) -> JsonResult:
    """Trả về thông tin các video nguồn: duration, resolution."""
    try:
        from ..video import ffprobe_duration
        from ..downloader import ffprobe_video_size
        paths = b.get("paths", [])
        if isinstance(paths, str):
            paths = [paths]
        from ..slideshow import liet_ke_video
        expanded = liet_ke_video(paths)
        results = []
        total_duration = 0.0
        for p in expanded:
            p = str(p).strip().strip('"')
            if not p or not os.path.isfile(p):
                results.append({"path": p, "error": "File không tồn tại"})
                continue
            try:
                dur = ffprobe_duration(p)
                size = ffprobe_video_size(p)
                total_duration += dur
                results.append({
                    "path": p,
                    "name": os.path.basename(p),
                    "duration": dur,
                    "width": size[0] if size else 0,
                    "height": size[1] if size else 0,
                })
            except Exception as e:
                results.append({"path": p, "error": str(e)[:200]})
        audio_dur = 0.0
        with _LOCK:
            audio_dur = float((STATE.get("manual") or {}).get("audio_duration") or 0)
        return {
            "videos": results,
            "paths": expanded,
            "total_video_duration": total_duration,
            "audio_duration": audio_dur,
            "trim_needed": total_duration > audio_dur > 0,
            "trim_seconds": max(0, total_duration - audio_dur) if audio_dur > 0 else 0,
        }, 200
    except Exception as e:
        return {"error": str(e)[:300]}, 500


def api_manual_mux(b: Dict) -> JsonResult:
    jid = int(b.get("id", 0) or 0)
    pr = get_project(jid)
    job = _find(jid)
    with _LOCK:
        current_audio = str((STATE.get("manual") or {}).get("audio_path") or "")
    audio_path = str(b.get("audio_path") or current_audio).strip().strip('"')
    if not pr or not job:
        return {"error": "Hãy chọn video cần ghép."}, 404
    if not audio_path or not os.path.isfile(audio_path):
        return {"error": "Hãy tạo hoặc chọn file âm thanh trước."}, 400
    if ffprobe_duration(audio_path) <= 0:
        return {"error": "Không đọc được file âm thanh."}, 400
    with _LOCK:
        if STATE["running"] or STATE["busy"]:
            return {"error": "Đang bận: " +
                    (STATE["busy"] or "đang xử lý")}, 409
        STATE["cancel"] = False
        STATE["running"] = True
        STATE["busy"] = "Đang ghép audio vào video…"
        manual = STATE["manual"]
        manual.update({"working": True, "status": "Đang xuất video…",
                       "error": "", "output_path": "",
                       "rev": int(manual.get("rev", 0)) + 1})
    _progress(pct=5, step="Ghép audio vào video", detail=os.path.basename(job["path"]))

    def _manual_mux_work(job_id=jid, selected_audio=os.path.abspath(audio_path)):
        try:
            project = get_project(job_id)
            selected_job = _find(job_id)
            if not project or not selected_job:
                raise RuntimeError("Video không còn trong hàng đợi.")
            span = _active_media_span(project)
            effective_duration = float(span["duration"])
            audio_duration = ffprobe_duration(selected_audio)
            if audio_duration > effective_duration + 0.1:
                _log(f"Audio dài hơn video {audio_duration-effective_duration:.1f}s; "
                     "phần vượt quá cuối video sẽ được cắt.", "warn")
            elif audio_duration < effective_duration - 0.1:
                _log(f"Audio ngắn hơn video {effective_duration-audio_duration:.1f}s; "
                     "phần cuối sẽ được chèn im lặng.", "info")

            stem, _ = _run_stem_for_project(project, span)
            out_dir = os.path.join(HERE, "output", stem)
            tmp_dir = os.path.join(out_dir, "_tmp", "manual_mux")
            os.makedirs(tmp_dir, exist_ok=True)
            local_rows = _project_rows_for_span(project, span)
            segs = _segments_from_rows(local_rows, use_vi=True)
            ass_path = None
            if project.get("options", {}).get("hardsub") and segs:
                ass_path = os.path.join(tmp_dir, f"{stem}.manual.ass")
                overlays.save_ass(
                    ass_path, segs, project["w"], project["h"],
                    project.get("sub_style"), use_placed=True)
            final = os.path.join(out_dir, f"{stem}.ghep_audio.mp4")
            work_pr = _render_project_for_span(project, span)
            _progress(pct=12, step="Ghép audio vào video",
                      detail="Đang áp dụng cắt/làm mờ/logo/phụ đề")
            if project.get("options", {}).get("render_chunked"):
                render_with_layers_chunked(
                    work_pr, selected_audio, final, ass_path,
                    segs, tmp_dir)
            else:
                render_with_layers(
                    work_pr, selected_audio, final, ass_path,
                    clip_duration=effective_duration if span.get("enabled") else None,
                    validate_full_source=not span.get("enabled"))
            with _LOCK:
                selected_job = _find(job_id)
                if selected_job:
                    selected_job.update({"status": "xong", "progress": 100,
                                         "output": final,
                                         "note": "Đã ghép audio vào video"})
                manual = STATE["manual"]
                manual.update({"working": False, "status": "Ghép video hoàn tất",
                               "output_path": final, "error": "",
                               "rev": int(manual.get("rev", 0)) + 1})
            _progress(pct=100, step="Ghép audio hoàn tất",
                      detail=os.path.basename(final))
            _log(f"Đã ghép audio vào video: {final}", "ok")
        except InterruptedError:
            _mark_manual_cancelled("Đã dừng ghép audio vào video.")
        except Exception as e:
            _log(f"Ghép audio vào video lỗi: {e}", "err")
            with _LOCK:
                manual = STATE["manual"]
                manual.update({"working": False, "status": "Ghép video lỗi",
                               "error": str(e)[:300],
                               "rev": int(manual.get("rev", 0)) + 1})
            _progress(pct=100, step="Ghép video lỗi", detail=str(e)[:160])
        finally:
            with _LOCK:
                STATE["running"] = False
                STATE["busy"] = ""

    submit_job(_manual_mux_work, name="Ghép audio vào video", resource="ffmpeg",
               metadata={"kind": "manual_mux"})
    return {"ok": True, "async": True}, 200
