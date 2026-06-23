"""Build and (optionally) deliver a short Russian interview report to Telegram."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from meeting_pipeline.config import Config
from meeting_pipeline.telegram_client import TelegramClient
from meeting_pipeline.utils import get_logger
from .analyze import InterviewAnalysisResult

log = get_logger("interview_pipeline.report")

_REC_LABEL = {
    "hire": "✅ Нанимать (hire)",
    "maybe": "🟡 Спорно (maybe)",
    "reject": "⛔ Отказать (reject)",
    "training": "🎓 На дообучение (training)",
}


def _bullets(items: List[str], empty: str = "—") -> str:
    items = [i for i in (items or []) if i]
    if not items:
        return empty
    return "\n".join(f"- {i}" for i in items)


def _s(v: Optional[int]) -> str:
    return f"{v}/10" if v is not None else "—"


def _theses_block(a: InterviewAnalysisResult) -> List[str]:
    """One line per thesis: «N. Title — score/10», with the comment if present."""
    if not a.theses:
        return []
    out = ["Оценка по 5 тезисам"]
    for t in a.theses:
        line = f"{t.get('id')}. {t.get('title')} — {_s(t.get('score'))}"
        comment = (t.get("comment") or "").strip()
        if comment:
            line += f": {comment}"
        out.append(line)
    return out


def build_interview_report_md(
    candidate: Dict[str, Any], analysis: InterviewAnalysisResult
) -> str:
    name = candidate.get("full_name") or "Кандидат"
    role = candidate.get("role") or "—"
    rec = _REC_LABEL.get(analysis.recommendation or "", "Не определено")
    parts: List[str] = [
        f"🧑‍💼 Собеседование: {name}",
        f"👔 Роль: {role}",
        f"📌 Рекомендация: {rec}",
        f"📊 Итоговая оценка: {_s(analysis.overall_score)}",
        "",
        "Кратко",
        analysis.summary or "—",
    ]
    theses = _theses_block(analysis)
    if theses:
        parts += [""] + theses
    if analysis.candidate_strengths:
        parts += ["", "Сильные стороны", _bullets(analysis.candidate_strengths)]
    if analysis.candidate_weaknesses:
        parts += ["", "Слабые стороны / риски", _bullets(analysis.candidate_weaknesses)]
    if analysis.red_flags:
        parts += ["", "🚩 Red flags", _bullets(analysis.red_flags)]
    if analysis.next_steps:
        parts += ["", "Следующие шаги", _bullets(analysis.next_steps)]
    if analysis.reasoning:
        parts += ["", "Обоснование решения", analysis.reasoning]
    return "\n".join(parts)


def deliver_interview_report(
    config: Config,
    candidate: Dict[str, Any],
    analysis: InterviewAnalysisResult,
    *,
    client: Optional[TelegramClient] = None,
) -> Dict[str, Any]:
    """Send the report to Telegram. Returns a structured result; never raises."""
    chat_id = config.interview_telegram_chat_id or config.telegram_management_chat_id
    tg = client or TelegramClient(config, chat_id=chat_id)
    if not tg.is_configured:
        return {"ok": False, "error": "Telegram not configured."}
    md = build_interview_report_md(candidate, analysis)
    result = tg.send_message(md)
    return {"ok": result.ok, "error": result.error, "parts_sent": result.parts_sent}
