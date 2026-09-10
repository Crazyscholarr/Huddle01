"""Chạy pipeline 4 bước (ASR -> dịch -> TTS -> render) trong luồng nền.

Đây là bản GUI của main.py: cùng các bước nhưng đọc/ghi dữ liệu qua project
của giao diện (rows), có tiến độ %, có huỷ giữa chừng và có cắt đoạn (span).
"""
from __future__ import annotations

import os
import traceback
from typing import Dict, List

from .. import srt_utils, overlays, speechmap
from ..srt_utils import Segment
from ..utils import start_file_log
from .state import (HERE, STATE, _LOCK, current_cancel_event,
                    bump_rev, _log, _progress, _find)
from .helpers import _call_filtered, _find_existing_dub_audio, _fmt_span_time
from .config_api import _load_cfg, _translation_api_params
from .projects import (get_project, _active_media_span, _run_stem_for_project,
                       _project_rows_for_span, _segments_from_rows,
                       _rows_from_segments, _sync_project_rows_for_span,
                       _load_local_rows_from_srt, _polish_project_vi,
                       _split_project_vi_on_punctuation,
                       _prepare_project_src_for_translation,
                       _render_project_for_span)
from .render import render_with_layers, render_with_layers_chunked


def _tu_dich_lai_dong_tieng_trung(stage_name: str, segs: List[Segment],
                                  local_rows: List[Dict], dich_nhom,
                                  enabled: bool = True,
                                  raise_on_fail: bool = True,
                                  attempts: int = 2,
                                  log=_log) -> bool:
    """Tự dịch lại đúng những dòng còn tiếng Trung, thay vì dừng cả pipeline.

    Dòng còn tiếng Trung sinh ra khi một lô dịch (nhất là chế độ browser) trả
    thiếu vài dòng lẻ - trước đây tới bước TTS/render mới bị chặn bằng
    RuntimeError và người dùng phải tự chạy lại bước Dịch. Giờ gom đúng các
    dòng bẩn, gọi lại provider đang cấu hình (qua `dich_nhom`) tối đa
    `attempts` lần; sửa được dòng nào thì ghi ngược vào `local_rows` để call
    site lưu lại project/file.

    Trả về True nếu có dòng được sửa. Nếu vẫn còn dòng bẩn:
      - raise_on_fail=True  -> ném RuntimeError (chốt chặn như hành vi cũ,
        để không bao giờ đọc to tiếng Trung trong video);
      - raise_on_fail=False -> chỉ cảnh báo rồi đi tiếp (dùng ngay sau bước
        Dịch, vì bước TTS phía sau sẽ thử thêm lần nữa rồi mới chặn).
    """
    from .. import translate as tr_mod

    def _dirty() -> List[int]:
        return [i for i, s in enumerate(segs) if tr_mod._contains_cjk(s.text)]

    def _liet_ke(indices: List[int]) -> str:
        sample = ", ".join(str(segs[i].index) for i in indices[:8])
        more = "" if len(indices) <= 8 else f", ... +{len(indices) - 8}"
        return sample + more

    if not _dirty():
        return False

    changed = False
    if enabled:
        for attempt in range(1, max(1, int(attempts)) + 1):
            dirty = _dirty()
            if not dirty:
                break
            log(f"Còn {len(dirty)} dòng tiếng Trung trước khi {stage_name} "
                f"(dòng {_liet_ke(dirty)}) -> tự dịch lại, "
                f"lần {attempt}/{attempts}...", "warn")
            try:
                dich_nhom([segs[i] for i in dirty])
            except Exception as e:
                log(f"Tự dịch lại thất bại: {str(e)[:200]}", "warn")
            for i in dirty:
                if not tr_mod._contains_cjk(segs[i].text):
                    if i < len(local_rows):
                        local_rows[i]["vi"] = segs[i].text
                    changed = True

    still = _dirty()
    if still:
        if raise_on_fail:
            note = (" Chương trình đã tự dịch lại nhưng vẫn chưa sạch."
                    if enabled else "")
            raise RuntimeError(
                f"Bản dịch còn tiếng Trung ở dòng {_liet_ke(still)}.{note} "
                f"Hãy chạy lại bước Dịch trước khi {stage_name}, hoặc sửa tay "
                "các dòng này trong bảng Sửa từng dòng.")
        log(f"Vẫn còn {len(still)} dòng tiếng Trung (dòng {_liet_ke(still)}) - "
            "tạm giữ tiếng gốc, bước sau sẽ tự dịch lại lần nữa.", "warn")
    elif changed:
        log(f"Đã tự dịch lại xong các dòng còn tiếng Trung trước khi {stage_name}.", "ok")
    return changed


def _bao_dam_ban_do_thoai(asr_mod, out_dir: str, stem: str, pr: Dict,
                          span: Dict, effective_duration: float):
    """Nạp/dựng BẢN ĐỒ THOẠI cho job hiện tại (mốc thời gian từng ký tự).

    Người dùng GUI hay bấm riêng từng bước (chỉ Dịch, chỉ Dựng giọng đọc), nên
    bước nào cũng phải tự bảo đảm có bản đồ - nếu không, đúng bước đó sẽ chia
    lại phụ đề theo tỉ lệ và làm voice lệch khỏi hình.
    """
    if speechmap.get_active() is not None:
        return speechmap.get_active()
    try:
        return asr_mod.ensure_speech_map(
            out_dir, stem, video_path=pr.get("video"),
            trim_start=span["start"] if span.get("enabled") else 0.0,
            trim_duration=effective_duration if span.get("enabled") else None)
    except Exception as e:
        _log(f"Không nạp được bản đồ thoại: {e}", "warn")
        return None


def run_pipeline(job_id: int, steps: List[str]):
    """steps ⊂ {asr, translate, tts, render}. Chạy trong luồng riêng."""
    from .. import asr as asr_mod, translate as tr_mod, tts as tts_mod, video as vid_mod

    job = _find(job_id)
    pr = get_project(job_id)
    if not job or not pr:
        return

    cfg = _load_cfg()
    a, tr, tc, vo = cfg.get("asr", {}), cfg.get("translation", {}), \
        cfg.get("tts", {}), cfg.get("video", {})

    span = _active_media_span(pr)
    stem, raw_stem = _run_stem_for_project(pr, span)
    effective_duration = float(span["duration"])
    out_dir = os.path.join(HERE, "output", stem)
    tmp_dir = os.path.join(out_dir, "_tmp")
    os.makedirs(os.path.join(tmp_dir, "clips"), exist_ok=True)
    audio_wav = os.path.join(tmp_dir, "audio16k.wav")
    src_srt = os.path.join(out_dir, f"{stem}.src.srt")
    vi_srt = os.path.join(out_dir, f"{stem}.vi.srt")
    process_log = os.path.join(out_dir, f"{stem}.quy_trinh.log")
    if start_file_log(process_log):
        _log(f"Nhật ký quy trình được lưu vào: {process_log}", "info")
    if stem != raw_stem:
        _log(f"Tên output đã được chuẩn hóa để Windows tạo thư mục được: {stem}", "info")
    if span.get("enabled"):
        _log(
            f"Cắt video: giữ từ {_fmt_span_time(span['start'])} "
            f"đến {_fmt_span_time(span['end'])} "
            f"({effective_duration / 60:.1f} phút).",
            "info",
        )
    if effective_duration >= 2 * 3600:
        _log("Video dài: ghi cứng phụ đề/làm mờ/xoá logo sẽ render lại toàn bộ hình. "
             "Nhanh nhất là tắt hardsub/blur/delogo và xuất SRT rời; khi chỉ thay "
             "audio, backend MP4Box sẽ copy video.", "info")

    total = len(steps)
    done = 0
    src_lang = a.get("source_language") or "auto"

    def bump(step_name, pct_in_step=0.0):
        _progress(pct=(done + pct_in_step) / max(1, total) * 100,
                  step=step_name, sub=done + 1)

    auto_retranslate = bool(tr.get("auto_retranslate", True))

    def _dich_nhom_segments(target_segs: List[Segment],
                            cache_path: str = None) -> None:
        """Dịch (tại chỗ) một nhóm segment bằng đúng provider đang cấu hình.

        Dùng cho cả bước Dịch chính lẫn việc tự dịch lại các dòng mà lô
        trước trả thiếu (còn nguyên tiếng Trung).
        """
        provider = str(tr.get("provider", "browser")).lower()
        name_hint = tr_mod.build_name_hint(
            tr.get("male_lead_name", ""),
            tr.get("female_lead_name", ""))
        if provider == "browser":
            _call_filtered(
                tr_mod.translate_via_browser, target_segs,
                os.path.join(HERE, tr.get("browser_profile",
                                          "browser_profile")),
                _kw={
                    "channel": tr.get("browser_channel", "msedge"),
                    "chunk_size": tr.get("chunk_size", 25),
                    "wait_reply": tr.get("wait_reply", 120),
                    "cache_path": cache_path,
                    "source_lang": src_lang if src_lang != "auto" else None,
                    "reset_every": tr.get("reset_every"),
                    "chars_per_sec": tr.get("chars_per_sec"),
                    "name_hint": name_hint,
                    "shorten_long_lines_enabled": tr.get("shorten_long_lines"),
                    # các tham số của bản cũ - tự bỏ nếu không còn
                    "mode": tr.get("browser_mode"),
                    "debug_port": tr.get("debug_port"),
                    "use_real_profile": tr.get("use_real_profile"),
                    "out_dir": out_dir,
                })
        else:
            api_key, model, api_base_url, api_timeout = \
                _translation_api_params(tr, provider)
            _call_filtered(
                tr_mod.translate_segments, target_segs,
                _kw={
                    "api_key": api_key,
                    "model": model,
                    "provider": provider,
                    "api_base_url": api_base_url,
                    "api_timeout": api_timeout,
                    "chunk_size": tr.get("chunk_size", 40),
                    "cache_path": cache_path,
                    "chars_per_sec": tr.get("chars_per_sec"),
                    "name_hint": name_hint,
                    "shorten_long_lines_enabled": tr.get("shorten_long_lines"),
                })

    def _chan_tieng_trung(stage_name: str, segs: List[Segment],
                          local_rows: List[Dict],
                          raise_on_fail: bool = True) -> bool:
        """Chốt chặn tiếng Trung, có tự dịch lại (translation.auto_retranslate)."""
        return _tu_dich_lai_dong_tieng_trung(
            stage_name, segs, local_rows, _dich_nhom_segments,
            enabled=auto_retranslate, raise_on_fail=raise_on_fail, log=_log)

    try:
        with _LOCK:
            STATE["running"] = True
            STATE["cancel"] = False
            job["status"] = "dang chay"

        # ---------------- 1. ASR ----------------
        if "asr" in steps:
            bump("Nhận dạng phụ đề gốc")
            if a.get("reuse_existing") and os.path.exists(src_srt):
                _log(f"Dùng lại phụ đề gốc: {src_srt}", "ok")
                _bao_dam_ban_do_thoai(asr_mod, out_dir, stem, pr, span,
                                      effective_duration)
                segs = srt_utils.load_srt_file(src_srt)
                lang = (a.get("source_language")
                        or asr_mod.guess_language(segs) or "auto")
                before_norm = len(segs)
                max_chars = 34 if str(lang).lower()[:2] in {"zh", "ja", "ko"} else 80
                segs = asr_mod.normalize_segments(segs, max_chars)
                if len(segs) != before_norm:
                    _log(f"Đã gom/sửa lại phụ đề gốc cũ: {before_norm} -> {len(segs)} dòng.", "ok")
                    srt_utils.save_srt_file(src_srt, segs)
                # Cùng bộ lọc với CLI: file cũ có thể còn câu BỊA + lỗi nghe
                # nhầm; trước đây chỉ main.py làm, GUI bỏ qua nên sửa
                # asr.corrections xong chạy lại GUI không thấy tác dụng.
                if a.get("filter_hallucinations", True):
                    segs, removed = asr_mod.drop_hallucinations(segs)
                    if removed:
                        _log(f"Đã bỏ {len(removed)} dòng nghi là câu BỊA trong "
                             "file cũ.", "warn")
                        srt_utils.save_srt_file(src_srt, segs)
                if a.get("corrections"):
                    nfix = asr_mod.apply_corrections(segs, a["corrections"])
                    if nfix:
                        _log(f"Đã sửa {nfix} dòng theo bảng asr.corrections.", "ok")
                        srt_utils.save_srt_file(src_srt, segs)
            else:
                _log("Tách audio...", "step")
                audio_wav = _call_filtered(vid_mod.ensure_audio, pr["video"], audio_wav,
                                           _kw={
                                               "loudnorm": a.get("loudnorm", True),
                                               "trim_start": span["start"] if span.get("enabled") else 0.0,
                                               "trim_duration": effective_duration if span.get("enabled") else None,
                                           })
                if current_cancel_event().is_set():
                    raise InterruptedError
                _log(f"Nhận dạng (backend={a.get('backend','paraformer')})...", "step")
                # Chỉ truyền những tham số mà transcribe() THẬT SỰ nhận. asr.py có
                # thể được nâng cấp/đổi chữ ký; gọi cứng sẽ vỡ với
                # "unexpected keyword argument". Cách này luôn khớp.
                segs, lang = _call_filtered(asr_mod.transcribe, audio_wav, _kw={
                    "backend": a.get("backend", "paraformer"),
                    "language": a.get("source_language"),
                    "model_size": a.get("model_size", "large-v3"),
                    "device": a.get("device", "cuda"),
                    "compute_type": a.get("compute_type", "float16"),
                    "batch_size": a.get("batch_size", 16),
                    "rescue_gaps": a.get("rescue_gaps", True),
                    "min_gap_seconds": a.get("min_gap_seconds", 25),
                    "max_rescue_rounds": a.get("max_rescue_rounds", 2),
                    "silence_db": a.get("silence_db", -45),
                    "min_coverage": a.get("min_coverage", 0.35),
                    "fallback_backend": a.get("fallback_backend", "faster-whisper"),
                    "hub": a.get("hub"),
                    "filter_hallucinations": a.get("filter_hallucinations"),
                    "corrections": a.get("corrections"),
                    "vocab_hint": a.get("vocab_hint"),
                })
                srt_utils.save_srt_file(src_srt, segs)
                sm = speechmap.get_active()
                if sm is not None:
                    sm.save(speechmap.default_path(out_dir, stem))
            local_rows = _rows_from_segments(segs, "src")
            tmp_pr = {"segments": local_rows}
            delta = _prepare_project_src_for_translation(tmp_pr, tr)
            if delta:
                local_rows = tmp_pr["segments"]
                segs = _segments_from_rows(local_rows, use_vi=False)
                srt_utils.save_srt_file(src_srt, segs)
                _log(f"Đã chuẩn bị câu gốc để dịch theo ý nghĩa: "
                     f"{len(segs) - delta} -> {len(segs)} dòng.", "ok")
            _sync_project_rows_for_span(pr, span, local_rows)
            bump_rev(job_id)
            src_lang = lang or src_lang
            done += 1

        # ---------------- 2. Dịch ----------------
        if "translate" in steps:
            bump("Dịch sang tiếng Việt")
            _bao_dam_ban_do_thoai(asr_mod, out_dir, stem, pr, span,
                                  effective_duration)
            local_rows = _project_rows_for_span(pr, span)
            segs = _segments_from_rows(local_rows, use_vi=False)
            if not segs and os.path.exists(src_srt):
                _log(f"Nạp lại phụ đề gốc từ file có sẵn: {src_srt}", "ok")
                local_rows = _load_local_rows_from_srt(src_srt)
                _sync_project_rows_for_span(pr, span, local_rows)
                segs = _segments_from_rows(local_rows, use_vi=False)
                src_lang = a.get("source_language") or asr_mod.guess_language(segs) or src_lang
            if not segs:
                raise RuntimeError("Chưa có phụ đề gốc - hãy chạy Nhận dạng trước.")
            tmp_pr = {"segments": local_rows}
            delta = _prepare_project_src_for_translation(tmp_pr, tr)
            if delta:
                local_rows = tmp_pr["segments"]
                segs = _segments_from_rows(local_rows, use_vi=False)
                srt_utils.save_srt_file(src_srt, segs)
                _log(f"Đã chuẩn bị câu gốc để dịch theo ý nghĩa trước khi dịch: "
                     f"{len(segs) - delta} -> {len(segs)} dòng.", "ok")
            cache_path = os.path.join(out_dir, f"{stem}.translate_cache.json")
            reused_translation = False
            split_vi = bool(tr.get("split_translated_on_punctuation", False))
            if tr.get("reuse_existing") and os.path.exists(vi_srt):
                vi_segments = srt_utils.load_srt_file(vi_srt)
                dirty_lines = [
                    s.index for s in vi_segments
                    if tr_mod._contains_cjk(s.text)
                ]
                if dirty_lines:
                    sample = ", ".join(str(x) for x in dirty_lines[:8])
                    more = "" if len(dirty_lines) <= 8 else f", ... +{len(dirty_lines) - 8}"
                    _log(f"Bản dịch cũ còn tiếng Trung ở dòng {sample}{more} "
                         "-> dịch lại để tránh TTS bị câm.", "warn")
                elif len(vi_segments) == len(segs):
                    _log(f"Dùng lại bản dịch: {vi_srt}", "ok")
                    for row, s in zip(local_rows, vi_segments):
                        row["vi"] = s.text
                    reused_translation = True
                else:
                    _log(f"Bản dịch cũ có {len(vi_segments)} dòng, phụ đề gốc có "
                         f"{len(segs)} dòng -> dịch lại để tránh dùng timestamp "
                         "đã bị lệch.", "warn")
            if reused_translation:
                tmp_pr = {"segments": local_rows}
                polished = _polish_project_vi(tmp_pr, tr)
                if polished is not None:
                    _log(f"Da chia lai sub Viet theo y cau: {len(_segments_from_rows(tmp_pr['segments'], use_vi=True))} dong.", "ok")
                if split_vi:
                    added = _split_project_vi_on_punctuation(tmp_pr)
                    if added:
                        _log(f"Da tach sub Viet theo dau cau: +{added} dong.", "ok")
                local_rows = tmp_pr["segments"]
                _sync_project_rows_for_span(pr, span, local_rows)
                srt_utils.save_srt_file(vi_srt, _segments_from_rows(local_rows, use_vi=True))
                done += 1
                bump_rev(job_id)
            else:
                _dich_nhom_segments(segs, cache_path)
                for row, s in zip(local_rows, segs):
                    row["vi"] = s.text
                # Lô nào trả thiếu thì dòng đó còn nguyên tiếng Trung - tự dịch
                # lại NGAY tại đây, lúc các dòng còn khớp 1-1 với câu gốc (chưa
                # bị polish chia lại). Vẫn hỏng thì chưa chặn: bước TTS sẽ thử
                # thêm lần nữa rồi mới dừng.
                _chan_tieng_trung("lưu bản dịch", segs, local_rows,
                                  raise_on_fail=False)
                tmp_pr = {"segments": local_rows}
                polished = _polish_project_vi(tmp_pr, tr)
                if polished is not None:
                    _log(f"Da chia lai sub Viet theo y cau: {len(_segments_from_rows(tmp_pr['segments'], use_vi=True))} dong.", "ok")
                if split_vi:
                    added = _split_project_vi_on_punctuation(tmp_pr)
                    if added:
                        _log(f"Da tach sub Viet theo dau cau: +{added} dong.", "ok")
                local_rows = tmp_pr["segments"]
                _sync_project_rows_for_span(pr, span, local_rows)
                bump_rev(job_id)
                srt_utils.save_srt_file(vi_srt, _segments_from_rows(local_rows, use_vi=True))
                done += 1

        # ---------------- 3. Lồng tiếng ----------------
        dub_wav = os.path.join(tmp_dir, "dub.wav")
        if "tts" in steps:
            bump("Dựng track giọng đọc")
            _bao_dam_ban_do_thoai(asr_mod, out_dir, stem, pr, span,
                                  effective_duration)
            local_rows = _project_rows_for_span(pr, span)
            segs = _segments_from_rows(local_rows, use_vi=True)
            if not segs and os.path.exists(src_srt):
                _log("Nạp lại phụ đề/bản dịch từ output cũ để dựng giọng.", "ok")
                local_rows = _load_local_rows_from_srt(src_srt, vi_srt)
                _sync_project_rows_for_span(pr, span, local_rows)
                segs = _segments_from_rows(local_rows, use_vi=True)
            if not segs:
                raise RuntimeError("Chưa có phụ đề - hãy chạy Nhận dạng/Dịch trước.")
            opt = pr["options"]
            tmp_pr = {"segments": local_rows}
            polished = _polish_project_vi(tmp_pr, tr)
            if polished is not None:
                local_rows = tmp_pr["segments"]
                _log(f"Da chia lai sub Viet theo y cau truoc TTS: {len(_segments_from_rows(local_rows, use_vi=True))} dong.", "ok")
                _sync_project_rows_for_span(pr, span, local_rows)
                srt_utils.save_srt_file(vi_srt, _segments_from_rows(local_rows, use_vi=True))
                bump_rev(job_id)
                segs = _segments_from_rows(local_rows, use_vi=True)
            if bool(tr.get("split_translated_on_punctuation", False)):
                tmp_pr = {"segments": local_rows}
                added = _split_project_vi_on_punctuation(tmp_pr)
                if added:
                    local_rows = tmp_pr["segments"]
                    _log(f"Da tach sub Viet theo dau cau truoc TTS: +{added} dong.", "ok")
                    _sync_project_rows_for_span(pr, span, local_rows)
                    srt_utils.save_srt_file(vi_srt, _segments_from_rows(local_rows, use_vi=True))
                    bump_rev(job_id)
                    segs = _segments_from_rows(local_rows, use_vi=True)
            if _chan_tieng_trung("dựng giọng đọc", segs, local_rows):
                _sync_project_rows_for_span(pr, span, local_rows)
                srt_utils.save_srt_file(vi_srt, _segments_from_rows(local_rows, use_vi=True))
                bump_rev(job_id)
                segs = _segments_from_rows(local_rows, use_vi=True)
            # Cảnh báo sớm nếu bản dịch dài hơn khung thời gian: mọi câu sẽ bị
            # nén/cắt và thoại kết thúc trước hình.
            tr_mod.log_reading_pressure(
                segs, tr.get("chars_per_sec", 15.0) or 15.0)
            # Xóa clips cũ để chắc chắn tạo lại với giọng/pitch mới
            clips_dir = os.path.join(tmp_dir, "clips")
            os.makedirs(clips_dir, exist_ok=True)
            # Đọc engine từ GUI trước, fallback về config.yaml
            tts_engine = opt.get("engine") or tc.get("engine") or "edge"
            tts_voice = opt.get("narrator_voice", tc.get("narrator_voice",
                                                         "vi-VN-NamMinhNeural"))
            tts_pitch = opt.get("narrator_pitch", tc.get("narrator_pitch", "+0Hz"))
            tts_rate = opt.get("base_rate", tc.get("base_rate", "+0%"))
            tts_max_speed = float(opt.get("max_speed", tc.get("max_speed", 1.6)))
            tts_min_gap = float(opt.get("min_gap", tc.get("min_gap", 0.08)))
            tts_max_overhang = float(opt.get(
                "max_overhang_seconds",
                tc.get("max_overhang_seconds", 0.75)) or 0.0)
            tts_sync_offset = float(opt.get("sync_offset_seconds",
                                            tc.get("sync_offset_seconds", 0.0)) or 0.0)
            tts_sync_mode = opt.get("sync_mode", tc.get("sync_mode", "cascade"))
            tts_trim_overflow = opt.get("trim_overflow", tc.get("trim_overflow", True))
            tts_voice_mode = opt.get("voice_mode", tc.get("voice_mode", "narrator"))
            if str(tts_engine).lower() == "edge":
                norm = tts_mod.normalize_edge_narrator(
                    {"voice": tts_voice, "pitch": tts_pitch})
                tts_voice, tts_pitch = norm["voice"], norm["pitch"]
            _log(f"TTS: engine={tts_engine}, voice={tts_voice}, "
                 f"pitch={tts_pitch}, rate={tts_rate}, "
                 f"mode={tts_voice_mode}, speed={tts_max_speed}, "
                 f"gap={tts_min_gap}, overhang={tts_max_overhang}, "
                 f"sync={tts_sync_mode}", "info")
            clips, starts, places = _call_filtered(
                tts_mod.build_voice_track, segs, clips_dir,
                _kw={
                    "total_duration": effective_duration,
                    "engine": tts_engine,
                    "voice_mode": tts_voice_mode,
                    "narrator": {"voice": tts_voice, "pitch": tts_pitch},
                    "base_rate": tts_rate,
                    "max_speed": tts_max_speed,
                    "min_gap": tts_min_gap,
                    "max_overhang": tts_max_overhang,
                    "sync_offset_seconds": tts_sync_offset,
                    "sync_mode": tts_sync_mode,
                    "trim_overflow": tts_trim_overflow,
                    "concurrency": tc.get("concurrency", 14),
                    "fail_report_path": os.path.join(out_dir,
                                                     f"{stem}.tts_loi.txt"),
                    "recover_drift": tc.get("recover_drift"),
                    "trim": tc.get("trim_silence", True),
                    "vieneu_voice": opt.get("narrator_voice") if tts_engine == "vieneu" else tc.get("vieneu_voice"),
                    "vieneu_voices": tc.get("vieneu_voices"),
                    "vieneu_options": tc.get("vieneu_options"),
                    "capcut_options": tc.get("capcut_options"),
                })
            from ..timeline import summarize
            _log(f"Chống đè thoại: {summarize(places)}", "ok")
            for row, s in zip(local_rows, segs):
                row["placed"] = s.placed_start
                row["speed"] = s.speed
                row["voice_dur"] = s.voice_duration
            _sync_project_rows_for_span(pr, span, local_rows)
            srt_utils.save_srt_file(vi_srt, segs, use_placed=True)
            bump_rev(job_id)
            vc = [c for c in clips if c]
            vs = [s for c, s in zip(clips, starts) if c]
            if not vc:
                raise RuntimeError(
                    "TTS không tạo được clip giọng Việt nào. Đã dừng trước khi xuất "
                    "video để tránh tạo file chỉ còn tiếng gốc + phụ đề. Xem file "
                    f"{stem}.tts_loi.txt trong thư mục output."
                )
            dub_wav = vid_mod.assemble_timeline_audio(
                vc, vs, effective_duration, dub_wav,
                mode=vo.get("audio_mix_mode", "auto"),
                chunk_seconds=vo.get("audio_mix_chunk_seconds", 120))
            done += 1

        # ---------------- 4. Xuất video ----------------
        if "render" in steps:
            bump("Xuất video")
            _bao_dam_ban_do_thoai(asr_mod, out_dir, stem, pr, span,
                                  effective_duration)
            opt = pr["options"]
            local_rows = _project_rows_for_span(pr, span)
            segs = _segments_from_rows(local_rows, use_vi=True)
            if not segs and os.path.exists(src_srt):
                _log("Nạp lại phụ đề/bản dịch từ output cũ để xuất video.", "ok")
                local_rows = _load_local_rows_from_srt(src_srt, vi_srt)
                _sync_project_rows_for_span(pr, span, local_rows)
                segs = _segments_from_rows(local_rows, use_vi=True)
            tmp_pr = {"segments": local_rows}
            polished = _polish_project_vi(tmp_pr, tr)
            if polished is not None:
                local_rows = tmp_pr["segments"]
                _log(f"Da chia lai sub Viet theo y cau truoc render: {len(_segments_from_rows(local_rows, use_vi=True))} dong.", "ok")
                _sync_project_rows_for_span(pr, span, local_rows)
                srt_utils.save_srt_file(vi_srt, _segments_from_rows(local_rows, use_vi=True))
                bump_rev(job_id)
                segs = _segments_from_rows(local_rows, use_vi=True)
            if bool(tr.get("split_translated_on_punctuation", False)):
                tmp_pr = {"segments": local_rows}
                added = _split_project_vi_on_punctuation(tmp_pr)
                if added:
                    local_rows = tmp_pr["segments"]
                    _log(f"Da tach sub Viet theo dau cau truoc render: +{added} dong.", "ok")
                    _sync_project_rows_for_span(pr, span, local_rows)
                    bump_rev(job_id)
                    segs = _segments_from_rows(local_rows, use_vi=True)
            if _chan_tieng_trung("xuất video", segs, local_rows):
                _sync_project_rows_for_span(pr, span, local_rows)
                srt_utils.save_srt_file(vi_srt, _segments_from_rows(local_rows, use_vi=True))
                bump_rev(job_id)
                segs = _segments_from_rows(local_rows, use_vi=True)
            ass_path = None
            if opt.get("hardsub") and segs:
                ass_path = os.path.join(tmp_dir, f"{stem}.ass")
                overlays.save_ass(ass_path, segs, pr["w"], pr["h"],
                                  pr.get("sub_style"), use_placed=True)
            if opt.get("export_srt") and segs:
                srt_utils.save_srt_file(vi_srt, segs, use_placed=True)

            dub_wav = _find_existing_dub_audio(tmp_dir) or dub_wav
            if not os.path.exists(dub_wav):
                dub_wav = None
            if dub_wav is None and segs:
                raise RuntimeError(
                    "Chưa có track giọng Việt. Hãy chạy 'Dựng giọng đọc' "
                    "thành công trước khi xuất video."
                )
            final = os.path.join(out_dir, f"{stem}.vietsub_dub.mp4")
            work_pr = _render_project_for_span(pr, span)
            if opt.get("render_chunked"):
                render_with_layers_chunked(work_pr, dub_wav, final, ass_path, segs, tmp_dir)
            else:
                render_with_layers(
                    work_pr, dub_wav, final, ass_path,
                    clip_duration=effective_duration if span.get("enabled") else None,
                    validate_full_source=not span.get("enabled"),
                )
            with _LOCK:
                job["output"] = final
            _log(f"Đã xuất: {final}", "ok")
            done += 1

        _progress(pct=100, step="Hoàn tất", detail="")
        with _LOCK:
            job["status"] = "xong"
        _log("HOÀN TẤT.", "ok")

    except InterruptedError:
        with _LOCK:
            job["status"] = "đã huỷ"
        _log("Đã huỷ theo yêu cầu.", "warn")
    except Exception as e:
        with _LOCK:
            job["status"] = "lỗi"
            job["note"] = str(e)[:200]
        _log(f"LỖI: {e}", "err")
        _log(traceback.format_exc()[-800:], "err")
    finally:
        with _LOCK:
            STATE["running"] = False
