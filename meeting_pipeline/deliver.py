"""Step 3 — Deliver.

Find the current completed L2 report for today, take its ``telegram_report_md``
and send it to the management Telegram chat (splitting long messages). If no
report exists, send a clear notification instead of failing silently.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from .config import Config
from .supabase_repo import SupabaseRepo
from .telegram_client import TelegramClient
from .utils import get_logger, parse_date

log = get_logger("meeting_pipeline.deliver")

MISSING_REPORT_MESSAGE = "Запись/отчёт за сегодня не найден."

# People to @-tag when no recording/report is found, so they notice and can act
# (Lilit and Emiliya). Telegram detects @mentions in plain text regardless of
# parse mode, so the notice is sent as plain text to avoid the "_" in a username
# being mis-parsed as Markdown.
MISSING_REPORT_MENTIONS = "@saakyans_21 @emilyaavanesyan"


def deliver_today(
    config: Config,
    *,
    date_str: Optional[str] = None,
    repo: Optional[SupabaseRepo] = None,
    telegram: Optional[TelegramClient] = None,
) -> Dict[str, Any]:
    """Deliver today's current L2 report to Telegram."""
    repo = repo or SupabaseRepo(config)
    telegram = telegram or TelegramClient(config)
    on_date: date = parse_date(date_str, config.timezone_offset_hours)

    report = repo.get_today_current_report(on_date)

    if not report or not (report.get("telegram_report_md") or "").strip():
        log.warning("No current L2 report for %s — sending notification.", on_date)
        notice = (
            f"{MISSING_REPORT_MESSAGE}\n\n"
            f"Дата: {on_date.isoformat()}\n\n"
            f"{MISSING_REPORT_MENTIONS}"
        )
        # Plain text (no Markdown) so the "_" in @saakyans_21 isn't treated as
        # italic markup; @mentions still notify the users.
        result = telegram.send_message(notice, parse_mode=None)
        return {
            "ok": False,
            "delivered": result.ok,
            "status": "report_not_found",
            "telegram": result,
        }

    md = report["telegram_report_md"]
    result = telegram.send_message(md, parse_mode="Markdown")

    detail = (
        f"delivered {result.parts_sent} part(s)" if result.ok else result.error
    )
    try:
        repo.mark_delivery_status(
            report["id"], delivered=result.ok, detail=detail
        )
    except Exception as exc:  # delivery status is best-effort
        log.warning("Could not record delivery status: %s", exc)

    if result.ok:
        log.info(
            "Delivered L2 report %s for %s (%d part(s)).",
            report["id"],
            on_date,
            result.parts_sent,
        )
    else:
        log.error("Telegram delivery failed: %s", result.error)

    return {
        "ok": result.ok,
        "delivered": result.ok,
        "status": "delivered" if result.ok else "delivery_failed",
        "analysis_id": report["id"],
        "telegram": result,
    }
