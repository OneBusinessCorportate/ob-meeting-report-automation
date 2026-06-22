#!/usr/bin/env python3
"""CLI: safe, read-only debugging of the Timeless meetings listing.

Use this when ``scripts/ingest_meeting.py`` logs
``Timeless returned 0 meeting(s)`` even though meetings clearly exist in the
Timeless UI. It answers, without ever printing the token:

  * whether ``TIMELESS_API_TOKEN`` is set (and its length only);
  * the exact base URL / path / auth scheme being used;
  * the exact URL called and the HTTP status returned;
  * the raw top-level response keys, the detected list key, and the count;
  * whether pagination markers are present;
  * whether the configured date params (start_date/end_date) actually work, or
    whether a different shape (from/to, created_after/before, since/until, ...)
    is what returns meetings;
  * whether the status filter is what's hiding the meetings;
  * and, if auth is rejected, which auth scheme the API accepts.

It performs ONLY GET requests (no writes) and prints a JSON report plus a
plain-language diagnosis. Nothing here logs or returns the secret token.

Examples:
    python scripts/debug_timeless_api.py --start-date 2026-05-26 --end-date 2026-06-09
    python scripts/debug_timeless_api.py --days-back 14
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_pipeline.config import load_config  # noqa: E402
from meeting_pipeline.timeless_client import TimelessClient  # noqa: E402
from meeting_pipeline.utils import get_logger  # noqa: E402

log = get_logger("scripts.debug_timeless_api")


def _parse_iso(value: str) -> date:
    return date.fromisoformat(value)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Safely debug the Timeless meetings listing (never prints the token)."
    )
    parser.add_argument("--start-date", help="Range start (YYYY-MM-DD).")
    parser.add_argument("--end-date", help="Range end (YYYY-MM-DD).")
    parser.add_argument(
        "--days-back",
        type=int,
        default=14,
        help="If --start-date is omitted, look back this many days from --end-date "
        "(or today). Default: 14.",
    )
    args = parser.parse_args()

    today = date.today()
    try:
        end_date = _parse_iso(args.end_date) if args.end_date else today
        if args.start_date:
            start_date = _parse_iso(args.start_date)
        else:
            start_date = end_date - timedelta(days=max(0, args.days_back))
    except ValueError as exc:
        log.error("Invalid date: %s (expected YYYY-MM-DD).", exc)
        return 2

    if start_date > end_date:
        log.error("--start-date (%s) is after --end-date (%s).", start_date, end_date)
        return 2

    config = load_config()
    client = TimelessClient(config)

    # Token presence is reported in the JSON below — never the value itself.
    if not client.is_configured:
        log.error(
            "Timeless is not configured: TIMELESS_API_TOKEN is %s and base url is %r. "
            "Set the token (and TIMELESS_API_BASE_URL if not using the default) and "
            "re-run.",
            "set" if config.timeless_api_token else "MISSING",
            client.base_url or "",
        )

    log.info(
        "Debugging Timeless listing at %s (auth: %s) for %s..%s — GET only, no writes.",
        client.base_url,
        client.auth_scheme,
        start_date.isoformat(),
        end_date.isoformat(),
    )

    report = client.diagnose_listing(start_date, end_date)
    print(json.dumps(report, ensure_ascii=False, indent=2, default=str))

    code = report.get("result_code")
    diagnosis = report.get("diagnosis") or ""
    # Codes that represent a clear, non-blocking answer (working, or an actionable
    # env fix). Everything else is a real blocker surfaced loudly for Render logs.
    ok_codes = {
        "ok_in_range",
        "no_meetings_in_range",
        "wrong_param",
        "status_filter",
        "auth_wrong_scheme",
    }
    if code in ok_codes:
        log.info(diagnosis)
        return 0
    log.warning(diagnosis)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
