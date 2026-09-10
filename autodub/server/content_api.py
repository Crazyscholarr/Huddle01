"""API cho kho ý tưởng và kế hoạch sản xuất nội dung YouTube.

Tác vụ nhập/phân tích/xuất chạy trong thread nền. Mỗi kết quả phân tích được
ghi ngay vào SQLite để mất mạng hoặc đóng ứng dụng cũng không mất phần đã làm.
"""
from __future__ import annotations

import asyncio
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, Iterable, Tuple

from ..content_pipeline import (
    ContentStore, analyze_many, analyze_record, append_content_calendar,
    canonical_source_url, export_plan, heuristic_analysis, load_source,
    normalize_record, public_record, record_was_used, sample_records,
)
from .config_api import _load_cfg
from .state import HERE, STATE, _LOCK, submit_job, _log


JsonResult = Tuple[Dict, int]


def _resolve_path(value: str, default: str) -> str:
    raw = os.path.expandvars(str(value or default).strip().strip('"'))
    return os.path.abspath(raw if os.path.isabs(raw) else os.path.join(HERE, raw))


def _settings() -> Dict:
    cfg = _load_cfg()
    cp = cfg.get("content_pipeline") if isinstance(cfg.get("content_pipeline"), dict) else {}
    return {
        "config": cfg,
        "database": _resolve_path(cp.get("database", ""), "data/content_ideas.sqlite"),
        "output_dir": _resolve_path(cp.get("output_dir", ""), "output/content_plans"),
        "provider": str(cp.get("provider") or "auto").lower(),
        "concurrency": max(1, min(8, int(cp.get("max_concurrency") or 3))),
    }


def _store() -> ContentStore:
    return ContentStore(_settings()["database"])


def _sync_content_calendar(records: Iterable[Dict], settings: Dict = None,
                           final_titles: Dict[str, str] = None,
                           status: str = "") -> str:
    """Ghi lịch một lần cho mỗi ý tưởng; file khóa sẽ chuyển sang bản dự phòng."""
    rows = [dict(item) for item in records if item]
    if not rows:
        return ""
    current = settings or _settings()
    cfg = current.get("config") if isinstance(current.get("config"), dict) else {}
    cp = cfg.get("content_pipeline") if isinstance(
        cfg.get("content_pipeline"), dict) else {}
    target = _resolve_path(
        cp.get("calendar_file", ""),
        os.path.join(str(current.get("output_dir") or "output/content_plans"),
                     "lich_noi_dung_goc_mit.xlsx"))
    duration = str(cp.get("target_duration") or "1h00 - 1h30")
    chosen = final_titles or {}
    for item in rows:
        target = append_content_calendar(
            item, target,
            final_title=str(chosen.get(str(item.get("id") or "")) or ""),
            target_duration=duration,
            status=str(status or item.get("status") or "Chưa làm"))
    return target


def _backfill_legacy_analysis(store: ContentStore, items: Iterable[Dict]) -> list:
    """Bù schema mới cho bài đã tải trước khi có hồ sơ viết lại.

    Chỉ bù các bản heuristic/offline; kết quả AI cũ thiếu trường sẽ được phân
    tích AI lại khi người dùng bấm viết, tránh gắn nhầm hồ sơ chung vào bản AI.
    """
    out = []
    keys = ("title_localized", "main_hook", "high_tension_scenes",
            "plot_twists", "must_change", "rewrite_brief")
    for source in items:
        item = dict(source)
        provider = str(item.get("analysis_provider") or "").lower()
        is_offline = not provider or any(
            token in provider for token in ("heuristic", "offline", "fallback"))
        changed = False
        if is_offline:
            fallback = heuristic_analysis(item)
            for key in keys:
                if not item.get(key) and fallback.get(key):
                    item[key] = fallback[key]
                    changed = True
            if len(list(item.get("titles") or [])) != 8:
                item["titles"] = fallback["titles"]
                changed = True
        if changed:
            store.upsert([item], overwrite=True)
        out.append(item)
    return out


def _state(**values) -> None:
    with _LOCK:
        state = STATE["content_pipeline"]
        state.update(values)
        state["rev"] = int(state.get("rev", 0)) + 1


def _activity(message: str, kind: str = "info", stage: str = "",
              item: str = "") -> None:
    """Ghi nhật ký ngắn cho riêng Kho ý tưởng để giao diện đọc realtime."""
    with _LOCK:
        state = STATE["content_pipeline"]
        rows = list(state.get("activity") or [])
        rows.append({"t": time.time(), "kind": str(kind or "info"),
                     "stage": str(stage or ""), "item": str(item or "")[:90],
                     "message": str(message or "")[:700]})
        state["activity"] = rows[-80:]
        state["rev"] = int(state.get("rev", 0)) + 1
    # Cùng dòng này xuất hiện ở cửa sổ CMD để người dùng vẫn theo dõi được khi
    # thu nhỏ giao diện hoặc khi WebView chưa kịp vẽ trạng thái mới.
    console_kind = "warn" if kind == "warning" else kind
    _log(f"Kho ý tưởng [{stage or 'tiến trình'}]: {message}", console_kind)


def _begin(active: str, status: str) -> bool:
    with _LOCK:
        state = STATE["content_pipeline"]
        if state.get("working"):
            return False
        state.update({
            "working": True, "active": active, "status": status,
            "progress": 0.0, "done": 0, "total": 0, "error": "",
            "warning": "", "activity": [], "current_stage": "prepare",
            "current_item": "", "started_at": time.time(),
            "ai_success": 0, "ai_failed": 0, "provider": "",
            "provider_model": "", "provider_configured": False,
            "provider_offline": False,
            "rev": int(state.get("rev", 0)) + 1,
        })
    return True


def _finish(status: str, error: str = "", **values) -> None:
    values.update({"working": False, "active": "", "status": status,
                   "error": str(error or "")[:500]})
    _state(**values)


def _configured_provider(cfg: Dict, provider: str) -> bool:
    try:
        from ..content_pipeline import _provider_params
        name, key, _model, _base, _timeout = _provider_params(cfg, provider)
        return name in {"browser", "perplexity_browser"} or bool(key)
    except Exception:
        return False


def _provider_status(cfg: Dict, provider: str) -> Dict:
    """Thông tin provider an toàn để hiện UI; tuyệt đối không trả API key."""
    try:
        from ..content_pipeline import _provider_params
        name, key, model, _base, _timeout = _provider_params(cfg, provider)
        browser = name in {"browser", "perplexity_browser"}
        offline = name in {"", "none", "heuristic", "offline"}
        return {"provider": name or "heuristic", "provider_model": model or "",
                "provider_configured": browser or (bool(key) and not offline),
                "provider_offline": offline}
    except Exception:
        return {"provider": str(provider or "auto"), "provider_model": "",
                "provider_configured": False, "provider_offline": False}


def api_content_list(query: Dict) -> JsonResult:
    try:
        search = str((query.get("search") or [""])[0])
        selected = str((query.get("selected") or ["0"])[0]).lower() in {"1", "true", "yes"}
        limit = int((query.get("limit") or ["1000"])[0] or 1000)
        store = _store()
        items = _backfill_legacy_analysis(
            store, store.list(search=search, selected_only=selected, limit=limit))
        settings = _settings()
        return {
            "items": [public_record(x) for x in items],
            "count": len(items),
            "selected_count": sum(1 for x in items if x.get("selected")),
            "database": settings["database"],
            "output_dir": settings["output_dir"],
            "default_provider": settings["provider"],
            "provider_configured": _configured_provider(
                settings["config"], settings["provider"]),
        }, 200
    except Exception as exc:
        return {"error": str(exc)}, 500


def api_content_item(query: Dict) -> JsonResult:
    record_id = str((query.get("id") or [""])[0]).strip()
    store = _store()
    found = store.get(record_id)
    item = (_backfill_legacy_analysis(store, [found])[0] if found else None)
    return (({"item": public_record(item, include_content=True)}, 200)
            if item else ({"error": "Không tìm thấy ý tưởng."}, 404))


def api_content_catalog(_query: Dict = None) -> JsonResult:
    """Nguồn và từ khóa dùng ở màn Tìm & tải nội dung."""
    try:
        from .. import story_sources
        return {"sources": story_sources.reference_catalog(),
                "chinese_keywords": story_sources.chinese_keyword_catalog()}, 200
    except Exception as exc:
        return {"error": str(exc)}, 500


def api_content_search(body: Dict) -> JsonResult:
    keyword = str(body.get("keyword") or "").strip()
    if not keyword:
        return {"error": "Hãy nhập từ khóa tìm chuyện."}, 400
    source_keys = body.get("source_keys") or ["zhihu_yanxuan"]
    if isinstance(source_keys, str):
        source_keys = [x.strip() for x in source_keys.split(",") if x.strip()]
    try:
        limit = max(1, min(50, int(body.get("limit") or 20)))
    except (TypeError, ValueError):
        limit = 20
    if not _begin("source_search", "Đang tìm chuyện thật trên các nguồn đã chọn…"):
        return {"error": "Kho ý tưởng đang chạy một tác vụ khác."}, 409
    _state(search_keyword=keyword, search_source_keys=list(source_keys),
           search_results=[], total=len(source_keys), done=0,
           download_errors=[], download_success=0, download_failed=0)

    def work() -> None:
        try:
            from .. import story_sources
            rows = story_sources.search_web_references(keyword, source_keys, limit)
            topic = str(body.get("topic") or "").strip()
            meaning = str(body.get("meaning") or "").strip()
            for row in rows:
                row.update({"keyword": keyword, "topic": topic, "meaning": meaning})
            if rows:
                _state(total=len(rows), done=0, progress=35,
                       status=f"Đã tìm thấy {len(rows)} link; đang đọc lượt xem và kiểm tra nội dung…")

                def enrich_progress(done: int, total: int, item: Dict) -> None:
                    title = str(item.get("title") or "")[:60]
                    _state(done=done, total=total,
                           progress=round(35 + done * 60 / max(1, total), 1),
                           status=f"Đang xếp hạng {done}/{total}: {title}")

                rows = story_sources.enrich_reference_rows(
                    rows, max_workers=4, progress=enrich_progress,
                    query=keyword)
            history = _store().source_history([row.get("url") or "" for row in rows])
            for row in rows:
                previous = history.get(canonical_source_url(row.get("url") or "")) or {}
                row.update({"selected": False,
                            "already_downloaded": bool(previous),
                            "existing_id": previous.get("id") or "",
                            "used_before": bool(previous.get("used")),
                            "used_at": previous.get("used_at") or "",
                            "download_status": ("Đã dùng" if previous.get("used")
                                                else "Đã có trong kho" if previous
                                                else "Chưa tải")})
            if not rows:
                _finish("Không tìm thấy bài phù hợp; không dùng dữ liệu mẫu thay thế.",
                        progress=100, done=0, total=0, search_results=[])
                return
            used_count = sum(1 for row in rows if row.get("used_before"))
            unreadable = sum(1 for row in rows if not row.get("content_ready"))
            _finish((f"Tìm thấy {len(rows)} bài đúng chủ đề, đã xếp độ liên quan trước lượt đọc"
                     + (f"; khóa {used_count} bài từng dùng" if used_count else "")
                     + (f"; {unreadable} bài chưa đọc được" if unreadable else "") + "."),
                    progress=100, done=len(rows), total=len(rows),
                    search_results=rows)
            _log(f"Kho ý tưởng: tìm thấy {len(rows)} nguồn cho '{keyword}'.", "ok")
        except Exception as exc:
            _finish("Tìm nguồn nội dung lỗi.", error=str(exc), search_results=[])
            _log(f"Tìm nguồn nội dung lỗi: {exc}", "err")

    submit_job(work, name="Tìm nguồn nội dung", resource="network",
               foreground=False, metadata={"kind": "content_search"})
    return {"ok": True, "async": True, "keyword": keyword}, 200


def _download_items(body: Dict) -> list:
    rows = body.get("items") or []
    if isinstance(rows, dict):
        rows = [rows]
    clean = [dict(x) for x in rows if isinstance(x, dict) and
             str(x.get("url") or x.get("source_url") or "").strip()]
    raw_urls = body.get("urls") or []
    if isinstance(raw_urls, str):
        raw_urls = raw_urls.splitlines()
    for raw in raw_urls:
        url = str(raw or "").strip()
        if url:
            clean.append({"url": url, "title": url, "source_name": "Link nhập tay",
                          "selected": True})
    out, seen = [], set()
    for item in clean:
        url = str(item.get("url") or item.get("source_url") or "").strip()
        if url not in seen:
            seen.add(url)
            item["url"] = url
            out.append(item)
    return out[:50]


def api_content_download(body: Dict) -> JsonResult:
    items = _download_items(body)
    if not items:
        return {"error": "Hãy chọn kết quả tìm kiếm hoặc dán link bài viết."}, 400
    store = _store()
    history = store.source_history([item.get("url") or "" for item in items])
    used = [history.get(canonical_source_url(item.get("url") or ""))
            for item in items]
    used = [item for item in used if item and item.get("used")]
    if used:
        names = ", ".join(str(item.get("title") or "")[:45] for item in used[:3])
        return {"error": "Đã chặn chuyện từng dùng, không được sản xuất lại: " + names}, 409
    # Link đã nằm trong kho thì không tải mạng lần nữa. Với một lô trộn, chỉ
    # xử lý các link mới; bài có sẵn vẫn giữ nguyên trong SQLite.
    items = [item for item in items
             if canonical_source_url(item.get("url") or "") not in history]
    if not items:
        return {"error": "Các chuyện đã có trong Kho đã tải; không cần tải lại."}, 409
    if not _begin("source_download", "Đang tải nội dung thật và lưu vào SQLite…"):
        return {"error": "Kho ý tưởng đang chạy một tác vụ khác."}, 409
    try:
        workers = max(1, min(4, int(body.get("concurrency") or 3), len(items)))
    except (TypeError, ValueError):
        workers = min(3, len(items))
    cookie = str(body.get("cookie") or "").strip()
    settings = _settings()
    provider = str(body.get("provider") or settings["provider"] or "auto").lower()
    auto_analyze = body.get("auto_analyze", True) is not False
    use_ai = auto_analyze and provider not in {"heuristic", "offline", "none"}
    provider_info = _provider_status(settings["config"], provider)
    browser_analysis = use_ai and provider_info["provider"] in {
        "browser", "perplexity_browser"}
    provider_label = str(provider_info["provider"] or provider).upper()
    model_label = str(provider_info["provider_model"] or "")
    provider_text = (provider_label + (f" · {model_label}" if model_label else ""))
    initial_warning = ""
    if use_ai and not provider_info["provider_configured"]:
        initial_warning = (f"{provider_text} chưa có API key hợp lệ; "
                           "bài sẽ được phân tích offline. Mở Cài đặt → API để sửa.")
    _state(total=len(items), done=0, download_success=0, download_failed=0,
           download_errors=[], provider=provider_info["provider"],
           provider_model=model_label,
           provider_configured=provider_info["provider_configured"],
           provider_offline=provider_info["provider_offline"],
           warning=initial_warning,
           status=(f"Đang tải toàn văn; sau đó phân tích bằng {provider_text}…"
                   if use_ai else "Đang tải toàn văn; sau đó phân tích offline…"
                   if auto_analyze else "Đang tải toàn văn…"))
    _activity((f"Bắt đầu {len(items)} bài · AI: {provider_text}."
               if use_ai else f"Bắt đầu tải {len(items)} bài · phân tích offline."
               if auto_analyze else f"Bắt đầu tải {len(items)} bài · không phân tích."),
              "warning" if initial_warning else "info", "prepare")
    if initial_warning:
        _activity(initial_warning, "warning", "provider")

    def work() -> None:
        from .. import story_sources
        store = _store()
        successes, errors, done = [], [], 0
        success_indices = {}
        item_progress = {index: 0.0 for index in range(len(items))}
        progress_lock = threading.Lock()

        def update_item(index: int, pct: float, status: str, stage: str,
                        label: str, log_kind: str = "", log_message: str = "") -> None:
            with progress_lock:
                item_progress[index] = max(item_progress.get(index, 0.0), float(pct))
                overall = round(sum(item_progress.values()) / max(1, len(items)), 1)
            _state(progress=overall, status=status, current_stage=stage,
                   current_item=label)
            if log_message:
                _activity(log_message, log_kind or "info", stage, label)

        def mark_result(url: str, status: str, error: str = "") -> None:
            with _LOCK:
                state = STATE["content_pipeline"]
                rows = [dict(x) for x in (state.get("search_results") or [])]
                changed = False
                for row in rows:
                    if str(row.get("url") or "") == url:
                        row["download_status"] = status
                        row["download_error"] = str(error or "")[:300]
                        changed = True
                if changed:
                    state["search_results"] = rows
                    state["rev"] = int(state.get("rev", 0)) + 1

        def fetch(index_item):
            index, source_item = index_item
            label = str(source_item.get("title") or source_item.get("url") or
                        f"Bài {index + 1}")[:90]
            update_item(index, 3, f"Bài {index + 1}/{len(items)}: đang tải toàn văn…",
                        "download", label, "info",
                        f"[{index + 1}/{len(items)}] Đang tải toàn văn nguồn.")
            fetched = story_sources.fetch_reference_article(
                source_item, cookie=cookie, timeout=35)
            record = normalize_record(fetched, index)
            record["selected"] = True
            update_item(index, 30,
                        f"Bài {index + 1}/{len(items)}: đã tải toàn văn; chuẩn bị phân tích…",
                        "downloaded", label, "ok",
                        f"Đã tải toàn văn ({int(record.get('word_count') or 0):,} từ).")
            if use_ai and not browser_analysis:
                def ai_progress(stage: str, message: str, pct: float) -> None:
                    kind = "err" if stage == "error" else (
                        "warning" if stage == "warning" else
                        "ok" if stage == "done" else "info")
                    update_item(index, 30 + float(pct) * .65,
                                f"Bài {index + 1}/{len(items)}: {message}",
                                "ai_" + stage, label, kind, message)

                record = analyze_record(
                    record, settings["config"], use_ai=True, provider=provider,
                    progress=ai_progress)
            elif auto_analyze:
                record.update(heuristic_analysis(record))
                record["analysis_provider"] = "offline-on-import"
                update_item(index, 92,
                            f"Bài {index + 1}/{len(items)}: đã phân tích offline; đang lưu…",
                            "offline", label, "ok", "Đã phân tích offline.")
            return record

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(fetch, pair): pair
                           for pair in enumerate(items)}
                for future in as_completed(futures):
                    item_index, source_item = futures[future]
                    done += 1
                    try:
                        record = future.result()
                        store.upsert([record])
                        successes.append(record)
                        success_indices[str(record.get("id") or "")] = item_index
                        mark_result(str(source_item.get("url") or ""), "Đã tải")
                        label = str(record.get("title_original") or "")[:65]
                        if browser_analysis:
                            update_item(item_index, 35,
                                        f"Đã tải {done}/{len(items)} · chờ phân tích trên trình duyệt…",
                                        "saved", label, "ok",
                                        "Đã lưu toàn văn vào SQLite; chờ trình duyệt phân tích.")
                        else:
                            update_item(item_index, 100,
                                        f"Đã hoàn tất {done}/{len(items)} · đang xử lý bài còn lại…",
                                        "saved", label, "ok",
                                        "Đã lưu bài và kết quả phân tích vào SQLite.")
                        _log(f"Đã tải bài {done}/{len(items)}: {label}", "ok")
                    except Exception as exc:
                        url = str(source_item.get("url") or "")
                        errors.append({"url": url, "error": str(exc)[:300]})
                        mark_result(url, "Lỗi", str(exc))
                        update_item(item_index, 100,
                                    f"Bài {done}/{len(items)} lỗi; chuyển sang bài còn lại…",
                                    "error", str(source_item.get("title") or url)[:90],
                                    "err", f"Không xử lý được bài: {str(exc)[:300]}")
                    ai_success = sum(1 for item in successes
                                     if str(item.get("analysis_provider") or "").startswith(
                                         ("nvidia:", "zenmux:", "gemini:", "tokenrouter:",
                                          "browser:", "perplexity_browser:")))
                    ai_failures = [str(item.get("analysis_error") or "") for item in successes
                                   if str(item.get("analysis_error") or "").strip()]
                    _state(done=done, total=len(items),
                           download_success=len(successes),
                           download_failed=len(errors),
                           download_errors=list(errors),
                           ai_success=ai_success, ai_failed=len(ai_failures),
                           warning=(ai_failures[0] if ai_failures else initial_warning),
                           status=(f"Đã xử lý {done}/{len(items)} · "
                                   f"lưu {len(successes)} · lỗi {len(errors)}"))
            if browser_analysis and successes:
                _activity(
                    f"Đã tải xong {len(successes)} bài; mở một phiên trình duyệt để phân tích tuần tự.",
                    "info", "ai_sending")

                def browser_activity(index: int, total: int, stage: str,
                                     message: str, pct: float) -> None:
                    record = successes[max(0, min(len(successes) - 1, index - 1))]
                    item_index = success_indices.get(str(record.get("id") or ""), index - 1)
                    label = str(record.get("title_original") or f"Bài {index}")[:90]
                    kind = "err" if stage == "error" else (
                        "warning" if stage == "warning" else
                        "ok" if stage == "done" else "info")
                    update_item(item_index, 35 + float(pct) * .65,
                                f"Phân tích trình duyệt {index}/{total}: {message}",
                                "ai_" + stage, label, kind, message)

                def browser_progress(done_count: int, total: int, item: Dict) -> None:
                    store.upsert([item], overwrite=True)
                    _state(done=done_count, total=total,
                           status=f"Đã phân tích trên trình duyệt {done_count}/{total} bài.")

                successes = asyncio.run(analyze_many(
                    successes, settings["config"], use_ai=True,
                    provider=provider_info["provider"], concurrency=1,
                    progress=browser_progress, activity=browser_activity))
                store.upsert(successes, overwrite=True)
            if not successes:
                message = errors[0]["error"] if errors else "Không có bài đủ nội dung."
                _finish("Không tải được bài nào vào kho.", error=message,
                        progress=100, done=done, total=len(items),
                        download_success=0, download_failed=len(errors),
                        download_errors=errors)
                return
            ai_count = sum(1 for item in successes
                           if str(item.get("analysis_provider") or "").startswith(
                               ("nvidia:", "zenmux:", "gemini:", "tokenrouter:",
                                "browser:", "perplexity_browser:")))
            ai_errors = [str(item.get("analysis_error") or "") for item in successes
                         if str(item.get("analysis_error") or "").strip()]
            calendar_path = (_sync_content_calendar(successes, settings)
                             if auto_analyze else "")
            if use_ai and ai_errors:
                status = (f"Đã tải {len(successes)} bài; AI thành công {ai_count}, "
                          f"phân tích offline thay thế {len(ai_errors)} bài"
                          + (f"; {len(errors)} bài tải lỗi." if errors else "."))
            elif use_ai:
                status = (f"Đã tải {len(successes)} bài vào kho SQLite; "
                          f"AI đã rút chất liệu cho {ai_count} bài"
                          + (f"; {len(errors)} bài lỗi." if errors else "."))
            elif auto_analyze:
                status = (f"Đã tải và phân tích offline {len(successes)} bài"
                          + (f"; {len(errors)} bài tải lỗi." if errors else "."))
            else:
                status = (f"Đã tải {len(successes)} bài vào kho SQLite"
                          + (f"; {len(errors)} bài tải lỗi." if errors else "."))
            _finish(status, progress=100, done=done, total=len(items),
                    count=len(store.list(limit=10000)),
                    download_success=len(successes), download_failed=len(errors),
                    download_errors=errors, calendar_path=calendar_path,
                    ai_success=ai_count, ai_failed=len(ai_errors),
                    warning=(ai_errors[0] if ai_errors else initial_warning),
                    current_stage="done")
            _activity(status, "warning" if ai_errors or errors else "ok", "done")
        except Exception as exc:
            _finish("Tải nội dung lỗi; các bài đã xong vẫn được giữ.", error=str(exc),
                    download_success=len(successes), download_failed=len(errors),
                    download_errors=errors)
            _activity(str(exc), "err", "error")

    submit_job(work, name="Tải và phân tích nội dung", resource="ai",
               foreground=False, metadata={"kind": "content_download"})
    return {"ok": True, "async": True, "total": len(items), **provider_info}, 200


def api_content_import(body: Dict) -> JsonResult:
    path = str(body.get("path") or "").strip()
    if not path:
        return {"error": "Hãy chọn file JSON hoặc SQLite."}, 400
    if not _begin("import", "Đang đọc và chuẩn hoá kho truyện…"):
        return {"error": "Kho ý tưởng đang chạy một tác vụ khác."}, 409

    def work() -> None:
        try:
            rows = load_source(path, limit=max(0, int(body.get("limit") or 0)),
                               table=str(body.get("table") or ""))
            count = _store().upsert(rows)
            _finish(f"Đã nhập {count} truyện từ {os.path.basename(path)}.",
                    progress=100, done=count, total=count, count=count,
                    input_path=os.path.abspath(path))
            _log(f"Kho ý tưởng: đã nhập {count} truyện.", "ok")
        except Exception as exc:
            _finish("Nhập dữ liệu lỗi.", error=str(exc))
            _log(f"Nhập kho ý tưởng lỗi: {exc}", "err")

    submit_job(work, name="Nhập kho ý tưởng", resource="sqlite",
               foreground=False, metadata={"kind": "content_import"})
    return {"ok": True, "async": True}, 200


def api_content_sample(_body: Dict = None) -> JsonResult:
    if not _begin("sample", "Đang tạo dữ liệu mẫu…"):
        return {"error": "Kho ý tưởng đang chạy một tác vụ khác."}, 409

    def work() -> None:
        try:
            rows = []
            for record in sample_records():
                item = dict(record)
                item.update(heuristic_analysis(item))
                rows.append(item)
            count = _store().upsert(rows)
            _finish(f"Đã tạo {count} ý tưởng mẫu để kiểm thử.", progress=100,
                    done=count, total=count, count=count)
        except Exception as exc:
            _finish("Tạo dữ liệu mẫu lỗi.", error=str(exc))

    submit_job(work, name="Tạo dữ liệu nội dung mẫu", resource="sqlite",
               foreground=False, metadata={"kind": "content_sample"})
    return {"ok": True, "async": True}, 200


def _selected_records(store: ContentStore, ids: Iterable[str], all_items: bool) -> list:
    clean = [str(x) for x in (ids or []) if str(x).strip()]
    if clean:
        return [item for item in (store.get(x) for x in clean) if item]
    return store.list(selected_only=not all_items, limit=10000)


def _body_ids(body: Dict) -> list:
    raw = body.get("ids") or []
    if isinstance(raw, str):
        raw = [raw]
    return list(dict.fromkeys(
        str(value).strip() for value in raw if str(value).strip()))


def api_content_analyze(body: Dict) -> JsonResult:
    if not _begin("analyze", "Đang chuẩn bị phân tích đa tầng…"):
        return {"error": "Kho ý tưởng đang chạy một tác vụ khác."}, 409
    store = _store()
    rows = _selected_records(store, body.get("ids") or [], bool(body.get("all")))
    if not rows:
        _finish("Không có ý tưởng được chọn.", error="Hãy tick ý tưởng hoặc chọn Phân tích tất cả.")
        return {"error": "Không có ý tưởng được chọn."}, 400
    settings = _settings()
    provider = str(body.get("provider") or settings["provider"] or "auto").lower()
    use_ai = bool(body.get("use_ai", True)) and provider not in {"heuristic", "offline", "none"}
    concurrency = max(1, min(8, int(body.get("concurrency") or settings["concurrency"])))
    provider_info = _provider_status(settings["config"], provider)
    provider_label = str(provider_info["provider"] or provider).upper()
    model_label = str(provider_info["provider_model"] or "")
    provider_text = provider_label + (f" · {model_label}" if model_label else "")
    initial_warning = ""
    if use_ai and not provider_info["provider_configured"]:
        initial_warning = (f"{provider_text} chưa có API key hợp lệ; sẽ dùng phân tích "
                           "offline. Mở Cài đặt → API để sửa.")
    _state(total=len(rows), provider=provider_info["provider"],
           provider_model=model_label,
           provider_configured=provider_info["provider_configured"],
           provider_offline=provider_info["provider_offline"],
           warning=initial_warning,
           status=(f"Đang phân tích {len(rows)} ý tưởng bằng {provider_text}…"
                   if use_ai else f"Đang phân tích offline {len(rows)} ý tưởng…"))
    _activity((f"Bắt đầu phân tích {len(rows)} bài · AI: {provider_text}."
               if use_ai else f"Bắt đầu phân tích offline {len(rows)} bài."),
              "warning" if initial_warning else "info", "prepare")
    if initial_warning:
        _activity(initial_warning, "warning", "provider")

    def work() -> None:
        try:
            item_progress = {index: 0.0 for index in range(len(rows))}
            progress_lock = threading.Lock()
            completed_results = []

            def activity(index: int, total: int, stage: str, message: str,
                         pct: float) -> None:
                item_index = max(0, index - 1)
                with progress_lock:
                    item_progress[item_index] = max(
                        item_progress.get(item_index, 0.0), float(pct))
                    overall = round(sum(item_progress.values()) / max(1, total), 1)
                title = str(rows[item_index].get("title_original") or
                            rows[item_index].get("title_localized") or f"Bài {index}")[:90]
                kind = "err" if stage == "error" else (
                    "warning" if stage == "warning" else
                    "ok" if stage == "done" else "info")
                _state(progress=overall, current_stage="ai_" + stage,
                       current_item=title,
                       status=f"Bài {index}/{total}: {message}")
                _activity(message, kind, "ai_" + stage, title)

            def progress(done: int, total: int, item: Dict) -> None:
                store.upsert([item], overwrite=True)
                completed_results.append(item)
                ai_ok = sum(1 for result in completed_results
                            if str(result.get("analysis_provider") or "").startswith(
                                ("nvidia:", "zenmux:", "gemini:", "tokenrouter:",
                                 "browser:", "perplexity_browser:")))
                failures = [str(result.get("analysis_error") or "")
                            for result in completed_results
                            if str(result.get("analysis_error") or "").strip()]
                _state(done=done, total=total,
                       ai_success=ai_ok, ai_failed=len(failures),
                       warning=(failures[0] if failures else initial_warning),
                       status=f"Đã phân tích {done}/{total}: "
                              f"{str(item.get('title_original') or '')[:70]}")

            results = asyncio.run(analyze_many(
                rows, settings["config"], use_ai=use_ai, provider=provider,
                concurrency=concurrency, progress=progress, activity=activity))
            ai_success = sum(1 for item in results
                             if str(item.get("analysis_provider") or "").startswith(
                                 ("nvidia:", "zenmux:", "gemini:", "tokenrouter:",
                                  "browser:", "perplexity_browser:")))
            ai_errors = [str(item.get("analysis_error") or "") for item in results
                         if str(item.get("analysis_error") or "").strip()]
            calendar_path = _sync_content_calendar(results, settings)
            if use_ai:
                status = (f"Đã phân tích xong {len(results)} ý tưởng · AI thành công "
                          f"{ai_success} · offline thay thế {len(ai_errors)}.")
            else:
                status = f"Đã phân tích offline xong {len(results)} ý tưởng."
            _finish(status, progress=100,
                    done=len(results), total=len(results), count=len(results),
                    calendar_path=calendar_path, ai_success=ai_success,
                    ai_failed=len(ai_errors),
                    warning=(ai_errors[0] if ai_errors else initial_warning),
                    current_stage="done")
            _activity(status, "warning" if ai_errors else "ok", "done")
            _log(f"Kho ý tưởng: phân tích xong {len(results)} truyện.", "ok")
        except Exception as exc:
            _finish("Phân tích ý tưởng lỗi; phần đã xong vẫn được giữ.", error=str(exc))
            _activity(str(exc), "err", "error")
            _log(f"Phân tích ý tưởng lỗi: {exc}", "err")

    submit_job(work, name="Phân tích kho ý tưởng", resource="ai",
               foreground=False, metadata={"kind": "content_analyze"})
    return {"ok": True, "async": True, "total": len(rows),
            **provider_info}, 200


def api_content_update(body: Dict) -> JsonResult:
    try:
        record_id = str(body.get("id") or "").strip()
        values = body.get("values") if isinstance(body.get("values"), dict) else {}
        item = _store().patch(record_id, values)
        _state(status="Đã lưu chỉnh sửa ý tưởng.")
        return {"ok": True, "item": public_record(item, include_content=True)}, 200
    except KeyError as exc:
        return {"error": str(exc)}, 404
    except Exception as exc:
        return {"error": str(exc)}, 400


def api_content_select(body: Dict) -> JsonResult:
    ids = body.get("ids") or []
    if isinstance(ids, str):
        ids = [ids]
    count = _store().set_selected(ids, bool(body.get("selected", True)))
    _state(selected_count=len(_store().list(selected_only=True, limit=10000)))
    return {"ok": True, "count": count}, 200


def api_content_delete(body: Dict) -> JsonResult:
    """Xóa mục chưa dùng khỏi kho; lịch sử đã dùng luôn được bảo vệ."""
    ids = _body_ids(body)
    if not ids:
        return {"error": "Hãy chọn ít nhất một mục cần xóa."}, 400
    with _LOCK:
        if STATE["content_pipeline"].get("working"):
            return {"error": "Kho ý tưởng đang chạy tác vụ khác; chưa thể xóa."}, 409
    try:
        store = _store()
        result = store.delete_many(ids, protect_used=True)
        deleted = len(result["deleted"])
        protected = len(result["protected"])
        missing = len(result["not_found"])
        status = f"Đã xóa {deleted} mục khỏi kho."
        if protected:
            status += f" Giữ lại {protected} mục đã dùng để chống lặp."
        if missing:
            status += f" {missing} mục không còn tồn tại."
        remaining = len(store.list(limit=10000))
        _state(status=status, count=remaining, deleted_count=deleted,
               protected_count=protected,
               selected_count=len(store.list(selected_only=True, limit=10000)))
        _log(status, "ok" if deleted else "warn")
        return {"ok": True, **result, "count": remaining, "status": status}, 200
    except Exception as exc:
        return {"error": "Xóa mục trong kho lỗi: " + str(exc)}, 500


def api_content_reload(body: Dict) -> JsonResult:
    """Tải lại toàn văn từ URL và phân tích lại đúng các mục trong hàng đợi."""
    ids = _body_ids(body)
    if not ids:
        return {"error": "Hãy chọn ít nhất một mục cần tải lại."}, 400
    store = _store()
    requested = [item for item in (store.get(record_id) for record_id in ids) if item]
    protected = [item for item in requested if record_was_used(item)]
    rows = [item for item in requested if not record_was_used(item)]
    if not rows:
        return {"error": (
            "Các mục đã chọn đều thuộc lịch sử đã dùng hoặc không còn tồn tại; "
            "hệ thống không tải lại để tránh làm trùng chuyện.")}, 409
    if not _begin("content_reload", "Đang chuẩn bị tải lại hàng đợi…"):
        return {"error": "Kho ý tưởng đang chạy một tác vụ khác."}, 409
    settings = _settings()
    provider = str(body.get("provider") or settings["provider"] or "auto").lower()
    use_ai = bool(body.get("use_ai", True)) and provider not in {
        "heuristic", "offline", "none"}
    cookie = str(body.get("cookie") or "").strip()
    try:
        workers = max(1, min(4, int(body.get("concurrency") or 3), len(rows)))
    except (TypeError, ValueError):
        workers = min(3, len(rows))
    resolved_provider = _provider_status(settings["config"], provider)["provider"]
    if resolved_provider in {"browser", "perplexity_browser"}:
        # Cùng một persistent profile không thể bị nhiều Playwright context giữ.
        workers = 1
    _state(total=len(rows), done=0, reload_success=0, reload_failed=0,
           reload_errors=[], protected_count=len(protected), provider=resolved_provider,
           provider_configured=_configured_provider(settings["config"], provider),
           status=f"Đang tải lại và phân tích 0/{len(rows)} mục…")

    def work() -> None:
        from .. import story_sources
        successes, errors, done = [], [], 0

        def reload_one(index_item):
            index, old = index_item
            record_id = str(old.get("id") or "")
            url = str(old.get("source_url") or "").strip()
            if url.startswith(("http://", "https://")):
                fetched = story_sources.fetch_reference_article({
                    "url": url,
                    "title": old.get("title_original") or old.get("title_localized"),
                    "source_name": old.get("source") or "Nguồn đã tải",
                    "language": old.get("language") or "",
                    "topic": old.get("primary_genre") or "",
                    "narrative_style": old.get("narrative_style") or "",
                    "content_form": old.get("content_form") or "",
                }, cookie=cookie, timeout=35, force_refresh=True)
                fresh = normalize_record(fetched, index)
                fresh["id"] = record_id
            else:
                fresh = dict(old)
                content = str(fresh.get("content") or fresh.get("raw_content") or "")
                if len(content.strip()) < 80:
                    raise ValueError(
                        "Mục không có URL nguồn và nội dung hiện có quá ngắn để phân tích lại.")
            fresh["selected"] = True
            fresh["reloaded_at"] = datetime.now().isoformat(timespec="seconds")
            result = analyze_record(
                fresh, settings["config"], use_ai=use_ai, provider=provider)
            result["id"] = record_id
            result["selected"] = True
            result["reloaded_at"] = fresh["reloaded_at"]
            store.upsert([result], overwrite=True)
            return result

        try:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futures = {pool.submit(reload_one, pair): pair[1]
                           for pair in enumerate(rows)}
                for future in as_completed(futures):
                    old = futures[future]
                    done += 1
                    try:
                        result = future.result()
                        successes.append(result)
                        _log("Đã tải lại %d/%d: %s" % (
                            done, len(rows),
                            str(result.get("title_localized") or
                                result.get("title_original") or "")[:65]), "ok")
                    except Exception as exc:
                        errors.append({
                            "id": str(old.get("id") or ""),
                            "title": str(old.get("title_localized") or
                                         old.get("title_original") or "")[:100],
                            "error": str(exc)[:300],
                        })
                    _state(done=done, total=len(rows),
                           progress=round(done * 100 / max(1, len(rows)), 1),
                           reload_success=len(successes),
                           reload_failed=len(errors), reload_errors=list(errors),
                           status=(f"Đã xử lý {done}/{len(rows)} · "
                                   f"tải lại {len(successes)} · lỗi {len(errors)}"))
            if not successes:
                message = errors[0]["error"] if errors else "Không có mục tải lại thành công."
                _finish("Tải lại hàng đợi thất bại; dữ liệu cũ vẫn được giữ.",
                        error=message, progress=100, done=done, total=len(rows),
                        reload_success=0, reload_failed=len(errors),
                        reload_errors=errors, protected_count=len(protected))
                return
            calendar_path = _sync_content_calendar(successes, settings)
            status = f"Đã tải lại và phân tích {len(successes)}/{len(rows)} mục."
            if errors:
                status += f" {len(errors)} mục lỗi vẫn giữ dữ liệu cũ."
            if protected:
                status += f" Bỏ qua {len(protected)} mục đã dùng."
            _finish(status, progress=100, done=done, total=len(rows),
                    count=len(store.list(limit=10000)),
                    reload_success=len(successes), reload_failed=len(errors),
                    reload_errors=errors, protected_count=len(protected),
                    calendar_path=calendar_path)
        except Exception as exc:
            _finish("Tải lại hàng đợi lỗi; dữ liệu cũ vẫn được giữ.", error=str(exc),
                    reload_success=len(successes), reload_failed=len(errors),
                    reload_errors=errors, protected_count=len(protected))

    submit_job(work, name="Tải lại hàng đợi nội dung", resource="ai",
               foreground=False, metadata={"kind": "content_reload"})
    return {"ok": True, "async": True, "total": len(rows),
            "protected": len(protected), "provider": provider,
            "provider_configured": _configured_provider(
                settings["config"], provider)}, 200


def api_content_export(body: Dict) -> JsonResult:
    if not _begin("export", "Đang tạo Excel và JSON kế hoạch…"):
        return {"error": "Kho ý tưởng đang chạy một tác vụ khác."}, 409
    store = _store()
    rows = _selected_records(store, body.get("ids") or [], bool(body.get("all")))
    if not rows:
        _finish("Không có ý tưởng để xuất.", error="Hãy chọn ít nhất một ý tưởng.")
        return {"error": "Không có ý tưởng để xuất."}, 400
    output_dir = _resolve_path(str(body.get("output_dir") or ""), _settings()["output_dir"])

    def work() -> None:
        try:
            result = export_plan(rows, output_dir=output_dir,
                                 stem=str(body.get("stem") or "ke_hoach_noi_dung"))
            _finish(f"Đã xuất {len(rows)} ý tưởng sang Excel và JSON.", progress=100,
                    done=len(rows), total=len(rows), export_xlsx=result["xlsx"],
                    export_json=result["json"], output_dir=output_dir)
            _log(f"Đã xuất kế hoạch: {result['xlsx']}", "ok")
        except Exception as exc:
            _finish("Xuất kế hoạch lỗi.", error=str(exc))
            _log(f"Xuất kế hoạch lỗi: {exc}", "err")

    submit_job(work, name="Xuất kế hoạch nội dung", resource="sqlite",
               foreground=False, metadata={"kind": "content_export"})
    return {"ok": True, "async": True, "total": len(rows)}, 200


def api_content_use_story(body: Dict) -> JsonResult:
    record_id = str(body.get("id") or "").strip()
    store = _store()
    item = store.get(record_id)
    if not item:
        return {"error": "Không tìm thấy ý tưởng."}, 404
    if record_was_used(item):
        return {"error": "Chuyện này đã có trong lịch sử sử dụng và bị khóa để tránh làm lại."}, 409
    titles = list(item.get("titles") or [])
    descriptions = list(item.get("descriptions") or [])
    title_index = max(0, min(len(titles) - 1, int(body.get("title_index") or 0))) if titles else 0
    description_index = max(0, min(len(descriptions) - 1,
                                   int(body.get("description_index") or 0))) if descriptions else 0
    title = (titles[title_index] if titles else
             item.get("title_localized") or item.get("title_original") or "").strip()
    description = descriptions[description_index] if descriptions else ""
    rewrite_brief = str(item.get("rewrite_brief") or "").strip()
    analysis_provider = str(item.get("analysis_provider") or "").lower()
    chinese_not_ai = (str(item.get("language") or "").lower() == "zh" and
                      (not rewrite_brief or any(token in analysis_provider
                       for token in ("heuristic", "offline", "fallback"))))
    if chinese_not_ai:
        return {"error": ("Truyện Trung chưa có hồ sơ hook/plot twist tiếng Việt. "
                          "Hãy Phân tích bằng Gemini Web hoặc Claude trên Perplexity "
                          "trước khi viết.")}, 409
    with _LOCK:
        manual = STATE["manual"]
        manual.update({
            "content_idea_id": record_id,
            "writer_title": title,
            "youtube_description": description,
            "youtube_tags": list(item.get("tags") or []),
            "content_outline": str(item.get("outline") or ""),
            "rewrite_brief": rewrite_brief,
            "status": "Đã nhận hồ sơ sáng tác; sẵn sàng viết mới bằng Perplexity.",
            "rev": int(manual.get("rev", 0)) + 1,
        })
    updated = store.patch(record_id, {
        "status": "Đang viết",
        "used_at": datetime.now().isoformat(timespec="seconds"),
        "usage_count": int(item.get("usage_count") or 0) + 1,
        "last_used_title": title,
        "selected": False,
    })
    try:
        calendar_path = _sync_content_calendar(
            [updated], final_titles={record_id: title}, status="Đang viết")
        _state(calendar_path=calendar_path)
        _log(f"Đã ghi tiêu đề được chọn vào lịch nội dung: {calendar_path}", "ok")
    except Exception as exc:
        # Không chặn AI Story chỉ vì workbook đang lỗi/không có quyền ghi.
        _log(f"Chưa ghi được lịch nội dung sau khi chọn tiêu đề: {exc}", "warn")
    return {"ok": True, "story": {
        "id": record_id, "title": title, "description": description,
        "tags": list(item.get("tags") or []), "outline": item.get("outline") or "",
        "title_localized": item.get("title_localized") or "",
        "rewrite_brief": rewrite_brief,
        "main_hook": item.get("main_hook") or "",
        "high_tension_scenes": list(item.get("high_tension_scenes") or []),
        "plot_twists": list(item.get("plot_twists") or []),
        "analysis_provider": item.get("analysis_provider") or "",
        "keywords": list(item.get("keywords") or []),
    }}, 200
