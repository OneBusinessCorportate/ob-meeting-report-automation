#!/usr/bin/env python3
"""CLI: Step 1 — ingest a meeting transcript into Supabase L1.

Examples:
    python scripts/ingest_meeting.py                 # Timeless API mode
    python scripts/ingest_meeting.py --file ./data/transcripts/meeting_2026_03_26.txt \
        --title "Утренняя планёрка бухгалтерии" --date 2026-03-26
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_pipeline.config import load_config  # noqa: E402
from meeting_pipeline.ingest import ingest_meeting  # noqa: E402
from meeting_pipeline.utils import get_logger  # noqa: E402

log = get_logger("scripts.ingest")


def main() -> int:
    parser = argparse.ArgumentParser(description="Ingest a meeting transcript (L1).")
    parser.add_argument("--file", help="Path to a local full-transcript text file.")
    parser.add_argument("--title", help="Meeting title.")
    parser.add_argument("--date", help="Meeting date (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--language", help="Transcript language code (e.g. hy, ru).")
    parser.add_argument(
        "--source-meeting-id",
        help="Override the source_meeting_id (dedup key).",
    )
    parser.add_argument(
        "--days-back",
        type=int,
        default=0,
        help="Backfill: ingest all completed Timeless meetings from the last N days.",
    )
    parser.add_argument("--start-date", help="Backfill range start (YYYY-MM-DD).")
    parser.add_argument("--end-date", help="Backfill range end (YYYY-MM-DD).")
    args = parser.parse_args()

    config = load_config()
    result = ingest_meeting(
        config,
        file_path=args.file,
        title=args.title,
        date_str=args.date,
        language=args.language,
        source_meeting_id=args.source_meeting_id,
        days_back=args.days_back,
        start_date_str=args.start_date,
        end_date_str=args.end_date,
    )

    log.info("Ingest result: status=%s ok=%s", result.get("status"), result.get("ok"))
    print(json.dumps({k: v for k, v in result.items() if k != "meeting"}, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
