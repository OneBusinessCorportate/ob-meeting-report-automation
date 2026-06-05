"""Interview / onboarding transcript processing (mode: interview_transcript_processing).

For each interview/onboarding call link from the «Обучающий центр ОВ» table:
  1. Resolve a stable ``source_call_id`` (from the Timeless URL).
  2. Upsert the call row (status=pending), deduped by source_call_id.
  3. Check whether a FULL transcript is available and fetch it — via Timeless
     API, or a local file fallback. (Never a summary.)
  4. Save the full transcript and mark status=saved; otherwise mark a clear
     status (transcript_not_available / manual_action_required / failed) with an
     error_message. Never crash — links are processed one by one and each gets
     a logged status.

Statuses: pending → transcript_found → saved
                  ↘ transcript_not_available | manual_action_required | failed

Idempotent: a call already ``saved`` is skipped unless ``force=True``.

This logic is intentionally kept SEPARATE from the daily morning-meeting report
(no AI L2, no Telegram) — the meeting type and purpose are different.
"""
from __future__ import annotations

import csv
import hashlib
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from meeting_pipeline.config import Config
from meeting_pipeline.ingest import build_raw_transcript
from meeting_pipeline.timeless_client import TimelessClient
from meeting_pipeline.utils import get_logger
from .interview_repo import (
    STATUS_FAILED,
    STATUS_MANUAL_ACTION_REQUIRED,
    STATUS_PENDING,
    STATUS_SAVED,
    STATUS_TRANSCRIPT_FOUND,
    STATUS_TRANSCRIPT_NOT_AVAILABLE,
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
    """Process a single interview link and persist the result + status."""
    source_call_id = _resolve_source_call_id(link)
    base = {
        "source_call_id": source_call_id,
        "call_url": link.call_url,
        "candidate_name": link.candidate_name,
        "call_type": link.call_type,
    }

    try:
        # Idempotency: skip calls already saved unless forced.
        existing = repo.get_by_source_call_id(source_id, source_call_id)
        if existing and existing.get("status") == STATUS_SAVED and not force:
            log.info("Call %s already saved; skipping (use --force).", source_call_id)
            return {**base, "ok": True, "status": "skipped"}

        call = repo.upsert_call(
            source_id=source_id,
            source_call_id=source_call_id,
            call_url=link.call_url,
            candidate_name=link.candidate_name,
            role=link.role,
            call_type=link.call_type,
            status=STATUS_PENDING,
            metadata={"links_source": link.metadata or {}},
        )

        # --- Step 2: check transcript availability + fetch ------------------
        text: Optional[str] = None
        segments: List[dict] = []
        language = default_language
        recording_url: Optional[str] = None
        duration: Optional[int] = None
        source_label = "Timeless"

        if link.transcript_file:
            text = _read_local_transcript(link.transcript_file)
            source_label = "local_file"
            if not text:
                repo.set_status(
                    call["id"],
                    STATUS_TRANSCRIPT_NOT_AVAILABLE,
                    error_message=f"Local transcript_file empty/missing: {link.transcript_file}",
                )
                log.warning("[%s] transcript_not_available (local file)", source_call_id)
                return {**base, "ok": False, "status": STATUS_TRANSCRIPT_NOT_AVAILABLE}

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
                # Recording may exist but no transcript is available via the API.
                repo.set_status(
                    call["id"],
                    STATUS_TRANSCRIPT_NOT_AVAILABLE,
                    error_message=result.error or "Full transcript not available.",
                )
                log.warning(
                    "[%s] transcript_not_available: %s", source_call_id, result.error
                )
                return {**base, "ok": False, "status": STATUS_TRANSCRIPT_NOT_AVAILABLE}

        else:
            # Cannot even attempt automatic fetch → a human must supply it.
            repo.set_status(
                call["id"],
                STATUS_MANUAL_ACTION_REQUIRED,
                error_message=(
                    "Timeless API not configured and no local transcript_file. "
                    "Provide a transcript_file in the CSV or configure TIMELESS_API_TOKEN."
                ),
            )
            log.warning("[%s] manual_action_required", source_call_id)
            return {**base, "ok": False, "status": STATUS_MANUAL_ACTION_REQUIRED}

        # Transcript located.
        repo.set_status(call["id"], STATUS_TRANSCRIPT_FOUND)
        log.info("[%s] transcript_found (%d chars)", source_call_id, len(text))

        # --- Step 3: save the FULL transcript -------------------------------
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
        log.info("[%s] saved", source_call_id)
        return {**base, "ok": True, "status": STATUS_SAVED, "chars": len(text)}

    except Exception as exc:  # one bad link must not stop the batch
        log.exception("[%s] failed: %s", source_call_id, exc)
        return {**base, "ok": False, "status": STATUS_FAILED, "error": str(exc)}


def export_status_list(results: List[Dict[str, Any]], output_path: str) -> str:
    """Write a clear per-link status list to a CSV file. Returns the path."""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = ["source_call_id", "candidate_name", "call_type", "status", "chars", "call_url", "error"]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields, extrasaction="ignore")
        writer.writeheader()
        for row in results:
            writer.writerow(row)
    log.info("Wrote status list for %d link(s) to %s", len(results), path)
    return str(path)


def transcribe_interviews(
    config: Config,
    *,
    urls: Optional[List[str]] = None,
    csv_path: Optional[str] = None,
    force: bool = False,
    output_path: Optional[str] = None,
    repo: Optional[InterviewRepo] = None,
    timeless: Optional[TimelessClient] = None,
) -> Dict[str, Any]:
    """Resolve interview links and process each one, one by one."""
    repo = repo or InterviewRepo(config)
    timeless = timeless or TimelessClient(config)

    links = load_links(config, urls=urls, csv_path=csv_path, client=repo.client)
    if not links:
        log.warning(
            "No interview links to process. Provide --input/--csv, --url, or set "
            "INTERVIEW_LINKS_TABLE."
        )
        return {"ok": True, "processed": 0, "results": [], "status_list": []}

    source = repo.ensure_timeless_source()
    results: List[Dict[str, Any]] = []
    for link in links:  # process links one by one
        results.append(
            transcribe_link(
                repo,
                timeless,
                source["id"],
                link,
                default_language=config.default_language,
                force=force,
            )
        )

    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1

    saved = counts.get(STATUS_SAVED, 0)
    needs_attention = sum(
        counts.get(s, 0)
        for s in (STATUS_TRANSCRIPT_NOT_AVAILABLE, STATUS_MANUAL_ACTION_REQUIRED, STATUS_FAILED)
    )
    log.info("=== STATUS LIST ===")
    for r in results:
        log.info(
            "  %-28s %-22s %s",
            r["source_call_id"],
            r["status"],
            r.get("candidate_name") or "",
        )
    log.info(
        "Interview processing: saved=%d, skipped=%d, needs_attention=%d (of %d).",
        saved,
        counts.get("skipped", 0),
        needs_attention,
        len(results),
    )

    out_file = None
    if output_path:
        out_file = export_status_list(results, output_path)

    return {
        "ok": needs_attention == 0,
        "processed": len(results),
        "saved": saved,
        "skipped": counts.get("skipped", 0),
        "needs_attention": needs_attention,
        "counts": counts,
        "status_list": [
            {k: r.get(k) for k in ("source_call_id", "status", "candidate_name", "error")}
            for r in results
        ],
        "status_file": out_file,
        "results": results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
