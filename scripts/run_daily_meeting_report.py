#!/usr/bin/env python3
"""CLI: combined daily run — ingest -> analyze -> deliver.

This is the script the Render cron job runs at 11:00 Armenia time.

Examples:
    # Production (Timeless API), full pipeline:
    python scripts/run_daily_meeting_report.py

    # MVP local-file fallback:
    python scripts/run_daily_meeting_report.py \
        --file ./data/transcripts/meeting_2026_03_26.txt \
        --title "Утренняя планёрка бухгалтерии" --date 2026-03-26

    # Deliver only (skip ingest/analyze):
    python scripts/run_daily_meeting_report.py --skip-ingest --skip-analyze
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_pipeline.analyze import analyze_pending  # noqa: E402
from meeting_pipeline.config import load_config  # noqa: E402
from meeting_pipeline.deliver import deliver_today  # noqa: E402
from meeting_pipeline.ingest import ingest_meeting  # noqa: E402
from meeting_pipeline.utils import get_logger  # noqa: E402

log = get_logger("scripts.daily")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run the full daily meeting report pipeline."
    )
    parser.add_argument("--file", help="Local transcript file (fallback mode).")
    parser.add_argument("--title", help="Meeting title.")
    parser.add_argument("--date", help="Date (YYYY-MM-DD). Defaults to today.")
    parser.add_argument("--language", help="Transcript language code.")
    parser.add_argument("--source-meeting-id", help="Override source_meeting_id.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-analyze even if a current completed L2 report already exists.",
    )
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--skip-analyze", action="store_true")
    parser.add_argument("--skip-deliver", action="store_true")
    args = parser.parse_args()

    config = load_config()
    summary = {}

    # --- Step 1: Ingest -------------------------------------------------------
    if not args.skip_ingest:
        log.info("=== STEP 1: INGEST ===")
        try:
            ingest_result = ingest_meeting(
                config,
                file_path=args.file,
                title=args.title,
                date_str=args.date,
                language=args.language,
                source_meeting_id=args.source_meeting_id,
            )
            summary["ingest"] = {
                "status": ingest_result.get("status"),
                "ok": ingest_result.get("ok"),
                "detail": ingest_result.get("detail"),
            }
            if not ingest_result.get("ok"):
                log.warning(
                    "Ingest did not produce new data (status=%s). "
                    "Continuing — analyze/deliver may still find existing data.",
                    ingest_result.get("status"),
                )
        except Exception as exc:
            log.exception("Ingest step failed: %s", exc)
            summary["ingest"] = {"status": "error", "ok": False, "detail": str(exc)}
    else:
        log.info("Skipping ingest (--skip-ingest).")

    # --- Step 2: Analyze ------------------------------------------------------
    if not args.skip_analyze:
        log.info("=== STEP 2: ANALYZE ===")
        try:
            # Look back MEETING_ANALYZE_LOOKBACK_DAYS so pending/failed meetings
            # (e.g. blocked earlier by AI quota) are retried automatically on the
            # next scheduled run — no manual command needed.
            analyze_result = analyze_pending(
                config,
                date_str=args.date,
                source_meeting_id=args.source_meeting_id,
                force=args.force,
                days_back=config.analyze_lookback_days,
            )
            summary["analyze"] = {
                "analyzed": analyze_result.get("analyzed"),
                "completed": analyze_result.get("completed"),
                "skipped": analyze_result.get("skipped"),
                "failed": analyze_result.get("failed"),
            }
        except Exception as exc:
            log.exception("Analyze step failed: %s", exc)
            summary["analyze"] = {"status": "error", "detail": str(exc)}
    else:
        log.info("Skipping analyze (--skip-analyze).")

    # --- Step 3: Deliver ------------------------------------------------------
    if not args.skip_deliver:
        log.info("=== STEP 3: DELIVER ===")
        try:
            deliver_result = deliver_today(config, date_str=args.date)
            summary["deliver"] = {
                "status": deliver_result.get("status"),
                "delivered": deliver_result.get("delivered"),
            }
        except Exception as exc:
            log.exception("Deliver step failed: %s", exc)
            summary["deliver"] = {"status": "error", "delivered": False}
    else:
        log.info("Skipping deliver (--skip-deliver).")

    log.info("=== DAILY RUN SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    delivered = summary.get("deliver", {}).get("delivered", True)
    return 0 if delivered else 1


if __name__ == "__main__":
    raise SystemExit(main())
