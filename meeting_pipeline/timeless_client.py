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

import time
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Callable, Dict, List, Optional

from .config import Config
from .utils import get_logger

log = get_logger("meeting_pipeline.timeless")

BLOCKER_MESSAGE = (
    "Timeless API not configured or full transcript endpoint unavailable"
)

# HTTP statuses worth retrying (transient): rate-limit + server errors.
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class TimelessResult:
    ok: bool
    error: Optional[str] = None
    meetings: List[Dict[str, Any]] = field(default_factory=list)
    transcript_text: Optional[str] = None
    segments: List[Dict[str, Any]] = field(default_factory=list)
    raw: Optional[dict] = None


class TimelessClient:
    def __init__(
        self,
        config: Config,
        session: Any = None,
        *,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self.base_url = (config.timeless_api_base_url or "").rstrip("/")
        self.token = config.timeless_api_token
        self.auth_scheme = (config.timeless_auth_scheme or "bearer").strip().lower()
        self.meetings_path = config.timeless_meetings_path or "meetings"
        self.transcript_templates = [
            t.strip()
            for t in (config.timeless_transcript_path_templates or "").split(",")
            if t.strip()
        ] or ["meetings/{id}/transcript"]
        self.max_retries = max(0, int(config.timeless_max_retries or 0))
        self._sleep = sleep  # injectable so tests don't actually wait
        if session is not None:
            self._session = session
        else:
            import requests  # imported lazily so tests don't require it

            self._session = requests.Session()

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.base_url)

    def _headers(self) -> Dict[str, str]:
        headers = {"Accept": "application/json"}
        if self.auth_scheme == "x-api-key":
            headers["X-API-Key"] = self.token or ""
        elif self.auth_scheme == "token":
            headers["Authorization"] = f"Token {self.token}"
        else:  # default: bearer
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    def _get(self, path: str, params: Optional[dict] = None):
        """GET with retry + exponential backoff on transient failures.

        Retries network errors and 429/5xx responses, honouring a ``Retry-After``
        header when present. Returns the final response (caller checks status),
        or ``None`` if every attempt raised before producing a response.
        """
        url = f"{self.base_url}/{path.lstrip('/')}"
        last_resp = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self._session.get(
                    url, headers=self._headers(), params=params or {}, timeout=30
                )
            except Exception as exc:  # network / DNS / timeout
                log.warning(
                    "Timeless GET /%s attempt %d/%d raised: %s",
                    path,
                    attempt + 1,
                    self.max_retries + 1,
                    exc,
                )
                if attempt < self.max_retries:
                    self._sleep(self._backoff(attempt))
                    continue
                return None
            last_resp = resp
            if resp.status_code in _RETRYABLE_STATUS and attempt < self.max_retries:
                delay = self._retry_after(resp) or self._backoff(attempt)
                log.warning(
                    "Timeless GET /%s returned HTTP %s; retrying in %.1fs",
                    path,
                    resp.status_code,
                    delay,
                )
                self._sleep(delay)
                continue
            return resp
        return last_resp

    @staticmethod
    def _backoff(attempt: int) -> float:
        return 2.0 ** attempt  # 1s, 2s, 4s, ...

    @staticmethod
    def _retry_after(resp: Any) -> Optional[float]:
        try:
            value = (getattr(resp, "headers", {}) or {}).get("Retry-After")
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    def list_today_meetings(self, on_date: date) -> TimelessResult:
        """Attempt to list completed meetings for ``on_date``.

        Returns ``ok = False`` with the blocker message if not configured or
        if the endpoint is unavailable — never raises. Follows pagination when
        the API exposes a ``next`` cursor or ``page``-style metadata.
        """
        if not self.is_configured:
            log.warning("Timeless API not configured (no TIMELESS_API_TOKEN).")
            return TimelessResult(ok=False, error=BLOCKER_MESSAGE)

        # Real Timeless API: GET /meetings?start_date=&end_date=&status=completed
        # (cursor pagination via next_cursor / cursor + limit).
        params = {
            "start_date": on_date.isoformat(),
            "end_date": on_date.isoformat(),
            "status": "completed",
            "limit": 100,
        }
        meetings, raw = self._list_all_pages(self.meetings_path, params)
        if meetings is not None:
            log.info("Timeless returned %d meeting(s).", len(meetings))
            return TimelessResult(ok=True, meetings=meetings, raw=raw)
        return TimelessResult(ok=False, error=BLOCKER_MESSAGE)

    def _list_all_pages(self, path: str, params: dict):
        """Fetch every page for a listing request.

        Returns ``(meetings, last_raw)`` on success or ``(None, None)`` if the
        first page did not yield a usable list.
        """
        collected: List[Dict[str, Any]] = []
        page_params = dict(params)
        last_raw: Optional[dict] = None
        seen_pages = 0
        while True:
            resp = self._get(path, page_params)
            if resp is None or resp.status_code != 200:
                if resp is not None:
                    log.warning("Timeless /%s returned HTTP %s", path, resp.status_code)
                return (collected, last_raw) if collected else (None, None)
            try:
                data = resp.json()
            except Exception:
                return (collected, last_raw) if collected else (None, None)
            last_raw = data if isinstance(data, dict) else {"meetings": data}
            batch = self._extract_meetings(data)
            if batch is None:
                return (collected, last_raw) if collected else (None, None)
            collected.extend(batch)
            seen_pages += 1

            next_params = self._next_page_params(data, page_params)
            # Stop on no cursor, an empty page, or a sanity cap.
            if not next_params or not batch or seen_pages >= 50:
                break
            page_params = next_params
        return collected, last_raw

    @staticmethod
    def _extract_meetings(data: Any) -> Optional[List[Dict[str, Any]]]:
        if isinstance(data, list):
            return data
        if isinstance(data, dict):
            for key in ("meetings", "data", "results", "items"):
                value = data.get(key)
                if isinstance(value, list):
                    return value
        return None

    @staticmethod
    def _next_page_params(data: Any, current: dict) -> Optional[dict]:
        """Derive the next page's params from common pagination shapes."""
        if not isinstance(data, dict):
            return None
        # Cursor style: {"next_cursor": "..."} or {"next": "..."}.
        for key in ("next_cursor", "nextCursor", "next"):
            cursor = data.get(key)
            if isinstance(cursor, str) and cursor:
                return {**current, "cursor": cursor}
        # Page-number style: {"page": 1, "total_pages": 3}.
        page = data.get("page")
        total = data.get("total_pages") or data.get("totalPages")
        if isinstance(page, int) and isinstance(total, int) and page < total:
            return {**current, "page": page + 1}
        return None

    @staticmethod
    def meeting_id_from_url(url: str) -> Optional[str]:
        """Extract a Timeless call/meeting id from a share link.

        Handles shapes like:
          https://app.timeless.day/meetings/<id>
          https://timeless.day/m/<id>?foo=bar
          https://api.timeless.day/v1/meetings/<id>/transcript
        Falls back to the last non-empty path segment. Returns None if nothing
        usable is found.
        """
        if not url or not isinstance(url, str):
            return None
        from urllib.parse import urlparse

        cleaned = url.strip()
        try:
            parsed = urlparse(cleaned)
        except Exception:
            return None
        segments = [s for s in (parsed.path or "").split("/") if s]
        # Skip well-known trailing keywords so we land on the id itself.
        skip = {"transcript", "full", "meetings", "m", "meeting", "v1"}
        for seg in reversed(segments):
            if seg.lower() not in skip:
                return seg
        return segments[-1] if segments else None

    def get_full_transcript_by_url(self, url: str) -> TimelessResult:
        """Fetch the FULL transcript for a Timeless call given its share link."""
        meeting_id = self.meeting_id_from_url(url)
        if not meeting_id:
            return TimelessResult(
                ok=False, error=f"Could not extract a meeting id from URL: {url}"
            )
        return self.get_full_transcript(meeting_id)

    def get_full_transcript(self, meeting_id: str) -> TimelessResult:
        """Attempt to fetch the FULL transcript for a meeting.

        On success returns ``transcript_text`` (+ optional ``segments``).
        On any failure returns the blocker message — and importantly, we do
        NOT fall back to a summary here.
        """
        if not self.is_configured:
            return TimelessResult(ok=False, error=BLOCKER_MESSAGE)

        attempts = [t.format(id=meeting_id) for t in self.transcript_templates]
        for path in attempts:
            resp = self._get(path)
            if resp is None:
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

        The real Timeless transcript has no single text field: it is rebuilt by
        joining ``segments[].text`` in order, resolving each ``segments[].speaker_id``
        against the top-level ``speakers[]`` (id -> name) map. Still tolerant of
        simpler shapes (a top-level text field, or segments with an inline
        ``speaker`` name) so local-file and legacy payloads keep working.
        Never uses a 'summary' / 'tldr' field as the transcript.
        """
        segments: List[Dict[str, Any]] = []
        if isinstance(data, dict):
            for key in ("transcript", "full_transcript", "text", "content"):
                value = data.get(key)
                if isinstance(value, str) and value.strip():
                    return value, data.get("segments", []) or []

            # Build a speaker_id -> name lookup from the speakers array.
            speaker_names: Dict[str, str] = {}
            for sp in data.get("speakers") or []:
                if isinstance(sp, dict) and sp.get("id") is not None:
                    speaker_names[str(sp["id"])] = sp.get("name") or ""

            raw_segments = data.get("segments") or data.get("utterances")
            if isinstance(raw_segments, list) and raw_segments:
                lines = []
                for seg in raw_segments:
                    if not isinstance(seg, dict):
                        continue
                    speaker = (
                        speaker_names.get(str(seg.get("speaker_id")))
                        or seg.get("speaker")
                        or seg.get("speaker_name")
                        or seg.get("speaker_id")
                        or ""
                    )
                    line = seg.get("text") or seg.get("content") or ""
                    if line:
                        lines.append(f"{speaker}: {line}".strip(": ").strip())
                segments = raw_segments
                if lines:
                    return "\n".join(lines), segments
        return "", segments

    # --- Diagnostics ----------------------------------------------------------
    def probe(
        self,
        sample_meeting_id: Optional[str] = None,
        *,
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> Dict[str, Any]:
        """Live connectivity + endpoint-discovery check (uses the API key).

        Calls the configured listing endpoint and, if a meeting id is found (or
        supplied), the transcript endpoints — reporting which paths returned 200
        and the top-level JSON keys of each response. Never raises and never logs
        the token. Intended for ``scripts/check_timeless.py`` so the real Timeless
        API shape can be confirmed on a live run.
        """
        report: Dict[str, Any] = {
            "configured": self.is_configured,
            "base_url": self.base_url,
            "auth_scheme": self.auth_scheme,
            "attempts": [],
            "discovered_meeting_id": None,
            "ok": False,
        }
        if not self.is_configured:
            report["error"] = "Not configured (missing TIMELESS_API_TOKEN/base url)."
            return report

        # 1) Listing endpoint.
        today = date.today().isoformat()
        list_params = {
            "start_date": start_date or today,
            "end_date": end_date or today,
            "status": "completed",
            "limit": 100,
        }
        resp = self._get(self.meetings_path, list_params)
        entry: Dict[str, Any] = {"path": self.meetings_path, "params": list_params}
        meeting_id = sample_meeting_id
        if resp is None:
            entry["result"] = "no response (network error)"
        else:
            entry["status"] = resp.status_code
            entry["json_keys"], parsed = self._safe_keys(resp)
            meetings = self._extract_meetings(parsed) if parsed is not None else None
            entry["meeting_count"] = len(meetings) if meetings is not None else None
            if meetings and not meeting_id:
                first = meetings[0]
                if isinstance(first, dict):
                    meeting_id = str(first.get("id") or first.get("meeting_id") or "") or None
        report["attempts"].append(entry)
        report["discovered_meeting_id"] = meeting_id

        # 2) Transcript endpoints (only if we have an id to try).
        if meeting_id:
            for template in self.transcript_templates:
                path = template.format(id=meeting_id)
                resp = self._get(path)
                t_entry: Dict[str, Any] = {"path": path}
                if resp is None:
                    t_entry["result"] = "no response (network error)"
                else:
                    t_entry["status"] = resp.status_code
                    t_entry["json_keys"], parsed = self._safe_keys(resp)
                    if resp.status_code == 200 and parsed is not None:
                        text, _ = self._extract_transcript(parsed)
                        t_entry["transcript_chars"] = len(text or "")
                        if text and text.strip():
                            report["ok"] = True
                            t_entry["working"] = True
                report["attempts"].append(t_entry)
                if report["ok"]:
                    break  # a working endpoint was found; no need to try the rest

        return report

    @staticmethod
    def _safe_keys(resp: Any):
        """Return (top-level json keys, parsed) without raising."""
        try:
            data = resp.json()
        except Exception:
            return None, None
        if isinstance(data, dict):
            return sorted(data.keys()), data
        if isinstance(data, list):
            return ["<list>"], data
        return ["<scalar>"], data
