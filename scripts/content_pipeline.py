#!/usr/bin/env python3
"""CLI kho truyện -> phân tích -> kế hoạch Excel/JSON."""
from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from autodub.content_pipeline import (  # noqa: E402
    ContentStore, analyze_many, export_plan, heuristic_analysis, load_source,
    sample_records,
)


def load_config() -> dict:
    import yaml
    with open(os.path.join(ROOT, "config.yaml"), "r", encoding="utf-8") as handle:
        return yaml.safe_load(handle) or {}


def rooted(value: str, default: str) -> str:
    path = os.path.expandvars(str(value or default).strip().strip('"'))
    return os.path.abspath(path if os.path.isabs(path) else os.path.join(ROOT, path))


def progress(done: int, total: int, item: dict) -> None:
    print("[%s] %d/%d · %s" % (
        time.strftime("%H:%M:%S"), done, total,
        str(item.get("title_original") or item.get("id") or "")[:90]), flush=True)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        description="AutoDubVN: biến kho truyện JSON/SQLite thành kế hoạch YouTube")
    p.add_argument("command", choices=("import", "analyze", "export", "run", "sample"))
    p.add_argument("--input", help="File JSON/SQLite từ kho thu thập")
    p.add_argument("--table", default="", help="Tên bảng SQLite (bỏ trống để tự dò)")
    p.add_argument("--limit", type=int, default=0)
    p.add_argument("--provider", default="auto",
                   help="auto|nvidia|zenmux|gemini|heuristic")
    p.add_argument("--concurrency", type=int, default=0)
    p.add_argument("--all", action="store_true", help="Xử lý cả mục chưa tick")
    p.add_argument("--output-dir", default="")
    return p


def main() -> int:
    args = parser().parse_args()
    cfg = load_config()
    cp = cfg.get("content_pipeline") if isinstance(cfg.get("content_pipeline"), dict) else {}
    store = ContentStore(rooted(cp.get("database", ""), "data/content_ideas.sqlite"))

    if args.command in {"import", "run"}:
        input_path = args.input or str(cp.get("weekly_input") or "")
        if not input_path:
            raise SystemExit("Thiếu --input (hoặc content_pipeline.weekly_input trong config.yaml).")
        rows = load_source(rooted(input_path, input_path), limit=max(0, args.limit),
                           table=args.table)
        print(f"Đã đọc {len(rows)} truyện; đang lưu vào {store.path}")
        store.upsert(rows)

    if args.command == "sample":
        rows = []
        for item in sample_records():
            item.update(heuristic_analysis(item)); rows.append(item)
        store.upsert(rows)
        print(f"Đã tạo {len(rows)} ý tưởng mẫu.")

    if args.command in {"analyze", "run"}:
        rows = store.list(selected_only=not args.all, limit=10000)
        if not rows and not args.all:
            rows = store.list(limit=10000)
            print("Chưa tick mục nào; phân tích toàn bộ kho hiện có.")
        if not rows:
            raise SystemExit("Kho ý tưởng đang trống.")
        provider = str(args.provider or cp.get("provider") or "auto").lower()
        use_ai = provider not in {"heuristic", "offline", "none"}
        concurrency = max(1, min(8, args.concurrency or int(cp.get("max_concurrency") or 3)))

        def persist(done: int, total: int, item: dict) -> None:
            store.upsert([item], overwrite=True); progress(done, total, item)

        results = asyncio.run(analyze_many(
            rows, cfg, use_ai=use_ai, provider=provider,
            concurrency=concurrency, progress=persist))
        store.upsert(results, overwrite=True)
        print(f"Đã phân tích {len(results)} ý tưởng bằng {provider}.")

    if args.command in {"export", "run", "sample"}:
        rows = store.list(selected_only=not args.all, limit=10000)
        if not rows:
            rows = store.list(limit=10000)
        if not rows:
            raise SystemExit("Không có dữ liệu để xuất.")
        output = rooted(args.output_dir or cp.get("output_dir", ""),
                        "output/content_plans")
        result = export_plan(rows, output_dir=output)
        print("Excel:", result["xlsx"])
        print("JSON :", result["json"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
