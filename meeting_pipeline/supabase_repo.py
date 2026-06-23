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

    # --- Team roster ----------------------------------------------------------
    def get_team_roster(self) -> List[Dict[str, str]]:
        """Internal team roster from ``mtg_participants``.

        This is the source of truth for "go through every accountant and flag
        the ones who said nothing". Returns ``[{"name":.., "role":..}]`` for
        every internal participant; ``role`` comes from ``metadata.role`` when
        present. Empty list when the table has no internal rows.

        The daily report covers the accounting stand-up only, so internal
        people with other roles (e.g. Гор the менеджер) are excluded — they
        otherwise show up in the "Не было" line of every report. Placeholder
        surnames in ``full_name`` ("Стелла Бухгалтер") are stripped to the
        first name; real surnames ("Наира Мхитарян") are kept.
        """
        meeting_roles = {"руководитель", "бухгалтер"}
        placeholder_surnames = {"бухгалтер", "менеджер", "руководитель"}
        rows = (
            self.client.table("mtg_participants")
            .select("full_name, email, metadata")
            .eq("is_internal", True)
            .execute()
        ).data or []
        roster: List[Dict[str, str]] = []
        for row in rows:
            name = (row.get("full_name") or "").strip()
            if not name:
                continue
            meta = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
            role = ((meta or {}).get("role") or "").strip()
            if role and role.lower() not in meeting_roles:
                continue
            parts = name.split()
            if len(parts) > 1 and parts[-1].lower() in placeholder_surnames:
                name = " ".join(parts[:-1])
            entry: Dict[str, str] = {"name": name, "role": role}
            email = (row.get("email") or "").strip()
            if email:
                entry["email"] = email
            roster.append(entry)
        return roster

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
        current completed L2. Single range query (matched on ``actual_start``,
        with an ``ingested_at`` fallback for rows that have no start time).
        """
        start_utc, _ = day_bounds_utc(start_date, self.config.timezone_offset_hours)
        _, end_utc = day_bounds_utc(end_date, self.config.timezone_offset_hours)

        by_start = (
            self.client.table("mtg_meetings")
            .select("*")
            .eq("status", "completed")
            .gte("actual_start", start_utc.isoformat())
            .lt("actual_start", end_utc.isoformat())
            .execute()
        ).data or []
        by_ingest = (
            self.client.table("mtg_meetings")
            .select("*")
            .eq("status", "completed")
            .gte("ingested_at", start_utc.isoformat())
            .lt("ingested_at", end_utc.isoformat())
            .execute()
        ).data or []

        seen: set = set()
        pending: List[Dict[str, Any]] = []
        for meeting in by_start + by_ingest:
            mid = meeting["id"]
            if mid in seen:
                continue
            seen.add(mid)
            if not self.has_current_completed_analysis(mid):
                pending.append(meeting)
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

    def record_failed_analysis(
        self,
        *,
        meeting_id: str,
        model_id: Optional[str] = None,
        prompt_version: Optional[str] = None,
        processing_time_ms: Optional[int] = None,
        ai_metadata: Optional[dict] = None,
        error_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a failed analysis without cluttering the table.

        - If the meeting already has a current *completed* report, the failure is
          NOT stored (we never clobber a good report).
        - If a current *failed* row already exists, it is updated in place (no new
          version) so repeated failures — e.g. transient AI quota errors — don't
          accumulate dozens of rows.
        - Otherwise a single failed row is created.
        """
        if self.has_current_completed_analysis(meeting_id):
            log.warning(
                "Meeting %s already has a completed report; not storing failure.",
                meeting_id,
            )
            return {}

        existing = (
            self.client.table("mtg_analyses")
            .select("id")
            .eq("meeting_id", meeting_id)
            .eq("is_current", True)
            .eq("status", "failed")
            .limit(1)
            .execute()
        ).data

        if existing:
            payload: Dict[str, Any] = {"status": "failed", "error_message": error_message}
            for key, value in (
                ("model_id", model_id),
                ("prompt_version", prompt_version),
                ("processing_time_ms", processing_time_ms),
                ("ai_metadata", ai_metadata),
            ):
                if value is not None:
                    payload[key] = value
            result = (
                self.client.table("mtg_analyses")
                .update(payload)
                .eq("id", existing[0]["id"])
                .execute()
            )
            log.info("Updated existing failed analysis for meeting %s", meeting_id)
            return result.data[0] if result.data else {}

        return self.create_analysis(
            meeting_id=meeting_id,
            status="failed",
            model_id=model_id,
            prompt_version=prompt_version,
            processing_time_ms=processing_time_ms,
            ai_metadata=ai_metadata,
            error_message=error_message,
        )

    def get_today_current_report(self, on_date: date) -> Optional[Dict[str, Any]]:
        """Return the current completed L2 report for a meeting held ``on_date``."""
        start_utc, end_utc = day_bounds_utc(on_date, self.config.timezone_offset_hours)

        meetings = (
            self.client.table("mtg_meetings")
            .select("id, title, actual_start, actual_end")
            .gte("actual_start", start_utc.isoformat())
            .lt("actual_start", end_utc.isoformat())
            .execute()
        ).data or []
        if not meetings:
            meetings = (
                self.client.table("mtg_meetings")
                .select("id, title, actual_start, actual_end")
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

    def get_recent_meeting_context(
        self, before, *, limit: int = 5
    ) -> List[Dict[str, Any]]:
        """Return short context from recent completed L2 reports.

        For the ``limit`` most recent meetings held strictly before ``before``
        (a datetime or ISO string), pull each meeting's current completed L2
        ``summary`` and its ``attention_points`` (from ``report_extras``). This
        lets the AI flag issues that recur across meetings. Best-effort: returns
        an empty list and never raises, so it can't break analysis.
        """
        try:
            before_iso = before.isoformat() if hasattr(before, "isoformat") else str(before)
            meetings = (
                self.client.table("mtg_meetings")
                .select("id, title, actual_start")
                .lt("actual_start", before_iso)
                .order("actual_start", desc=True)
                .limit(limit)
                .execute()
            ).data or []

            context: List[Dict[str, Any]] = []
            for meeting in meetings:
                rows = (
                    self.client.table("mtg_analyses")
                    .select("summary, ai_metadata")
                    .eq("meeting_id", meeting["id"])
                    .eq("is_current", True)
                    .eq("status", "completed")
                    .limit(1)
                    .execute()
                ).data
                if not rows:
                    continue
                extras = ((rows[0].get("ai_metadata") or {}).get("report_extras") or {})
                context.append(
                    {
                        "date": (meeting.get("actual_start") or "")[:10],
                        "title": meeting.get("title"),
                        "summary": rows[0].get("summary"),
                        "attention_points": extras.get("attention_points") or [],
                    }
                )
            return context
        except Exception as exc:  # context is best-effort, never crash analysis
            log.warning("Could not load prior meeting context: %s", exc)
            return []

    def get_previous_meeting_tasks(self, before) -> Optional[Dict[str, Any]]:
        """Action items from the most recent analyzed meeting before ``before``.

        Powers the «динамика» block: the AI checks each previous task against
        today's transcript and the report shows the completion rate per
        accountant. Best-effort: returns None and never raises.
        """
        try:
            before_iso = before.isoformat() if hasattr(before, "isoformat") else str(before)
            meetings = (
                self.client.table("mtg_meetings")
                .select("id, title, actual_start")
                .lt("actual_start", before_iso)
                .order("actual_start", desc=True)
                .limit(5)
                .execute()
            ).data or []
            for meeting in meetings:
                rows = (
                    self.client.table("mtg_analyses")
                    .select("action_items")
                    .eq("meeting_id", meeting["id"])
                    .eq("is_current", True)
                    .eq("status", "completed")
                    .limit(1)
                    .execute()
                ).data
                if not rows:
                    continue
                action_items = rows[0].get("action_items") or []
                tasks = [
                    {
                        "task": (item.get("text") or "").strip(),
                        "assignee": item.get("assignee") or "Не указано",
                        "deadline": item.get("deadline") or "Не указано",
                    }
                    for item in action_items
                    if isinstance(item, dict) and (item.get("text") or "").strip()
                ]
                if tasks:
                    return {
                        "date": (meeting.get("actual_start") or "")[:10],
                        "tasks": tasks,
                    }
            return None
        except Exception as exc:  # best-effort, never crash analysis
            log.warning("Could not load previous meeting tasks: %s", exc)
            return None

    def get_prior_meeting_stats(self, before, *, limit: int = 5) -> List[Dict[str, Any]]:
        """Per-meeting stats for the analytics block, oldest first.

        For up to ``limit`` analyzed meetings held strictly before ``before``:
        the meeting date, the effectiveness score, the completion of THAT
        meeting's «задачи с прошлой планёрки» (team total and per assignee),
        who was absent and the manager's talk share — so the report can show
        trends over time (completion rate, attendance, talk balance, score).
        Best-effort: returns an empty list and never raises.
        """
        try:
            before_iso = before.isoformat() if hasattr(before, "isoformat") else str(before)
            meetings = (
                self.client.table("mtg_meetings")
                .select("id, actual_start")
                .lt("actual_start", before_iso)
                .order("actual_start", desc=True)
                .limit(limit)
                .execute()
            ).data or []
            stats: List[Dict[str, Any]] = []
            for meeting in meetings:
                rows = (
                    self.client.table("mtg_analyses")
                    .select("ai_metadata")
                    .eq("meeting_id", meeting["id"])
                    .eq("is_current", True)
                    .eq("status", "completed")
                    .limit(1)
                    .execute()
                ).data
                if not rows:
                    continue
                extras = ((rows[0].get("ai_metadata") or {}).get("report_extras") or {})
                statuses = [
                    s for s in extras.get("previous_tasks_status") or []
                    if isinstance(s, dict) and (s.get("task") or "").strip()
                ]
                done = partial = assessed = 0
                per_assignee: Dict[str, Dict[str, int]] = {}
                for status in statuses:
                    assignee = (status.get("assignee") or "Не указано").strip()
                    bucket = per_assignee.setdefault(
                        assignee, {"done": 0, "partial": 0, "assessed": 0, "total": 0}
                    )
                    bucket["total"] += 1
                    state = (status.get("status") or "").strip().lower()
                    # «не упоминалось» stays out of the score: nobody asked, so
                    # it must not count against the person (fair grading).
                    if state == "не упоминалось":
                        continue
                    bucket["assessed"] += 1
                    assessed += 1
                    if state == "выполнено":
                        bucket["done"] += 1
                        done += 1
                    elif state == "частично":
                        bucket["partial"] += 1
                        partial += 1
                breakdown = [
                    p for p in extras.get("participant_breakdown") or []
                    if isinstance(p, dict) and (p.get("name") or "").strip()
                ]
                # Per-person client load (number of cases voiced) for people who
                # actually took part — powers the workload trend per accountant.
                workload: Dict[str, int] = {}
                for participant in breakdown:
                    if not participant.get("participated"):
                        continue
                    cases = participant.get("cases")
                    workload[participant["name"].strip()] = (
                        len(cases) if isinstance(cases, list) else 0
                    )
                stats.append(
                    {
                        "date": (meeting.get("actual_start") or "")[:10],
                        "score": (extras.get("effectiveness") or {}).get("score"),
                        "tasks_done": done,
                        "tasks_partial": partial,
                        "tasks_assessed": assessed,
                        "tasks_total": len(statuses),
                        "per_assignee": per_assignee,
                        "has_participation": bool(breakdown),
                        "absent": [
                            p["name"] for p in breakdown if not p.get("participated")
                        ],
                        "workload": workload,
                        # Manager-conduct checklist statuses (ordered) so the
                        # analytics can spot chronic gaps, e.g. «не спрашивает
                        # про прошлые задачи N планёрок подряд».
                        "criteria": [
                            (c or {}).get("status")
                            for c in (extras.get("effectiveness") or {}).get("criteria") or []
                        ],
                        "manager_pct": (extras.get("talk_share") or {}).get("manager_pct"),
                    }
                )
            stats.reverse()  # oldest first, so trends read left to right
            return stats
        except Exception as exc:  # analytics are best-effort
            log.warning("Could not load prior meeting stats: %s", exc)
            return []

    # --- Daily send log (idempotency across cron re-runs) ----------------------
    def claim_daily_send(self, on_date: date, kind: str) -> bool:
        """Atomically claim today's one-send slot for ``kind``.

        Returns True when this run claimed the slot (caller should send) and
        False when an earlier run already did — so however often the cron
        fires, each Telegram notification goes out at most once per day.
        Fail-open: a missing table or a transient DB error must not silence
        the report entirely.
        """
        payload = {"report_date": on_date.isoformat(), "kind": kind}
        try:
            result = (
                self.client.table("mtg_delivery_log")
                .upsert(
                    payload,
                    on_conflict="report_date,kind",
                    ignore_duplicates=True,
                )
                .execute()
            )
            return bool(result.data)
        except Exception as exc:
            log.warning("Could not claim daily send slot (%s): %s", kind, exc)
            return True

    def release_daily_send(self, on_date: date, kind: str) -> None:
        """Release a claimed slot after a failed send so a later run retries."""
        try:
            (
                self.client.table("mtg_delivery_log")
                .delete()
                .eq("report_date", on_date.isoformat())
                .eq("kind", kind)
                .execute()
            )
        except Exception as exc:
            log.warning("Could not release daily send slot (%s): %s", kind, exc)

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
