#!/usr/bin/env python3
"""CLI — «Обучающий центр / анализ собеседований» full sync.

Reads the candidate/interview table (Google Sheet «Бух», or a local xlsx/CSV),
fetches the FULL transcript per interview (Timeless → Google Docs → manual file),
stores raw + cleaned transcript in Supabase, runs the AI analysis (Armenian-aware,
Russian output) and saves the assessment + scores + status. Optionally sends a
short Telegram report per analyzed interview.

Examples:
    # Use the configured Google Sheet (INTERVIEW_SPREADSHEET_ID + Google creds,
    # or INTERVIEW_SHEET_CSV_URL), analyze, and store everything:
    python scripts/sync_interviews.py

    # Read a local xlsx export of the sheet (any tab via --tab, default «Бух»):
    python scripts/sync_interviews.py --xlsx ./data/training_center.xlsx --tab Бух

    # Only fetch + store transcripts, skip the AI analysis:
    python scripts/sync_interviews.py --xlsx ./data/training_center.xlsx --no-analyze

    # Re-process already-finished interviews (new analysis version):
    python scripts/sync_interviews.py --force

    # Also send a Telegram report per interview:
    python scripts/sync_interviews.py --deliver
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interview_pipeline.pipeline import sync_interviews  # noqa: E402
from interview_pipeline.sheet_source import SheetCandidate  # noqa: E402
from meeting_pipeline.config import load_config  # noqa: E402
from meeting_pipeline.utils import get_logger  # noqa: E402

log = get_logger("scripts.sync_interviews")


def _parse_links(items):
    """Parse repeatable --link "Имя|URL" (or just URL) into SheetCandidate rows."""
    out = []
    for i, raw in enumerate(items or [], start=1):
        if "|" in raw:
            name, url = raw.split("|", 1)
        else:
            name, url = f"Кандидат {i}", raw
        url = url.strip()
        if not url:
            continue
        out.append(
            SheetCandidate(
                full_name=name.strip() or f"Кандидат {i}",
                track="buh",
                role="бухгалтер",
                call_url=url,
                source_sheet="manual",
                source_row=i,
            )
        )
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync + analyze training-center interviews.")
    parser.add_argument("--xlsx", help="Local .xlsx export of the «Обучающий центр ОВ» sheet.")
    parser.add_argument("--csv", help="Local .csv export of a single sheet tab.")
    parser.add_argument(
        "--link", action="append", dest="links",
        help='Add a link directly, format "Имя|URL" (repeatable). No sheet/creds needed.',
    )
    parser.add_argument(
        "--tab", action="append", dest="tabs",
        help="Sheet tab(s) to read (repeatable; default «Бух»).",
    )
    parser.add_argument("--no-analyze", action="store_true", help="Skip the AI analysis step.")
    parser.add_argument("--deliver", action="store_true", help="Send a Telegram report per interview.")
    parser.add_argument("--force", action="store_true", help="Re-process finished interviews.")
    parser.add_argument("--limit", type=int, help="Process at most N candidate rows (debug).")
    args = parser.parse_args()

    config = load_config()
    manual = _parse_links(args.links) if args.links else None
    result = sync_interviews(
        config,
        xlsx_path=args.xlsx,
        csv_path=args.csv,
        tabs=args.tabs,
        candidates=manual,
        do_analyze=False if args.no_analyze else None,
        do_deliver=True if args.deliver else None,
        force=args.force,
        limit=args.limit,
    )

    printable = {k: v for k, v in result.items() if k != "results"}
    print(json.dumps(printable, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
