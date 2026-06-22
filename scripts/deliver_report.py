#!/usr/bin/env python3
"""CLI: Step 3 — deliver today's current L2 report to Telegram.

Examples:
    python scripts/deliver_report.py
    python scripts/deliver_report.py --date 2026-03-26
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_pipeline.config import load_config  # noqa: E402
from meeting_pipeline.deliver import deliver_today  # noqa: E402
from meeting_pipeline.utils import get_logger  # noqa: E402

log = get_logger("scripts.deliver")


def main() -> int:
    parser = argparse.ArgumentParser(description="Deliver an L2 report to Telegram.")
    parser.add_argument("--date", help="Date (YYYY-MM-DD). Defaults to today.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-send even if this report was already delivered to the chat.",
    )
    args = parser.parse_args()

    config = load_config()
    result = deliver_today(config, date_str=args.date, force=args.force)

    log.info(
        "Deliver result: status=%s delivered=%s",
        result.get("status"),
        result.get("delivered"),
    )
    printable = {k: v for k, v in result.items() if k != "telegram"}
    tg = result.get("telegram")
    if tg is not None:
        printable["telegram"] = {
            "ok": tg.ok,
            "parts_sent": tg.parts_sent,
            "error": tg.error,
        }
    print(json.dumps(printable, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("delivered") else 1


if __name__ == "__main__":
    raise SystemExit(main())
