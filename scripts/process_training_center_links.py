#!/usr/bin/env python3
"""CLI (mode: interview_transcript_processing).

Process interview / onboarding call links from the «Обучающий центр ОВ» table:
    call link → check transcript availability → fetch FULL transcript → save →
    per-link status (saved / transcript_not_available / manual_action_required /
    failed). This mode does NOT produce an AI report or Telegram message.

Examples:
    python scripts/process_training_center_links.py --input ./data/training_center_links.csv
    python scripts/process_training_center_links.py --url https://app.timeless.day/meetings/abc123
    python scripts/process_training_center_links.py --input ./data/links.csv \
        --output ./data/interviews/status.csv --force
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from interview_pipeline.transcribe import transcribe_interviews  # noqa: E402
from meeting_pipeline.config import load_config  # noqa: E402
from meeting_pipeline.utils import get_logger  # noqa: E402

log = get_logger("scripts.process_training_center_links")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Process interview/onboarding call links (Обучающий центр ОВ)."
    )
    parser.add_argument(
        "--input",
        "--csv",
        dest="input",
        help="CSV of links exported from the «Обучающий центр ОВ» table.",
    )
    parser.add_argument(
        "--url",
        action="append",
        dest="urls",
        help="Interview call link (repeatable).",
    )
    parser.add_argument(
        "--output",
        help="Where to write the per-link status CSV "
        "(default: ./data/interviews/status_<timestamp>.csv).",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Re-process calls already marked saved.",
    )
    args = parser.parse_args()

    output = args.output
    if output is None and (args.input or args.urls):
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        output = f"./data/interviews/status_{stamp}.csv"

    config = load_config()
    result = transcribe_interviews(
        config,
        urls=args.urls,
        csv_path=args.input,
        force=args.force,
        output_path=output,
    )

    log.info(
        "Done: processed=%s saved=%s skipped=%s needs_attention=%s status_file=%s",
        result.get("processed"),
        result.get("saved"),
        result.get("skipped"),
        result.get("needs_attention"),
        result.get("status_file"),
    )
    printable = {k: v for k, v in result.items() if k != "results"}
    print(json.dumps(printable, ensure_ascii=False, indent=2, default=str))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
