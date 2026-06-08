"""Supabase data-access layer for the L1 (mtg_meetings) / L2 (mtg_analyses) tables.

Uses the service-role key (server-side only). All functions are written
against the EXISTING ``mtg_*`` schema — this module never creates a parallel
system. Versioning of analyses relies on the existing
``supersede_old_analyses`` trigger: inserting a row with ``is_current = true``
automatically demotes the previous current analysis to
``is_current = false, status = 'superseded'``.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, List, Optional

from .config import Config
from .utils import day_bounds_utc, get_logger

log = get_logger("meeting_pipeline.supabase")

# Mirror of the spec'd Timeless source defaults.
TIMELESS_SOURCE_DEFAULTS = {
    "name": "timeless",
    "display_name": "Timeless",
    "api_base_url": "https://api.timeless.day/v1",
    "is_active": True,
}


class SupabaseRepo:
    def __init__(self, config: Config, client: Any = None):
        self.config = config
        if client is not None:
            self.client = client
        else:
            config.require_supabase()
            from supabase import create_client  # imported lazily

            self.client = create_client(
                config.supabase_url, config.supabase_service_role_key
            )

    # --- Sources --------------------------------------------------------------
    def ensure_source(
        self,
        name: str,
        display_name: Optional[str] = None,
        api_base_url: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Return the source row for ``name``, creating it if missing.

        Idempotent: the Timeless source already exists in production, so this
        normally just fetches it.
        """
        existing = (
            self.client.table("mtg_sources").select("*").eq("name", name).execute()
        )
        if existing.data:
            return existing.data[0]

        defaults = (
            TIMELESS_SOURCE_DEFAULTS if name == "timeless" else {"name": name}
        )
        payload = {
            "name": name,
            "display_name": display_name or defaults.get("display_name", name),
            "api_base_url": api_base_url or defaults.get("api_base_url"),
            "is_active": True,
        }
        log.info("Creating mtg_sources row for source '%s'", name)
        created = self.client.table("mtg_sources").insert(payload).execute()
        return created.data[0]

    # --- Meetings (L1) --------------------------------------------------------
    def upsert_meeting(
        self,
        *,
        source_id: str,
        source_meeting_id: str,
        title: str,
        status: str = "completed",
        scheduled_start: Optional[str] = None,
        actual_start: Optional[str] = None,
        actual_end: Optional[str] = None,
        duration_seconds: Optional[int] = None,
        recording_url: Optional[str] = None,
        transcript_language: Optional[str] = None,
        raw_transcript: Optional[dict] = None,
        raw_notes: Optional[str] = None,
        raw_summary: Optional[str] = None,
        raw_action_items: Optional[list] = None,
        raw_documents: Optional[list] = None,
        metadata: Optional[dict] = None,
        source_fetched_at: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Insert or update a meeting, deduped by (source_id, source_meeting_id)."""
        payload: Dict[str, Any] = {
            "source_id": source_id,
            "source_meeting_id": source_meeting_id,
            "title": title,
            "status": status,
        }
        optional = {
            "scheduled_start": scheduled_start,
            "actual_start": actual_start,
            "actual_end": actual_end,
            "duration_seconds": duration_seconds,
            "recording_url": recording_url,
            "transcript_language": transcript_language,
            "raw_transcript": raw_transcript,
            "raw_notes": raw_notes,
            "raw_summary": raw_summary,
            "raw_action_items": raw_action_items,
            "raw_documents": raw_documents,
            "metadata": metadata,
            "source_fetched_at": source_fetched_at,
        }
        payload.update({k: v for k, v in optional.items() if v is not None})

        log.info(
            "Upserting meeting source_meeting_id=%s status=%s",
            source_meeting_id,
            status,
        )
        result = (
            self.client.table("mtg_meetings")
            .upsert(payload, on_conflict="source_id,source_meeting_id")
            .execute()
        )
        return result.data[0]

    def get_meeting_by_source_meeting_id(
        self, source_id: str, source_meeting_id: str
    ) -> Optional[Dict[str, Any]]:
        result = (
            self.client.table("mtg_meetings")
            .select("*")
            .eq("source_id", source_id)
            .eq("source_meeting_id", source_meeting_id)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None

    def get_today_meetings_without_analysis(
        self, on_date: date
    ) -> List[Dict[str, Any]]:
        """Completed L1 meetings for ``on_date`` lacking a current completed L2.

        "Today" is the local Armenian day, matched against ``actual_start``
        (falling back to ``ingested_at`` when actual_start is null).
        """
        start_utc, end_utc = day_bounds_utc(on_date, self.config.timezone_offset_hours)

        meetings = (
            self.client.table("mtg_meetings")
            .select("*")
            .eq("status", "completed")
            .gte("actual_start", start_utc.isoformat())
            .lt("actual_start", end_utc.isoformat())
            .execute()
        ).data or []

        # Fallback for meetings ingested today without an actual_start.
        if not meetings:
            meetings = (
                self.client.table("mtg_meetings")
                .select("*")
                .eq("status", "completed")
                .gte("ingested_at", start_utc.isoformat())
                .lt("ingested_at", end_utc.isoformat())
                .execute()
            ).data or []

        pending: List[Dict[str, Any]] = []
        for meeting in meetings:
            current = (
                self.client.table("mtg_analyses")
                .select("id")
                .eq("meeting_id", meeting["id"])
                .eq("is_current", True)
                .eq("status", "completed")
                .limit(1)
                .execute()
            ).data
            if not current:
                pending.append(meeting)
        log.info(
            "Found %d completed meeting(s) for %s without a current L2 report",
            len(pending),
            on_date.isoformat(),
        )
        return pending

    def get_meetings_without_analysis_in_range(
        self, start_date: date, end_date: date
    ) -> List[Dict[str, Any]]:
        """Completed L1 meetings across an inclusive local-date range lacking a
        current completed L2. Delegates per day to reuse the same matching logic.
        """
        from datetime import timedelta

        seen: set = set()
        pending: List[Dict[str, Any]] = []
        day = start_date
        while day <= end_date:
            for meeting in self.get_today_meetings_without_analysis(day):
                if meeting["id"] not in seen:
                    seen.add(meeting["id"])
                    pending.append(meeting)
            day += timedelta(days=1)
        return pending

    # --- Analyses (L2) --------------------------------------------------------
    def has_current_completed_analysis(self, meeting_id: str) -> bool:
        """True if the meeting already has a current, completed L2 report."""
        existing = (
            self.client.table("mtg_analyses")
            .select("id")
            .eq("meeting_id", meeting_id)
            .eq("is_current", True)
            .eq("status", "completed")
            .limit(1)
            .execute()
        ).data
        return bool(existing)

    def _next_version(self, meeting_id: str) -> int:
        existing = (
            self.client.table("mtg_analyses")
            .select("version")
            .eq("meeting_id", meeting_id)
            .order("version", desc=True)
            .limit(1)
            .execute()
        ).data
        return (existing[0]["version"] + 1) if existing else 1

    def create_analysis(
        self,
        *,
        meeting_id: str,
        status: str,
        model_id: Optional[str] = None,
        prompt_version: Optional[str] = None,
        summary: Optional[str] = None,
        topics: Optional[list] = None,
        action_items: Optional[list] = None,
        open_questions: Optional[list] = None,
        people_mentioned: Optional[list] = None,
        problems_risks: Optional[list] = None,
        sentiment: Optional[str] = None,
        meeting_mood: Optional[dict] = None,
        late_start: Optional[bool] = None,
        late_start_minutes: Optional[int] = None,
        mgmt_recommendations: Optional[list] = None,
        telegram_report_md: Optional[str] = None,
        ai_metadata: Optional[dict] = None,
        processing_time_ms: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Create a new L2 analysis row.

        Always inserts the next version with ``is_current = true`` so the DB
        trigger supersedes the previous current analysis. A ``failed`` row is
        stored the same way (with an ``error_message``) instead of crashing.
        """
        version = self._next_version(meeting_id)
        payload: Dict[str, Any] = {
            "meeting_id": meeting_id,
            "version": version,
            "status": status,
            "is_current": True,
        }
        optional = {
            "model_id": model_id,
            "prompt_version": prompt_version,
            "summary": summary,
            "topics": topics,
            "action_items": action_items,
            "open_questions": open_questions,
            "people_mentioned": people_mentioned,
            "problems_risks": problems_risks,
            "sentiment": sentiment,
            "meeting_mood": meeting_mood,
            "late_start": late_start,
            "late_start_minutes": late_start_minutes,
            "mgmt_recommendations": mgmt_recommendations,
            "telegram_report_md": telegram_report_md,
            "ai_metadata": ai_metadata,
            "processing_time_ms": processing_time_ms,
            "error_message": error_message,
        }
        payload.update({k: v for k, v in optional.items() if v is not None})

        log.info(
            "Creating L2 analysis meeting_id=%s version=%d status=%s",
            meeting_id,
            version,
            status,
        )
        result = self.client.table("mtg_analyses").insert(payload).execute()
        return result.data[0]

    def get_today_current_report(self, on_date: date) -> Optional[Dict[str, Any]]:
        """Return the current completed L2 report for a meeting held ``on_date``."""
        start_utc, end_utc = day_bounds_utc(on_date, self.config.timezone_offset_hours)

        meetings = (
            self.client.table("mtg_meetings")
            .select("id, title, actual_start")
            .gte("actual_start", start_utc.isoformat())
            .lt("actual_start", end_utc.isoformat())
            .execute()
        ).data or []
        if not meetings:
            meetings = (
                self.client.table("mtg_meetings")
                .select("id, title, actual_start")
                .gte("ingested_at", start_utc.isoformat())
                .lt("ingested_at", end_utc.isoformat())
                .execute()
            ).data or []

        for meeting in meetings:
            analysis = (
                self.client.table("mtg_analyses")
                .select("*")
                .eq("meeting_id", meeting["id"])
                .eq("is_current", True)
                .eq("status", "completed")
                .order("version", desc=True)
                .limit(1)
                .execute()
            ).data
            if analysis:
                row = analysis[0]
                row["_meeting"] = meeting
                return row
        return None

    def mark_delivery_status(
        self,
        analysis_id: str,
        *,
        delivered: bool,
        detail: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record Telegram delivery outcome inside ``ai_metadata.delivery``."""
        from datetime import datetime, timezone as _tz

        current = (
            self.client.table("mtg_analyses")
            .select("ai_metadata")
            .eq("id", analysis_id)
            .limit(1)
            .execute()
        ).data
        metadata = (current[0].get("ai_metadata") if current else None) or {}
        metadata["delivery"] = {
            "delivered": delivered,
            "detail": detail,
            "at": datetime.now(_tz.utc).isoformat(),
        }
        result = (
            self.client.table("mtg_analyses")
            .update({"ai_metadata": metadata})
            .eq("id", analysis_id)
            .execute()
        )
        log.info(
            "Marked delivery status for analysis=%s delivered=%s",
            analysis_id,
            delivered,
        )
        return result.data[0] if result.data else {}
