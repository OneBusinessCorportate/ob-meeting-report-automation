"""AI client that turns a full transcript into a structured L2 report.

Supports two providers, selected via ``AI_PROVIDER`` (``anthropic`` default, or
``gemini``). Both speak the same ``messages.create(...)`` contract — the Gemini
backend is adapted in :mod:`meeting_pipeline.gemini_client`.

The wrapper forces Russian output, grounded extraction (no invented facts) and
a strict JSON response. If the model returns invalid JSON we surface a clear
error so the caller can store a ``failed`` analysis (and, optionally, a safe
fallback markdown) instead of crashing.

Install requirement: ``anthropic>=0.39`` and/or ``google-genai>=1.0``
(see requirements.txt).
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from .config import Config
from .prompts import meeting_analysis_v1 as prompt_v1
from .utils import extract_json, get_logger

log = get_logger("meeting_pipeline.ai")

# Fields that map directly to mtg_analyses columns.
_COLUMN_FIELDS = (
    "summary",
    "topics",
    "action_items",
    "open_questions",
    "people_mentioned",
    "problems_risks",
    "sentiment",
    "meeting_mood",
    "late_start",
    "late_start_minutes",
    "mgmt_recommendations",
    "telegram_report_md",
)

# Extra grounded fields the schema requests but which have no dedicated column.
# They are preserved inside ``mtg_analyses.ai_metadata.report_extras``.
_EXTRA_FIELDS = (
    "effectiveness",
    "attention_points",
    "decisions",
    "praised",
    "criticized",
    "participant_breakdown",
    "manager_reactions",
    "followup_on_previous_tasks",
    "who_took_ownership",
    "talk_share",
)

# The report is only considered usable if at least these core fields are present.
_REQUIRED_FIELDS = ("summary", "telegram_report_md")

_VALID_SENTIMENTS = {"positive", "neutral", "negative", "mixed"}


def build_provider_client(config: Config) -> Any:
    """Create the raw provider client (Anthropic or Gemini adapter).

    Both expose the same ``messages.create(...)`` contract, so callers can use
    one code path regardless of ``AI_PROVIDER``. Shared by the meeting report
    and the interview analysis pipelines.
    """
    if (config.ai_provider or "anthropic").strip().lower() == "gemini":
        config.require_gemini()
        from .gemini_client import GeminiClient  # imported lazily

        return GeminiClient(api_key=config.gemini_api_key)
    config.require_anthropic()
    from anthropic import Anthropic  # imported lazily

    return Anthropic(api_key=config.anthropic_api_key)


@dataclass
class AnalysisResult:
    ok: bool
    report: Dict[str, Any] = field(default_factory=dict)
    extras: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    model_id: Optional[str] = None
    prompt_version: Optional[str] = None
    processing_time_ms: Optional[int] = None
    ai_metadata: Dict[str, Any] = field(default_factory=dict)
    raw_text: Optional[str] = None


class AIClient:
    def __init__(self, config: Config, client: Any = None):
        self.config = config
        self.model_id = config.ai_model_id
        self.prompt_version = config.ai_prompt_version or prompt_v1.PROMPT_VERSION
        self.provider = config.ai_provider
        if client is not None:
            self.client = client
        else:
            self.client = build_provider_client(config)

    def analyze(
        self,
        transcript_text: str,
        *,
        title: Optional[str] = None,
        meeting_date: Optional[str] = None,
        language: Optional[str] = None,
        time_range: Optional[str] = None,
        participants: Optional[List[str]] = None,
        team_roster: Optional[List[Any]] = None,
        prior_context: Optional[List[Any]] = None,
        max_tokens: Optional[int] = None,
    ) -> AnalysisResult:
        """Generate a structured L2 report from the FULL transcript."""
        # Generous output budget: the structured report can be large, and a too
        # small cap truncates the JSON mid-object -> "invalid JSON" failures.
        if max_tokens is None:
            max_tokens = getattr(self.config, "ai_max_output_tokens", 16384)
        if not transcript_text or not transcript_text.strip():
            return AnalysisResult(
                ok=False,
                error="Empty transcript: cannot generate analysis.",
                model_id=self.model_id,
                prompt_version=self.prompt_version,
            )

        user_prompt = prompt_v1.build_user_prompt(
            transcript_text,
            title=title,
            meeting_date=meeting_date,
            language=language,
            time_range=time_range,
            participants=participants,
            team_roster=team_roster,
            prior_context=prior_context,
        )

        # Try once; if the model returns empty/unparseable JSON (most often a
        # response truncated at max_tokens), retry once with a doubled budget so
        # a long meeting self-heals instead of failing the whole report.
        start = time.monotonic()
        text = ""
        usage: Dict[str, Any] = {}
        parsed = None
        budgets = [max_tokens, min(max_tokens * 2, 32768)]
        for attempt, budget in enumerate(budgets):
            try:
                response = self.client.messages.create(
                    model=self.model_id,
                    max_tokens=budget,
                    system=prompt_v1.SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": user_prompt}],
                )
            except Exception as exc:
                log.error("%s API call failed: %s", self.provider, exc)
                return AnalysisResult(
                    ok=False,
                    error=f"AI request failed: {exc}",
                    model_id=self.model_id,
                    prompt_version=self.prompt_version,
                    processing_time_ms=int((time.monotonic() - start) * 1000),
                )

            text = self._response_text(response)
            usage = self._usage(response)
            parsed = extract_json(text)
            if parsed is not None:
                break
            log.error(
                "Model returned non-JSON / invalid JSON (attempt %d, max_tokens=%d, "
                "raw_len=%d, tail=%r).",
                attempt + 1,
                budget,
                len(text or ""),
                (text or "")[-160:],
            )
            if attempt + 1 < len(budgets) and budget < budgets[-1]:
                log.warning(
                    "Retrying analysis with a larger output budget (%d).", budgets[-1]
                )

        elapsed_ms = int((time.monotonic() - start) * 1000)

        if parsed is None:
            return AnalysisResult(
                ok=False,
                error="AI returned invalid JSON; could not parse structured report.",
                model_id=self.model_id,
                prompt_version=self.prompt_version,
                processing_time_ms=elapsed_ms,
                ai_metadata={"usage": usage},
                raw_text=text,
            )

        missing = [f for f in _REQUIRED_FIELDS if not parsed.get(f)]
        if missing:
            log.error("AI report missing required field(s): %s", ", ".join(missing))
            return AnalysisResult(
                ok=False,
                error=f"AI report missing required field(s): {', '.join(missing)}",
                model_id=self.model_id,
                prompt_version=self.prompt_version,
                processing_time_ms=elapsed_ms,
                ai_metadata={"usage": usage},
                raw_text=text,
            )

        report = self._normalize(parsed)
        extras = {k: parsed.get(k) for k in _EXTRA_FIELDS if k in parsed}
        return AnalysisResult(
            ok=True,
            report=report,
            extras=extras,
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            processing_time_ms=elapsed_ms,
            ai_metadata={"usage": usage},
            raw_text=text,
        )

    # --- helpers --------------------------------------------------------------
    @staticmethod
    def _response_text(response: Any) -> str:
        try:
            parts = []
            for block in response.content:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
            return "\n".join(parts)
        except Exception:
            return str(getattr(response, "content", "") or "")

    @staticmethod
    def _usage(response: Any) -> Dict[str, Any]:
        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        return {
            "input_tokens": getattr(usage, "input_tokens", None),
            "output_tokens": getattr(usage, "output_tokens", None),
        }

    @staticmethod
    def _normalize(parsed: Dict[str, Any]) -> Dict[str, Any]:
        """Keep only column-backed fields and sanitise the sentiment enum value."""
        report = {k: parsed.get(k) for k in _COLUMN_FIELDS if k in parsed}
        sentiment = report.get("sentiment")
        if sentiment is not None and sentiment not in _VALID_SENTIMENTS:
            report["sentiment"] = "neutral"
        return report
