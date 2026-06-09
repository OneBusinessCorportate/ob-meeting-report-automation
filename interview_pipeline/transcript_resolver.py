"""Resolve the FULL transcript for an interview link.

Resolution order (per the boss's instruction: "try Timeless, if nothing use
Docs"):
  1. explicit local file (manual MVP fallback / CSV transcript_file column);
  2. Timeless API (when configured) — the corporate recordings live here;
  3. Google Docs / Drive link from the sheet (the transcripts we actually have
     today are Google Docs) — read via the Google API, or a public export URL;
  4. otherwise: not available → the caller records a clear status.

We always return the FULL transcript, never a summary. Never raises.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from meeting_pipeline.config import Config
from meeting_pipeline.timeless_client import TimelessClient
from meeting_pipeline.utils import get_logger

log = get_logger("interview_pipeline.resolver")

_GOOGLE_DOC_ID = re.compile(r"/document/d/([A-Za-z0-9_\-]+)")
_DRIVE_FILE_ID = re.compile(r"/file/d/([A-Za-z0-9_\-]+)|[?&]id=([A-Za-z0-9_\-]+)")


@dataclass
class TranscriptResult:
    ok: bool
    source: Optional[str] = None              # timeless | google_docs | manual_file | none
    text: Optional[str] = None
    segments: List[Dict[str, Any]] = field(default_factory=list)
    language: Optional[str] = None
    recording_url: Optional[str] = None
    duration_seconds: Optional[int] = None
    raw_payload: Optional[dict] = None
    error: Optional[str] = None


def is_google_doc_link(url: Optional[str]) -> bool:
    if not url:
        return False
    low = url.lower()
    return "docs.google.com" in low or "drive.google.com" in low


def google_doc_id(url: str) -> Optional[str]:
    m = _GOOGLE_DOC_ID.search(url or "")
    if m:
        return m.group(1)
    m = _DRIVE_FILE_ID.search(url or "")
    if m:
        return m.group(1) or m.group(2)
    return None


class TranscriptResolver:
    def __init__(
        self,
        config: Config,
        *,
        timeless: Optional[TimelessClient] = None,
        session: Any = None,
    ):
        self.config = config
        self.timeless = timeless or TimelessClient(config)
        self._session = session  # injectable for tests

    # --- public API -----------------------------------------------------------
    def resolve(
        self, call_url: Optional[str], *, transcript_file: Optional[str] = None
    ) -> TranscriptResult:
        # 1) explicit local file
        if transcript_file:
            return self._from_file(transcript_file)

        # 2) Timeless API first
        if self.timeless.is_configured and call_url:
            res = self.timeless.get_full_transcript_by_url(call_url)
            if res.ok and res.transcript_text and res.transcript_text.strip():
                raw = res.raw if isinstance(res.raw, dict) else {}
                return TranscriptResult(
                    ok=True,
                    source="timeless",
                    text=res.transcript_text,
                    segments=res.segments or [],
                    language=raw.get("language"),
                    recording_url=raw.get("recording_url"),
                    duration_seconds=raw.get("duration_seconds"),
                    raw_payload=raw or None,
                )
            log.info("Timeless had no transcript for %s; trying Google Docs.", call_url)

        # 3) Google Docs / Drive link from the sheet
        if is_google_doc_link(call_url):
            return self._from_google_doc(call_url)  # type: ignore[arg-type]

        # 4) nothing usable
        if not call_url:
            return TranscriptResult(ok=False, source="none", error="No interview link.")
        return TranscriptResult(
            ok=False,
            source="none",
            error=(
                "No full transcript could be fetched (Timeless returned nothing and "
                "the link is not a readable Google Doc)."
            ),
        )

    # --- backends -------------------------------------------------------------
    def _from_file(self, path_str: str) -> TranscriptResult:
        path = Path(path_str)
        if not path.exists():
            return TranscriptResult(
                ok=False, source="manual_file", error=f"Local transcript file not found: {path}"
            )
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return TranscriptResult(
                ok=False, source="manual_file", error=f"Local transcript file empty: {path}"
            )
        return TranscriptResult(ok=True, source="manual_file", text=text)

    def _from_google_doc(self, url: str) -> TranscriptResult:
        doc_id = google_doc_id(url)
        if not doc_id:
            return TranscriptResult(
                ok=False, source="google_docs", error=f"Could not extract a Google Doc id from {url}"
            )
        # Prefer the authenticated Drive export (works for docs shared with the
        # service account); fall back to the public export URL.
        text = self._drive_export(doc_id) or self._public_doc_export(doc_id)
        if text and text.strip():
            return TranscriptResult(
                ok=True, source="google_docs", text=text.strip(), raw_payload={"doc_id": doc_id}
            )
        return TranscriptResult(
            ok=False,
            source="google_docs",
            error=(
                f"Google Doc {doc_id} is not readable. Share it with the service "
                "account (or make it accessible) and ensure GOOGLE_SERVICE_ACCOUNT_* "
                "is set."
            ),
        )

    def _drive_export(self, doc_id: str) -> Optional[str]:
        from .google_creds import has_google_credentials

        if not has_google_credentials(self.config):
            return None
        try:
            from .google_creds import authorized_session

            session = authorized_session(self.config)
            url = f"https://www.googleapis.com/drive/v3/files/{doc_id}/export"
            resp = session.get(url, params={"mimeType": "text/plain"}, timeout=30)
            if resp.status_code == 200:
                return resp.text
            log.warning("Drive export for %s returned HTTP %s", doc_id, resp.status_code)
        except Exception as exc:  # missing deps / no access — fall through
            log.warning("Drive export failed for %s: %s", doc_id, exc)
        return None

    def _public_doc_export(self, doc_id: str) -> Optional[str]:
        """Try the public export endpoint (works only for shared/anyone docs)."""
        try:
            session = self._session
            if session is None:
                import requests  # lazy

                session = requests.Session()
            url = f"https://docs.google.com/document/d/{doc_id}/export"
            resp = session.get(url, params={"format": "txt"}, timeout=30)
            ctype = resp.headers.get("Content-Type", "") if hasattr(resp, "headers") else ""
            # A login redirect returns HTML, not text/plain — reject it.
            if resp.status_code == 200 and "text/html" not in ctype.lower():
                return resp.text
        except Exception as exc:
            log.warning("Public doc export failed for %s: %s", doc_id, exc)
        return None
