"""Telegram Bot API client (via ``requests``).

Handles Markdown delivery, long-message splitting and clear error reporting.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, List, Optional

from .config import Config
from .utils import get_logger, split_telegram_message

log = get_logger("meeting_pipeline.telegram")


@dataclass
class TelegramResult:
    ok: bool
    parts_sent: int = 0
    error: Optional[str] = None
    responses: List[Any] = field(default_factory=list)


class TelegramClient:
    def __init__(
        self,
        config: Config,
        chat_id: Optional[str] = None,
        session: Any = None,
    ):
        self.config = config
        self.token = config.telegram_bot_token
        self.chat_id = chat_id or config.telegram_management_chat_id
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
        """Send a (possibly long) markdown message, splitting as needed."""
        if not self.is_configured:
            return TelegramResult(
                ok=False,
                error="Telegram not configured (missing token or chat id).",
            )

        parts = split_telegram_message(text)
        responses = []
        for index, part in enumerate(parts, start=1):
            payload = {
                "chat_id": self.chat_id,
                "text": part,
                "parse_mode": parse_mode,
                "disable_web_page_preview": True,
            }
            try:
                resp = self._session.post(self._api_url(), json=payload, timeout=30)
            except Exception as exc:
                log.error("Telegram send failed on part %d: %s", index, exc)
                return TelegramResult(
                    ok=False,
                    parts_sent=index - 1,
                    error=f"Telegram request failed: {exc}",
                    responses=responses,
                )

            ok = getattr(resp, "status_code", None) == 200
            body = self._safe_json(resp)
            responses.append(body)
            if not ok or not (isinstance(body, dict) and body.get("ok")):
                detail = body.get("description") if isinstance(body, dict) else None
                log.error(
                    "Telegram API error on part %d: HTTP %s %s",
                    index,
                    getattr(resp, "status_code", "?"),
                    detail,
                )
                return TelegramResult(
                    ok=False,
                    parts_sent=index - 1,
                    error=f"Telegram API error: {detail or 'unknown'}",
                    responses=responses,
                )
            log.info("Sent Telegram message part %d/%d", index, len(parts))

        return TelegramResult(ok=True, parts_sent=len(parts), responses=responses)

    @staticmethod
    def _safe_json(resp: Any):
        try:
            return resp.json()
        except Exception:
            return {"ok": False, "description": "non-JSON response"}
