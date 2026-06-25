#!/usr/bin/env python3
"""Evening ArmSoft/TaxService cross-check delivery (18:00 Armenia / 14:00 UTC).

Sends a standalone message with today's ArmSoft document activity and
TaxService invoice data so bookkeepers can review before the next morning's
planning meeting. Runs separately from the morning report cron.
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from meeting_pipeline.config import Config
from meeting_pipeline.deliver import deliver_armsoft_today
from meeting_pipeline.utils import get_logger

log = get_logger("scripts.deliver_armsoft")


def main() -> int:
    config = Config()
    result = deliver_armsoft_today(config)
    status = result.get("status")
    if result.get("ok"):
        log.info("ArmSoft delivery: %s", status)
        return 0
    log.error("ArmSoft delivery failed: %s", status)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
