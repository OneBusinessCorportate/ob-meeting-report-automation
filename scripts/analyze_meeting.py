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
    args = parser.parse_args()

    config = load_config()
    result = analyze_pending(
        config,
        date_str=args.date,
        source_meeting_id=args.source_meeting_id,
    )

    log.info(
        "Analyze result: analyzed=%s completed=%s failed=%s",
        result.get("analyzed"),
        result.get("completed"),
        result.get("failed"),
    )
    summary = {k: v for k, v in result.items() if k != "results"}
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
