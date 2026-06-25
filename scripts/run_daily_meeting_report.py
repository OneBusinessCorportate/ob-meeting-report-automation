#!/usr/bin/env python3
"""CLI: combined daily run — ingest -> analyze -> deliver.

This is the script the Render cron job runs at 11:30 Armenia time.

If the report is not ready at 11:30, the script retries every 30 minutes
(up to 4 times, covering until 13:30 Armenia) so a late transcript is still
delivered automatically without manual intervention.

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
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_pipeline.analyze import analyze_pending  # noqa: E402
from meeting_pipeline.config import load_config  # noqa: E402
from meeting_pipeline.deliver import deliver_today  # noqa: E402
from meeting_pipeline.ingest import ingest_meeting  # noqa: E402
from meeting_pipeline.utils import get_logger  # noqa: E402

log = get_logger("scripts.daily")

_RETRY_INTERVAL = 30 * 60   # 30 minutes between retries
_MAX_RETRIES = 3             # 3 retries → 4 total checks (e.g. 10:00/10:30/11:00/11:30 Armenia)
# Only retry on transient "not yet available" or Telegram failure.
# "report_not_found" (notice sent) and "notice_already_sent" are terminal — stop retrying.
_RETRY_STATUSES = {"no_report_waiting", "delivery_failed"}


def _run_pipeline(config, args: argparse.Namespace, summary: dict, *, check_num: int = 1, final_check: bool = False) -> None:
    """Run one ingest→analyze→deliver pass, updating *summary* in-place."""
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

    if not args.skip_analyze:
        log.info("=== STEP 2: ANALYZE ===")
        try:
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

    if not args.skip_deliver:
        log.info("=== STEP 3: DELIVER ===")
        try:
            deliver_result = deliver_today(config, date_str=args.date, check_num=check_num, final_check=final_check)
            summary["deliver"] = {
                "status": deliver_result.get("status"),
                "delivered": deliver_result.get("delivered"),
            }
        except Exception as exc:
            log.exception("Deliver step failed: %s", exc)
            summary["deliver"] = {"status": "error", "delivered": False}
    else:
        log.info("Skipping deliver (--skip-deliver).")


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
    parser.add_argument(
        "--final-check",
        action="store_true",
        help=(
            "Treat this as the last attempt for the day: send the 'no calls' notice "
            "immediately if no report is found instead of waiting silently. "
            "Used by the afternoon retry cron."
        ),
    )
    parser.add_argument("--skip-ingest", action="store_true")
    parser.add_argument("--skip-analyze", action="store_true")
    parser.add_argument("--skip-deliver", action="store_true")
    args = parser.parse_args()

    config = load_config()
    summary: dict = {}

    # First attempt (check 1). When --final-check is passed (afternoon cron)
    # this is the only attempt and sends the "no calls" notice immediately.
    _run_pipeline(config, args, summary, check_num=1, final_check=args.final_check)

    # Retry loop (only when NOT in --final-check mode).
    # Checks 1–3 send "Запись не найден…" and return "no_report_waiting" → retry.
    # Check 4 (retry_num == _MAX_RETRIES) sends "📭 Сегодня звонков не было."
    # All pipeline steps are idempotent so re-running on the same day is safe.
    for retry_num in range(1, _MAX_RETRIES + 1):
        deliver_status = summary.get("deliver", {}).get("status")
        if deliver_status not in _RETRY_STATUSES:
            break
        check_num = retry_num + 1          # checks 2, 3, 4
        is_final = not args.final_check and (retry_num == _MAX_RETRIES)
        log.info(
            "Delivery pending (%s) — check %d/%d in %d min%s (sleeping).",
            deliver_status, check_num, _MAX_RETRIES + 1, _RETRY_INTERVAL // 60,
            " [final]" if is_final else "",
        )
        time.sleep(_RETRY_INTERVAL)
        _run_pipeline(config, args, summary, check_num=check_num, final_check=is_final)

    log.info("=== DAILY RUN SUMMARY ===")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))

    deliver_summary = summary.get("deliver", {})
    delivered = deliver_summary.get("delivered", True)
    # A repeat run on the same day sends nothing on purpose — that's success.
    if deliver_summary.get("status") in ("already_delivered", "notice_already_sent"):
        delivered = True
    return 0 if delivered else 1


if __name__ == "__main__":
    raise SystemExit(main())
