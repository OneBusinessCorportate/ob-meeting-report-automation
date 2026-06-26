#!/usr/bin/env python3
"""CLI: send the "report not found / will re-check in 30 min" notice on demand.

Single-purpose entrypoint for the manually-triggered Render cron job
(``ob-meeting-missing-notice``). Pressing "Trigger run" on that job in the
Render dashboard sends this exact message to the management chat — no shell
required. Always sends; ignores the normal once-per-day idempotency lock.

Examples:
    python scripts/send_missing_notice.py
    python scripts/send_missing_notice.py --date 2026-06-26
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_pipeline.config import load_config  # noqa: E402
from meeting_pipeline.deliver import send_missing_report_notice  # noqa: E402
from meeting_pipeline.utils import get_logger  # noqa: E402

log = get_logger("scripts.send_missing_notice")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Send the 'report not found / re-checking in 30 min' notice."
    )
    parser.add_argument("--date", help="Date (YYYY-MM-DD). Defaults to today.")
    args = parser.parse_args()

    config = load_config()
    config.require_telegram()
    result = send_missing_report_notice(config, date_str=args.date)

    log.info("Send result: status=%s delivered=%s", result.get("status"), result.get("delivered"))
    tg = result.get("telegram")
    printable = {k: v for k, v in result.items() if k != "telegram"}
    if tg is not None:
        printable["telegram"] = {"ok": tg.ok, "parts_sent": tg.parts_sent, "error": tg.error}
    print(json.dumps(printable, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("delivered") else 1


if __name__ == "__main__":
    raise SystemExit(main())
