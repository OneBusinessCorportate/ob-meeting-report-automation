"""Time-based scheduler for the interview sync (no extra Render service needed).

The existing daily cron calls this every weekday. Based on the elapsed time
since the last completed scheduled run (tracked in ``intv_sync_logs``), it
decides whether anything is due:

  * FULL sync  ~every 30 days — reads the sheet, fetches transcripts, runs the
    AI analysis, stores everything. Resets BOTH the full and mini timers.
  * MINI sync  ~every 15 days — refreshes candidates + fetches new transcripts
    only (no AI). Resets the mini timer.

So a refresh happens ~every 15 days and a full analyzed pass ~every 30 days,
triggered by the first daily run on/after each threshold. Idempotent: nothing
is re-processed unless due, and the markers are only written when a run
actually processed rows (so an unconfigured sheet just retries next day).
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from meeting_pipeline.config import Config
from meeting_pipeline.utils import get_logger
from .analysis_store import InterviewStore
from .pipeline import sync_interviews

log = get_logger("interview_pipeline.schedule")


def decide_kind(
    last_full: Optional[datetime],
    last_mini: Optional[datetime],
    now: datetime,
    *,
    full_days: int = 30,
    mini_days: int = 15,
) -> Optional[str]:
    """Return 'full', 'mini', or None depending on what is due."""
    full_due = last_full is None or (now - last_full).days >= full_days
    if full_due:
        return "full"
    mini_due = last_mini is None or (now - last_mini).days >= mini_days
    if mini_due:
        return "mini"
    return None


def run_scheduled(
    config: Config,
    *,
    store: Optional[InterviewStore] = None,
    now: Optional[datetime] = None,
    force_kind: Optional[str] = None,
) -> Dict[str, Any]:
    """Decide what's due and run it. Never raises on 'nothing due'."""
    full_days = int(os.environ.get("INTERVIEW_FULL_INTERVAL_DAYS", "30"))
    mini_days = int(os.environ.get("INTERVIEW_MINI_INTERVAL_DAYS", "15"))
    store = store or InterviewStore(config)
    now = now or datetime.now(timezone.utc)

    last_full = store.last_schedule_run("full")
    last_mini = store.last_schedule_run("mini")
    kind = force_kind or decide_kind(
        last_full, last_mini, now, full_days=full_days, mini_days=mini_days
    )
    if kind is None:
        log.info(
            "Interview sync not due (last full=%s, mini=%s; thresholds %d/%d days).",
            last_full, last_mini, full_days, mini_days,
        )
        return {"ran": False, "reason": "not_due",
                "last_full": str(last_full), "last_mini": str(last_mini)}

    do_analyze = kind == "full"
    log.info("Scheduled interview sync due: kind=%s (analyze=%s).", kind, do_analyze)
    result = sync_interviews(config, do_analyze=do_analyze, store=store)

    # Only mark the timer when something actually processed (so an unconfigured
    # sheet retries next day instead of waiting out the whole interval).
    if result.get("processed", 0) > 0:
        msg = f"{kind} sync: {result.get('processed')} processed, {result.get('errors')} errors"
        store.log_schedule("mini", message=msg, detail={"kind": kind})
        if kind == "full":
            store.log_schedule("full", message=msg, detail={"kind": kind})
    else:
        log.warning("Scheduled %s sync processed 0 rows; timer not marked (will retry).", kind)

    return {"ran": True, "kind": kind, "do_analyze": do_analyze, **{
        k: result.get(k) for k in ("processed", "analysis_done", "errors", "counts", "run_id")
    }}
