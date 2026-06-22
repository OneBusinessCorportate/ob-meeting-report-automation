#!/usr/bin/env python3
"""Run the interview sync IF it is due (full ~30 days / mini ~15 days).

Called by the daily cron (scripts/run_daily.py). Time-based, idempotent, and
safe to run every day — it does nothing until a threshold is reached. Never
crashes the caller: a missing Supabase config or any error exits 0 with a log.

Manual use:
    python scripts/run_interview_schedule.py            # run if due
    python scripts/run_interview_schedule.py --kind full   # force a full run
    python scripts/run_interview_schedule.py --kind mini   # force a mini run
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_pipeline.config import load_config  # noqa: E402
from meeting_pipeline.utils import get_logger  # noqa: E402

log = get_logger("scripts.run_interview_schedule")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run interview sync if due.")
    parser.add_argument("--kind", choices=["full", "mini"], help="Force a specific run.")
    args = parser.parse_args()

    config = load_config()
    if not config.has_supabase:
        log.warning("Supabase not configured; skipping interview schedule.")
        return 0
    try:
        from interview_pipeline.schedule import run_scheduled

        result = run_scheduled(config, force_kind=args.kind)
        print(json.dumps(result, ensure_ascii=False, default=str))
    except Exception as exc:  # never break the daily cron
        log.exception("Interview schedule failed (ignored): %s", exc)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
