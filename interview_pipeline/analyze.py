"""Interview analysis — turn a cleaned transcript into a structured assessment.

Reuses the shared provider client (Anthropic / Gemini) and the strict-JSON
interview prompt. Output is normalised (scores clamped 0–10, recommendation
mapped to hire|maybe|reject|training) so a slightly-off model response still
stores cleanly instead of crashing. Returns ok=False with a clear error when
the transcript is empty or the model output cannot be parsed.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from meeting_pipeline.ai_client import build_provider_client
from meeting_pipeline.config import Config
from meeting_pipeline.utils import extract_json, get_logger
from .prompts import interview_analysis_v1 as prompt_v1

log = get_logger("interview_pipeline.analyze")

_VALID_RECOMMENDATIONS = {"hire", "maybe", "reject", "training"}
# Map common synonyms / languages to the canonical recommendation values.
_RECOMMENDATION_ALIASES = {
    "hire": "hire", "yes": "hire", "нанять": "hire", "принять": "hire", "offer": "hire",
    "maybe": "maybe", "возможно": "maybe", "спорно": "maybe", "consider": "maybe",
    "hold": "maybe", "под вопросом": "maybe",
    "reject": "reject", "no": "reject", "отказ": "reject", "отказать": "reject",
    "decline": "reject",
    "training": "training", "train": "training", "обучение": "training",
    "дообучение": "training", "needs more training": "training", "needs training": "training",
}


@dataclass
class InterviewAnalysisResult:
    ok: bool
    transcript_language: Optional[str] = None
    summary: Optional[str] = None
    summary_original: Optional[str] = None
    candidate_strengths: List[str] = field(default_factory=list)
    candidate_weaknesses: List[str] = field(default_factory=list)
    red_flags: List[str] = field(default_factory=list)
    next_steps: List[str] = field(default_factory=list)
    communication_score: Optional[int] = None
    professional_score: Optional[int] = None
    motivation_score: Optional[int] = None
    overall_score: Optional[int] = None
    recommendation: Optional[str] = None
    reasoning: Optional[str] = None
    model_id: Optional[str] = None
    prompt_version: Optional[str] = None
    processing_time_ms: Optional[int] = None
    ai_metadata: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None


def _as_str_list(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, list):
        out = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                # tolerate [{"text": "..."}] shapes
                text = item.get("text") or item.get("point") or ""
                if text:
                    out.append(str(text))
        return out
    return [str(value)]


def _clamp_score(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        num = round(float(value))
    except (TypeError, ValueError):
        return None
    return max(0, min(10, int(num)))


def _normalize_recommendation(value: Any) -> Optional[str]:
    if not value:
        return None
    key = str(value).strip().lower()
    if key in _VALID_RECOMMENDATIONS:
        return key
    if key in _RECOMMENDATION_ALIASES:
        return _RECOMMENDATION_ALIASES[key]
    # Try a contained keyword (e.g. "recommend: hire").
    for alias, canonical in _RECOMMENDATION_ALIASES.items():
        if alias in key:
            return canonical
    return None


class InterviewAnalyzer:
    def __init__(self, config: Config, client: Any = None):
        self.config = config
        self.model_id = config.ai_model_id
        self.prompt_version = config.interview_analysis_prompt_version or prompt_v1.PROMPT_VERSION
        self.client = client if client is not None else build_provider_client(config)

    def analyze(
        self,
        transcript_text: str,
        *,
        candidate_name: Optional[str] = None,
        role: Optional[str] = None,
        interview_type: Optional[str] = None,
        language: Optional[str] = None,
        max_tokens: int = 4096,
    ) -> InterviewAnalysisResult:
        if not transcript_text or not transcript_text.strip():
            return InterviewAnalysisResult(
                ok=False,
                error="Empty transcript: cannot analyze.",
                model_id=self.model_id,
                prompt_version=self.prompt_version,
            )

        user_prompt = prompt_v1.build_user_prompt(
            transcript_text,
            candidate_name=candidate_name,
            role=role,
            interview_type=interview_type,
            language=language,
        )
        start = time.monotonic()
        try:
            response = self.client.messages.create(
                model=self.model_id,
                max_tokens=max_tokens,
                system=prompt_v1.SYSTEM_PROMPT,
                messages=[{"role": "user", "content": user_prompt}],
            )
        except Exception as exc:
            log.error("Interview AI call failed: %s", exc)
            return InterviewAnalysisResult(
                ok=False,
                error=f"AI request failed: {exc}",
                model_id=self.model_id,
                prompt_version=self.prompt_version,
                processing_time_ms=int((time.monotonic() - start) * 1000),
            )

        elapsed_ms = int((time.monotonic() - start) * 1000)
        text = self._response_text(response)
        usage = self._usage(response)
        parsed = extract_json(text)
        if parsed is None:
            return InterviewAnalysisResult(
                ok=False,
                error="AI returned invalid JSON; could not parse the analysis.",
                model_id=self.model_id,
                prompt_version=self.prompt_version,
                processing_time_ms=elapsed_ms,
                ai_metadata={"usage": usage, "raw_text": text},
            )

        summary = (parsed.get("summary") or "").strip()
        if not summary:
            return InterviewAnalysisResult(
                ok=False,
                error="AI analysis missing required field 'summary'.",
                model_id=self.model_id,
                prompt_version=self.prompt_version,
                processing_time_ms=elapsed_ms,
                ai_metadata={"usage": usage, "raw_text": text},
            )

        recommendation = _normalize_recommendation(parsed.get("recommendation"))
        return InterviewAnalysisResult(
            ok=True,
            transcript_language=parsed.get("transcript_language") or language,
            summary=summary,
            summary_original=(parsed.get("summary_original") or "").strip() or None,
            candidate_strengths=_as_str_list(parsed.get("candidate_strengths")),
            candidate_weaknesses=_as_str_list(parsed.get("candidate_weaknesses")),
            red_flags=_as_str_list(parsed.get("red_flags")),
            next_steps=_as_str_list(parsed.get("next_steps")),
            communication_score=_clamp_score(parsed.get("communication_score")),
            professional_score=_clamp_score(parsed.get("professional_score")),
            motivation_score=_clamp_score(parsed.get("motivation_score")),
            overall_score=_clamp_score(parsed.get("overall_score")),
            recommendation=recommendation,
            reasoning=(parsed.get("reasoning") or "").strip() or None,
            model_id=self.model_id,
            prompt_version=self.prompt_version,
            processing_time_ms=elapsed_ms,
            ai_metadata={
                "usage": usage,
                "raw_recommendation": parsed.get("recommendation"),
            },
        )

    @staticmethod
    def _response_text(response: Any) -> str:
        try:
            parts = []
            for block in response.content:
                txt = getattr(block, "text", None)
                if txt:
                    parts.append(txt)
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
