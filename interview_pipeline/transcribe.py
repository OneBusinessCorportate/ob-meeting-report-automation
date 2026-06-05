"""Step: link → transcript → save result (task II).

For each interview/onboarding call link:
  1. Resolve a stable ``source_call_id`` (from the Timeless URL).
  2. Upsert the call row (status=processing), deduped by source_call_id.
  3. Fetch the FULL transcript — via Timeless API, or a local file fallback.
  4. Save the full transcript and mark status=done; on failure store a clear
     status (transcript_not_found / failed) + error_message. Never crash.

Idempotent: a call already marked ``done`` is skipped unless ``force=True``.
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, List, Optional

from meeting_pipeline.config import Config
from meeting_pipeline.ingest import build_raw_transcript
from meeting_pipeline.timeless_client import TimelessClient
from meeting_pipeline.utils import get_logger
from .interview_repo import (
    STATUS_DONE,
    STATUS_FAILED,
    STATUS_PROCESSING,
    STATUS_TRANSCRIPT_NOT_FOUND,
    InterviewRepo,
)
from .links_source import InterviewLink, load_links

log = get_logger("interview_pipeline.transcribe")


def _resolve_source_call_id(link: InterviewLink) -> str:
    if link.source_call_id:
        return link.source_call_id
    extracted = TimelessClient.meeting_id_from_url(link.call_url)
    if extracted:
        return extracted
    # Last resort: a stable hash of the URL so dedup still works.
    return "url_" + hashlib.sha1(link.call_url.encode("utf-8")).hexdigest()[:16]


def _read_local_transcript(path_str: str) -> Optional[str]:
    path = Path(path_str)
    if not path.exists():
        log.warning("Local transcript file not found: %s", path)
        return None
    text = path.read_text(encoding="utf-8").strip()
    return text or None


def transcribe_link(
    repo: InterviewRepo,
    timeless: TimelessClient,
    source_id: str,
    link: InterviewLink,
    *,
    default_language: str,
    force: bool = False,
) -> Dict[str, Any]:
    """Transcribe a single interview link and persist the result."""
    source_call_id = _resolve_source_call_id(link)

    try:
        # Idempotency: skip calls already transcribed unless forced.
        existing = repo.get_by_source_call_id(source_id, source_call_id)
        if existing and existing.get("status") == STATUS_DONE and not force:
            log.info(
                "Interview call %s already done; skipping (use --force).",
                source_call_id,
            )
            return {"ok": True, "status": "skipped", "source_call_id": source_call_id}

        call = repo.upsert_call(
            source_id=source_id,
            source_call_id=source_call_id,
            call_url=link.call_url,
            candidate_name=link.candidate_name,
            role=link.role,
            call_type=link.call_type,
            status=STATUS_PROCESSING,
            metadata={"links_source": link.metadata or {}},
        )

        # --- Fetch full transcript ------------------------------------------
        text: Optional[str] = None
        segments: List[dict] = []
        language = default_language
        recording_url: Optional[str] = None
        duration: Optional[int] = None

        if link.transcript_file:
            text = _read_local_transcript(link.transcript_file)
            source_label = "local_file"
        elif timeless.is_configured:
            result = timeless.get_full_transcript_by_url(link.call_url)
            if result.ok and result.transcript_text:
                text = result.transcript_text
                segments = result.segments or []
                if isinstance(result.raw, dict):
                    language = result.raw.get("language") or language
                    recording_url = result.raw.get("recording_url")
                    duration = result.raw.get("duration_seconds")
            else:
                log.warning(
                    "Timeless full transcript unavailable for %s: %s",
                    link.call_url,
                    result.error,
                )
            source_label = "Timeless"
        else:
            log.warning(
                "Timeless API not configured and no local transcript_file for %s. "
                "Provide a transcript_file in the CSV for MVP fallback.",
                link.call_url,
            )
            repo.set_status(
                call["id"],
                STATUS_TRANSCRIPT_NOT_FOUND,
                error_message=(
                    "Timeless API not configured or full transcript endpoint "
                    "unavailable, and no local transcript_file fallback provided."
                ),
            )
            return {
                "ok": False,
                "status": STATUS_TRANSCRIPT_NOT_FOUND,
                "source_call_id": source_call_id,
            }

        if not text:
            repo.set_status(
                call["id"],
                STATUS_TRANSCRIPT_NOT_FOUND,
                error_message="Full transcript not found for this call link.",
            )
            return {
                "ok": False,
                "status": STATUS_TRANSCRIPT_NOT_FOUND,
                "source_call_id": source_call_id,
            }

        # --- Save full transcript -------------------------------------------
        raw_transcript = build_raw_transcript(
            text,
            language=language,
            source=source_label,
            segments=segments,
            kind="full_transcript",
        )
        repo.save_transcript(
            call["id"],
            raw_transcript=raw_transcript,
            transcript_language=language,
            recording_url=recording_url,
            duration_seconds=duration,
        )
        return {
            "ok": True,
            "status": STATUS_DONE,
            "source_call_id": source_call_id,
            "chars": len(text),
        }

    except Exception as exc:  # one bad link must not stop the batch
        log.exception("Error transcribing interview link %s: %s", link.call_url, exc)
        return {
            "ok": False,
            "status": STATUS_FAILED,
            "source_call_id": source_call_id,
            "error": str(exc),
        }


def transcribe_interviews(
    config: Config,
    *,
    urls: Optional[List[str]] = None,
    csv_path: Optional[str] = None,
    force: bool = False,
    repo: Optional[InterviewRepo] = None,
    timeless: Optional[TimelessClient] = None,
) -> Dict[str, Any]:
    """Resolve interview links and transcribe each one."""
    repo = repo or InterviewRepo(config)
    timeless = timeless or TimelessClient(config)

    links = load_links(config, urls=urls, csv_path=csv_path, client=repo.client)
    if not links:
        log.warning(
            "No interview links to process. Provide --url, --csv, or set "
            "INTERVIEW_LINKS_TABLE."
        )
        return {"ok": True, "processed": 0, "results": []}

    source = repo.ensure_timeless_source()
    results = [
        transcribe_link(
            repo,
            timeless,
            source["id"],
            link,
            default_language=config.default_language,
            force=force,
        )
        for link in links
    ]

    done = sum(1 for r in results if r["status"] == STATUS_DONE)
    skipped = sum(1 for r in results if r["status"] == "skipped")
    failed = sum(1 for r in results if r["status"] in (STATUS_FAILED, STATUS_TRANSCRIPT_NOT_FOUND))
    log.info(
        "Interview transcription: %d done, %d skipped, %d failed/not-found (of %d).",
        done,
        skipped,
        failed,
        len(results),
    )
    return {
        "ok": failed == 0,
        "processed": len(results),
        "done": done,
        "skipped": skipped,
        "failed": failed,
        "results": results,
    }
