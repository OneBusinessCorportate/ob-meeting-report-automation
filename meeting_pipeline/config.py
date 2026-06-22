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
    # Query-param names used when listing meetings by date range. The real
    # Timeless API param names are not documented to us, so they are overridable
    # from the environment — run `scripts/debug_timeless_api.py` to discover the
    # names that actually return meetings (e.g. "from"/"to", "created_after").
    timeless_start_param: str = field(
        default_factory=lambda: _get("TIMELESS_START_PARAM", "start_date")
    )
    timeless_end_param: str = field(
        default_factory=lambda: _get("TIMELESS_END_PARAM", "end_date")
    )
    # Value sent as the meeting status filter. Set to empty (TIMELESS_STATUS_FILTER=)
    # to omit the filter entirely — some workspaces/endpoints 200-but-return-0
    # when this value is wrong. Read raw so an explicit empty string is honoured.
    timeless_status_filter: str = field(
        default_factory=lambda: os.environ.get("TIMELESS_STATUS_FILTER", "completed").strip()
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
        default_factory=lambda: _get("AI_PROMPT_VERSION", "full_transcript_prompt_v2")
    )
    # Max output tokens for the structured L2 report. The report (full
    # participant breakdown + telegram_report_md) can be large; an 8192 cap
    # truncated the JSON on longer transcripts, producing "invalid JSON"
    # failures. Default generously and allow override per environment.
    ai_max_output_tokens: int = field(
        default_factory=lambda: int(_get("AI_MAX_OUTPUT_TOKENS", "16384"))
    )

    # --- Telegram ---
    telegram_bot_token: Optional[str] = field(
        default_factory=lambda: _get("TELEGRAM_BOT_TOKEN")
    )
    telegram_management_chat_id: Optional[str] = field(
        default_factory=lambda: _get("TELEGRAM_MANAGEMENT_CHAT_ID")
    )
    # Retry transient Telegram failures (timeouts, 429, 5xx) with exponential
    # backoff so a report is never lost after a single timeout.
    telegram_max_retries: int = field(
        default_factory=lambda: int(_get("TELEGRAM_MAX_RETRIES", "4"))
    )
    telegram_request_timeout: int = field(
        default_factory=lambda: int(_get("TELEGRAM_REQUEST_TIMEOUT", "30"))
    )

    # --- Meeting defaults ---
    default_source: str = field(
        default_factory=lambda: _get("MEETING_DEFAULT_SOURCE", "timeless")
    )
    default_language: str = field(
        default_factory=lambda: _get("MEETING_DEFAULT_LANGUAGE", "hy")
    )
    delivery_time: str = field(
        default_factory=lambda: _get("MEETING_DELIVERY_TIME", "11:30")
    )
    # How many days back the daily run also re-checks for pending/failed meetings,
    # so transient failures (e.g. AI quota) auto-recover on the next scheduled run
    # without any manual command. 0 = today only.
    analyze_lookback_days: int = field(
        default_factory=lambda: int(_get("MEETING_ANALYZE_LOOKBACK_DAYS", "0"))
    )
    # Known accounting-team roster, so the report can go through EVERY accountant
    # by name and flag those who said nothing as "не принимал(а) участия".
    # Format: comma-separated "Имя:роль" (role optional), e.g.
    #   MEETING_TEAM_ROSTER="Эмилия:руководитель,Анна:бухгалтер,Давид:бухгалтер"
    meeting_team_roster_raw: Optional[str] = field(
        default_factory=lambda: _get("MEETING_TEAM_ROSTER")
    )
    # Extra ASR fixes applied to transcripts before analysis, on top of the
    # built-in ones (see analyze.TRANSCRIPT_CORRECTIONS). Format:
    #   MEETING_TRANSCRIPT_CORRECTIONS="неправильно=>правильно,wrong2=>right2"
    transcript_corrections_raw: Optional[str] = field(
        default_factory=lambda: _get("MEETING_TRANSCRIPT_CORRECTIONS")
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

    # --- Interview ANALYSIS pipeline («Обучающий центр / анализ собеседований») ---
    # Source of the candidate/interview table. Auto-detected when unset:
    # google_api (service account) > csv_url (published CSV) > local file.
    interview_sheet_source: Optional[str] = field(
        default_factory=lambda: _get("INTERVIEW_SHEET_SOURCE")
    )
    interview_spreadsheet_id: Optional[str] = field(
        default_factory=lambda: _get("INTERVIEW_SPREADSHEET_ID")
    )
    # Comma-separated tab names to read (the «Бух» tab holds the interview links).
    interview_sheet_tabs: str = field(
        default_factory=lambda: _get("INTERVIEW_SHEET_TABS", "Бух")
    )
    interview_sheet_csv_url: Optional[str] = field(
        default_factory=lambda: _get("INTERVIEW_SHEET_CSV_URL")
    )
    interview_local_xlsx: Optional[str] = field(
        default_factory=lambda: _get("INTERVIEW_LOCAL_XLSX")
    )
    # Google service-account credentials (for Sheets / Docs / Drive). Either an
    # inline JSON blob or a path to the JSON key file.
    google_service_account_json: Optional[str] = field(
        default_factory=lambda: _get("GOOGLE_SERVICE_ACCOUNT_JSON")
    )
    google_service_account_file: Optional[str] = field(
        default_factory=lambda: _get("GOOGLE_SERVICE_ACCOUNT_FILE")
    )
    # Feature toggles for the analysis pipeline outputs.
    interview_analysis_enabled: bool = field(
        default_factory=lambda: _get("INTERVIEW_ANALYSIS_ENABLED", "true") == "true"
    )
    interview_telegram_enabled: bool = field(
        default_factory=lambda: _get("INTERVIEW_TELEGRAM_ENABLED", "false") == "true"
    )
    interview_sheet_writeback_enabled: bool = field(
        default_factory=lambda: _get("INTERVIEW_SHEET_WRITEBACK_ENABLED", "false")
        == "true"
    )
    # Optional dedicated chat for interview reports; falls back to the management chat.
    interview_telegram_chat_id: Optional[str] = field(
        default_factory=lambda: _get("INTERVIEW_TELEGRAM_CHAT_ID")
    )
    interview_analysis_prompt_version: str = field(
        default_factory=lambda: _get(
            "INTERVIEW_ANALYSIS_PROMPT_VERSION", "interview_analysis_v2_5theses"
        )
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

    @property
    def meeting_team_roster(self) -> list:
        """Parse ``MEETING_TEAM_ROSTER`` into ``[{"name":.., "role":..}]``.

        Accepts ``"Имя:роль"`` entries separated by commas (or newlines); the
        role part is optional. Returns ``[]`` when unset.
        """
        raw = self.meeting_team_roster_raw
        if not raw:
            return []
        roster = []
        for chunk in raw.replace("\n", ",").split(","):
            chunk = chunk.strip()
            if not chunk:
                continue
            name, _, role = chunk.partition(":")
            name = name.strip()
            if name:
                roster.append({"name": name, "role": role.strip()})
        return roster

    @property
    def transcript_corrections(self) -> dict:
        """Parse ``MEETING_TRANSCRIPT_CORRECTIONS`` into ``{wrong: right}``."""
        raw = self.transcript_corrections_raw
        if not raw:
            return {}
        corrections = {}
        for chunk in raw.replace("\n", ",").split(","):
            wrong, sep, right = chunk.partition("=>")
            if sep and wrong.strip():
                corrections[wrong.strip()] = right.strip()
        return corrections

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
