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
        self.start_param = config.timeless_start_param or "start_date"
        self.end_param = config.timeless_end_param or "end_date"
        # ``timeless_status_filter`` may be an explicit empty string -> omit it.
        self.status_filter = (config.timeless_status_filter or "").strip()
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
        """List completed meetings for a single day (convenience wrapper)."""
        return self.list_meetings(on_date, on_date)

    def list_meetings(self, start_date: date, end_date: date) -> TimelessResult:
        """List completed meetings in the inclusive ``[start_date, end_date]`` range.

        Returns ``ok = False`` with the blocker message if not configured or
        if the endpoint is unavailable — never raises. Follows the API's cursor
        pagination (``next_cursor``) across pages.
        """
        if not self.is_configured:
            log.warning("Timeless API not configured (no TIMELESS_API_TOKEN).")
            return TimelessResult(ok=False, error=BLOCKER_MESSAGE)

        # Real Timeless API: GET /meetings?<start>=&<end>=&status=completed
        # (cursor pagination via next_cursor / cursor + limit). The param names
        # and status value are configurable because the API is undocumented to
        # us — see scripts/debug_timeless_api.py to discover the right ones.
        params = {
            self.start_param: start_date.isoformat(),
            self.end_param: end_date.isoformat(),
            "limit": 100,
        }
        if self.status_filter:
            params["status"] = self.status_filter
        meetings, raw = self._list_all_pages(self.meetings_path, params)
        if meetings is not None:
            log.info(
                "Timeless returned %d meeting(s) for %s..%s.",
                len(meetings),
                start_date.isoformat(),
                end_date.isoformat(),
            )
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
    # Candidate date param-name pairs to try when the configured ones return 0.
    # Order matters only for reporting; each is tried independently.
    _DATE_PARAM_CANDIDATES = [
        ("start_date", "end_date"),
        ("from", "to"),
        ("start", "end"),
        ("date_from", "date_to"),
        ("created_after", "created_before"),
        ("after", "before"),
        ("from_date", "to_date"),
        ("since", "until"),
    ]

    def diagnose_listing(
        self, start_date: date, end_date: date
    ) -> Dict[str, Any]:
        """Safe, read-only diagnosis of why the listing endpoint returns 0.

        Runs a matrix of GET requests against the meetings endpoint — varying the
        date param names, the status filter, and (only if auth fails) the auth
        scheme — and reports the HTTP status, top-level JSON keys, the detected
        list key, the meeting count, and any pagination markers for each. Never
        raises and NEVER prints/returns the token (only whether one is present
        and its length). Intended for ``scripts/debug_timeless_api.py``.
        """
        token = self.token or ""
        report: Dict[str, Any] = {
            "token_present": bool(token),
            "token_length": len(token),
            "base_url": self.base_url,
            "meetings_path": self.meetings_path,
            "auth_scheme": self.auth_scheme,
            "configured_url": f"{self.base_url}/{self.meetings_path.lstrip('/')}",
            "configured_start_param": self.start_param,
            "configured_end_param": self.end_param,
            "configured_status_filter": self.status_filter or "(none)",
            "date_range": f"{start_date.isoformat()}..{end_date.isoformat()}",
            "variants": [],
            "auth_scheme_probes": [],
            "result_code": None,
            "diagnosis": None,
        }
        if not self.is_configured:
            report["result_code"] = "not_configured"
            report["diagnosis"] = (
                "Not configured: set TIMELESS_API_TOKEN (and TIMELESS_API_BASE_URL "
                "if not using the default)."
            )
            return report

        s, e = start_date.isoformat(), end_date.isoformat()

        # 1) Baseline: exactly what the real ingest path sends.
        baseline_params = {
            self.start_param: s,
            self.end_param: e,
            "limit": 100,
        }
        if self.status_filter:
            baseline_params["status"] = self.status_filter
        baseline = self._probe_variant("configured (used by ingest)", baseline_params)
        report["variants"].append(baseline)

        # If auth is failing, the param matrix is pointless — probe schemes instead.
        if baseline.get("status") in (401, 403):
            report["auth_scheme_probes"] = self._probe_auth_schemes(baseline_params)
            report["result_code"], report["diagnosis"] = self._auth_diagnosis(
                report["auth_scheme_probes"]
            )
            return report

        # 2) Same date params but WITHOUT the status filter.
        if self.status_filter:
            report["variants"].append(
                self._probe_variant(
                    "configured date params, NO status filter",
                    {self.start_param: s, self.end_param: e, "limit": 100},
                )
            )

        # 3) NO params at all (what does the endpoint return by default?).
        report["variants"].append(self._probe_variant("no params (raw listing)", {}))

        # 4) Every candidate date param-name pair (no status filter, to isolate
        #    the date-param question from the status question).
        for sp, ep in self._DATE_PARAM_CANDIDATES:
            if (sp, ep) == (self.start_param, self.end_param):
                continue  # already covered by the baseline / no-status variant
            report["variants"].append(
                self._probe_variant(
                    f"date params: {sp}/{ep} (no status)",
                    {sp: s, ep: e, "limit": 100},
                )
            )

        report["result_code"], report["diagnosis"] = self._listing_diagnosis(
            report["variants"]
        )
        return report

    def _probe_variant(self, label: str, params: dict) -> Dict[str, Any]:
        """One safe GET; returns a structured, token-free summary."""
        entry: Dict[str, Any] = {"label": label, "params": dict(params)}
        resp = self._get(self.meetings_path, params)
        if resp is None:
            entry["status"] = None
            entry["result"] = "no response (network error after retries)"
            return entry
        entry["status"] = resp.status_code
        keys, parsed = self._safe_keys(resp)
        entry["json_keys"] = keys
        if resp.status_code != 200:
            entry["error_excerpt"] = self._error_excerpt(parsed, resp)
            entry["count"] = None
            return entry
        meetings = self._extract_meetings(parsed) if parsed is not None else None
        entry["list_key"] = self._detect_list_key(parsed)
        entry["count"] = len(meetings) if meetings is not None else None
        entry["pagination"] = self._pagination_markers(parsed)
        return entry

    def _probe_auth_schemes(self, params: dict) -> List[Dict[str, Any]]:
        """Try each auth header style; report which yields a non-401/403."""
        probes: List[Dict[str, Any]] = []
        original = self.auth_scheme
        try:
            for scheme in ("bearer", "x-api-key", "token"):
                self.auth_scheme = scheme
                resp = self._get(self.meetings_path, params)
                probe = {"auth_scheme": scheme}
                if resp is None:
                    probe["status"] = None
                    probe["result"] = "no response (network error)"
                else:
                    probe["status"] = resp.status_code
                probes.append(probe)
        finally:
            self.auth_scheme = original
        return probes

    @staticmethod
    def _detect_list_key(data: Any) -> Optional[str]:
        if isinstance(data, list):
            return "<top-level list>"
        if isinstance(data, dict):
            for key in ("meetings", "data", "results", "items"):
                if isinstance(data.get(key), list):
                    return key
        return None

    @staticmethod
    def _pagination_markers(data: Any) -> Dict[str, Any]:
        """Surface any pagination-looking fields so the caller can confirm them."""
        markers: Dict[str, Any] = {}
        if isinstance(data, dict):
            for key in (
                "next_cursor", "nextCursor", "next", "cursor",
                "has_more", "hasMore", "page", "total_pages", "totalPages",
                "total", "count", "limit", "offset",
            ):
                if key in data:
                    markers[key] = data[key]
        return markers

    @staticmethod
    def _error_excerpt(parsed: Any, resp: Any) -> str:
        """A short, safe excerpt of a non-200 body (never contains the token)."""
        if isinstance(parsed, (dict, list)):
            import json as _json

            try:
                return _json.dumps(parsed, ensure_ascii=False)[:300]
            except Exception:
                return str(parsed)[:300]
        text = getattr(resp, "text", "") or ""
        return text[:300]

    @staticmethod
    def _auth_diagnosis(probes: List[Dict[str, Any]]):
        """Return (result_code, message) for the auth-failure path."""
        working = [p["auth_scheme"] for p in probes if p.get("status") not in (None, 401, 403)]
        if working:
            return "auth_wrong_scheme", (
                "AUTH: the configured auth scheme was rejected (401/403), but "
                f"scheme(s) {working} were accepted. Set TIMELESS_AUTH_SCHEME to "
                f"'{working[0]}'."
            )
        return "auth_blocker", (
            "AUTH BLOCKER: every auth scheme (bearer / x-api-key / token) returned "
            "401/403. The TIMELESS_API_TOKEN is likely invalid, expired, or lacks "
            "API access for this workspace. Re-issue the token in Timeless."
        )

    @classmethod
    def _listing_diagnosis(cls, variants: List[Dict[str, Any]]):
        """Return (result_code, message) explaining the listing result.

        The crucial subtlety: this API silently ignores *unknown* query params,
        so an alternative date-param name that returns the SAME count as the
        no-params listing is being ignored — it is NOT a working filter. A param
        is only "honoured" when it changes the count versus the unfiltered list.
        """
        any_200 = [v for v in variants if v.get("status") == 200]
        if not any_200:
            statuses = sorted(
                {v.get("status") for v in variants}, key=lambda x: (x is None, x)
            )
            return "unreachable", (
                f"No variant returned HTTP 200 (statuses seen: {statuses}). The "
                "endpoint/auth is wrong or the API is unreachable."
            )

        def _find(pred):
            return next((v for v in variants if pred(v)), None)

        configured = _find(lambda v: v["label"].startswith("configured (used by ingest)"))
        no_params = _find(lambda v: not v.get("params"))
        no_status = _find(lambda v: "NO status filter" in v["label"])
        all_count = no_params.get("count") if no_params else None  # unfiltered total
        configured_count = configured.get("count") if configured else None

        # 1) The ingest range already returns meetings — nothing to fix.
        if isinstance(configured_count, int) and configured_count > 0:
            return "ok_in_range", (
                f"OK: the configured params returned {configured_count} meeting(s) "
                "for this range. The Timeless integration is working."
            )

        # 2) The status filter is hiding meetings: with the SAME date params,
        #    dropping the status filter turns 0 into >0.
        if (
            no_status
            and isinstance(no_status.get("count"), int)
            and no_status["count"] > 0
        ):
            return "status_filter", (
                "STATUS FILTER is hiding meetings: the configured date range returns "
                f"{no_status['count']} meeting(s) without the status filter but 0 with "
                "status=completed. Set TIMELESS_STATUS_FILTER= (empty) in Render, or to "
                "the value the API actually uses."
            )

        # 3) A DIFFERENT date param name actually filters (count differs from the
        #    unfiltered total) and returns meetings while the configured one is 0
        #    -> the configured param names are wrong.
        for v in variants:
            if v is configured or v is no_params or v is no_status:
                continue
            c = v.get("count")
            if isinstance(c, int) and c > 0 and c != all_count:
                return "wrong_param", (
                    f"WRONG DATE PARAM NAMES: variant '{v['label']}' returned {c} "
                    "meeting(s) — a real filtered subset — while the configured "
                    "start_date/end_date returned 0. Set TIMELESS_START_PARAM / "
                    f"TIMELESS_END_PARAM to match these params: {v['params']}."
                )

        # 4) Nothing anywhere, even with no filter -> the documented hard blocker.
        if not all_count:  # 0 or None
            return "empty_workspace", (
                "Timeless API does not expose meetings for this token/workspace/date "
                "range. Even with NO date filter the listing returned 0 meetings. "
                "Confirm the TIMELESS_API_TOKEN belongs to the workspace that holds "
                "the meetings and has API access to them."
            )

        # 5) There ARE meetings overall, the configured date params ARE honoured
        #    (0 differs from the unfiltered total, and every other param name just
        #    echoes the unfiltered total = ignored), so the range is simply empty.
        return "no_meetings_in_range", (
            f"NO MEETINGS IN THIS DATE RANGE. The workspace has {all_count} meeting(s) "
            "in total, but none in the requested range. The configured "
            "start_date/end_date params are working correctly (every other param name "
            f"is ignored by the API and just echoes the full {all_count}). This is NOT "
            "an integration bug — re-run ingest with the date range that actually "
            "contains meetings (see the dates in the Timeless UI)."
        )

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
            self.start_param: start_date or today,
            self.end_param: end_date or today,
            "limit": 100,
        }
        if self.status_filter:
            list_params["status"] = self.status_filter
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
