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

from datetime import date, timedelta
from typing import Any, Dict, Optional

from .config import Config
from .report_render import (
    meeting_time_range,
    render_analytics_message,
    render_armsoft_message,
    render_telegram_report,
)
from .supabase_repo import SupabaseRepo
from .telegram_client import TelegramClient
from .utils import get_logger, parse_date

log = get_logger("meeting_pipeline.deliver")

# Sent on each intermediate check (checks 1–3) when no report is found yet.
MISSING_REPORT_MESSAGE = "Запись/отчёт за сегодня не найден."
MISSING_REPORT_PROMPT = "Бот будет проверять наличие отчёта каждые 30 минут и отправит сообщение, как только найдёт его."
MISSING_REPORT_MENTIONS = "@saakyans_21 @emilyaavanesyan"

# Sent once on the final check (check 4 / --final-check) when all attempts failed.
NO_CALLS_MESSAGE = "📭 Сегодня звонков не было."

# mtg_delivery_log kinds:
# - "no_report_check:{n}" — idempotency key for the n-th intermediate notice.
# - NOTICE_SEND_KIND     — idempotency key for the final "no calls" notice.
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
) -> tuple[Optional[str], Optional[str]]:
    """Re-render a stored analysis with the current rigid template.

    Returns ``(report_md, analytics_md)``: the main report (no analytics) and
    the standalone analytics message sent right after.  Either can be None.
    Returns ``(None, None)`` when the analysis predates the structured extras
    or rendering fails — the caller falls back to the stored
    ``telegram_report_md``.  Delivery must never break because of a template
    problem.
    """
    try:
        extras = (report.get("ai_metadata") or {}).get("report_extras") or {}
        # Without the structured extras there is nothing to fill the template
        # with (only legacy free-form text) — keep the stored message.
        if not (extras.get("effectiveness") or extras.get("participant_breakdown")):
            return None, None

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
        actual_start = meeting.get("actual_start")
        try:
            prior_stats = repo.get_prior_meeting_stats(actual_start) if actual_start else []
        except Exception as exc:  # prior stats are best-effort
            log.warning("Could not load prior meeting stats for rendering: %s", exc)
            prior_stats = []
        report_md = render_telegram_report(
            data,
            meeting_date=meeting_date,
            time_range=meeting_time_range(meeting, config.timezone_offset_hours),
            team_roster=team_roster,
            prior_stats=prior_stats,
            include_analytics=False,
        )
        analytics_md = render_analytics_message(
            data,
            meeting_date=meeting_date,
            team_roster=team_roster,
            prior_stats=prior_stats,
        )
        return report_md, (analytics_md or None)
    except Exception as exc:
        log.exception("Re-rendering stored report failed; using stored text: %s", exc)
        return None, None


def deliver_today(
    config: Config,
    *,
    date_str: Optional[str] = None,
    force: bool = False,
    force_notice: bool = False,
    final_check: bool = False,
    check_num: int = 1,
    repo: Optional[SupabaseRepo] = None,
    telegram: Optional[TelegramClient] = None,
) -> Dict[str, Any]:
    """Deliver today's current L2 report to Telegram.

    Automatic runs (the cron) send each report at most once; ``force=True``
    (CLI: ``--force``) re-sends deliberately from a manual trigger.

    When no report exists the behaviour depends on which check this is:

    * Checks 1–3 (``final_check=False``): send "Запись/отчёт за сегодня не
      найден… будем проверять каждые 30 минут" and return
      ``status="no_report_waiting"`` so the caller sleeps and retries.
      Each check has its own idempotency key so the same message is not
      sent twice within a single check window.

    * Check 4 / afternoon cron (``final_check=True``): send the definitive
      "📭 Сегодня звонков не было." notice (once per day) and return
      ``status="report_not_found"``.

    ``force_notice=True`` (CLI: ``--force-notice``) re-sends the final
    "no calls" notice even if it was already sent today — useful for testing.
    """
    repo = repo or SupabaseRepo(config)
    telegram = telegram or TelegramClient(config)
    on_date: date = parse_date(date_str, config.timezone_offset_hours)

    report = repo.get_today_current_report(on_date)

    if not report or not (report.get("telegram_report_md") or "").strip():
        if final_check or force_notice:
            # ── Final check: send "📭 Сегодня звонков не было." ──────────────
            if force_notice:
                repo.release_daily_send(on_date, NOTICE_SEND_KIND)
            if not repo.claim_daily_send(on_date, NOTICE_SEND_KIND):
                log.info(
                    "'No calls' notice for %s already sent — skipping.", on_date
                )
                return {
                    "ok": False,
                    "delivered": False,
                    "status": "notice_already_sent",
                    "telegram": None,
                }
            log.warning(
                "No report after all checks for %s — sending 'no calls' notice.",
                on_date,
            )
            notice = f"{NO_CALLS_MESSAGE}\n\nДата: {on_date.isoformat()}"
            result = telegram.send_message(notice, parse_mode=None)
            if not result.ok:
                repo.release_daily_send(on_date, NOTICE_SEND_KIND)
            return {
                "ok": False,
                "delivered": result.ok,
                "status": "report_not_found",
                "telegram": result,
            }

        # ── Intermediate check: "Запись не найден, будем проверять…" ─────────
        check_kind = f"no_report_check:{check_num}"
        if not repo.claim_daily_send(on_date, check_kind):
            log.info(
                "Check %d notice for %s already sent — skipping.", check_num, on_date
            )
            return {
                "ok": False,
                "delivered": False,
                "status": "no_report_waiting",
                "telegram": None,
            }
        log.warning(
            "No report for %s (check %d) — sending interim notification.",
            on_date, check_num,
        )
        # Plain text so the "_" in usernames isn't parsed as Markdown italic.
        notice = (
            f"{MISSING_REPORT_MESSAGE}\n\n"
            f"Дата: {on_date.isoformat()}\n\n"
            f"{MISSING_REPORT_PROMPT}\n\n"
            f"{MISSING_REPORT_MENTIONS}"
        )
        result = telegram.send_message(notice, parse_mode=None)
        if not result.ok:
            repo.release_daily_send(on_date, check_kind)
        return {
            "ok": False,
            "delivered": result.ok,
            "status": "no_report_waiting",
            "telegram": result,
        }

    delivery = (report.get("ai_metadata") or {}).get("delivery") or {}
    report_send_kind = f"report:{report['id']}"
    already_sent = delivery.get("delivered") or not repo.claim_daily_send(
        on_date, report_send_kind
    )
    if already_sent and not force:
        log.info(
            "Report %s was already delivered today — not sending again "
            "(use --force to re-send).",
            report["id"],
        )
        return {
            "ok": True,
            "delivered": True,
            "status": "already_delivered",
            "analysis_id": report["id"],
            "telegram": None,
        }
    if already_sent:
        log.info("Re-sending already-delivered report %s (--force).", report["id"])

    md, analytics_md = _render_with_current_template(config, repo, report)
    if md:
        log.info("Report %s re-rendered with the current template.", report["id"])
    else:
        md = report["telegram_report_md"]
        analytics_md = None
    result = telegram.send_message(md, parse_mode="Markdown")
    if not result.ok:
        # Free the slot so the next run can retry the delivery.
        repo.release_daily_send(on_date, report_send_kind)

    # Analytics goes out as a separate message right after the main report.
    # Best-effort: a failure here is logged but does not fail the whole delivery.
    analytics_result = None
    if result.ok and analytics_md:
        analytics_result = telegram.send_message(analytics_md, parse_mode="Markdown")
        if analytics_result.ok:
            log.info("Sent analytics follow-up for report %s.", report["id"])
        else:
            log.error(
                "Analytics follow-up failed for report %s: %s",
                report["id"],
                analytics_result.error,
            )

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
        "analytics_sent": bool(analytics_result and analytics_result.ok),
        "telegram": result,
    }


ARMSOFT_SEND_KIND_PREFIX = "armsoft"


def deliver_armsoft_today(
    config: Config,
    *,
    date_str: Optional[str] = None,
    force: bool = False,
    repo: Optional[SupabaseRepo] = None,
    telegram: Optional[TelegramClient] = None,
) -> Dict[str, Any]:
    """Deliver the evening ArmSoft/TaxService cross-check message.

    Runs at 18:00 Armenia (14:00 UTC) so bookkeepers can review before the
    next morning's planning meeting. Idempotent: sends at most once per day.
    """
    repo = repo or SupabaseRepo(config)
    telegram = telegram or TelegramClient(config)
    on_date: date = parse_date(date_str, config.timezone_offset_hours)

    send_kind = f"{ARMSOFT_SEND_KIND_PREFIX}:{on_date.isoformat()}"
    if not force and not repo.claim_daily_send(on_date, send_kind):
        log.info("ArmSoft report for %s already sent today.", on_date)
        return {"ok": True, "delivered": True, "status": "already_delivered", "telegram": None}

    # Fetch today's activity: get_armsoft_portfolio_activity(before_date) returns
    # data for the day BEFORE before_date, so we pass tomorrow to get today's data.
    activity_date = on_date.isoformat()
    before_date = (on_date + timedelta(days=1)).isoformat()
    try:
        armsoft_activity = repo.get_armsoft_portfolio_activity(before_date)
    except Exception as exc:
        log.warning("Could not load Armsoft activity: %s", exc)
        armsoft_activity = []
    try:
        taxservice_activity = repo.get_taxservice_activity(before_date)
    except Exception as exc:
        log.warning("Could not load TaxService activity: %s", exc)
        taxservice_activity = []

    if not armsoft_activity and not taxservice_activity:
        log.info("No ArmSoft/TaxService data for %s — skipping evening message.", on_date)
        repo.release_daily_send(on_date, send_kind)
        return {"ok": True, "delivered": False, "status": "no_data", "telegram": None}

    md = render_armsoft_message(
        meeting_date=activity_date,
        armsoft_activity=armsoft_activity,
        taxservice_activity=taxservice_activity,
    )
    if not md.strip():
        log.info("ArmSoft message rendered empty for %s — skipping.", on_date)
        repo.release_daily_send(on_date, send_kind)
        return {"ok": True, "delivered": False, "status": "no_data", "telegram": None}

    result = telegram.send_message(md, parse_mode="Markdown")
    if not result.ok:
        repo.release_daily_send(on_date, send_kind)

    if result.ok:
        log.info("Delivered ArmSoft evening report for %s.", on_date)
    else:
        log.error("ArmSoft delivery failed for %s: %s", on_date, result.error)

    return {
        "ok": result.ok,
        "delivered": result.ok,
        "status": "delivered" if result.ok else "delivery_failed",
        "telegram": result,
    }
