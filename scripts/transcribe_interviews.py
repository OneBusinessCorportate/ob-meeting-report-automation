#!/usr/bin/env python3
"""CLI (task II): transcribe interview / onboarding call links.

Flow: link → Timeless full transcript (or local file) → save in Supabase
``interview_calls`` → update processing status.

Examples:
    # From a CSV exported from the «Обучающий центр ОВ» Notion table:
    python scripts/transcribe_interviews.py --csv ./data/interviews/links.csv

    # One or more links directly:
    python scripts/transcribe_interviews.py --url https://app.timeless.day/meetings/abc123

    # MVP local-file fallback (transcript_file column in the CSV), force re-run:
    python scripts/transcribe_interviews.py --csv ./data/interviews/sample_links.csv --force
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interview_pipeline.transcribe import transcribe_interviews  # noqa: E402
from meeting_pipeline.config import load_config  # noqa: E402
from meeting_pipeline.utils import get_logger  # noqa: E402

log = get_logger("scripts.transcribe_interviews")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Transcribe interview/onboarding call links into Supabase."
    )
    parser.add_argument(
        "--url",
        action="append",
        dest="urls",
        help="Interview call link (repeatable).",
    )
    parser.add_argument("--csv", help="CSV of links exported from the Notion table.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-transcribe calls already marked done.",
    )
    args = parser.parse_args()

    config = load_config()
    result = transcribe_interviews(
        config, urls=args.urls, csv_path=args.csv, force=args.force
    )

    log.info(
        "Done: processed=%s saved=%s skipped=%s needs_attention=%s",
        result.get("processed"),
        result.get("saved"),
        result.get("skipped"),
        result.get("needs_attention"),
    )
    printable = {k: v for k, v in result.items() if k != "results"}
    print(json.dumps(printable, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
