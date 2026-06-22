"""Telegram Bot API client (via ``requests``).

Handles Markdown delivery, long-message splitting, clear error reporting and —
because network egress to Telegram can be flaky — automatic retries with
exponential backoff on transient failures (timeouts, 429, 5xx). This mirrors the
self-healing retry loop used by the interview digest so a meeting report is not
lost after a single timeout.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Callable, List, Optional

from .config import Config
from .utils import get_logger, split_telegram_message, to_telegram_markdown

log = get_logger("meeting_pipeline.telegram")

# HTTP statuses worth retrying (server-side / rate limit), as opposed to a 400
# Markdown parse error which is handled by the plain-text fallback instead.
_TRANSIENT_STATUSES = {429, 500, 502, 503, 504}


@dataclass
class TelegramResult:
    ok: bool
    parts_sent: int = 0
    error: Optional[str] = None
    responses: List[Any] = field(default_factory=list)


@dataclass
class _PartResult:
    ok: bool
    body: Any = None
    error: Optional[str] = None


class TelegramClient:
    def __init__(
        self,
        config: Config,
        chat_id: Optional[str] = None,
        session: Any = None,
        sleep: Optional[Callable[[float], None]] = None,
    ):
        self.config = config
        self.token = config.telegram_bot_token
        self.chat_id = chat_id or config.telegram_management_chat_id
        self.max_retries = max(0, int(getattr(config, "telegram_max_retries", 4) or 0))
        self.timeout = int(getattr(config, "telegram_request_timeout", 30) or 30)
        # Injectable so tests don't actually sleep during backoff.
        self._sleep = sleep or time.sleep
        if session is not None:
            self._session = session
        else:
            import requests  # imported lazily

            self._session = requests.Session()

    @property
    def is_configured(self) -> bool:
        return bool(self.token and self.chat_id)

    def _api_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}/sendMessage"

    def send_message(
        self, text: str, parse_mode: str = "Markdown"
    ) -> TelegramResult:
        """Send a (possibly long) message, splitting and retrying as needed."""
        if not self.is_configured:
            return TelegramResult(
                ok=False,
                error="Telegram not configured (missing token or chat id).",
            )

        parts = split_telegram_message(text)
        responses: List[Any] = []
        for index, part in enumerate(parts, start=1):
            result = self._send_part(part, parse_mode, index)
            if result.body is not None:
                responses.append(result.body)
            if not result.ok:
                return TelegramResult(
                    ok=False,
                    parts_sent=index - 1,
                    error=result.error,
                    responses=responses,
                )
            log.info("Sent Telegram message part %d/%d", index, len(parts))

        return TelegramResult(ok=True, parts_sent=len(parts), responses=responses)

    def _send_part(
        self, part: str, parse_mode: Optional[str], index: int
    ) -> _PartResult:
        """Send one message part, retrying transient failures with backoff."""
        for attempt in range(1, self.max_retries + 2):  # 1 initial + N retries
            try:
                resp = self._post_part(part, parse_mode)
            except Exception as exc:  # network error / timeout
                if attempt <= self.max_retries:
                    delay = 2 ** attempt
                    log.warning(
                        "Telegram send error on part %d (attempt %d): %s; "
                        "retrying in %ds.",
                        index, attempt, exc, delay,
                    )
                    self._sleep(delay)
                    continue
                return _PartResult(ok=False, error=f"Telegram request failed: {exc}")

            status = getattr(resp, "status_code", None)
            body = self._safe_json(resp)

            if self._is_ok(status, body):
                return _PartResult(ok=True, body=body)

            # Markdown parse error (HTTP 400): retry this part once as plain text
            # so the report still lands. Not transient — don't loop on it.
            if parse_mode and status == 400:
                log.warning(
                    "Telegram rejected Markdown on part %d (%s); retrying as plain text.",
                    index,
                    body.get("description") if isinstance(body, dict) else "?",
                )
                try:
                    resp = self._post_part(part, None)
                except Exception as exc:
                    return _PartResult(ok=False, error=f"Telegram request failed: {exc}")
                status = getattr(resp, "status_code", None)
                body = self._safe_json(resp)
                if self._is_ok(status, body):
                    return _PartResult(ok=True, body=body)

            # Transient server-side / rate-limit errors: back off and retry.
            if status in _TRANSIENT_STATUSES and attempt <= self.max_retries:
                delay = self._retry_after(body) or (2 ** attempt)
                log.warning(
                    "Telegram transient HTTP %s on part %d (attempt %d); "
                    "retrying in %ds.",
                    status, index, attempt, delay,
                )
                self._sleep(delay)
                continue

            detail = body.get("description") if isinstance(body, dict) else None
            log.error(
                "Telegram API error on part %d: HTTP %s %s",
                index, status, detail,
            )
            return _PartResult(
                ok=False,
                body=body,
                error=f"Telegram API error: {detail or status or 'unknown'}",
            )

        # Loop exhausted without a definitive result.
        return _PartResult(ok=False, error="Telegram delivery failed after retries.")

    @staticmethod
    def _is_ok(status: Any, body: Any) -> bool:
        return status == 200 and isinstance(body, dict) and bool(body.get("ok"))

    @staticmethod
    def _retry_after(body: Any) -> Optional[int]:
        """Honour Telegram's ``parameters.retry_after`` on 429 when present."""
        if isinstance(body, dict):
            params = body.get("parameters")
            if isinstance(params, dict) and params.get("retry_after"):
                try:
                    return int(params["retry_after"])
                except (TypeError, ValueError):
                    return None
        return None

    def _post_part(self, text: str, parse_mode: Optional[str]):
        # Telegram legacy Markdown uses *bold*, not GFM **bold**.
        if parse_mode == "Markdown":
            text = to_telegram_markdown(text)
        payload = {
            "chat_id": self.chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        return self._session.post(self._api_url(), json=payload, timeout=self.timeout)

    @staticmethod
    def _safe_json(resp: Any):
        try:
            return resp.json()
        except Exception:
            return {"ok": False, "description": "non-JSON response"}
