"""Shared helpers: logging, dates, JSON parsing and Telegram message splitting."""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any, List, Optional

# Telegram hard limit per message is 4096 chars; leave a little headroom.
TELEGRAM_MAX_LEN = 4000

_LOG_CONFIGURED = False


def get_logger(name: str = "meeting_pipeline") -> logging.Logger:
    """Return a configured logger with a clear, consistent format."""
    global _LOG_CONFIGURED
    if not _LOG_CONFIGURED:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        _LOG_CONFIGURED = True
    return logging.getLogger(name)


# --- Dates --------------------------------------------------------------------

def armenia_tz(offset_hours: int = 4) -> timezone:
    return timezone(timedelta(hours=offset_hours))


def today_in_armenia(offset_hours: int = 4) -> date:
    """The current local date in Armenia (UTC+4)."""
    return datetime.now(armenia_tz(offset_hours)).date()


def parse_date(value: Optional[str], offset_hours: int = 4) -> date:
    """Parse a ``YYYY-MM-DD`` string, defaulting to today (Armenia time)."""
    if not value:
        return today_in_armenia(offset_hours)
    return datetime.strptime(value.strip(), "%Y-%m-%d").date()


def day_bounds_utc(d: date, offset_hours: int = 4):
    """Return the [start, end) UTC timestamps covering a local Armenian day.

    Used to query meetings whose ``actual_start`` falls on a given local date.
    """
    tz = armenia_tz(offset_hours)
    start_local = datetime(d.year, d.month, d.day, tzinfo=tz)
    end_local = start_local + timedelta(days=1)
    return (
        start_local.astimezone(timezone.utc),
        end_local.astimezone(timezone.utc),
    )


# --- JSON ---------------------------------------------------------------------

def extract_json(text: str) -> Optional[dict]:
    """Best-effort extraction of a JSON object from a model response.

    Handles plain JSON, ```json fenced blocks, and leading/trailing prose.
    Returns ``None`` if no valid JSON object can be parsed.
    """
    if not text:
        return None

    candidate = text.strip()

    # Strip markdown code fences if present.
    fence = re.search(r"```(?:json)?\s*(.*?)```", candidate, re.DOTALL)
    if fence:
        candidate = fence.group(1).strip()

    # Fast path.
    try:
        parsed = json.loads(candidate)
        return parsed if isinstance(parsed, dict) else None
    except json.JSONDecodeError:
        pass

    # Fallback: grab the outermost {...} span.
    start = candidate.find("{")
    end = candidate.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(candidate[start : end + 1])
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


# --- Telegram message splitting ----------------------------------------------

def split_telegram_message(text: str, max_len: int = TELEGRAM_MAX_LEN) -> List[str]:
    """Split a long markdown message into Telegram-sized chunks.

    Splits on paragraph and line boundaries where possible so markdown stays
    readable; only hard-splits a single oversized line as a last resort.
    """
    text = text or ""
    if len(text) <= max_len:
        return [text] if text else [""]

    chunks: List[str] = []
    current = ""

    def flush() -> None:
        nonlocal current
        if current:
            chunks.append(current.rstrip("\n"))
            current = ""

    for block in text.split("\n"):
        # +1 accounts for the newline we re-add.
        if len(current) + len(block) + 1 <= max_len:
            current += block + "\n"
            continue

        flush()

        if len(block) <= max_len:
            current = block + "\n"
        else:
            # A single line longer than the limit: hard-split it.
            for i in range(0, len(block), max_len):
                piece = block[i : i + max_len]
                if len(piece) == max_len:
                    chunks.append(piece)
                else:
                    current = piece + "\n"

    flush()
    return chunks or [""]


def to_telegram_markdown(text: str) -> str:
    """Adapt GitHub-flavoured markdown to Telegram's legacy ``Markdown`` mode.

    The stored ``telegram_report_md`` uses GFM ``**bold**``, but Telegram's
    legacy parser expects single-asterisk ``*bold*`` — double asterisks render
    incorrectly or trigger an HTTP 400. This converts ``**x**`` -> ``*x*`` while
    leaving the rest of the message untouched. The canonical stored value is not
    modified; this runs only at send time.
    """
    if not text:
        return text
    return re.sub(r"\*\*(.+?)\*\*", r"*\1*", text)


def coalesce(value: Any, default: str = "Не указано") -> Any:
    """Return ``default`` for empty/None values (Russian 'Not specified')."""
    if value is None:
        return default
    if isinstance(value, str) and not value.strip():
        return default
    return value
