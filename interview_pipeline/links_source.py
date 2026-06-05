"""Where interview call links come from.

The «Обучающий центр ОВ» table lives in Notion (outside this system's direct
access), so links are provided through a pluggable source, in priority order:

1. Explicit URLs passed on the CLI (`--url ...`).
2. A CSV exported from the Notion table (`--csv ...`).
3. An optional Supabase table (`INTERVIEW_LINKS_TABLE`) if the team mirrors the
   links into Supabase.

CSV columns (header row, case-insensitive; only ``call_url`` is required):
    call_url, candidate_name, role, call_type, source_call_id, transcript_file
"""
from __future__ import annotations

import csv
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from meeting_pipeline.config import Config
from meeting_pipeline.utils import get_logger

log = get_logger("interview_pipeline.links")


@dataclass
class InterviewLink:
    call_url: str
    candidate_name: Optional[str] = None
    role: Optional[str] = None
    call_type: str = "interview"
    source_call_id: Optional[str] = None
    transcript_file: Optional[str] = None  # local full-transcript fallback file
    metadata: Dict[str, Any] = field(default_factory=dict)


def _row_to_link(row: Dict[str, Any], default_role: str) -> Optional[InterviewLink]:
    # Normalise keys to lowercase, stripped.
    norm = {
        (k or "").strip().lower(): (v.strip() if isinstance(v, str) else v)
        for k, v in row.items()
    }
    url = norm.get("call_url") or norm.get("url") or norm.get("link")
    if not url:
        return None
    return InterviewLink(
        call_url=url,
        candidate_name=norm.get("candidate_name") or norm.get("candidate") or None,
        role=norm.get("role") or default_role,
        call_type=norm.get("call_type") or "interview",
        source_call_id=norm.get("source_call_id") or None,
        transcript_file=norm.get("transcript_file") or None,
    )


def from_csv(csv_path: str, default_role: str = "бухгалтер") -> List[InterviewLink]:
    path = Path(csv_path)
    if not path.exists():
        raise FileNotFoundError(f"Links CSV not found: {path}")
    links: List[InterviewLink] = []
    with path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh)
        for row in reader:
            link = _row_to_link(row, default_role)
            if link:
                links.append(link)
    log.info("Loaded %d interview link(s) from CSV %s", len(links), path)
    return links


def from_urls(urls: List[str], default_role: str = "бухгалтер") -> List[InterviewLink]:
    links = [InterviewLink(call_url=u.strip(), role=default_role) for u in urls if u and u.strip()]
    log.info("Loaded %d interview link(s) from CLI", len(links))
    return links


def from_supabase(
    client: Any, table: str, default_role: str = "бухгалтер"
) -> List[InterviewLink]:
    """Read links from a Supabase table (expects at least a ``call_url`` column)."""
    rows = client.table(table).select("*").execute().data or []
    links = [link for link in (_row_to_link(r, default_role) for r in rows) if link]
    log.info("Loaded %d interview link(s) from Supabase table %s", len(links), table)
    return links


def load_links(
    config: Config,
    *,
    urls: Optional[List[str]] = None,
    csv_path: Optional[str] = None,
    client: Any = None,
) -> List[InterviewLink]:
    """Resolve interview links from the highest-priority available source."""
    if urls:
        return from_urls(urls, config.interview_default_role)
    if csv_path:
        return from_csv(csv_path, config.interview_default_role)
    if config.interview_links_table and client is not None:
        return from_supabase(
            client, config.interview_links_table, config.interview_default_role
        )
    return []
