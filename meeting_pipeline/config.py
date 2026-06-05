"""Central configuration loaded from environment variables.

All secrets come from the environment (or a local ``.env`` that is never
committed). Nothing here should ever contain a hard-coded credential.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Optional

try:  # python-dotenv is optional; the app also works with real env vars.
    from dotenv import load_dotenv

    load_dotenv()
except Exception:  # pragma: no cover - dotenv is a convenience only
    pass


def _get(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(name, default)
    if value is not None:
        value = value.strip()
    return value or default


@dataclass
class Config:
    # --- Supabase (L1 / L2 storage) ---
    supabase_url: Optional[str] = field(default_factory=lambda: _get("SUPABASE_URL"))
    supabase_service_role_key: Optional[str] = field(
        default_factory=lambda: _get("SUPABASE_SERVICE_ROLE_KEY")
    )

    # --- Timeless (meeting source) ---
    timeless_api_token: Optional[str] = field(
        default_factory=lambda: _get("TIMELESS_API_TOKEN")
    )
    timeless_api_base_url: str = field(
        default_factory=lambda: _get(
            "TIMELESS_API_BASE_URL", "https://api.timeless.day/v1"
        )
    )

    # --- AI (Anthropic) ---
    anthropic_api_key: Optional[str] = field(
        default_factory=lambda: _get("ANTHROPIC_API_KEY")
    )
    ai_model_id: str = field(
        default_factory=lambda: _get("AI_MODEL_ID", "claude-sonnet-4-20250514")
    )
    ai_prompt_version: str = field(
        default_factory=lambda: _get("AI_PROMPT_VERSION", "full_transcript_prompt_v1")
    )

    # --- Telegram ---
    telegram_bot_token: Optional[str] = field(
        default_factory=lambda: _get("TELEGRAM_BOT_TOKEN")
    )
    telegram_management_chat_id: Optional[str] = field(
        default_factory=lambda: _get("TELEGRAM_MANAGEMENT_CHAT_ID")
    )

    # --- Meeting defaults ---
    default_source: str = field(
        default_factory=lambda: _get("MEETING_DEFAULT_SOURCE", "timeless")
    )
    default_language: str = field(
        default_factory=lambda: _get("MEETING_DEFAULT_LANGUAGE", "hy")
    )
    delivery_time: str = field(
        default_factory=lambda: _get("MEETING_DELIVERY_TIME", "11:00")
    )

    # Armenia is UTC+4 (no DST). Used for "today" boundaries and scheduling notes.
    timezone_offset_hours: int = 4

    # --- Validation helpers ---------------------------------------------------
    @property
    def has_supabase(self) -> bool:
        return bool(self.supabase_url and self.supabase_service_role_key)

    @property
    def has_timeless(self) -> bool:
        return bool(self.timeless_api_token)

    @property
    def has_anthropic(self) -> bool:
        return bool(self.anthropic_api_key)

    @property
    def has_telegram(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_management_chat_id)

    def require_supabase(self) -> None:
        if not self.has_supabase:
            raise RuntimeError(
                "Supabase is not configured. Set SUPABASE_URL and "
                "SUPABASE_SERVICE_ROLE_KEY in your environment / .env file."
            )

    def require_anthropic(self) -> None:
        if not self.has_anthropic:
            raise RuntimeError(
                "Anthropic is not configured. Set ANTHROPIC_API_KEY in your "
                "environment / .env file."
            )

    def require_telegram(self) -> None:
        if not self.has_telegram:
            raise RuntimeError(
                "Telegram is not configured. Set TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_MANAGEMENT_CHAT_ID in your environment / .env file."
            )


def load_config() -> Config:
    """Build a :class:`Config` from the current environment."""
    return Config()
