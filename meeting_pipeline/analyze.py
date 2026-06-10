"""Step 2 — Analyze.

Find completed L1 meetings without a current completed L2 report, extract the
FULL transcript from ``raw_transcript``, send it to the AI, and store the
structured L2 report in ``mtg_analyses``. On AI failure, persist a ``failed``
row with an ``error_message`` instead of crashing.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from .ai_client import AIClient
from .config import Config
from .supabase_repo import SupabaseRepo
from .utils import get_logger, parse_date

log = get_logger("meeting_pipeline.analyze")


def extract_full_transcript(meeting: Dict[str, Any]) -> Optional[str]:
    """Pull the full transcript text out of an L1 meeting's ``raw_transcript``.

    Only the transcript ``text`` is used — never a summary field.
    """
    raw = meeting.get("raw_transcript")
    if isinstance(raw, dict):
        text = raw.get("text")
        if isinstance(text, str) and text.strip():
            return text
    elif isinstance(raw, str) and raw.strip():
        return raw
    return None


def _participants_from_meeting(meeting: Dict[str, Any]) -> List[str]:
    meta = meeting.get("metadata") or {}
    participants = meta.get("participants")
    return participants if isinstance(participants, list) else []


def analyze_meeting(
    repo: SupabaseRepo,
    ai: AIClient,
    meeting: Dict[str, Any],
    *,
    force: bool = False,
) -> Dict[str, Any]:
    """Analyze a single L1 meeting and store the resulting L2 report.

    Idempotent by default: if a current completed L2 analysis already exists
    for this meeting, it is skipped (no duplicate version) unless ``force`` is
    set. Any unexpected error is caught so a single meeting cannot crash a batch.
    """
    # Idempotency guard — avoid creating duplicate L2 versions on rerun.
    if not force and repo.has_current_completed_analysis(meeting["id"]):
        log.info(
            "Meeting %s already has a current completed L2 report; skipping "
            "(use force=True to re-analyze).",
            meeting["id"],
        )
        return {"ok": True, "status": "skipped", "analysis": None}

    try:
        return _analyze_meeting_inner(repo, ai, meeting)
    except Exception as exc:  # never let one meeting crash the run
        log.exception("Unexpected error analyzing meeting %s: %s", meeting["id"], exc)
        try:
            analysis = repo.record_failed_analysis(
                meeting_id=meeting["id"],
                model_id=ai.model_id,
                prompt_version=ai.prompt_version,
                error_message=f"unexpected_error: {exc}",
            )
        except Exception:  # storing the failure must not crash either
            analysis = None
        return {"ok": False, "status": "failed", "analysis": analysis}


def _analyze_meeting_inner(
    repo: SupabaseRepo,
    ai: AIClient,
    meeting: Dict[str, Any],
) -> Dict[str, Any]:
    transcript = extract_full_transcript(meeting)
    meeting_date = (meeting.get("actual_start") or "")[:10] or None

    if not transcript:
        log.warning(
            "Meeting %s has no full transcript; storing failed analysis.",
            meeting["id"],
        )
        analysis = repo.record_failed_analysis(
            meeting_id=meeting["id"],
            model_id=ai.model_id,
            prompt_version=ai.prompt_version,
            error_message="transcript_not_found: raw_transcript.text is empty",
        )
        return {"ok": False, "status": "failed", "analysis": analysis}

    # Roster source of truth: explicit MEETING_TEAM_ROSTER env override if set,
    # otherwise the internal team in mtg_participants. This is what lets the
    # report flag accountants who said nothing as "не принимал(а) участия".
    team_roster = getattr(ai.config, "meeting_team_roster", []) or []
    if not team_roster:
        try:
            team_roster = repo.get_team_roster()
        except Exception as exc:  # roster is best-effort; never crash analysis
            log.warning("Could not load team roster from mtg_participants: %s", exc)
            team_roster = []

    result = ai.analyze(
        transcript,
        title=meeting.get("title"),
        meeting_date=meeting_date,
        language=meeting.get("transcript_language"),
        participants=_participants_from_meeting(meeting),
        team_roster=team_roster,
    )

    if not result.ok:
        log.error("AI analysis failed for meeting %s: %s", meeting["id"], result.error)
        analysis = repo.record_failed_analysis(
            meeting_id=meeting["id"],
            model_id=result.model_id,
            prompt_version=result.prompt_version,
            processing_time_ms=result.processing_time_ms,
            ai_metadata=result.ai_metadata or None,
            error_message=result.error,
        )
        return {"ok": False, "status": "failed", "analysis": analysis}

    report = result.report
    # Preserve extra grounded fields (decision log, praise/criticism) that have
    # no dedicated column inside ai_metadata.report_extras.
    ai_metadata = dict(result.ai_metadata or {})
    if result.extras:
        ai_metadata["report_extras"] = result.extras

    analysis = repo.create_analysis(
        meeting_id=meeting["id"],
        status="completed",
        model_id=result.model_id,
        prompt_version=result.prompt_version,
        summary=report.get("summary"),
        topics=report.get("topics"),
        action_items=report.get("action_items"),
        open_questions=report.get("open_questions"),
        people_mentioned=report.get("people_mentioned"),
        problems_risks=report.get("problems_risks"),
        sentiment=report.get("sentiment"),
        meeting_mood=report.get("meeting_mood"),
        late_start=report.get("late_start"),
        late_start_minutes=report.get("late_start_minutes"),
        mgmt_recommendations=report.get("mgmt_recommendations"),
        telegram_report_md=report.get("telegram_report_md"),
        ai_metadata=ai_metadata or None,
        processing_time_ms=result.processing_time_ms,
    )
    log.info("Stored completed L2 analysis %s for meeting %s", analysis["id"], meeting["id"])
    return {"ok": True, "status": "completed", "analysis": analysis}


def analyze_pending(
    config: Config,
    *,
    date_str: Optional[str] = None,
    source_meeting_id: Optional[str] = None,
    force: bool = False,
    days_back: int = 0,
    start_date_str: Optional[str] = None,
    end_date_str: Optional[str] = None,
    repo: Optional[SupabaseRepo] = None,
    ai: Optional[AIClient] = None,
) -> Dict[str, Any]:
    """Analyze pending meetings for a date, a date range, or one specific meeting.

    Pass ``days_back`` (e.g. 14) or ``start_date_str``/``end_date_str`` to process
    a backfilled range in one call. Safe to rerun: meetings that already have a
    current completed L2 report are skipped unless ``force`` is set.
    """
    repo = repo or SupabaseRepo(config)
    ai = ai or AIClient(config)

    if source_meeting_id:
        source = repo.ensure_source(config.default_source)
        meeting = repo.get_meeting_by_source_meeting_id(source["id"], source_meeting_id)
        meetings = [meeting] if meeting else []
        if not meeting:
            log.warning("No meeting found with source_meeting_id=%s", source_meeting_id)
    else:
        on_date: date = parse_date(date_str, config.timezone_offset_hours)
        # Resolve an optional date range (backfill); otherwise just the one day.
        start = end = None
        if start_date_str or end_date_str:
            start = parse_date(start_date_str, config.timezone_offset_hours) if start_date_str else on_date
            end = parse_date(end_date_str, config.timezone_offset_hours) if end_date_str else on_date
        elif days_back and days_back > 0:
            from datetime import timedelta

            end = on_date
            start = on_date - timedelta(days=days_back)

        if start or end:
            meetings = repo.get_meetings_without_analysis_in_range(start or end, end or start)
        else:
            # The pending query already filters out meetings with a current report.
            meetings = repo.get_today_meetings_without_analysis(on_date)

    results = [analyze_meeting(repo, ai, m, force=force) for m in meetings if m]
    completed = sum(1 for r in results if r["status"] == "completed")
    skipped = sum(1 for r in results if r["status"] == "skipped")
    failed = sum(1 for r in results if r["status"] == "failed")
    return {
        "ok": failed == 0,
        "analyzed": len(results),
        "completed": completed,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }
