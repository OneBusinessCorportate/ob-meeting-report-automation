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
) -> Dict[str, Any]:
    """Analyze a single L1 meeting and store the resulting L2 report."""
    transcript = extract_full_transcript(meeting)
    meeting_date = (meeting.get("actual_start") or "")[:10] or None

    if not transcript:
        log.warning(
            "Meeting %s has no full transcript; storing failed analysis.",
            meeting["id"],
        )
        analysis = repo.create_analysis(
            meeting_id=meeting["id"],
            status="failed",
            model_id=ai.model_id,
            prompt_version=ai.prompt_version,
            error_message="transcript_not_found: raw_transcript.text is empty",
        )
        return {"ok": False, "status": "failed", "analysis": analysis}

    result = ai.analyze(
        transcript,
        title=meeting.get("title"),
        meeting_date=meeting_date,
        language=meeting.get("transcript_language"),
        participants=_participants_from_meeting(meeting),
    )

    if not result.ok:
        log.error("AI analysis failed for meeting %s: %s", meeting["id"], result.error)
        analysis = repo.create_analysis(
            meeting_id=meeting["id"],
            status="failed",
            model_id=result.model_id,
            prompt_version=result.prompt_version,
            processing_time_ms=result.processing_time_ms,
            ai_metadata=result.ai_metadata or None,
            error_message=result.error,
        )
        return {"ok": False, "status": "failed", "analysis": analysis}

    report = result.report
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
        ai_metadata=result.ai_metadata or None,
        processing_time_ms=result.processing_time_ms,
    )
    log.info("Stored completed L2 analysis %s for meeting %s", analysis["id"], meeting["id"])
    return {"ok": True, "status": "completed", "analysis": analysis}


def analyze_pending(
    config: Config,
    *,
    date_str: Optional[str] = None,
    source_meeting_id: Optional[str] = None,
    repo: Optional[SupabaseRepo] = None,
    ai: Optional[AIClient] = None,
) -> Dict[str, Any]:
    """Analyze all pending meetings for a date, or one specific meeting."""
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
        meetings = repo.get_today_meetings_without_analysis(on_date)

    results = [analyze_meeting(repo, ai, m) for m in meetings if m]
    completed = sum(1 for r in results if r["ok"])
    return {
        "ok": completed > 0 or not meetings,
        "analyzed": len(results),
        "completed": completed,
        "failed": len(results) - completed,
        "results": results,
    }
