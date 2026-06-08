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

from typing import Any, Dict, List, Optional

# Anthropic uses "assistant" for model turns; Gemini calls the same role "model".
_ROLE_MAP = {"assistant": "model", "user": "user", "system": "user"}


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

    def __init__(self, genai_client: Any) -> None:
        self._client = genai_client

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

        response = self._client.models.generate_content(
            model=model,
            contents=self._to_contents(messages),
            config=config,
        )

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

    def __init__(self, api_key: Optional[str] = None, *, genai_client: Any = None) -> None:
        if genai_client is None:
            from google import genai  # imported lazily; optional dependency

            genai_client = genai.Client(api_key=api_key)
        self.messages = _Messages(genai_client)
