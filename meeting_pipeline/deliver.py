"""Step 3 — Deliver.

Find the current completed L2 report for today, render its Telegram text from
the stored structured fields using the CURRENT rigid template, and send it to
the management Telegram chat (splitting long messages). If no report exists,
send a clear notification instead of failing silently.

Rendering at delivery time means template improvements apply to already-stored
analyses too — no AI re-run needed. Analyses stored without the structured
extras fall back to their stored ``telegram_report_md``.
"""
from __future__ import annotations

from datetime import date
from typing import Any, Dict, Optional

from .config import Config
from .report_render import meeting_time_range, render_telegram_report
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

# mtg_delivery_log kinds: one (date, kind) row per Telegram send. The cron may
# fire many times a day (manual re-runs, a mis-set schedule on Render), but
# each message must reach the chat at most once per day.
NOTICE_SEND_KIND = "missing_report_notice"

# Analysis-row columns that feed the template alongside ai_metadata.report_extras.
_RENDER_COLUMNS = (
    "action_items",
    "open_questions",
    "problems_risks",
    "late_start",
    "late_start_minutes",
)


def _render_with_current_template(
    config: Config, repo: SupabaseRepo, report: Dict[str, Any]
) -> Optional[str]:
    """Re-render a stored analysis with the current rigid template.

    Returns None (caller falls back to the stored ``telegram_report_md``) when
    the analysis predates the structured extras or rendering fails — delivery
    must never break because of a template problem.
    """
    try:
        extras = (report.get("ai_metadata") or {}).get("report_extras") or {}
        # Without the structured extras there is nothing to fill the template
        # with (only legacy free-form text) — keep the stored message.
        if not (extras.get("effectiveness") or extras.get("participant_breakdown")):
            return None

        data = {k: report.get(k) for k in _RENDER_COLUMNS if report.get(k) is not None}
        data.update(extras)

        team_roster = getattr(config, "meeting_team_roster", []) or []
        if not team_roster:
            try:
                team_roster = repo.get_team_roster()
            except Exception as exc:  # roster is best-effort
                log.warning("Could not load team roster for rendering: %s", exc)
                team_roster = []

        meeting = report.get("_meeting") or {}
        meeting_date = (meeting.get("actual_start") or "")[:10] or None
        return render_telegram_report(
            data,
            meeting_date=meeting_date,
            time_range=meeting_time_range(meeting, config.timezone_offset_hours),
            team_roster=team_roster,
        )
    except Exception as exc:
        log.exception("Re-rendering stored report failed; using stored text: %s", exc)
        return None


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
        if not repo.claim_daily_send(on_date, NOTICE_SEND_KIND):
            log.info(
                "Missing-report notice for %s was already sent today — "
                "not sending again.",
                on_date,
            )
            return {
                "ok": False,
                "delivered": False,
                "status": "notice_already_sent",
                "telegram": None,
            }
        log.warning("No current L2 report for %s — sending notification.", on_date)
        notice = (
            f"{MISSING_REPORT_MESSAGE}\n\n"
            f"Дата: {on_date.isoformat()}\n\n"
            f"{MISSING_REPORT_MENTIONS}"
        )
        # Plain text (no Markdown) so the "_" in @saakyans_21 isn't treated as
        # italic markup; @mentions still notify the users.
        result = telegram.send_message(notice, parse_mode=None)
        if not result.ok:
            # Free the slot so the next run can retry the notice.
            repo.release_daily_send(on_date, NOTICE_SEND_KIND)
        return {
            "ok": False,
            "delivered": result.ok,
            "status": "report_not_found",
            "telegram": result,
        }

    delivery = (report.get("ai_metadata") or {}).get("delivery") or {}
    report_send_kind = f"report:{report['id']}"
    if delivery.get("delivered") or not repo.claim_daily_send(
        on_date, report_send_kind
    ):
        log.info(
            "Report %s was already delivered today — not sending again.",
            report["id"],
        )
        return {
            "ok": True,
            "delivered": True,
            "status": "already_delivered",
            "analysis_id": report["id"],
            "telegram": None,
        }

    md = _render_with_current_template(config, repo, report)
    if md:
        log.info("Report %s re-rendered with the current template.", report["id"])
    else:
        md = report["telegram_report_md"]
    result = telegram.send_message(md, parse_mode="Markdown")
    if not result.ok:
        # Free the slot so the next run can retry the delivery.
        repo.release_daily_send(on_date, report_send_kind)

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
