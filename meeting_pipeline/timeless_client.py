"""Timeless API client.

IMPORTANT: We use the FULL TRANSCRIPT only. The Timeless TL;DR / summary is
NOT a substitute for the full transcript. If the API cannot provide a full
transcript, this client returns a clear blocker result instead of faking one
from the summary.

The exact Timeless transcript endpoint is not publicly documented to us. This
client makes a best-effort attempt against a small set of plausible endpoints
and, on any failure or when no token is configured, returns a structured
result object with ``ok = False`` and an explanatory ``error``. The caller
then falls back to the local-file mode.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Dict, List, Optional

from .config import Config
from .utils import get_logger

log = get_logger("meeting_pipeline.timeless")

BLOCKER_MESSAGE = (
    "Timeless API not configured or full transcript endpoint unavailable"
)


@dataclass
class TimelessResult:
    ok: bool
    error: Optional[str] = None
    meetings: List[Dict[str, Any]] = field(default_factory=list)
    transcript_text: Optional[str] = None
    segments: List[Dict[str, Any]] = field(default_factory=list)
    raw: Optional[dict] = None


class TimelessClient:
    def __init__(self, config: Config, session: Any = None):
        self.config = config
        self.base_url = (config.timeless_api_base_url or "").rstrip("/")
        self.token = config.timeless_api_token
        if session is not None:
            self._session = session
        else:
            import requests  # imported lazily so tests don't require it

            self._session = requests.Session()

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.base_url)

    def _headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.token}",
            "Accept": "application/json",
        }

    def _get(self, path: str, params: Optional[dict] = None):
        url = f"{self.base_url}/{path.lstrip('/')}"
        return self._session.get(
            url, headers=self._headers(), params=params or {}, timeout=30
        )

    def list_today_meetings(self, on_date: date) -> TimelessResult:
        """Attempt to list completed meetings for ``on_date``.

        Returns ``ok = False`` with the blocker message if not configured or
        if the endpoint is unavailable — never raises.
        """
        if not self.is_configured:
            log.warning("Timeless API not configured (no TIMELESS_API_TOKEN).")
            return TimelessResult(ok=False, error=BLOCKER_MESSAGE)

        # Plausible endpoint shapes; we try them in order.
        attempts = [
            ("meetings", {"date": on_date.isoformat(), "status": "completed"}),
            ("meetings", {"day": on_date.isoformat()}),
        ]
        for path, params in attempts:
            try:
                resp = self._get(path, params)
            except Exception as exc:  # network / DNS / timeout
                log.warning("Timeless request to /%s failed: %s", path, exc)
                continue
            if resp.status_code == 200:
                try:
                    data = resp.json()
                except Exception:
                    continue
                meetings = data.get("meetings", data) if isinstance(data, dict) else data
                if isinstance(meetings, list):
                    log.info("Timeless returned %d meeting(s).", len(meetings))
                    return TimelessResult(ok=True, meetings=meetings, raw=data)
            else:
                log.warning(
                    "Timeless /%s returned HTTP %s", path, resp.status_code
                )
        return TimelessResult(ok=False, error=BLOCKER_MESSAGE)

    def get_full_transcript(self, meeting_id: str) -> TimelessResult:
        """Attempt to fetch the FULL transcript for a meeting.

        On success returns ``transcript_text`` (+ optional ``segments``).
        On any failure returns the blocker message — and importantly, we do
        NOT fall back to a summary here.
        """
        if not self.is_configured:
            return TimelessResult(ok=False, error=BLOCKER_MESSAGE)

        attempts = [
            f"meetings/{meeting_id}/transcript",
            f"meetings/{meeting_id}/transcript/full",
            f"transcripts/{meeting_id}",
        ]
        for path in attempts:
            try:
                resp = self._get(path)
            except Exception as exc:
                log.warning("Timeless transcript request /%s failed: %s", path, exc)
                continue
            if resp.status_code != 200:
                log.warning("Timeless /%s returned HTTP %s", path, resp.status_code)
                continue
            try:
                data = resp.json()
            except Exception:
                continue

            text, segments = self._extract_transcript(data)
            if text and text.strip():
                log.info(
                    "Fetched full transcript for meeting %s (%d chars).",
                    meeting_id,
                    len(text),
                )
                return TimelessResult(
                    ok=True,
                    transcript_text=text,
                    segments=segments,
                    raw=data,
                )
        return TimelessResult(ok=False, error=BLOCKER_MESSAGE)

    @staticmethod
    def _extract_transcript(data: Any):
        """Pull a plain-text transcript + segments out of a Timeless payload.

        Tolerant of several shapes; returns ("", []) if nothing usable found.
        Never uses a 'summary' / 'tldr' field as the transcript.
        """
        segments: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            for key in ("transcript", "full_transcript", "text", "content"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value, data.get("segments", []) or []
            raw_segments = data.get("segments") or data.get("utterances")
            if isinstance(raw_segments, list) and raw_segments:
                lines = []
                for seg in raw_segments:
                    if not isinstance(seg, dict):
                        continue
                    speaker = seg.get("speaker") or seg.get("speaker_name") or ""
                    line = seg.get("text") or seg.get("content") or ""
                    if line:
                        lines.append(f"{speaker}: {line}".strip(": ").strip())
                segments = raw_segments
                if lines:
                    return "\n".join(lines), segments
        return "", segments
