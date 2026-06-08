"""Gemini adapter exposing the small slice of the Anthropic client interface
that :class:`meeting_pipeline.ai_client.AIClient` relies on.

The rest of the pipeline only ever calls ``client.messages.create(...)`` and
reads ``response.content[].text`` / ``response.usage.{input,output}_tokens``.
This wrapper maps that contract onto the ``google-genai`` SDK so a single env
var (``AI_PROVIDER=gemini``) can switch the backing model with no other code
changes.

Install requirement: ``google-genai>=1.0`` (see requirements.txt).
"""
from __future__ import annotations

import time
from typing import Any, Callable, Dict, List, Optional

from .utils import get_logger

log = get_logger("meeting_pipeline.gemini")

# Anthropic uses "assistant" for model turns; Gemini calls the same role "model".
_ROLE_MAP = {"assistant": "model", "user": "user", "system": "user"}

# Substrings that mark a transient, retryable error from the Gemini API.
_RETRYABLE_MARKERS = (
    "429",
    "500",
    "502",
    "503",
    "504",
    "RESOURCE_EXHAUSTED",
    "UNAVAILABLE",
    "INTERNAL",
    "DEADLINE_EXCEEDED",
)


class _Block:
    """Mimics an Anthropic content block (only ``.text`` is consumed)."""

    def __init__(self, text: str) -> None:
        self.text = text


class _Usage:
    """Mimics ``response.usage`` with the two token counts we record."""

    def __init__(self, input_tokens: Optional[int], output_tokens: Optional[int]) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens


class _Response:
    """Mimics an Anthropic ``Message`` response."""

    def __init__(self, text: str, usage: _Usage) -> None:
        self.content = [_Block(text)]
        self.usage = usage


class _Messages:
    """Implements the ``messages.create(...)`` call shape on top of Gemini."""

    def __init__(
        self,
        genai_client: Any,
        *,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._client = genai_client
        self._max_retries = max(0, max_retries)
        self._sleep = sleep

    @staticmethod
    def _to_contents(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        contents: List[Dict[str, Any]] = []
        for msg in messages:
            role = _ROLE_MAP.get(msg.get("role", "user"), "user")
            content = msg.get("content", "")
            if isinstance(content, str):
                parts = [{"text": content}]
            else:  # already a list of blocks/parts
                parts = [
                    {"text": getattr(b, "text", None) or b.get("text", "")}
                    if not isinstance(b, str)
                    else {"text": b}
                    for b in content
                ]
            contents.append({"role": role, "parts": parts})
        return contents

    def create(
        self,
        *,
        model: str,
        max_tokens: int,
        system: Optional[str] = None,
        messages: List[Dict[str, Any]],
    ) -> _Response:
        # google-genai accepts a plain dict for ``config`` (no need to import the
        # ``types`` module), which keeps this wrapper trivially unit-testable.
        config: Dict[str, Any] = {
            "max_output_tokens": max_tokens,
            # The prompt demands strict JSON; ask Gemini to honour that natively.
            "response_mime_type": "application/json",
        }
        if system:
            config["system_instruction"] = system
        # Gemini 2.5 models "think" by consuming output tokens before answering,
        # which can starve/truncate the JSON. Disable thinking for this strict
        # extraction task so the full budget goes to the structured report.
        if "2.5" in model:
            config["thinking_config"] = {"thinking_budget": 0}

        contents = self._to_contents(messages)
        for attempt in range(self._max_retries + 1):
            try:
                response = self._client.models.generate_content(
                    model=model, contents=contents, config=config
                )
                break
            except Exception as exc:  # transient 429/5xx -> retry with backoff
                message = str(exc)
                retryable = any(m in message for m in _RETRYABLE_MARKERS)
                # A hard "limit: 0" quota (model not on this tier) is not worth retrying.
                if "limit: 0" in message or not retryable or attempt >= self._max_retries:
                    raise
                delay = 2.0 ** attempt
                log.warning(
                    "Gemini call failed (attempt %d/%d), retrying in %.0fs: %s",
                    attempt + 1,
                    self._max_retries + 1,
                    delay,
                    message.split(".")[0],
                )
                self._sleep(delay)

        text = getattr(response, "text", None) or ""
        meta = getattr(response, "usage_metadata", None)
        usage = _Usage(
            getattr(meta, "prompt_token_count", None) if meta else None,
            getattr(meta, "candidates_token_count", None) if meta else None,
        )
        return _Response(text, usage)


class GeminiClient:
    """Drop-in stand-in for ``anthropic.Anthropic`` backed by ``google-genai``.

    Pass ``genai_client`` to inject a fake in tests; otherwise a real
    ``google.genai.Client`` is created lazily from ``api_key``.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        genai_client: Any = None,
        max_retries: int = 4,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if genai_client is None:
            from google import genai  # imported lazily; optional dependency

            genai_client = genai.Client(api_key=api_key)
        self.messages = _Messages(genai_client, max_retries=max_retries, sleep=sleep)
