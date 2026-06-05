"""Supabase data-access for the ``interview_calls`` table (task II).

This table tracks each interview/onboarding call link, its processing status,
and the FULL transcript. See ``sql/interview_calls.sql`` for the schema; apply
it once to the Supabase project before running the pipeline against a real DB.

Statuses: pending → processing → done | transcript_not_found | failed.
Deduped by (source_id, source_call_id) so reruns never duplicate a call.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from meeting_pipeline.config import Config
from meeting_pipeline.supabase_repo import SupabaseRepo
from meeting_pipeline.utils import get_logger

log = get_logger("interview_pipeline.repo")

STATUS_PENDING = "pending"
STATUS_PROCESSING = "processing"
STATUS_DONE = "done"
STATUS_TRANSCRIPT_NOT_FOUND = "transcript_not_found"
STATUS_FAILED = "failed"


class InterviewRepo:
    def __init__(self, config: Config, client: Any = None):
        self.config = config
        self.table = config.interview_calls_table
        if client is not None:
            self.client = client
        else:
            config.require_supabase()
            from supabase import create_client

            self.client = create_client(
                config.supabase_url, config.supabase_service_role_key
            )
        # Reuse the meeting repo purely for the shared `timeless` source registry.
        self._sources = SupabaseRepo(config, client=self.client)

    def ensure_timeless_source(self) -> Dict[str, Any]:
        return self._sources.ensure_source("timeless")

    def get_by_source_call_id(
        self, source_id: str, source_call_id: str
    ) -> Optional[Dict[str, Any]]:
        rows = (
            self.client.table(self.table)
            .select("*")
            .eq("source_id", source_id)
            .eq("source_call_id", source_call_id)
            .limit(1)
            .execute()
        ).data
        return rows[0] if rows else None

    def upsert_call(
        self,
        *,
        source_id: str,
        source_call_id: str,
        call_url: str,
        candidate_name: Optional[str] = None,
        role: Optional[str] = None,
        call_type: str = "interview",
        status: str = STATUS_PENDING,
        metadata: Optional[dict] = None,
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "source_id": source_id,
            "source_call_id": source_call_id,
            "call_url": call_url,
            "call_type": call_type,
            "status": status,
        }
        optional = {
            "candidate_name": candidate_name,
            "role": role,
            "metadata": metadata,
        }
        payload.update({k: v for k, v in optional.items() if v is not None})
        result = (
            self.client.table(self.table)
            .upsert(payload, on_conflict="source_id,source_call_id")
            .execute()
        )
        return result.data[0]

    def save_transcript(
        self,
        call_id: str,
        *,
        raw_transcript: dict,
        transcript_language: Optional[str] = None,
        recording_url: Optional[str] = None,
        duration_seconds: Optional[int] = None,
    ) -> Dict[str, Any]:
        from datetime import datetime, timezone

        payload: Dict[str, Any] = {
            "raw_transcript": raw_transcript,
            "status": STATUS_DONE,
            "error_message": None,
            "fetched_at": datetime.now(timezone.utc).isoformat(),
        }
        optional = {
            "transcript_language": transcript_language,
            "recording_url": recording_url,
            "duration_seconds": duration_seconds,
        }
        payload.update({k: v for k, v in optional.items() if v is not None})
        result = (
            self.client.table(self.table)
            .update(payload)
            .eq("id", call_id)
            .execute()
        )
        log.info("Saved full transcript for interview call %s (status=done)", call_id)
        return result.data[0] if result.data else {}

    def set_status(
        self, call_id: str, status: str, *, error_message: Optional[str] = None
    ) -> Dict[str, Any]:
        payload: Dict[str, Any] = {"status": status}
        if error_message is not None:
            payload["error_message"] = error_message
        result = (
            self.client.table(self.table)
            .update(payload)
            .eq("id", call_id)
            .execute()
        )
        log.info("Interview call %s -> status=%s", call_id, status)
        return result.data[0] if result.data else {}
