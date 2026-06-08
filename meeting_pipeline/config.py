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


# Default model per provider, applied when AI_MODEL_ID is not set explicitly.
_DEFAULT_MODELS = {
    "anthropic": "claude-sonnet-4-20250514",
    "gemini": "gemini-2.5-pro",
}


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
    # The exact Timeless endpoints/auth are not documented to us, so they are
    # overridable from the environment — they can be corrected in Render without
    # a code change once the real shapes are confirmed (see scripts/check_timeless.py).
    # Auth header style: "bearer" -> Authorization: Bearer, "x-api-key" -> X-API-Key,
    # "token" -> Authorization: Token.
    timeless_auth_scheme: str = field(
        default_factory=lambda: _get("TIMELESS_AUTH_SCHEME", "bearer")
    )
    # Listing endpoint (relative to base url) for completed meetings.
    timeless_meetings_path: str = field(
        default_factory=lambda: _get("TIMELESS_MEETINGS_PATH", "meetings")
    )
    # Comma-separated transcript path templates; "{id}" is substituted.
    timeless_transcript_path_templates: str = field(
        default_factory=lambda: _get(
            "TIMELESS_TRANSCRIPT_PATH_TEMPLATES",
            "meetings/{id}/transcript,meetings/{id}/transcript/full,transcripts/{id}",
        )
    )
    # Retries for transient (network / 429 / 5xx) Timeless errors.
    timeless_max_retries: int = field(
        default_factory=lambda: int(_get("TIMELESS_MAX_RETRIES", "3"))
    )

    # --- AI provider selection ---
    # "anthropic" (default) or "gemini". Switches which SDK/key the AIClient uses.
    ai_provider: str = field(default_factory=lambda: _get("AI_PROVIDER", "anthropic"))
    anthropic_api_key: Optional[str] = field(
        default_factory=lambda: _get("ANTHROPIC_API_KEY")
    )
    gemini_api_key: Optional[str] = field(
        default_factory=lambda: _get("GEMINI_API_KEY")
    )
    # Left as None when AI_MODEL_ID is unset; resolved per-provider in __post_init__.
    ai_model_id: Optional[str] = field(default_factory=lambda: _get("AI_MODEL_ID"))
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

    # --- Interview / onboarding transcription (task II) ---
    # Supabase table that stores interview calls + full transcript + status.
    interview_calls_table: str = field(
        default_factory=lambda: _get("INTERVIEW_CALLS_TABLE", "interview_calls")
    )
    # Optional Supabase table holding the call links (if not using a CSV/Notion).
    interview_links_table: Optional[str] = field(
        default_factory=lambda: _get("INTERVIEW_LINKS_TABLE")
    )
    interview_default_role: str = field(
        default_factory=lambda: _get("INTERVIEW_DEFAULT_ROLE", "бухгалтер")
    )

    # Armenia is UTC+4 (no DST). Used for "today" boundaries and scheduling notes.
    timezone_offset_hours: int = 4

    def __post_init__(self) -> None:
        # Normalise the provider and fill the model default for that provider.
        self.ai_provider = (self.ai_provider or "anthropic").strip().lower()
        if not self.ai_model_id:
            self.ai_model_id = _DEFAULT_MODELS.get(
                self.ai_provider, _DEFAULT_MODELS["anthropic"]
            )

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
    def has_gemini(self) -> bool:
        return bool(self.gemini_api_key)

    @property
    def has_ai(self) -> bool:
        """True when the selected provider has a usable key."""
        return self.has_gemini if self.ai_provider == "gemini" else self.has_anthropic

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

    def require_gemini(self) -> None:
        if not self.has_gemini:
            raise RuntimeError(
                "Gemini is not configured. Set GEMINI_API_KEY in your "
                "environment / .env file."
            )

    def require_ai(self) -> None:
        """Validate the key for whichever provider is selected."""
        if self.ai_provider == "gemini":
            self.require_gemini()
        else:
            self.require_anthropic()

    def require_telegram(self) -> None:
        if not self.has_telegram:
            raise RuntimeError(
                "Telegram is not configured. Set TELEGRAM_BOT_TOKEN and "
                "TELEGRAM_MANAGEMENT_CHAT_ID in your environment / .env file."
            )


def load_config() -> Config:
    """Build a :class:`Config` from the current environment."""
    return Config()
