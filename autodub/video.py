"""Xử lý audio/video bằng ffmpeg: tách audio, đổi tốc độ, ghép timeline,
che sub bằng blur, và render cuối (ưu tiên NVENC cho nhanh)."""
from __future__ import annotations

import math
import os
import re
import tempfile
import time
import uuid
from typing import List, Optional, Sequence, Dict

from .utils import (log, run, ffprobe_duration, has_nvenc, has_cuda_decode,
                    ffprobe_video_codec, ffprobe_video_size, ffprobe_fps,
                    nvenc_encode_args)


def _discard_partial(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _pcm_mib(total_duration: float, sr: int, channels: int = 1,
             bytes_per_sample: int = 2) -> float:
    return (max(0.0, float(total_duration or 0.0))
            * sr * channels * bytes_per_sample) / (1024 ** 2)


def preferred_asr_audio_path(out_path: str, total_duration: float,
                             sr: int = 16000) -> str:
    """Prefer FLAC for long ASR extracts to avoid huge temporary WAV files."""
    if (os.path.splitext(out_path)[1].lower() == ".wav"
            and (float(total_duration or 0.0) >= 1800.0
                 or _pcm_mib(total_duration, sr) >= 256.0)):
        return os.path.splitext(out_path)[0] + ".flac"
    return out_path


def extract_audio(video: str, out_wav: str, sr: int = 16000,
                  loudnorm: bool = True, trim_start: float = 0.0,
                  trim_duration: Optional[float] = None) -> str:
    """Tách audio về mono 16kHz - chuẩn đầu vào cho ASR.

    loudnorm=True: chuẩn hoá âm lượng. Rất quan trọng vì audio quá nhỏ khiến VAD
    nằm sát ngưỡng quyết định và bỏ sót cả đoạn có tiếng nói.
    Nếu bản mono bị triệt tiêu (2 kênh ngược pha), tự lấy riêng kênh trái.
    """
    af = "loudnorm=I=-16:TP=-1.5:LRA=11" if loudnorm else None
    trim_start = max(0.0, float(trim_start or 0.0))
    trim_duration = (None if trim_duration is None
                     else max(0.01, float(trim_duration)))
    cmd = ["ffmpeg", "-y"]
    if trim_start > 0:
        cmd += ["-ss", f"{trim_start:.3f}"]
    if trim_duration is not None:
        cmd += ["-t", f"{trim_duration:.3f}"]
    cmd += ["-i", video, "-vn", "-ac", "1", "-ar", str(sr)]
    if af:
        cmd += ["-af", af]
    cmd += [*_audio_encode_args(out_wav), out_wav]
    try:
        run(cmd)
    except Exception:
        _discard_partial(out_wav)
        raise

    # Kiểm tra bản mono có bị "câm" bất thường không
    try:
        res = run(["ffmpeg", "-hide_banner", "-i", out_wav, "-af", "volumedetect",
                   "-f", "null", "-"], check=False)
        import re as _re
        m = _re.search(r"mean_volume:\s*(-?[\d.]+) dB", res.stderr or "")
        if m and float(m.group(1)) < -60:
            log("Bản mono gần như im lặng (2 kênh có thể ngược pha) - "
                "thử lấy riêng kênh trái...", "warn")
            cmd2 = ["ffmpeg", "-y"]
            if trim_start > 0:
                cmd2 += ["-ss", f"{trim_start:.3f}"]
            if trim_duration is not None:
                cmd2 += ["-t", f"{trim_duration:.3f}"]
            cmd2 += ["-i", video, "-vn", "-ar", str(sr),
                     "-af", "pan=mono|c0=c0" + (f",{af}" if af else ""),
                     *_audio_encode_args(out_wav), out_wav]
            try:
                run(cmd2)
            except Exception:
                _discard_partial(out_wav)
                raise
    except InterruptedError:
        raise
    except Exception:
        if not os.path.exists(out_wav) or os.path.getsize(out_wav) <= 1024:
            raise
        pass
    return out_wav


def ensure_audio(video: str, out_wav: str, sr: int = 16000,
                 loudnorm: bool = True, reuse_existing: bool = True,
                 trim_start: float = 0.0,
                 trim_duration: Optional[float] = None) -> str:
    """Tách audio nếu cần; dùng lại file WAV cũ khi độ dài còn khớp video."""
    target_duration = None
    if trim_duration is not None:
        target_duration = max(0.01, float(trim_duration))
    else:
        try:
            target_duration = ffprobe_duration(video)
        except Exception:
            target_duration = None
    if target_duration:
        preferred = preferred_asr_audio_path(out_wav, target_duration, sr)
        if preferred != out_wav:
            log(
                f"Audio ASR dai (~{_pcm_mib(target_duration, sr):.0f} MiB neu WAV) "
                f"- luu {os.path.basename(preferred)} de tiet kiem o dia.",
                "info",
            )
            out_wav = preferred
    if reuse_existing and os.path.exists(out_wav) and os.path.getsize(out_wav) > 1024:
        audio_dur = ffprobe_duration(out_wav)
        video_dur = target_duration or ffprobe_duration(video)
        # Timestamp ASR được tính trên file audio này.  Sai khác 1% từng được
        # chấp nhận ở đây, tức có thể tái dùng một track ngắn hơn gần 100 giây
        # cho video 3 giờ và làm SRT/voice đi trước hình tăng dần về cuối.
        # File vừa tách bằng FFmpeg chỉ sai vài sample; 0.5 s đã là biên rộng.
        tolerance = 0.5
        if audio_dur > 0 and (video_dur <= 0 or abs(audio_dur - video_dur) <= tolerance):
            log(f"Dùng lại audio đã tách: {out_wav}", "ok")
            return out_wav
        log("Audio đã tách cũ lệch độ dài video - tách lại từ đầu.", "warn")
    return extract_audio(video, out_wav, sr=sr, loudnorm=loudnorm,
                         trim_start=trim_start, trim_duration=trim_duration)


def _atempo_chain(speed: float) -> str:
    """atempo mỗi bộ lọc chỉ nhận 0.5–2.0 -> nối chuỗi nếu vượt."""
    speed = max(0.5, float(speed))
    parts = []
    while speed > 2.0:
        parts.append("atempo=2.0")
        speed /= 2.0
    parts.append(f"atempo={speed:.5f}")
    return ",".join(parts)


def change_speed(in_wav: str, out_wav: str, speed: float,
                 max_duration: Optional[float] = None) -> str:
    """Tăng/giảm tốc độ audio mà KHÔNG đổi cao độ, có thể cắt theo slot."""
    duration_limit = None
    try:
        if max_duration is not None:
            duration_limit = max(0.01, float(max_duration))
    except (TypeError, ValueError):
        duration_limit = None

    if abs(speed - 1.0) < 1e-3 and duration_limit is None:
        # Không cần đổi -> copy nhanh
        run(["ffmpeg", "-y", "-i", in_wav, "-c", "copy", out_wav], check=False)
        if not os.path.exists(out_wav):
            run(["ffmpeg", "-y", "-i", in_wav, out_wav])
        return out_wav

    filters = []
    if abs(speed - 1.0) >= 1e-3:
        filters.append(_atempo_chain(speed))
    if duration_limit is not None:
        filters.append(f"atrim=start=0:duration={duration_limit:.3f}")
        filters.append("asetpts=PTS-STARTPTS")
    run([
        "ffmpeg", "-y", "-i", in_wav, "-filter:a", ",".join(filters), out_wav,
    ])
    return out_wav


def trim_silence(in_path: str, out_path: str, threshold_db: int = -45,
                 keep_pad: float = 0.06) -> str:
    """Cắt khoảng lặng ĐẦU và CUỐI của một clip TTS.

    edge-tts luôn chèn một quãng lặng nhỏ ở hai đầu mỗi câu. Với 170 câu/tập,
    chỗ lặng thừa đó cộng lại thành hàng chục giây - chính là một phần khiến
    giọng đọc tụt lại phía sau hình. Cắt đi thì không mất chữ nào mà lấy lại
    được rất nhiều thời gian.
    """
    f = (f"silenceremove=start_periods=1:start_silence={keep_pad}:"
         f"start_threshold={threshold_db}dB:detection=peak,"
         "areverse,"
         f"silenceremove=start_periods=1:start_silence={keep_pad}:"
         f"start_threshold={threshold_db}dB:detection=peak,"
         "areverse")
    try:
        run(["ffmpeg", "-y", "-i", in_path, "-af", f, out_path], quiet=True)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 512:
            return out_path
    except Exception:
        pass
    return in_path          # cắt hỏng thì dùng bản gốc, không được làm mất câu


def compact_long_silences(in_path: str, out_path: str,
                          threshold_db: int = -35,
                          trigger_duration: float = 0.10,
                          keep_silence: float = 0.12) -> str:
    """Rút các khoảng im bất thường bên trong clip TTS nhưng không cắt lời.

    Một số lần edge-tts trả audio có gần một giây im sau từng từ. Chỉ gọi hàm
    này khi lớp trên đã phát hiện clip dài bất thường so với số ký tự; vì thế
    nhịp kể bình thường của các engine khác không bị can thiệp.
    """
    f = ("silenceremove=stop_periods=-1:"
         f"stop_duration={max(0.05, float(trigger_duration)):.3f}:"
         f"stop_threshold={int(threshold_db)}dB:"
         f"stop_silence={max(0.05, float(keep_silence)):.3f}:detection=rms")
    try:
        run(["ffmpeg", "-y", "-i", in_path, "-af", f,
             *_audio_encode_args(out_path), out_path], quiet=True)
        if os.path.exists(out_path) and os.path.getsize(out_path) > 512:
            return out_path
    except Exception:
        pass
    return in_path


def _timeline_pcm_gib(total_duration: float, sr: int,
                      channels: int = 2, bytes_per_sample: int = 2) -> float:
    return (max(0.0, float(total_duration)) * sr * channels * bytes_per_sample) / (1024 ** 3)


def _timeline_output_path(out_path: str, total_duration: float, sr: int) -> str:
    """Avoid plain WAV for long dub tracks.

    WAV is uncompressed, so a few hours of stereo 48 kHz audio can eat multiple
    GiB and fail on low-free-space drives even below the 4 GiB WAV danger zone.
    FLAC keeps silence tiny and is accepted by the render step.
    """
    if (os.path.splitext(out_path)[1].lower() == ".wav"
            and (float(total_duration or 0.0) >= 3600
                 or _timeline_pcm_gib(total_duration, sr) >= 1.0)):
        return os.path.splitext(out_path)[0] + ".flac"
    return out_path


def _audio_encode_args(out_path: str) -> List[str]:
    ext = os.path.splitext(out_path)[1].lower()
    if ext == ".flac":
        return ["-c:a", "flac", "-compression_level", "5"]
    if ext in (".m4a", ".aac"):
        return ["-c:a", "aac", "-b:a", "192k"]
    if ext == ".mp3":
        return ["-c:a", "libmp3lame", "-b:a", "192k"]
    if ext == ".wav":
        return ["-rf64", "auto", "-c:a", "pcm_s16le"]
    return ["-c:a", "pcm_s16le"]


def _mix_batch(items, total_ms, out_wav, sr, label="",
               window_start_ms: float = 0.0,
               clip_durations_ms: Optional[Dict[str, float]] = None):
    """Mix clips into one audio window.

    window_start_ms lets the FFmpeg fallback render short timeline windows instead
    of producing a full-duration WAV for every batch.
    """
    if label:
        log(f"  {label}: trộn {len(items)} clip...", "info")
    t0 = time.monotonic()
    window_ms = max(1.0, float(total_ms))
    clip_durations_ms = clip_durations_ms or {}
    prepared = []
    for path, start_ms in items:
        start_ms = float(start_ms)
        rel_ms = start_ms - float(window_start_ms)
        skip_ms = max(0.0, -rel_ms)
        delay_ms = max(0.0, rel_ms)
        if delay_ms >= window_ms:
            continue
        keep_ms = window_ms - delay_ms
        dur_ms = clip_durations_ms.get(path)
        if dur_ms is not None and dur_ms > 0:
            keep_ms = min(keep_ms, max(0.0, float(dur_ms) - skip_ms))
        if keep_ms <= 1.0:
            continue
        prepared.append((path, delay_ms, skip_ms, keep_ms))

    if not prepared:
        run(["ffmpeg", "-y", "-f", "lavfi", "-t", f"{window_ms / 1000.0:.3f}",
             "-i", f"anullsrc=r={sr}:cl=stereo",
             "-ac", "2", "-ar", str(sr), *_audio_encode_args(out_wav), out_wav])
        if label:
            log(f"  {label}: xong trong {time.monotonic() - t0:.1f}s.", "ok")
        return out_wav

    inputs = []
    for path, *_ in prepared:
        inputs += ["-i", path]
    parts, labels = [], []
    for i, (_, delay_ms, skip_ms, keep_ms) in enumerate(prepared):
        chain = (f"[{i}:a]aresample={sr},"
                 f"atrim=start={skip_ms / 1000.0:.3f}:duration={keep_ms / 1000.0:.3f},"
                 "asetpts=PTS-STARTPTS")
        d = max(0, int(round(delay_ms)))
        if d:
            chain += f",adelay={d}:all=1"
        parts.append(f"{chain}[a{i}]")
        labels.append(f"[a{i}]")
    if len(prepared) == 1:
        graph = parts[0].replace("[a0]", "[mixed]")
    else:
        graph = ";".join(parts) + ";" + "".join(labels) + \
            f"amix=inputs={len(prepared)}:normalize=0:dropout_transition=0[mixed]"
    # ép đúng độ dài cửa sổ (pad im lặng cuối, cắt nếu dư)
    graph += f";[mixed]apad,atrim=0:{window_ms/1000.0:.3f},asetpts=N/SR/TB[out]"
    cmd = ["ffmpeg", "-y", *inputs, "-filter_complex", graph,
           "-map", "[out]", "-ac", "2", "-ar", str(sr), *_audio_encode_args(out_wav), out_wav]
    run(cmd)
    if label:
        log(f"  {label}: xong trong {time.monotonic() - t0:.1f}s.", "ok")
    return out_wav


def _assemble_torch(
    items: List[tuple],
    total_duration: float,
    out_wav: str,
    sr: int = 48000,
) -> str:
    """Ghép timeline bằng torchaudio — KHÔNG spawn FFmpeg cho mỗi clip.

    Thuật toán:
      1. Tạo tensor zeros 2×N (stereo) kích thước đúng độ dài video.
      2. Load từng clip WAV vào tensor, cộng dồn tại offset đúng.
      3. Clamp [-1, 1] rồi save 1 lần duy nhất.

    Nhanh hơn FFmpeg filter_complex ~10-20× vì không có overhead spawn process.
    """
    import torch
    import torchaudio

    total_samples = int(math.ceil(total_duration * sr)) + sr  # thêm 1s đệm
    mixed = torch.zeros(2, total_samples, dtype=torch.float32)

    ok = 0
    for path, start_sec in items:
        try:
            wav, orig_sr = torchaudio.load(path)
        except Exception as e:
            log(f"  Bỏ qua clip lỗi {os.path.basename(path)}: {e}", "warn")
            continue
        # Resample nếu clip có sr khác
        if orig_sr != sr:
            wav = torchaudio.functional.resample(wav, orig_sr, sr)
        # Ép stereo
        if wav.shape[0] == 1:
            wav = wav.expand(2, -1)
        elif wav.shape[0] > 2:
            wav = wav[:2]
        start_s = max(0, int(start_sec * sr))
        end_s = start_s + wav.shape[1]
        if start_s >= total_samples:
            continue
        if end_s > total_samples:
            wav = wav[:, :total_samples - start_s]
            end_s = total_samples
        mixed[:, start_s:end_s] += wav
        ok += 1

    mixed = mixed.clamp(-1.0, 1.0)
    torchaudio.save(out_wav, mixed, sr)
    return out_wav


def _load_clip_for_stream(path: str, sr: int):
    import torchaudio

    wav, orig_sr = torchaudio.load(path)
    if orig_sr != sr:
        wav = torchaudio.functional.resample(wav, orig_sr, sr)
    if wav.shape[0] == 1:
        wav = wav.expand(2, -1)
    elif wav.shape[0] > 2:
        wav = wav[:2]
    return wav.numpy().astype("float32", copy=False)


def _assemble_stream_torch(
    items: List[tuple],
    total_duration: float,
    out_wav: str,
    sr: int = 48000,
    chunk_seconds: float = 30.0,
) -> str:
    """Ghép audio theo từng chunk để video nhiều giờ không ngốn hàng chục GB RAM."""
    import numpy as np
    import soundfile as sf

    items = sorted(items, key=lambda x: x[1])
    total_samples = int(math.ceil(total_duration * sr)) + sr
    chunk_samples = max(sr, int(chunk_seconds * sr))
    active = []  # (start_sample, end_sample, wav_2ch)
    next_i = 0
    ok = 0

    with sf.SoundFile(out_wav, mode="w", samplerate=sr, channels=2,
                      subtype="PCM_16") as writer:
        for chunk_start in range(0, total_samples, chunk_samples):
            chunk_end = min(total_samples, chunk_start + chunk_samples)
            while next_i < len(items):
                path, start_sec = items[next_i]
                start_sample = max(0, int(round(float(start_sec) * sr)))
                if start_sample >= chunk_end:
                    break
                next_i += 1
                if start_sample >= total_samples:
                    continue
                try:
                    wav = _load_clip_for_stream(path, sr)
                except Exception as e:
                    log(f"  Bỏ qua clip lỗi {os.path.basename(path)}: {e}", "warn")
                    continue
                end_sample = min(total_samples, start_sample + wav.shape[1])
                if end_sample <= chunk_start:
                    continue
                active.append((start_sample, end_sample, wav))
                ok += 1

            buf = np.zeros((2, chunk_end - chunk_start), dtype=np.float32)
            kept = []
            for start_sample, end_sample, wav in active:
                if end_sample <= chunk_start:
                    continue
                if start_sample < chunk_end:
                    ov_start = max(start_sample, chunk_start)
                    ov_end = min(end_sample, chunk_end)
                    src_a = ov_start - start_sample
                    src_b = ov_end - start_sample
                    dst_a = ov_start - chunk_start
                    dst_b = ov_end - chunk_start
                    buf[:, dst_a:dst_b] += wav[:, src_a:src_b]
                if end_sample > chunk_end:
                    kept.append((start_sample, end_sample, wav))
            active = kept

            np.clip(buf, -1.0, 1.0, out=buf)
            writer.write(buf.T)

    return out_wav


# Mỗi lệnh ffmpeg chỉ nối tối đa bấy nhiêu file. Windows giới hạn dòng lệnh
# ~32k ký tự; truyện 200k ký tự sinh ~800 clip TTS, nhét một lệnh là vỡ ngay.
_CONCAT_BATCH = 48


def _concat_audio_chunks(chunk_files: Sequence[str], out_path: str, sr: int,
                         work_dir: str,
                         normalize_loudness: bool = False) -> str:
    chunk_files = [p for p in chunk_files if p and os.path.exists(p)]
    if not chunk_files:
        run(["ffmpeg", "-y", "-f", "lavfi", "-t", "0.2",
             "-i", f"anullsrc=r={sr}:cl=stereo",
             "-ac", "2", "-ar", str(sr), *_audio_encode_args(out_path), out_path])
        return out_path

    if len(chunk_files) > _CONCAT_BATCH:
        # Nối PHÂN TẦNG: gộp từng nhóm 48 file thành file trung gian FLAC
        # (không mất chất lượng) rồi nối tiếp các file trung gian.
        parent = work_dir if work_dir and os.path.isdir(work_dir) \
            else (os.path.dirname(os.path.abspath(out_path)) or ".")
        with tempfile.TemporaryDirectory(prefix="_noi_tang_", dir=parent) as td:
            mids: List[str] = []
            for gi in range(0, len(chunk_files), _CONCAT_BATCH):
                mid = os.path.join(td, f"tang_{gi // _CONCAT_BATCH:04d}.flac")
                _concat_audio_chunks(chunk_files[gi:gi + _CONCAT_BATCH],
                                     mid, sr, td,
                                     normalize_loudness=normalize_loudness)
                mids.append(mid)
            # Các clip gốc đã được cân ở tầng dưới. Không loudnorm lại từng
            # khối trung gian vì sẽ làm thay đổi mức giữa các nhóm 48 câu.
            return _concat_audio_chunks(
                mids, out_path, sr, td, normalize_loudness=False)

    inputs: List[str] = []
    chains: List[str] = []
    labels: List[str] = []
    for i, path in enumerate(chunk_files):
        inputs += ["-i", path]
        label = f"a{i}"
        loudness = "loudnorm=I=-18:TP=-2:LRA=7," \
            if normalize_loudness else ""
        chains.append(
            f"[{i}:a]{loudness}aresample={sr},"
            "aformat=sample_fmts=s16:channel_layouts=stereo,"
            "asetpts=PTS-STARTPTS"
            f"[{label}]"
        )
        labels.append(f"[{label}]")
    graph = ";".join(chains)
    if len(chunk_files) == 1:
        graph += f";{labels[0]}anull[joined]"
    else:
        graph += ";" + "".join(labels) + \
            f"concat=n={len(chunk_files)}:v=0:a=1[joined]"
    # loudnorm đôi khi trả cả clip ngắn trong một AVFrame lớn hơn 65.535 mẫu.
    # FLAC không mã hoá được block lớn như vậy (thực tế đã gặp 69.104 mẫu ở
    # nhóm 48 câu). Chia lại frame trước encoder; p=0 không chèn thêm im lặng.
    graph += ";[joined]asetnsamples=n=4096:p=0[out]"

    try:
        run(["ffmpeg", "-y", *inputs, "-filter_complex", graph,
             "-map", "[out]", "-ac", "2", "-ar", str(sr),
             *_audio_encode_args(out_path), out_path])
    except Exception:
        try:
            if os.path.exists(out_path):
                os.remove(out_path)
        except OSError:
            pass
        raise
    return out_path


def concat_audio_clips(chunk_files: Sequence[str], out_path: str,
                       sr: int = 48000,
                       normalize_loudness: bool = False) -> str:
    """Nối tuần tự các clip thoại và chuẩn hoá chúng về một track âm thanh.

    Hàm public này phục vụ cả timeline lồng tiếng lẫn công cụ tạo audio từ văn
    bản. Dùng filter concat thay vì ghép byte nên MP3/WAV/M4A đầu vào có thể
    khác sample-rate hoặc số kênh. ``normalize_loudness=True`` cân từng clip
    về -18 LUFS, chặn đỉnh -2 dB trước khi nối; phù hợp khi nhiều giọng TTS có
    mức âm đầu ra khác nhau.
    """
    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    return _concat_audio_chunks(
        chunk_files, out_path, max(8000, int(sr or 48000)),
        os.path.dirname(os.path.abspath(out_path)),
        normalize_loudness=normalize_loudness)


def _assemble_ffmpeg_chunked(
    items_sec: List[tuple],
    total_duration: float,
    out_path: str,
    sr: int = 48000,
    batch: int = 60,
    chunk_seconds: float = 120.0,
) -> str:
    """FFmpeg fallback that mixes short time windows, not full-video batch WAVs."""
    chunk_ms = int(max(30.0, float(chunk_seconds or 120.0)) * 1000)
    items_ms = sorted([(p, float(s) * 1000.0) for p, s in items_sec], key=lambda x: x[1])
    durations_ms: Dict[str, float] = {}
    for path, _ in items_ms:
        if path not in durations_ms:
            try:
                durations_ms[path] = max(0.0, ffprobe_duration(path) * 1000.0)
            except Exception:
                durations_ms[path] = 0.0

    full_total_ms = int(math.ceil(total_duration * 1000)) + 200
    active_end_ms = 0.0
    for path, start_ms in items_ms:
        dur_ms = durations_ms.get(path, 0.0)
        if dur_ms <= 0:
            dur_ms = 1000.0
        active_end_ms = max(active_end_ms, start_ms + dur_ms)
    if active_end_ms > 0:
        total_ms = int(min(full_total_ms, math.ceil(active_end_ms) + 500))
        if total_ms + chunk_ms < full_total_ms:
            log(
                f"Track TTS có âm đến {total_ms / 1000 / 60:.1f} phút; "
                "đoạn im lặng cuối sẽ được đệm ở bước render để tiết kiệm ổ đĩa.",
                "info",
            )
    else:
        total_ms = min(full_total_ms, 1000)

    n_chunks = max(1, math.ceil(total_ms / chunk_ms))
    log(f"FFmpeg fallback streaming: {n_chunks} mảnh, mỗi mảnh tối đa {chunk_ms / 1000:.0f}s.", "info")
    tmp_parent = os.path.dirname(out_path) or "."
    chunk_ext = ".flac" if os.path.splitext(out_path)[1].lower() == ".flac" else ".wav"
    if os.path.exists(out_path):
        try:
            os.remove(out_path)
        except OSError:
            pass

    with tempfile.TemporaryDirectory(prefix="_mix_chunks_", dir=tmp_parent) as td:
        chunk_files: List[str] = []
        for ci in range(n_chunks):
            start_ms = ci * chunk_ms
            win_ms = min(chunk_ms, total_ms - start_ms)
            end_ms = start_ms + win_ms
            active = []
            for path, clip_start_ms in items_ms:
                dur_ms = durations_ms.get(path, 0.0)
                clip_end_ms = clip_start_ms + dur_ms if dur_ms > 0 else clip_start_ms + win_ms
                if clip_start_ms < end_ms and clip_end_ms > start_ms:
                    active.append((path, clip_start_ms))

            chunk_path = os.path.join(td, f"chunk_{ci:04d}{chunk_ext}")
            if len(active) <= max(1, batch):
                _mix_batch(active, win_ms, chunk_path, sr,
                           window_start_ms=start_ms,
                           clip_durations_ms=durations_ms)
            else:
                sub_files = []
                for si in range(0, len(active), max(1, batch)):
                    sub_path = os.path.join(td, f"chunk_{ci:04d}_sub_{si // max(1, batch):03d}{chunk_ext}")
                    _mix_batch(active[si:si + max(1, batch)], win_ms, sub_path, sr,
                               window_start_ms=start_ms,
                               clip_durations_ms=durations_ms)
                    sub_files.append(sub_path)
                _mix_batch([(p, 0.0) for p in sub_files], win_ms, chunk_path, sr)
                for sub_path in sub_files:
                    try:
                        os.remove(sub_path)
                    except OSError:
                        pass

            chunk_files.append(chunk_path)
            if n_chunks <= 20 or ci == 0 or ci + 1 == n_chunks or (ci + 1) % 10 == 0:
                log(f"  Mảnh {ci + 1}/{n_chunks}: {len(active)} clip.", "info")

        log(f"Gộp {len(chunk_files)} mảnh audio thành {os.path.basename(out_path)}...", "step")
        _concat_audio_chunks(chunk_files, out_path, sr, td)
    return out_path


def assemble_timeline_audio(
    clip_paths: List[str],
    placed_starts: List[float],
    total_duration: float,
    out_wav: str,
    sr: int = 48000,
    batch: int = 60,
    mode: str = "auto",
    chunk_seconds: float = 120.0,
) -> str:
    """Đặt từng clip TTS lên đúng mốc thời gian — ưu tiên torchaudio (nhanh 10-20×),
    fallback sang FFmpeg filter_complex nếu torchaudio không có.
    """
    items_sec = [(p, s) for p, s in zip(clip_paths, placed_starts)
                 if p and os.path.exists(p)]
    total_ms = int(math.ceil(total_duration * 1000)) + 200
    timeline_out = _timeline_output_path(out_wav, total_duration, sr)
    if timeline_out != out_wav:
        log(
            f"Track giọng Việt dài (~{_timeline_pcm_gib(total_duration, sr):.1f} GiB nếu WAV) "
            f"- lưu {os.path.basename(timeline_out)} để tránh giới hạn WAV 4GB.",
            "info",
        )
        if os.path.exists(out_wav):
            try:
                os.remove(out_wav)
            except OSError:
                pass

    if not items_sec:
        run(["ffmpeg", "-y", "-f", "lavfi", "-t", f"{total_duration + 0.2:.3f}",
             "-i", f"anullsrc=r={sr}:cl=stereo",
             "-ac", "2", "-ar", str(sr), *_audio_encode_args(timeline_out), timeline_out])
        return timeline_out

    t_total = time.monotonic()
    log(f"Ghép {len(items_sec)} clip lên timeline...", "step")

    est_gib = (max(0.0, total_duration) * sr * 2 * 4) / (1024 ** 3)
    mix_mode = str(mode or "auto").strip().lower()
    skip_torch = mix_mode in ("ffmpeg", "filter", "filter_complex", "safe", "wdac")
    if mix_mode in ("ram", "memory", "fast", "torch"):
        long_mix = False
    elif mix_mode in ("stream", "streaming", "low-ram"):
        long_mix = True
    else:
        long_mix = est_gib >= 14.0 or len(items_sec) >= 20000
    if skip_torch:
        log("Ghép audio bằng FFmpeg để tránh DLL tăng tốc bị Application Control chặn.", "info")
    elif long_mix:
        try:
            log(f"Ghép audio kiểu streaming (ước tính buffer RAM {est_gib:.1f} GiB).", "info")
            _assemble_stream_torch(items_sec, total_duration, timeline_out, sr)
            log(f"Ghép timeline (streaming) xong trong {time.monotonic() - t_total:.1f}s.", "ok")
            return timeline_out
        except ImportError:
            log("Thiếu torchaudio/soundfile cho streaming — dùng cách dự phòng.", "warn")
        except Exception as e:
            log(f"Ghép streaming lỗi ({e}) — dùng cách dự phòng.", "warn")

    # ── Đường nhanh: torchaudio ─────────────────────────────────────────────
    if not skip_torch:
        try:
            _assemble_torch(items_sec, total_duration, timeline_out, sr)
            log(f"Ghép timeline (torchaudio) xong trong {time.monotonic() - t_total:.1f}s.", "ok")
            return timeline_out
        except ImportError as e:
            log(f"torchaudio không dùng được ({e}) — dùng FFmpeg fallback.", "warn")
        except Exception as e:
            log(f"torchaudio lỗi ({e}) — thử streaming trước khi fallback FFmpeg.", "warn")
            try:
                _assemble_stream_torch(items_sec, total_duration, timeline_out, sr)
                log(f"Ghép timeline (streaming) xong trong {time.monotonic() - t_total:.1f}s.", "ok")
                return timeline_out
            except Exception as e2:
                log(f"streaming cũng lỗi ({e2}) — dùng FFmpeg fallback.", "warn")

    # ── Fallback: FFmpeg filter_complex (stream theo mảnh thời gian) ─────────
    items_ms = [(p, s * 1000.0) for p, s in items_sec]
    n_batches = math.ceil(len(items_ms) / batch)
    log(f"FFmpeg fallback: {n_batches} lô logic, mỗi lô tối đa {batch} clip.", "info")

    if len(items_ms) <= batch and timeline_out == out_wav:
        _mix_batch(items_ms, total_ms, timeline_out, sr, label="Lô 1/1")
        log(f"Ghép timeline (FFmpeg) xong trong {time.monotonic() - t_total:.1f}s.", "ok")
        return timeline_out

    _assemble_ffmpeg_chunked(items_sec, total_duration, timeline_out, sr,
                             batch=batch, chunk_seconds=chunk_seconds)
    log(f"Ghép timeline (FFmpeg) xong trong {time.monotonic() - t_total:.1f}s.", "ok")
    return timeline_out


def _ass_color(value: str, default: str = "&H00FFFFFF") -> str:
    """Đổi "#RRGGBB" (cách viết quen thuộc) sang mã màu của ASS: &HAABBGGRR.

    ASS đảo thứ tự byte (BGR) nên viết thẳng mã hex web vào là ra SAI MÀU -
    đỏ hoá xanh dương. Hàm này lo phần đó.
    """
    v = str(value or "").strip().lstrip("#")
    if not v:
        return default
    if v.upper().startswith("&H"):
        return v
    if len(v) == 3:
        v = "".join(c * 2 for c in v)
    if len(v) != 6:
        return default
    try:
        r, g, b = v[0:2], v[2:4], v[4:6]
        return f"&H00{b}{g}{r}".upper()
    except Exception:
        return default


def build_subtitle_style(style: Optional[dict] = None) -> str:
    """Dựng chuỗi force_style cho bộ lọc subtitles của ffmpeg."""
    st = dict(style or {})
    align_map = {
        "top-left": 7, "top-center": 8, "top-right": 9,
        "mid-left": 4, "mid-center": 5, "mid-right": 6,
        "bottom-left": 1, "bottom-center": 2, "bottom-right": 3,
    }
    align = align_map.get(str(st.get("align", "mid-center")), 5)
    parts = [
        f"FontName={st.get('font', 'Arial')}",
        f"FontSize={int(st.get('size', 22))}",
        f"PrimaryColour={_ass_color(st.get('color', '#FFFF00'))}",
        f"OutlineColour={_ass_color(st.get('outline_color', '#000000'), '&H00000000')}",
        f"BackColour={_ass_color(st.get('shadow_color', '#000000'), '&H00000000')}",
        f"BorderStyle={3 if st.get('box') else 1}",
        f"Outline={float(st.get('outline', 2))}",
        f"Shadow={float(st.get('shadow', 0))}",
        f"Bold={1 if st.get('bold', True) else 0}",
        f"Italic={1 if st.get('italic') else 0}",
        f"Alignment={align}",
        f"MarginV={int(st.get('margin_bottom', 30))}",
    ]
    return ",".join(parts)


def _ffmpeg_sub_path(srt_path: str) -> str:
    """Đưa file subtitle về một đường dẫn AN TOÀN cho bộ lọc subtitles.

    Bộ lọc này phân tích chuỗi nên dấu ':' của ổ đĩa, dấu '\\' và ký tự tiếng
    Trung trong tên file rất dễ làm vỡ lệnh. Chép ra thư mục tạm với tên thuần
    ASCII là hết chuyện.
    """
    import shutil
    import tempfile
    import uuid
    ext = os.path.splitext(srt_path)[1].lower() or ".srt"
    safe = os.path.join(tempfile.gettempdir(), f"autodub_hardsub_{os.getpid()}_{uuid.uuid4().hex[:8]}{ext}")
    try:
        shutil.copyfile(srt_path, safe)
    except Exception:
        safe = srt_path
    return safe.replace("\\", "/").replace(":", "\\:")


def render_final(
    video: str,
    dub_wav: str,
    out_path: str,
    blur_bottom_ratio: float = 0.0,
    blur_strength: int = 20,
    keep_original_db: Optional[float] = None,
    subtitle_srt: Optional[str] = None,
    delogo: Optional[str] = None,
    regions: Optional[Sequence[Dict]] = None,
    use_gpu: bool = True,
    crf: int = 20,
    subtitle_style: Optional[dict] = None,
    force_h264: bool = True,
    x264_preset: str = "superfast",
    cpu_threads: int = 4,
) -> str:
    """Render video cuối.

    blur_bottom_ratio > 0 : làm mờ một dải ở ĐÁY màn hình (che sub gốc), ví dụ 0.18
                            = 18% chiều cao dưới cùng. =0 thì không re-encode video (copy, siêu nhanh).
    keep_original_db      : None = thay hẳn audio gốc bằng lồng tiếng.
                            số âm (vd -18) = trộn audio gốc ở mức nhỏ làm nền nhạc.
    subtitle_srt          : nếu có -> hardsub phụ đề tiếng Việt vào hình.
    use_gpu               : dùng h264_nvenc (RTX 3060) nếu máy hỗ trợ.
    """
    gpu = use_gpu and has_nvenc()
    cpu_threads = max(1, int(cpu_threads or 4))
    need_reencode = blur_bottom_ratio > 0 or bool(subtitle_srt) or bool(delogo) or bool(regions)
    src_dur = ffprobe_duration(video)
    if src_dur <= 0:
        raise RuntimeError("Không đọc được thời lượng video gốc, dừng để tránh xuất file lỗi.")

    # Bilibili hay phát HEVC (H.265), có khi còn 10-bit. Chép nguyên luồng đó
    # sang MP4 thì file vẫn ĐÚNG CHUẨN nhưng Windows Photos / Movies & TV KHÔNG
    # mở được (báo "format is currently unsupported or the file is corrupted")
    # vì Windows không kèm sẵn bộ giải mã HEVC. Chuyển sang H.264 8-bit thì máy
    # nào, điện thoại nào, web nào cũng phát được.
    src_codec, src_pix = ffprobe_video_codec(video)
    ten_bit = "10" in src_pix or "12" in src_pix
    if force_h264 and not need_reencode and (src_codec not in ("h264", "") or ten_bit):
        need_reencode = True
        log(f"Video gốc là {src_codec.upper() or '?'}"
            + (f" {src_pix}" if ten_bit else "")
            + " - Windows Photos không mở được loại này. Đang chuyển sang H.264 "
              "cho mọi máy đều xem được (tắt bằng video.force_h264: false).", "info")

    inputs = ["-i", video, "-i", dub_wav]
    filters = []
    vlabel = "0:v"

    def _src():
        return vlabel if filters else "0:v"

    if blur_bottom_ratio > 0:
        r = min(0.6, max(0.02, blur_bottom_ratio))
        # crop dải đáy (crop hiểu iw/ih), làm mờ, rồi overlay lại ĐÁY.
        # overlay chỉ hiểu H (cao video chính) và h (cao lớp phủ) -> đặt y = H-h.
        crop_h = f"ih*{r:.4f}"
        crop_y = f"ih*{1.0 - r:.4f}"
        # avgblur thay cho gblur: cùng độ mờ nhìn bằng mắt (SSIM 0.99 so với
        # gblur=sigma=20) nhưng rẻ hơn ~2.6 lần vì nó cộng dồn theo hàng/cột,
        # chi phí không tăng theo bán kính. Trên chuỗi render đầy đủ (mờ + phụ
        # đề + x264) đo được nhanh hơn ~1.4 lần.
        filters.append(
            f"[0:v]crop=iw:{crop_h}:0:{crop_y},avgblur={max(1, int(blur_strength or 20))}[blur];"
            f"[0:v][blur]overlay=0:H-h[vb]"
        )
        vlabel = "vb"

    if delogo or regions:
        from . import overlays
        vw, vh = ffprobe_video_size(video)
        vw, vh = vw or 1280, vh or 720

    if delogo:
        # Xoá mờ logo cháy cứng ở góc. Định dạng "x:y:w:h" (pixel).
        try:
            x, y, w, h = [int(float(v)) for v in str(delogo).split(":")]
        except Exception:
            log(f"Bỏ qua delogo (định dạng phải là 'x:y:w:h'): {delogo!r}", "warn")
        else:
            d = overlays.clamp_delogo({"x": x, "y": y, "w": w, "h": h}, vw, vh)
            if not d:
                log(f"Bỏ qua delogo {delogo!r}: không nằm trong khung {vw}x{vh} "
                    "hoặc vùng quá nhỏ.", "warn")
            else:
                if (d["x"], d["y"], d["w"], d["h"]) != (x, y, w, h):
                    log(f"delogo {x}:{y}:{w}:{h} chạm/vượt mép khung {vw}x{vh} - "
                        f"đã co về {d['x']}:{d['y']}:{d['w']}:{d['h']} (ffmpeg cần "
                        "chừa viền quanh vùng).", "warn")
                filters.append(f"[{_src()}]delogo=x={d['x']}:y={d['y']}"
                               f":w={d['w']}:h={d['h']}[vd]")
                vlabel = "vd"

    if regions:
        rg_filters, rg_label = overlays.build_overlay_filters(
            regions, vw, vh, src_label=_src())
        if rg_filters:
            filters.extend(rg_filters)
            vlabel = rg_label

    if subtitle_srt:
        sub = _ffmpeg_sub_path(subtitle_srt)
        style = build_subtitle_style(subtitle_style)
        filters.append(f"[{_src()}]subtitles='{sub}':charenc=UTF-8:"
                       f"force_style='{style}'[vs]")
        vlabel = "vs"
        log(f"Ghi phụ đề Việt lên hình ({style.split(',')[2]})", "info")

    # Audio
    alabel = "1:a"
    if keep_original_db is not None:
        filters.append(
            f"[0:a]volume={keep_original_db}dB[bg];"
            f"[1:a]apad,atrim=0:{src_dur:.3f},asetpts=N/SR/TB[dubpad];"
            f"[bg][dubpad]amix=inputs=2:duration=first:dropout_transition=0,"
            f"apad,atrim=0:{src_dur:.3f},asetpts=N/SR/TB[aout]"
        )
        alabel = "aout"
    else:
        filters.append(
            f"[1:a]apad,atrim=0:{src_dur:.3f},asetpts=N/SR/TB[aout]")
        alabel = "aout"

    src_fps = ffprobe_fps(video)
    src_w, src_h = ffprobe_video_size(video)

    def _video_args(use_nvenc: bool, hw: str):
        if not need_reencode:
            return ["-c:v", "copy"]
        if use_nvenc:
            # hw=full: khung chưa rời GPU nên -pix_fmt (đổi màu CPU) không áp được.
            return nvenc_encode_args(
                crf, pix_fmt=None if hw == "full" else "yuv420p",
                fps=src_fps, width=src_w, height=src_h)
        v = ["-c:v", "libx264", "-preset", str(x264_preset or "superfast"),
             "-crf", str(crf), "-threads", str(cpu_threads)]
        if hw == "full":
            return v + ["-profile:v", "high"]
        # yuv420p + profile high: mẫu số chung mà MỌI trình phát đều đọc được
        # (nguồn 10-bit sẽ được hạ về 8-bit ở đây).
        return v + ["-pix_fmt", "yuv420p", "-profile:v", "high"]

    tail = ["-c:a", "aac", "-b:a", "192k", "-ac", "2",
            "-movflags", "+faststart",      # cho phép phát/tua ngay khi chưa tải xong
            out_path]

    def _build_cmd(use_nvenc: bool, hw: str):
        """Dựng lệnh ffmpeg. hw = 'none' hoặc 'full'.

        'full' = khung hình nằm nguyên trên GPU từ lúc giải mã tới lúc mã
        hoá, không copy qua lại RAM lần nào. Đo trên RTX 3060 với clip 1080p
        3 phút: đổi HEVC sang H.264 mất 15.0s theo đường thường, còn 10.0s
        theo đường này.

        Chỉ dùng được khi KHÔNG có filter hình nào. Đã thử cả cách chỉ bật
        '-hwaccel cuda' rồi tải khung hình về RAM cho filter chạy: cùng clip
        đó, mờ đáy + phụ đề mất 26.7s không hwaccel nhưng 40.8s khi bật, vì
        tiền copy GPU->RAM->GPU đắt hơn tiền giải mã tiết kiệm được. Nên
        đường đó đã bị bỏ.
        """
        c = ["ffmpeg", "-y"]
        if hw == "full":
            c += ["-hwaccel", "cuda", "-hwaccel_output_format", "cuda"]
        c += inputs
        fl = list(filters)
        vlab = vlabel
        if hw == "full":
            fl.append("[0:v]scale_cuda=format=nv12[vhw]")
            vlab = "vhw"
        if fl:
            c += ["-filter_complex", ";".join(fl)]
        # vlabel chỉ là NHÃN filtergraph khi đã có filter VIDEO (blur/delogo/sub).
        # Nếu chỉ có filter AUDIO (vd bật keep_original_db mà tắt hardsub/blur/delogo)
        # thì vlabel vẫn là "0:v" - phải map THẲNG luồng gốc, KHÔNG bọc ngoặc vuông:
        # "[0:v]" là nhãn filtergraph không tồn tại -> ffmpeg báo lỗi và render chết.
        c += ["-map", f"[{vlab}]" if vlab != "0:v" else "0:v"]
        c += ["-map", f"[{alabel}]" if alabel == "aout" else alabel]
        return c + _video_args(use_nvenc, hw) + tail

    render_timeout = int(max(7200, src_dur * 1.5 + 1800)) if need_reencode \
        else int(max(3600, min(21600, src_dur * 0.20 + 1800)))

    # Chỉ hỏi GPU khi thật sự sắp mã hoá lại: lúc chỉ copy luồng thì không có
    # gì để giải mã, mà lúc có filter hình thì đường GPU lại chậm hơn.
    co_filter_hinh = vlabel != "0:v"
    if gpu and need_reencode and not co_filter_hinh and has_cuda_decode():
        bac_hw = ["full", "none"]
    else:
        bac_hw = ["none"]

    log("Render " + ("(GPU NVENC)" if gpu and need_reencode
                     else "(copy video)" if not need_reencode
                     else f"(CPU x264, threads={cpu_threads})")
        + (", giải mã luôn trên GPU" if bac_hw[0] == "full" else "") + " ...",
        "step")

    def _run_render(use_nvenc: bool, hw: str = "none"):
        """Chạy FFmpeg render với hiển thị tiến độ % (dựa trên thời gian đã xử lý)."""
        rcmd = _build_cmd(use_nvenc, hw)
        # Không có thời lượng hoặc video ngắn -> chạy bình thường không cần progress
        if src_dur < 1.0:
            run(rcmd, quiet=True, timeout=render_timeout)
            return

        # Chạy FFmpeg với -progress pipe:1 để đọc tiến độ realtime
        rcmd_prog = rcmd[:1] + ["-progress", "pipe:1"] + rcmd[1:]
        t_render = time.monotonic()
        last_pct = -1
        loi_ffmpeg = None
        err_tail = []

        def _render_line(raw: str) -> None:
            nonlocal last_pct
            line = str(raw or "").strip()
            if line.startswith("out_time_us="):
                try:
                    us = int(line.split("=", 1)[1])
                    pct = min(100, int(us / (src_dur * 1_000_000) * 100))
                    if pct >= last_pct + 5:
                        elapsed = time.monotonic() - t_render
                        if pct > 0:
                            eta = elapsed / pct * (100 - pct)
                            log(f"  Render: {pct}% | đã {elapsed:.0f}s | còn ~{eta:.0f}s", "info")
                        else:
                            log(f"  Render: {pct}% | đã {elapsed:.0f}s", "info")
                        last_pct = pct
                except (ValueError, ZeroDivisionError):
                    pass
            elif line:
                err_tail.append(line)
                del err_tail[:-80]

        try:
            result = run(rcmd_prog, check=False, quiet=True,
                         timeout=render_timeout, line_callback=_render_line)
            if result.returncode != 0:
                err = "\n".join(err_tail)[-2000:]
                # Ghi lại rồi ném ở NGOÀI khối try: ném ngay tại đây sẽ rơi
                # vào nhánh except bên dưới và render lại lần nữa cho một lệnh
                # đã biết chắc là hỏng.
                loi_ffmpeg = RuntimeError(
                    f"FFmpeg render lỗi ({result.returncode}):\n{err}")
        except InterruptedError:
            raise
        except FileNotFoundError:
            raise
        except Exception:
            # Fallback: lỗi pipe hoặc timeout -> chạy lại bình thường
            run(rcmd, quiet=True, timeout=render_timeout)

        if loi_ffmpeg is not None:
            raise loi_ffmpeg
        log(f"  Render xong trong {time.monotonic() - t_render:.1f}s.", "ok")

    def _loi_chuoi_filter(e: Exception) -> bool:
        """Sai sót trong chuỗi filter thì đổi encoder cũng hỏng y hệt."""
        return any(s in str(e) for s in ("Logo area is outside of the frame",
                                         "Error reinitializing filters",
                                         "Failed to configure",
                                         "Error initializing filter"))

    loi_cuoi = None
    for hw in bac_hw:
        try:
            _run_render(gpu, hw)
            loi_cuoi = None
            break
        except FileNotFoundError:
            raise
        except Exception as e:
            loi_cuoi = e
            if hw == "none":
                break
            # Máy nào không nuốt được đường GPU thì lùi một bậc. Những lỗi này
            # lộ ra ngay lúc dựng chuỗi filter nên không tốn mấy giây.
            log(f"Đường GPU '{hw}' không chạy được "
                f"({str(e).splitlines()[-1][:100]}) - thử lại bậc thấp hơn...",
                "warn")
    if loi_cuoi is not None:
        if not (gpu and need_reencode) or _loi_chuoi_filter(loi_cuoi):
            raise loi_cuoi
        # NVENC hay từ chối vài loại nguồn (10-bit, độ phân giải lạ, driver cũ).
        # Thà render bằng CPU chậm hơn còn hơn không ra file.
        log(f"NVENC không render được ({str(loi_cuoi).splitlines()[0][:100]}) - "
            "chuyển sang CPU x264...", "warn")
        _run_render(False)

    # Kiểm tra thành phẩm: có hình, có tiếng, đúng độ dài. Thà báo ngay còn hơn
    # để người dùng mở file rồi mới phát hiện hỏng.
    out_codec, out_pix = ffprobe_video_codec(out_path)
    out_dur = ffprobe_duration(out_path)
    src_dur = ffprobe_duration(video)
    max_drift = max(2.0, src_dur * 0.05)
    if not out_codec:
        raise RuntimeError("File xuất ra KHÔNG có luồng hình - render hỏng.")
    elif src_dur > 0 and out_dur + max_drift < src_dur:
        raise RuntimeError(
            f"File xuất bị cắt cụt: {out_dur:.1f}s trong khi video gốc {src_dur:.1f}s.")
    elif src_dur > 0 and abs(out_dur - src_dur) > max_drift:
        log(f"File xuất ra dài {out_dur:.1f}s trong khi video gốc {src_dur:.1f}s "
            "- có thể bị cắt cụt.", "warn")
    elif out_codec == "h264" and out_pix == "yuv420p":
        log(f"Video cuối: H.264 8-bit, {out_dur:.1f}s - phát được trên Windows "
            "Photos / điện thoại / web.", "ok")
    else:
        log(f"Video cuối: {out_codec.upper()} {out_pix}, {out_dur:.1f}s. Lưu ý "
            "Windows Photos có thể KHÔNG mở được - dùng VLC, hoặc bật "
            "video.force_h264: true.", "warn")
    return out_path
