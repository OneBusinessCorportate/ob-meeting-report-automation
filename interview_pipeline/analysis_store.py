"""Supabase data-access for the interview-analysis schema (intv_* tables).

Tables (see sql/interview_analysis_schema.sql):
  intv_candidates, intv_interviews, intv_transcripts, intv_transcript_segments,
  intv_analyses, intv_scores, intv_sync_logs.

Design notes:
  * Candidates are deduped by (track, normalized_name).
  * Interviews are deduped by (source_id, source_call_id) — safe to rerun.
  * Transcripts are 1:1 with an interview (upserted); raw text is kept separate
    from cleaned text. Segments are upserted by (transcript_id, idx).
  * Analyses are versioned with is_current; the DB trigger supersedes the old
    current row. Scores are 1:1 with an analysis row.
  * Everything uses the service-role key (server-side only).
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from meeting_pipeline.config import Config
from meeting_pipeline.supabase_repo import SupabaseRepo
from meeting_pipeline.utils import get_logger

log = get_logger("interview_pipeline.store")

# Interview processing status state machine.
STATUS_NEW = "new"
STATUS_LINK_MISSING = "link_missing"
STATUS_TRANSCRIPT_PENDING = "transcript_pending"
STATUS_TRANSCRIPT_READY = "transcript_ready"
STATUS_ANALYSIS_PENDING = "analysis_pending"
STATUS_ANALYSIS_DONE = "analysis_done"
STATUS_ERROR = "error"

TERMINAL_DONE_STATUSES = {STATUS_ANALYSIS_DONE}

CANDIDATES = "intv_candidates"
INTERVIEWS = "intv_interviews"
TRANSCRIPTS = "intv_transcripts"
SEGMENTS = "intv_transcript_segments"
ANALYSES = "intv_analyses"
SCORES = "intv_scores"
SYNC_LOGS = "intv_sync_logs"


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", (name or "").strip()).lower()


def _drop_none(d: Dict[str, Any]) -> Dict[str, Any]:
    return {k: v for k, v in d.items() if v is not None}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class InterviewStore:
    def __init__(self, config: Config, client: Any = None):
        self.config = config
        if client is not None:
            self.client = client
        else:
            config.require_supabase()
            from supabase import create_client

            self.client = create_client(
                config.supabase_url, config.supabase_service_role_key
            )
        self._sources = SupabaseRepo(config, client=self.client)

    def ensure_timeless_source(self) -> Dict[str, Any]:
        return self._sources.ensure_source("timeless")

    # --- candidates -----------------------------------------------------------
    def upsert_candidate(self, cand: Any) -> Dict[str, Any]:
        payload = _drop_none(
            {
                "full_name": cand.full_name,
                "normalized_name": normalize_name(cand.full_name),
                "track": cand.track or "buh",
                "role": cand.role,
                "email": cand.email,
                "phone": cand.phone,
                "contact_raw": cand.contact_raw,
                "resume_comment": cand.resume_comment,
                "sheet_status": cand.sheet_status,
                "grade_start": cand.grade_start,
                "test_score": cand.test_score,
                "test_sent_at": cand.test_sent_at,
                "probation_start": cand.probation_start,
                "terminated_at": cand.terminated_at,
                "termination_reason": cand.termination_reason,
                "source_sheet": cand.source_sheet,
                "source_row": cand.source_row,
                "metadata": cand.metadata or None,
            }
        )
        result = (
            self.client.table(CANDIDATES)
            .upsert(payload, on_conflict="track,normalized_name")
            .execute()
        )
        return result.data[0]

    # --- interviews -----------------------------------------------------------
    def get_interview(self, source_id: str, source_call_id: str) -> Optional[Dict[str, Any]]:
        rows = (
            self.client.table(INTERVIEWS)
            .select("*")
            .eq("source_id", source_id)
            .eq("source_call_id", source_call_id)
            .limit(1)
            .execute()
        ).data
        return rows[0] if rows else None

    def upsert_interview(
        self,
        *,
        candidate_id: str,
        source_id: Optional[str],
        source_call_id: str,
        call_url: Optional[str] = None,
        interview_type: str = "interview",
        status: str = STATUS_NEW,
        title: Optional[str] = None,
        language: Optional[str] = None,
        sheet_ref: Optional[dict] = None,
        metadata: Optional[dict] = None,
    ) -> Dict[str, Any]:
        payload = _drop_none(
            {
                "candidate_id": candidate_id,
                "source_id": source_id,
                "source_call_id": source_call_id,
                "call_url": call_url,
                "interview_type": interview_type,
                "status": status,
                "title": title,
                "language": language,
                "sheet_ref": sheet_ref,
                "metadata": metadata,
            }
        )
        result = (
            self.client.table(INTERVIEWS)
            .upsert(payload, on_conflict="source_id,source_call_id")
            .execute()
        )
        return result.data[0]

    def set_interview_status(
        self, interview_id: str, status: str, *, error_message: Optional[str] = None,
        extra: Optional[dict] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"status": status}
        # Clear a stale error when moving to a non-error status.
        payload["error_message"] = error_message
        if extra:
            payload.update(extra)
        result = (
            self.client.table(INTERVIEWS).update(payload).eq("id", interview_id).execute()
        )
        log.info("Interview %s -> status=%s", interview_id, status)
        return result.data[0] if result.data else {}

    # --- transcripts ----------------------------------------------------------
    def save_transcript(
        self,
        interview_id: str,
        *,
        raw_text: str,
        cleaned_text: str,
        language: Optional[str],
        source: Optional[str],
        segments: Optional[List[dict]] = None,
        raw_payload: Optional[dict] = None,
        stats: Optional[Dict[str, int]] = None,
    ) -> Dict[str, Any]:
        stats = stats or {}
        payload = _drop_none(
            {
                "interview_id": interview_id,
                "language": language,
                "source": source,
                "raw_text": raw_text,
                "cleaned_text": cleaned_text,
                "raw_payload": raw_payload,
                "char_count": stats.get("char_count"),
                "word_count": stats.get("word_count"),
                "segment_count": stats.get("segment_count"),
                "speaker_count": stats.get("speaker_count"),
            }
        )
        transcript = (
            self.client.table(TRANSCRIPTS)
            .upsert(payload, on_conflict="interview_id")
            .execute()
        ).data[0]
        if segments:
            self._save_segments(transcript["id"], interview_id, segments)
        return transcript

    def _save_segments(self, transcript_id: str, interview_id: str, segments: List[dict]) -> None:
        rows = [
            _drop_none(
                {
                    "transcript_id": transcript_id,
                    "interview_id": interview_id,
                    "idx": seg["idx"],
                    "speaker": seg.get("speaker"),
                    "start_ms": seg.get("start_ms"),
                    "end_ms": seg.get("end_ms"),
                    "text": seg.get("text"),
                }
            )
            for seg in segments
            if seg.get("text")
        ]
        if rows:
            self.client.table(SEGMENTS).upsert(
                rows, on_conflict="transcript_id,idx"
            ).execute()

    # --- analyses + scores ----------------------------------------------------
    def has_current_completed_analysis(self, interview_id: str) -> bool:
        rows = (
            self.client.table(ANALYSES)
            .select("id")
            .eq("interview_id", interview_id)
            .eq("is_current", True)
            .eq("status", "completed")
            .limit(1)
            .execute()
        ).data
        return bool(rows)

    def _next_version(self, interview_id: str) -> int:
        rows = (
            self.client.table(ANALYSES)
            .select("version")
            .eq("interview_id", interview_id)
            .order("version", desc=True)
            .limit(1)
            .execute()
        ).data
        return (rows[0]["version"] + 1) if rows else 1

    def create_analysis(
        self,
        *,
        interview_id: str,
        candidate_id: Optional[str],
        status: str,
        model_id: Optional[str] = None,
        prompt_version: Optional[str] = None,
        transcript_language: Optional[str] = None,
        summary: Optional[str] = None,
        summary_original: Optional[str] = None,
        candidate_strengths: Optional[list] = None,
        candidate_weaknesses: Optional[list] = None,
        red_flags: Optional[list] = None,
        next_steps: Optional[list] = None,
        recommendation: Optional[str] = None,
        reasoning: Optional[str] = None,
        ai_metadata: Optional[dict] = None,
        processing_time_ms: Optional[int] = None,
        error_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        version = self._next_version(interview_id)
        payload = _drop_none(
            {
                "interview_id": interview_id,
                "candidate_id": candidate_id,
                "version": version,
                "is_current": True,
                "status": status,
                "model_id": model_id,
                "prompt_version": prompt_version,
                "transcript_language": transcript_language,
                "summary": summary,
                "summary_original": summary_original,
                "candidate_strengths": candidate_strengths,
                "candidate_weaknesses": candidate_weaknesses,
                "red_flags": red_flags,
                "next_steps": next_steps,
                "recommendation": recommendation,
                "reasoning": reasoning,
                "ai_metadata": ai_metadata,
                "processing_time_ms": processing_time_ms,
                "error_message": error_message,
            }
        )
        result = self.client.table(ANALYSES).insert(payload).execute()
        log.info(
            "Created analysis interview=%s version=%d status=%s", interview_id, version, status
        )
        return result.data[0]

    def record_failed_analysis(
        self,
        *,
        interview_id: str,
        candidate_id: Optional[str],
        model_id: Optional[str] = None,
        prompt_version: Optional[str] = None,
        processing_time_ms: Optional[int] = None,
        ai_metadata: Optional[dict] = None,
        error_message: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Record a failure without clobbering a good report or piling up rows."""
        if self.has_current_completed_analysis(interview_id):
            log.warning("Interview %s already has a completed analysis; keeping it.", interview_id)
            return {}
        existing = (
            self.client.table(ANALYSES)
            .select("id")
            .eq("interview_id", interview_id)
            .eq("is_current", True)
            .eq("status", "failed")
            .limit(1)
            .execute()
        ).data
        if existing:
            payload = _drop_none(
                {
                    "status": "failed",
                    "error_message": error_message,
                    "model_id": model_id,
                    "prompt_version": prompt_version,
                    "processing_time_ms": processing_time_ms,
                    "ai_metadata": ai_metadata,
                }
            )
            payload["error_message"] = error_message  # ensure it's set even if None
            result = (
                self.client.table(ANALYSES).update(payload).eq("id", existing[0]["id"]).execute()
            )
            return result.data[0] if result.data else {}
        return self.create_analysis(
            interview_id=interview_id,
            candidate_id=candidate_id,
            status="failed",
            model_id=model_id,
            prompt_version=prompt_version,
            processing_time_ms=processing_time_ms,
            ai_metadata=ai_metadata,
            error_message=error_message,
        )

    def create_scores(
        self,
        *,
        analysis_id: str,
        interview_id: str,
        candidate_id: Optional[str],
        communication_score: Optional[int],
        professional_score: Optional[int],
        motivation_score: Optional[int],
        overall_score: Optional[int],
        score_scale: str = "0-10",
    ) -> Dict[str, Any]:
        payload = _drop_none(
            {
                "analysis_id": analysis_id,
                "interview_id": interview_id,
                "candidate_id": candidate_id,
                "score_scale": score_scale,
                "communication_score": communication_score,
                "professional_score": professional_score,
                "motivation_score": motivation_score,
                "overall_score": overall_score,
            }
        )
        result = (
            self.client.table(SCORES).upsert(payload, on_conflict="analysis_id").execute()
        )
        return result.data[0] if result.data else {}

    # --- logging --------------------------------------------------------------
    def log_event(
        self,
        run_id: str,
        stage: str,
        *,
        level: str = "info",
        status: Optional[str] = None,
        interview_id: Optional[str] = None,
        candidate_id: Optional[str] = None,
        message: Optional[str] = None,
        detail: Optional[dict] = None,
    ) -> None:
        payload = _drop_none(
            {
                "run_id": run_id,
                "stage": stage,
                "level": level,
                "status": status,
                "interview_id": interview_id,
                "candidate_id": candidate_id,
                "message": message,
                "detail": detail,
            }
        )
        try:
            self.client.table(SYNC_LOGS).insert(payload).execute()
        except Exception as exc:  # logging must never break the pipeline
            log.warning("Could not write sync log (%s): %s", stage, exc)

    @staticmethod
    def new_run_id() -> str:
        return str(uuid.uuid4())
