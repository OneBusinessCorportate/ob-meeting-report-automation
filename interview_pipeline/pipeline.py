"""Orchestrator for the «Обучающий центр / анализ собеседований» pipeline.

End-to-end, per the project spec:
  Google Sheet «Бух»  →  upsert candidate + interview (deduped)
      →  resolve FULL transcript (Timeless → Google Docs → manual file)
      →  store raw + cleaned transcript (+ segments), status transcript_ready
      →  AI analysis (Armenian-aware, Russian) → store analysis + scores
      →  status analysis_done  (+ optional Telegram report, sheet write-back)

Robustness guarantees:
  * candidates/interviews are deduped — safe to rerun, no duplicates;
  * one bad row never stops the batch (each row is isolated in try/except);
  * raw transcript is stored separately from cleaned text;
  * empty/missing links and empty transcripts are handled with a clear status,
    never a crash;
  * analysis can be re-run (versioned, is_current); --force re-processes done rows;
  * every stage is logged to intv_sync_logs.
"""
from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from meeting_pipeline.config import Config
from meeting_pipeline.timeless_client import TimelessClient
from meeting_pipeline.utils import get_logger
from .analysis_store import (
    STATUS_ANALYSIS_DONE,
    STATUS_ANALYSIS_PENDING,
    STATUS_ERROR,
    STATUS_LINK_MISSING,
    STATUS_TRANSCRIPT_PENDING,
    STATUS_TRANSCRIPT_READY,
    InterviewStore,
)
from .analyze import InterviewAnalyzer
from .report import deliver_interview_report
from .sheet_source import SheetCandidate, load_candidates
from .text_clean import clean_transcript, normalize_segments, transcript_stats
from .transcript_resolver import TranscriptResolver, google_doc_id, is_google_doc_link

log = get_logger("interview_pipeline.pipeline")


def _hash(*parts: str) -> str:
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def resolve_source_call_id(cand: SheetCandidate) -> str:
    """Stable id for dedup. Google Doc id, Timeless meeting id, or a hash."""
    url = cand.call_url
    if url:
        if is_google_doc_link(url):
            doc_id = google_doc_id(url)
            if doc_id:
                return f"gdoc_{doc_id}"
        tid = TimelessClient.meeting_id_from_url(url)
        if tid:
            return tid
        return f"url_{_hash(url)}"
    # No link: one stable placeholder interview per candidate (status link_missing).
    return f"nolink_{cand.track}_{_hash(cand.track, (cand.full_name or '').lower())}"


def _infer_interview_type(cand: SheetCandidate) -> str:
    status = (cand.sheet_status or "").lower()
    if "обуч" in status or "onboard" in status:
        return "onboarding"
    return "interview"


def process_candidate(
    store: InterviewStore,
    resolver: TranscriptResolver,
    analyzer: Optional[InterviewAnalyzer],
    source_id: Optional[str],
    cand: SheetCandidate,
    *,
    run_id: str,
    do_analyze: bool,
    do_deliver: bool,
    config: Config,
    force: bool = False,
) -> Dict[str, Any]:
    """Process a single candidate row. Never raises."""
    base = {"candidate": cand.full_name, "track": cand.track}
    try:
        candidate = store.upsert_candidate(cand)
        candidate_id = candidate["id"]
        source_call_id = resolve_source_call_id(cand)
        sheet_ref = {
            "sheet": cand.source_sheet,
            "row": cand.source_row,
            "column": cand.source_column,
        }

        # --- no link: record a link_missing interview and stop here -----------
        if not cand.call_url and not cand.transcript_file:
            interview = store.upsert_interview(
                candidate_id=candidate_id,
                source_id=source_id,
                source_call_id=source_call_id,
                interview_type=_infer_interview_type(cand),
                status=STATUS_LINK_MISSING,
                sheet_ref=sheet_ref,
            )
            store.set_interview_status(interview["id"], STATUS_LINK_MISSING)
            store.log_event(
                run_id, "transcript", level="warning", status=STATUS_LINK_MISSING,
                interview_id=interview["id"], candidate_id=candidate_id,
                message="No interview/transcript link in the sheet.",
            )
            return {**base, "ok": True, "status": STATUS_LINK_MISSING}

        # Idempotency: skip already-finished interviews unless forced. Check the
        # stored row BEFORE the upsert (which would otherwise reset the status).
        existing = store.get_interview(source_id, source_call_id)
        if not force and existing and existing.get("status") == STATUS_ANALYSIS_DONE:
            return {**base, "ok": True, "status": "skipped", "interview_id": existing["id"]}

        interview = store.upsert_interview(
            candidate_id=candidate_id,
            source_id=source_id,
            source_call_id=source_call_id,
            call_url=cand.call_url,
            interview_type=_infer_interview_type(cand),
            status=STATUS_TRANSCRIPT_PENDING,
            sheet_ref=sheet_ref,
        )
        interview_id = interview["id"]

        # --- resolve transcript ----------------------------------------------
        store.set_interview_status(interview_id, STATUS_TRANSCRIPT_PENDING)
        res = resolver.resolve(cand.call_url, transcript_file=cand.transcript_file)
        if not res.ok or not (res.text and res.text.strip()):
            store.set_interview_status(
                interview_id, STATUS_ERROR, error_message=res.error or "No transcript.",
                extra={"transcript_source": res.source},
            )
            store.log_event(
                run_id, "transcript", level="error", status=STATUS_ERROR,
                interview_id=interview_id, candidate_id=candidate_id,
                message=res.error, detail={"source": res.source},
            )
            return {**base, "ok": False, "status": STATUS_ERROR, "error": res.error,
                    "interview_id": interview_id}

        cleaned = clean_transcript(res.text)
        segments = normalize_segments(res.segments)
        stats = transcript_stats(res.text, res.segments)
        store.save_transcript(
            interview_id,
            raw_text=res.text,
            cleaned_text=cleaned,
            language=res.language or config.default_language,
            source=res.source,
            segments=segments,
            raw_payload=res.raw_payload,
            stats=stats,
        )
        store.set_interview_status(
            interview_id, STATUS_TRANSCRIPT_READY,
            extra={
                "transcript_source": res.source,
                "language": res.language or config.default_language,
                "recording_url": res.recording_url,
                "duration_seconds": res.duration_seconds,
                "fetched_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        store.log_event(
            run_id, "transcript", status=STATUS_TRANSCRIPT_READY,
            interview_id=interview_id, candidate_id=candidate_id,
            message=f"Transcript saved from {res.source} ({stats['char_count']} chars).",
        )

        if not do_analyze or analyzer is None:
            return {**base, "ok": True, "status": STATUS_TRANSCRIPT_READY,
                    "interview_id": interview_id, "chars": stats["char_count"]}

        # Idempotency for the analysis step.
        if not force and store.has_current_completed_analysis(interview_id):
            store.set_interview_status(interview_id, STATUS_ANALYSIS_DONE)
            return {**base, "ok": True, "status": STATUS_ANALYSIS_DONE,
                    "interview_id": interview_id}

        # --- AI analysis ------------------------------------------------------
        store.set_interview_status(interview_id, STATUS_ANALYSIS_PENDING)
        analysis = analyzer.analyze(
            cleaned,
            candidate_name=cand.full_name,
            role=cand.role,
            interview_type=interview["interview_type"],
            language=res.language or config.default_language,
        )
        if not analysis.ok:
            store.record_failed_analysis(
                interview_id=interview_id,
                candidate_id=candidate_id,
                model_id=analysis.model_id,
                prompt_version=analysis.prompt_version,
                processing_time_ms=analysis.processing_time_ms,
                ai_metadata=analysis.ai_metadata,
                error_message=analysis.error,
            )
            store.set_interview_status(interview_id, STATUS_ERROR, error_message=analysis.error)
            store.log_event(
                run_id, "analysis", level="error", status=STATUS_ERROR,
                interview_id=interview_id, candidate_id=candidate_id, message=analysis.error,
            )
            return {**base, "ok": False, "status": STATUS_ERROR, "error": analysis.error,
                    "interview_id": interview_id}

        analysis_row = store.create_analysis(
            interview_id=interview_id,
            candidate_id=candidate_id,
            status="completed",
            model_id=analysis.model_id,
            prompt_version=analysis.prompt_version,
            transcript_language=analysis.transcript_language,
            summary=analysis.summary,
            summary_original=analysis.summary_original,
            candidate_strengths=analysis.candidate_strengths,
            candidate_weaknesses=analysis.candidate_weaknesses,
            red_flags=analysis.red_flags,
            next_steps=analysis.next_steps,
            recommendation=analysis.recommendation,
            reasoning=analysis.reasoning,
            ai_metadata=analysis.ai_metadata,
            processing_time_ms=analysis.processing_time_ms,
        )
        store.create_scores(
            analysis_id=analysis_row["id"],
            interview_id=interview_id,
            candidate_id=candidate_id,
            communication_score=analysis.communication_score,
            professional_score=analysis.professional_score,
            motivation_score=analysis.motivation_score,
            overall_score=analysis.overall_score,
        )
        store.set_interview_status(interview_id, STATUS_ANALYSIS_DONE)
        store.log_event(
            run_id, "analysis", status=STATUS_ANALYSIS_DONE,
            interview_id=interview_id, candidate_id=candidate_id,
            message=f"Analysis done: {analysis.recommendation}",
            detail={"overall_score": analysis.overall_score},
        )

        if do_deliver:
            delivery = deliver_interview_report(config, candidate, analysis)
            store.log_event(
                run_id, "deliver", level="info" if delivery.get("ok") else "warning",
                status="delivered" if delivery.get("ok") else "delivery_failed",
                interview_id=interview_id, candidate_id=candidate_id,
                message=delivery.get("error"),
            )

        return {
            **base, "ok": True, "status": STATUS_ANALYSIS_DONE, "interview_id": interview_id,
            "recommendation": analysis.recommendation, "overall_score": analysis.overall_score,
        }
    except Exception as exc:  # one bad row must not stop the batch
        log.exception("Failed processing candidate %s: %s", cand.full_name, exc)
        store.log_event(
            run_id, "run", level="error", status=STATUS_ERROR,
            message=f"Unhandled error for {cand.full_name}: {exc}",
        )
        return {**base, "ok": False, "status": STATUS_ERROR, "error": str(exc)}


def sync_interviews(
    config: Config,
    *,
    xlsx_path: Optional[str] = None,
    csv_path: Optional[str] = None,
    tabs: Optional[List[str]] = None,
    candidates: Optional[List[SheetCandidate]] = None,
    do_analyze: Optional[bool] = None,
    do_deliver: Optional[bool] = None,
    force: bool = False,
    limit: Optional[int] = None,
    store: Optional[InterviewStore] = None,
    resolver: Optional[TranscriptResolver] = None,
    analyzer: Optional[InterviewAnalyzer] = None,
) -> Dict[str, Any]:
    """Run the full sync. Returns a structured summary.

    ``candidates`` lets callers supply rows directly (e.g. links added on the
    CLI) and bypass reading the Google Sheet — useful when the sheet API isn't
    configured.
    """
    store = store or InterviewStore(config)
    resolver = resolver or TranscriptResolver(config)
    do_analyze = config.interview_analysis_enabled if do_analyze is None else do_analyze
    do_deliver = config.interview_telegram_enabled if do_deliver is None else do_deliver
    if do_analyze and analyzer is None:
        analyzer = InterviewAnalyzer(config)

    run_id = store.new_run_id()
    if candidates is None:
        candidates = load_candidates(config, xlsx_path=xlsx_path, csv_path=csv_path, tabs=tabs)
    if limit:
        candidates = candidates[:limit]
    store.log_event(
        run_id, "sheet_read", status="ok",
        message=f"Loaded {len(candidates)} candidate row(s).",
        detail={"tabs": tabs or config.interview_sheet_tabs},
    )
    if not candidates:
        return {"ok": True, "run_id": run_id, "processed": 0, "results": [], "counts": {}}

    source = store.ensure_timeless_source()
    source_id = source.get("id")

    results: List[Dict[str, Any]] = []
    for cand in candidates:
        results.append(
            process_candidate(
                store, resolver, analyzer, source_id, cand,
                run_id=run_id, do_analyze=do_analyze, do_deliver=do_deliver,
                config=config, force=force,
            )
        )

    counts: Dict[str, int] = {}
    for r in results:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    done = counts.get(STATUS_ANALYSIS_DONE, 0)
    errors = counts.get(STATUS_ERROR, 0)
    log.info("=== INTERVIEW SYNC SUMMARY (run %s) ===", run_id)
    for r in results:
        log.info("  %-22s %-16s %s", (r.get("candidate") or "")[:22], r["status"], r.get("error") or "")
    log.info(
        "Processed %d | analysis_done=%d | link_missing=%d | errors=%d",
        len(results), done, counts.get(STATUS_LINK_MISSING, 0), errors,
    )
    store.log_event(
        run_id, "run", level="error" if errors else "info",
        status="completed",
        message=f"Sync done: {len(results)} processed, {done} analyzed, {errors} errors.",
        detail={"counts": counts},
    )
    return {
        "ok": errors == 0,
        "run_id": run_id,
        "processed": len(results),
        "analysis_done": done,
        "errors": errors,
        "counts": counts,
        "results": results,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
