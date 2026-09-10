"""Backend cho giao diện đồ hoạ AutoDubVN.

Chạy một máy chủ HTTP nhỏ (chỉ dùng thư viện chuẩn của Python) rồi mở giao diện
trong trình duyệt sẵn có. Không cài thêm .exe nào -> không bị Device Guard chặn.

API chính:
    GET  /                     giao diện
    GET  /api/state            trạng thái hàng đợi + tiến độ
    POST /api/queue/add        {path} hoặc {url}
    POST /api/queue/remove     {id}
    GET  /api/project?id=      dự án (vùng phủ, kiểu phụ đề, các dòng)
    POST /api/project          lưu dự án
    GET  /api/video?id=        phát video (hỗ trợ Range để tua)
    POST /api/detect_sub       {id} tự dò vùng sub cứng
    GET  /api/preview?id=&t=   khung hình đã áp 3 lớp (PNG)
    POST /api/run              {id, steps:[...]} chạy pipeline
    POST /api/cancel

Gói này từng là MỘT file server.py ~2.700 dòng; nay tách theo trách nhiệm:
    state.py       trạng thái chung (queue, progress, khoá luồng) + log
    helpers.py     tiện ích tên file an toàn, đọc file văn bản, dọn file tạm
    config_api.py  đọc/ghi mục translation/tts của config.yaml cho GUI
    projects.py    dữ liệu dự án + quy đổi dòng/khoảng thời gian (span)
    render.py      xem trước + xuất video (copy/NVENC/MP4Box/chia phần)
    pipeline.py    chạy 4 bước ASR -> dịch -> TTS -> render
    manual_api.py  luồng kể chuyện (TTS văn bản, nhạc nền, slideshow, ghép audio)
    http_api.py    HTTP server + routing

Mọi cách import cũ vẫn chạy nguyên: `from autodub import server; server.serve()`.
"""
from .state import (HERE, UI_DIR, CONFIG_PATH, STATE, PROJECTS, REV,
                    _LOCK, _NEXT_ID, _CANCEL_EVENT,
                    JOB_MANAGER, current_cancel_event, submit_job,
                    shutdown_background_jobs,
                    bump_rev, _log, _progress, _find)
from .helpers import (_safe_path_stem, _output_stem_for_video,
                      _output_dir_for_video, _doc_file_van_ban,
                      _call_filtered, _float_or_none, _fmt_span_time,
                      _find_existing_dub_audio, _path_under,
                      _cleanup_temp_files)
from .config_api import (_load_cfg, _TRANSLATION_GUI_KEYS,
                         _translation_cfg_for_gui, _tts_cfg_for_gui,
                         _yaml_scalar, _save_translation_cfg,
                         _translation_api_params, _test_translation_api)
from .projects import (PROJECT_STATE_KEYS, default_project, get_project,
                       _project_state_path, _load_project_state,
                       _save_project_state, _segments_from_project,
                       _split_project_vi_on_punctuation, _polish_project_vi,
                       _split_project_src_on_punctuation,
                       _prepare_project_src_for_translation,
                       _load_existing_segments_into_project,
                       _active_media_span, _run_stem_for_project,
                       _project_rows_for_span, _segments_from_rows,
                       _rows_from_segments, _sync_project_rows_for_span,
                       _load_local_rows_from_srt, _regions_for_span,
                       _render_project_for_span)
from .render import (render_preview, render_with_layers,
                     render_with_layers_chunked, _RENDER_PART_CACHE_VERSION,
                     _render_timeout_seconds, _is_timeout_error,
                     _render_gpu_then_cpu, _render_mp4box_replace_audio,
                     _chunk_bounds, _local_regions_for_chunk,
                     _local_segments_for_chunk, _file_cache_signature,
                     _stable_cache_hash, _render_part_cache_key,
                     _concat_mp4_parts)
from .pipeline import run_pipeline
from .http_api import Handler, QuietServer, serve
