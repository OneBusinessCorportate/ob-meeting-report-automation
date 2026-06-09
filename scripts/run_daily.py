#!/usr/bin/env python3
"""Daily cron entrypoint — runs BOTH jobs in one Render service:

  1) the morning meeting report (scripts/run_daily_meeting_report.py), and
  2) the interview-sync schedule check (scripts/run_interview_schedule.py),
     which runs a FULL sync ~every 30 days and a MINI sync ~every 15 days.

The two steps are isolated: the interview step never affects the meeting
report's exit status, and vice versa. The overall exit code reflects the
meeting report (the time-critical 11:00 deliverable).
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PY = sys.executable


def _run(script: str, *args: str) -> int:
    cmd = [PY, str(ROOT / "scripts" / script), *args]
    print(f"=== run_daily: {' '.join(cmd)} ===", flush=True)
    try:
        return subprocess.call(cmd, cwd=str(ROOT))
    except Exception as exc:  # noqa: BLE001
        print(f"run_daily: {script} raised: {exc}", file=sys.stderr, flush=True)
        return 1


def main() -> int:
    # 1) Time-critical morning meeting report (determines the exit code).
    meeting_rc = _run("run_daily_meeting_report.py")
    # 2) Interview sync schedule (best-effort; never fails the cron).
    _run("run_interview_schedule.py")
    return meeting_rc


if __name__ == "__main__":
    raise SystemExit(main())
