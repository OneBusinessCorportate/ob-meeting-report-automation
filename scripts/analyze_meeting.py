#!/usr/bin/env python3
"""CLI: Step 2 — analyze completed L1 meetings and store L2 reports.

Examples:
    python scripts/analyze_meeting.py                  # all pending for today
    python scripts/analyze_meeting.py --date 2026-03-26
    python scripts/analyze_meeting.py \
        --source-meeting-id timeless_manual_2026_03_26_accounting_sync
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_pipeline.analyze import analyze_pending  # noqa: E402
from meeting_pipeline.config import load_config  # noqa: E402
from meeting_pipeline.utils import get_logger  # noqa: E402

log = get_logger("scripts.analyze")


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze meetings into L2 reports.")
    parser.add_argument("--date", help="Date (YYYY-MM-DD). Defaults to today.")
    parser.add_argument(
        "--source-meeting-id",
        help="Analyze a single meeting by its source_meeting_id.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-analyze even if a current completed L2 report already exists.",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=0,
        help="Analyze all pending meetings from the last N days (range mode).",
    )
    parser.add_argument("--start-date", help="Range start (YYYY-MM-DD).")
    parser.add_argument("--end-date", help="Range end (YYYY-MM-DD).")
    args = parser.parse_args()

    config = load_config()
    result = analyze_pending(
        config,
        date_str=args.date,
        source_meeting_id=args.source_meeting_id,
        force=args.force,
        days_back=args.days_back,
        start_date_str=args.start_date,
        end_date_str=args.end_date,
    )

    log.info(
        "Analyze result: analyzed=%s completed=%s skipped=%s failed=%s",
        result.get("analyzed"),
        result.get("completed"),
        result.get("skipped"),
        result.get("failed"),
    )
    summary = {k: v for k, v in result.items() if k != "results"}
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
