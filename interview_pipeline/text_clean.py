"""Transcript normalization — turn a raw transcript into a clean analysis input.

The cleaned text is stored SEPARATELY from the raw text (raw stays verbatim so
we can always re-process). Cleaning is deliberately conservative: it never
drops spoken content, it only tidies whitespace and obvious noise so the model
sees a readable transcript.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

# Leading "[00:01:23]" / "00:01" / "(12:34)" style timestamps at line start.
_TS_PREFIX = re.compile(r"^\s*[\[\(]?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?[\]\)]?\s*[-–—]?\s*")
_MULTISPACE = re.compile(r"[ \t]{2,}")
_MULTINEWLINE = re.compile(r"\n{3,}")


def clean_transcript(text: str) -> str:
    """Normalize a transcript for analysis without losing spoken content."""
    if not text:
        return ""
    lines: List[str] = []
    for raw_line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = _TS_PREFIX.sub("", raw_line)
        line = _MULTISPACE.sub(" ", line).rstrip()
        lines.append(line)
    cleaned = "\n".join(lines).strip()
    cleaned = _MULTINEWLINE.sub("\n\n", cleaned)
    return cleaned


def transcript_stats(raw_text: str, segments: Optional[List[dict]] = None) -> Dict[str, int]:
    """Compute lightweight stats stored alongside the transcript."""
    text = raw_text or ""
    speakers = set()
    for seg in segments or []:
        if isinstance(seg, dict):
            sp = seg.get("speaker") or seg.get("speaker_name") or seg.get("speaker_id")
            if sp not in (None, ""):
                speakers.add(str(sp))
    return {
        "char_count": len(text),
        "word_count": len(text.split()),
        "segment_count": len(segments or []),
        "speaker_count": len(speakers),
    }


def normalize_segments(segments: Optional[List[dict]]) -> List[Dict[str, Any]]:
    """Map raw transcript segments to the intv_transcript_segments shape.

    Tolerant of the different field names Timeless / other sources use. Returns
    an ordered list of {idx, speaker, start_ms, end_ms, text}. Segments without
    text are skipped.
    """
    out: List[Dict[str, Any]] = []
    for i, seg in enumerate(segments or []):
        if not isinstance(seg, dict):
            continue
        text = (seg.get("text") or seg.get("content") or "").strip()
        if not text:
            continue
        speaker = (
            seg.get("speaker")
            or seg.get("speaker_name")
            or seg.get("speaker_id")
        )
        start_ms, end_ms = _resolve_times(seg)
        out.append(
            {
                "idx": len(out),
                "speaker": str(speaker) if speaker not in (None, "") else None,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "text": text,
            }
        )
    return out


def _resolve_times(seg: Dict[str, Any]) -> Tuple[Optional[int], Optional[int]]:
    """Best-effort extraction of start/end times in milliseconds."""
    def _ms(value: Any) -> Optional[int]:
        if value is None:
            return None
        try:
            num = float(value)
        except (TypeError, ValueError):
            return None
        # Heuristic: values < 10000 are most likely seconds, convert to ms.
        return int(num * 1000) if num < 10000 else int(num)

    start = seg.get("start_ms")
    end = seg.get("end_ms")
    if start is None:
        start = _ms(seg.get("start") if seg.get("start") is not None else seg.get("start_time"))
    if end is None:
        end = _ms(seg.get("end") if seg.get("end") is not None else seg.get("end_time"))
    return start, end
