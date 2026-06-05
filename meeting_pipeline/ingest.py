"""Step 1 — Ingest.

Save raw meeting data into Supabase L1 (``mtg_meetings``). The FULL transcript
is stored in ``raw_transcript`` as JSONB. Supports two modes:

1. Timeless API (automatic) when ``TIMELESS_API_TOKEN`` is configured.
2. Local transcript file (MVP fallback) via ``--file``.

Missing transcript / recording is handled gracefully — no crash. A clear
status string is returned (``recording_not_found`` / ``transcript_not_found``).
"""
from __future__ import annotations

from datetime import date, datetime, time
from pathlib import Path
from typing import Any, Dict, Optional

from .config import Config
from .supabase_repo import SupabaseRepo
from .timeless_client import TimelessClient
from .utils import armenia_tz, get_logger, parse_date

log = get_logger("meeting_pipeline.ingest")

STATUS_OK = "ingested"
STATUS_RECORDING_NOT_FOUND = "recording_not_found"
STATUS_TRANSCRIPT_NOT_FOUND = "transcript_not_found"


def build_raw_transcript(
    text: str,
    *,
    language: str,
    source: str = "Timeless",
    segments: Optional[list] = None,
    kind: str = "full_transcript",
) -> Dict[str, Any]:
    """Build the ``raw_transcript`` JSONB payload for the full transcript."""
    return {
        "type": kind,
        "language": language,
        "source": source,
        "text": text,
        "segments": segments or [],
    }


def _local_morning_utc(on_date: date, offset_hours: int) -> str:
    """Approximate the morning standup time (09:00 local) as a UTC ISO string.

    We only set this when the source provides no real timestamp; it keeps the
    "today" queries (which key off ``actual_start``) working for a given date.
    """
    from datetime import timezone as _tz

    local = datetime.combine(on_date, time(9, 0), tzinfo=armenia_tz(offset_hours))
    return local.astimezone(_tz.utc).isoformat()


def ingest_from_file(
    repo: SupabaseRepo,
    config: Config,
    *,
    file_path: str,
    title: Optional[str] = None,
    on_date: Optional[date] = None,
    language: Optional[str] = None,
    source_meeting_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Ingest a full transcript from a local text file (fallback mode)."""
    path = Path(file_path)
    on_date = on_date or parse_date(None, config.timezone_offset_hours)
    language = language or config.default_language

    if not path.exists():
        log.error("Transcript file not found: %s", path)
        return {
            "status": STATUS_TRANSCRIPT_NOT_FOUND,
            "ok": False,
            "detail": f"Local transcript file not found: {path}",
        }

    text = path.read_text(encoding="utf-8").strip()
    if not text:
        log.error("Transcript file is empty: %s", path)
        return {
            "status": STATUS_TRANSCRIPT_NOT_FOUND,
            "ok": False,
            "detail": f"Local transcript file is empty: {path}",
        }

    source = repo.ensure_source(config.default_source)
    smid = source_meeting_id or f"manual_{on_date.strftime('%Y_%m_%d')}"
    raw_transcript = build_raw_transcript(
        text,
        language=language,
        source=source.get("display_name", "Timeless"),
        kind="full_transcript",
    )

    meeting = repo.upsert_meeting(
        source_id=source["id"],
        source_meeting_id=smid,
        title=title or "Утренняя планёрка бухгалтерии",
        status="completed",
        actual_start=_local_morning_utc(on_date, config.timezone_offset_hours),
        transcript_language=language,
        raw_transcript=raw_transcript,
        metadata={"ingest_mode": "local_file", "file": str(path)},
    )
    log.info("Ingested meeting from file: id=%s smid=%s", meeting["id"], smid)
    return {
        "status": STATUS_OK,
        "ok": True,
        "meeting": meeting,
        "source_meeting_id": smid,
    }


def ingest_from_timeless(
    repo: SupabaseRepo,
    config: Config,
    timeless: TimelessClient,
    *,
    on_date: Optional[date] = None,
) -> Dict[str, Any]:
    """Ingest today's completed meeting(s) from the Timeless API."""
    on_date = on_date or parse_date(None, config.timezone_offset_hours)

    listing = timeless.list_today_meetings(on_date)
    if not listing.ok:
        log.warning("Timeless listing unavailable: %s", listing.error)
        return {
            "status": STATUS_RECORDING_NOT_FOUND,
            "ok": False,
            "detail": listing.error,
        }
    if not listing.meetings:
        log.warning("Timeless returned no meetings for %s", on_date.isoformat())
        return {
            "status": STATUS_RECORDING_NOT_FOUND,
            "ok": False,
            "detail": "No Timeless meetings found for today.",
        }

    source = repo.ensure_source(config.default_source)
    ingested = []
    for tm in listing.meetings:
        meeting_id = str(tm.get("id") or tm.get("meeting_id") or "")
        if not meeting_id:
            continue
        transcript = timeless.get_full_transcript(meeting_id)
        if not transcript.ok or not transcript.transcript_text:
            log.warning(
                "Full transcript unavailable for Timeless meeting %s: %s",
                meeting_id,
                transcript.error,
            )
            # Record the meeting without a transcript so the gap is visible.
            meeting = repo.upsert_meeting(
                source_id=source["id"],
                source_meeting_id=f"timeless_{meeting_id}",
                title=tm.get("title") or "Утренняя планёрка бухгалтерии",
                status="failed",
                recording_url=tm.get("recording_url"),
                metadata={
                    "ingest_mode": "timeless_api",
                    "transcript_status": STATUS_TRANSCRIPT_NOT_FOUND,
                    "timeless_meeting_id": meeting_id,
                },
            )
            ingested.append(
                {"meeting": meeting, "status": STATUS_TRANSCRIPT_NOT_FOUND}
            )
            continue

        raw_transcript = build_raw_transcript(
            transcript.transcript_text,
            language=tm.get("language") or config.default_language,
            source="Timeless",
            segments=transcript.segments,
        )
        meeting = repo.upsert_meeting(
            source_id=source["id"],
            source_meeting_id=f"timeless_{meeting_id}",
            title=tm.get("title") or "Утренняя планёрка бухгалтерии",
            status="completed",
            actual_start=tm.get("actual_start") or tm.get("start_time"),
            actual_end=tm.get("actual_end") or tm.get("end_time"),
            duration_seconds=tm.get("duration_seconds"),
            recording_url=tm.get("recording_url"),
            transcript_language=tm.get("language") or config.default_language,
            raw_transcript=raw_transcript,
            raw_summary=tm.get("summary"),  # summary kept as ADDITIONAL raw only
            metadata={
                "ingest_mode": "timeless_api",
                "timeless_meeting_id": meeting_id,
            },
        )
        ingested.append({"meeting": meeting, "status": STATUS_OK})

    ok = any(item["status"] == STATUS_OK for item in ingested)
    return {
        "status": STATUS_OK if ok else STATUS_TRANSCRIPT_NOT_FOUND,
        "ok": ok,
        "ingested": ingested,
    }


def ingest_meeting(
    config: Config,
    *,
    file_path: Optional[str] = None,
    title: Optional[str] = None,
    date_str: Optional[str] = None,
    language: Optional[str] = None,
    source_meeting_id: Optional[str] = None,
    repo: Optional[SupabaseRepo] = None,
    timeless: Optional[TimelessClient] = None,
) -> Dict[str, Any]:
    """Top-level ingest entry point.

    Uses the local file when ``file_path`` is given; otherwise tries Timeless.
    """
    repo = repo or SupabaseRepo(config)
    on_date = parse_date(date_str, config.timezone_offset_hours)

    if file_path:
        log.info("Ingest mode: LOCAL FILE (%s)", file_path)
        return ingest_from_file(
            repo,
            config,
            file_path=file_path,
            title=title,
            on_date=on_date,
            language=language,
            source_meeting_id=source_meeting_id,
        )

    log.info("Ingest mode: TIMELESS API")
    timeless = timeless or TimelessClient(config)
    if not timeless.is_configured:
        log.warning(
            "Timeless API not configured and no --file provided. "
            "Nothing to ingest."
        )
        return {
            "status": STATUS_RECORDING_NOT_FOUND,
            "ok": False,
            "detail": (
                "Timeless API not configured or full transcript endpoint "
                "unavailable, and no local --file fallback was provided."
            ),
        }
    return ingest_from_timeless(repo, config, timeless, on_date=on_date)
