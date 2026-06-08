#!/usr/bin/env python3
"""CLI: live Timeless API connectivity + endpoint-discovery check.

Uses ``TIMELESS_API_TOKEN`` to call the configured endpoints and reports which
paths return HTTP 200 and the top-level JSON keys of each response — so the real
Timeless API shape can be confirmed without guessing. The token is never printed.

If the discovered endpoints differ from the defaults, set the corrected values
in the environment (no code change needed):

    TIMELESS_MEETINGS_PATH=...
    TIMELESS_TRANSCRIPT_PATH_TEMPLATES=meetings/{id}/transcript,...
    TIMELESS_AUTH_SCHEME=bearer|x-api-key|token

Examples:
    python scripts/check_timeless.py
    python scripts/check_timeless.py --meeting-id <known_id>
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_pipeline.config import load_config  # noqa: E402
from meeting_pipeline.timeless_client import TimelessClient  # noqa: E402
from meeting_pipeline.utils import get_logger  # noqa: E402

log = get_logger("scripts.check_timeless")


def main() -> int:
    parser = argparse.ArgumentParser(description="Probe the live Timeless API.")
    parser.add_argument(
        "--meeting-id",
        help="A known meeting id to test transcript endpoints against.",
    )
    args = parser.parse_args()

    config = load_config()
    client = TimelessClient(config)
    if not client.is_configured:
        log.error(
            "Timeless is not configured. Set TIMELESS_API_TOKEN (and optionally "
            "TIMELESS_API_BASE_URL) before running this check."
        )
        return 2

    log.info("Probing Timeless API at %s (auth: %s) ...", client.base_url, client.auth_scheme)
    report = client.probe(sample_meeting_id=args.meeting_id)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    if report.get("ok"):
        log.info("SUCCESS: a working transcript endpoint was found.")
        return 0
    log.warning(
        "No working full-transcript endpoint confirmed. Review the 'attempts' "
        "above and set TIMELESS_MEETINGS_PATH / TIMELESS_TRANSCRIPT_PATH_TEMPLATES "
        "/ TIMELESS_AUTH_SCHEME to match the real API."
    )
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
