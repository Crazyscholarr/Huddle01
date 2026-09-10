"""Tự dò vùng phụ đề CHÁY CỨNG trong video (sub tiếng Trung dính vào hình).

Cách làm (không cần OpenCV):
  1. Lấy mẫu N khung hình rải đều, thu nhỏ về xám, đọc thẳng bằng ffmpeg.
  2. Chữ phụ đề có ĐỘ TƯƠNG PHẢN NGANG rất cao (viền chữ) -> đếm điểm ảnh có
     gradient ngang mạnh cho từng hàng.
  3. Chữ còn NHẤP NHÁY theo thời gian (câu này đổi sang câu khác) trong khi nền
     thường ổn định hơn -> nhân thêm độ lệch chuẩn theo thời gian.
  4. Lấy dải hàng liên tiếp có điểm số cao nhất ở nửa dưới khung hình.

Nếu thiếu numpy thì trả về gợi ý mặc định (dải đáy) thay vì báo lỗi.
"""
from __future__ import annotations

import os
import subprocess
import time
from typing import Dict, Optional

from .utils import (log, ffprobe_duration, ffprobe_video_size, _NO_WINDOW,
                    active_cancel_event, register_running_process,
                    unregister_running_process)
from .overlays import suggest_subtitle_band


def _grab_gray_frames(video: str, n: int, width: int, height: int,
                      duration: float):
    """Trả về mảng numpy (n, height, width) ảnh xám, hoặc None.

    Lấy các khung SONG SONG 6 luồng: mỗi khung là một lần ffmpeg tua + giải mã
    (~1-2s với phim dài), 24 khung chạy tuần tự từng làm nút "Tự dò sub cứng"
    mất 30-60 giây.
    """
    try:
        import numpy as np
    except ImportError:
        return None
    from concurrent.futures import ThreadPoolExecutor, as_completed

    start = duration * 0.08
    end = duration * 0.92
    if end <= start:
        start, end = 0.0, max(0.1, duration)
    step = (end - start) / max(1, n - 1) if n > 1 else 0.0

    def _grab(i: int):
        t = start + step * i
        cmd = ["ffmpeg", "-nostdin", "-v", "error", "-ss", f"{t:.3f}",
               "-i", video, "-frames:v", "1",
               "-vf", f"scale={width}:{height},format=gray",
               "-f", "rawvideo", "-"]
        token = active_cancel_event("ffmpeg")
        proc = None
        try:
            proc = subprocess.Popen(
                cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, creationflags=_NO_WINDOW)
            register_running_process(proc, cancel_event=token, resource="ffmpeg")
            deadline = time.monotonic() + 30.0
            while True:
                if token is not None and token.is_set():
                    proc.terminate()
                    raise InterruptedError("Đã hủy dò phụ đề cứng.")
                try:
                    stdout, _stderr = proc.communicate(timeout=.25)
                    break
                except subprocess.TimeoutExpired:
                    if time.monotonic() >= deadline:
                        proc.kill()
                        return i, None
            if token is not None and token.is_set():
                raise InterruptedError("Đã hủy dò phụ đề cứng.")
            if proc.returncode != 0:
                return i, None
        except InterruptedError:
            raise
        except Exception:
            return i, None
        finally:
            if proc is not None:
                unregister_running_process(proc)
        buf = stdout or b""
        if len(buf) < width * height:
            return i, None
        arr = np.frombuffer(buf[:width * height], dtype=np.uint8)
        return i, arr.reshape(height, width).astype(np.float32)

    got = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for fut in as_completed([pool.submit(_grab, i) for i in range(n)]):
            i, arr = fut.result()
            if arr is not None:
                got[i] = arr

    # Giữ đúng THỨ TỰ THỜI GIAN: điểm "nhấp nháy" tính bằng std theo trục
    # thời gian nên khung phải xếp theo mốc, không phải theo thứ tự hoàn thành.
    frames = [got[i] for i in sorted(got)]
    if len(frames) < 2:
        return None
    return np.stack(frames)


def detect_hardsub_region(
    video: str,
    samples: int = 24,
    search_top_ratio: float = 0.45,
    pad: int = 8,
) -> Dict:
    """Trả về vùng {"x","y","w","h","type":"blur","strength"} bao quanh sub cứng.

    search_top_ratio: chỉ tìm từ mốc này trở xuống (0.45 = nửa dưới khung hình),
    vì phụ đề gần như luôn nằm ở phía dưới.
    """
    vw, vh = ffprobe_video_size(video)
    if vw <= 0 or vh <= 0:
        return suggest_subtitle_band(1280, 720)

    dur = ffprobe_duration(video)
    if dur <= 0:
        return suggest_subtitle_band(vw, vh)

    SW = 320
    SH = max(60, int(round(SW * vh / vw)))
    SH -= SH % 2

    try:
        import numpy as np
    except ImportError:
        log("Không có numpy nên không tự dò được - dùng dải đáy mặc định.", "warn")
        return suggest_subtitle_band(vw, vh)

    stack = _grab_gray_frames(video, samples, SW, SH, dur)
    if stack is None:
        log("Không lấy được khung hình mẫu - dùng dải đáy mặc định.", "warn")
        return suggest_subtitle_band(vw, vh)

    # (1) độ tương phản ngang: |I[x+1] - I[x]|
    grad = np.abs(np.diff(stack, axis=2))               # (n, SH, SW-1)
    edge_rows = (grad > 28).sum(axis=2).astype(np.float32)   # (n, SH)
    edge_score = edge_rows.mean(axis=0)                  # (SH,)

    # (2) nhấp nháy theo thời gian
    temporal = stack.std(axis=0).mean(axis=1)            # (SH,)

    score = edge_score * (1.0 + temporal / (temporal.max() + 1e-6))

    y0 = int(SH * max(0.0, min(0.9, search_top_ratio)))
    region_score = score[y0:]
    if region_score.size == 0:
        return suggest_subtitle_band(vw, vh)

    thr = max(region_score.mean() + region_score.std() * 0.8,
              region_score.max() * 0.45)
    hot = region_score >= thr
    if not hot.any():
        log("Không thấy vùng chữ rõ rệt - dùng dải đáy mặc định.", "warn")
        return suggest_subtitle_band(vw, vh)

    # dải liên tiếp dài nhất
    best_len = best_start = cur_len = 0
    cur_start = None
    for i, v in enumerate(hot):
        if v:
            if cur_start is None:
                cur_start = i
            cur_len += 1
            if cur_len > best_len:
                best_len, best_start = cur_len, cur_start
        else:
            cur_start, cur_len = None, 0

    ry0 = y0 + best_start
    ry1 = ry0 + best_len

    # (3) giới hạn bề ngang: cột nào có chữ
    band = stack[:, ry0:ry1, :]
    cgrad = np.abs(np.diff(band, axis=2))
    col = (cgrad > 28).sum(axis=(0, 1)).astype(np.float32)
    if col.size and col.max() > 0:
        cthr = col.max() * 0.18
        cols = np.where(col >= cthr)[0]
        cx0, cx1 = (int(cols[0]), int(cols[-1]) + 2) if cols.size else (0, SW)
    else:
        cx0, cx1 = 0, SW

    sx, sy = vw / SW, vh / SH
    x = max(0, int(cx0 * sx) - pad * 2)
    w = min(vw - x, int((cx1 - cx0) * sx) + pad * 4)
    y = max(0, int(ry0 * sy) - pad)
    h = min(vh - y, int((ry1 - ry0) * sy) + pad * 2)

    w -= w % 2
    h -= h % 2
    if w < 40 or h < 16:
        return suggest_subtitle_band(vw, vh)

    log(f"Dò được vùng sub cứng: x={x} y={y} w={w} h={h}", "ok")
    return {"x": x, "y": y, "w": w, "h": h, "type": "blur", "strength": 22}
