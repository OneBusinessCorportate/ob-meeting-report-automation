"""Deterministic Telegram report renderer (structure by script, values by AI).

Per leadership feedback (2026-06-11) the report TEXT is built here, in code,
with a fixed structure — the AI only fills in the values via the structured
JSON fields. Previously the model wrote ``telegram_report_md`` itself and kept
re-inventing the layout («просто это стал другой отчет»): blocks disappeared,
stray lines like «Настроение: продуктивное» appeared. Now the layout cannot
drift: every run produces the same sections in the same order.

The layout is the approved report with the requested fixes applied
(second leadership review, 2026-06-12):

- score line without emoji/verdict/«Что было на встрече» label — just
  «ОЦЕНКА ВСТРЕЧИ: N из 10» and the ✅/🟡/❌ checklist with «Руководитель»;
- «Кто сколько говорил» compact, with a blank line above so it stands out;
- no «Кто был» / «Не было» lines — attendance is visible from the per-person
  blocks, where non-participants come FIRST as one-liners
  («👤 Имя - не принимал(а) участия.»);
- mandatory «Отчёт за вчера / План на сегодня / Блокеры» per accountant:
  ❌ when not voiced, «–» when the person explicitly said there is nothing;
- manager remarks grouped («Общее» first): the name on its own line, every
  remark on its own numbered line below, no «поручила»;
- risks: severity icon BEFORE the number («🔴 1. …»), then one
  «Что решили: …» line (❌ when no next step was discussed) — no
  Ответственный/Срок/Как контролируем lines;
- tasks on control grouped by assignee (👤 Имя: 1. … Срок: ❌);
- «Открытые вопросы» at the end; «Обратить внимание» is stored but not shown.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

MISSING = "❌"  # value was not voiced on the meeting
NONE_DASH = "–"  # the person explicitly said there is nothing

# Canonical checklist labels (order matters and matches the prompt schema).
CRITERIA_LABELS = [
    "Все высказались",
    "Руководитель задавала вопросы",
    "Руководитель поставила задачи",
    "Руководитель поделилась новостями",
    "Руководитель кого-то похвалила",
    "Руководитель спросила про прошлые задачи",
]

_STATUS_ICONS = {"выполнено": "✅", "частично": "🟡", "не выполнено": "❌"}
_SEVERITY_ICONS = {"high": "🔴", "medium": "🟡", "low": "🟢"}

# Values meaning "the fact is absent from the transcript" (→ ❌).
_MISSING_WORDS = {"", "не указано", "не указан", "не указана", "❌", "none", "null"}
# Values meaning "the person explicitly said there is nothing" (→ –).
_NONE_WORDS = {"нет", "-", "–", "нет блокеров", "блокеров нет", "без блокеров", "ничего"}


# Placeholder surnames seen in mtg_participants ("Оля Бухгалтер", "Гор Менеджер").
_PLACEHOLDER_SURNAMES = {"бухгалтер", "менеджер", "руководитель"}


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _first_name(name: Any) -> str:
    cleaned = _clean(name)
    return cleaned.split()[0] if cleaned else ""


def _person(name: Any, roster_firsts: set) -> str:
    """Display name for a person: first name for team members, full otherwise.

    The model sometimes returns full names ("Эмилия Аванесян", "Оля Бухгалтер")
    even though the report shows first names only. Collective assignees
    ("Все бухгалтеры") and external names ("Гюльчоре Балаян") must NOT be
    clipped to their first word.
    """
    cleaned = _clean(name)
    if not cleaned:
        return ""
    parts = cleaned.split()
    if parts[0].lower() in roster_firsts:
        return parts[0]
    if len(parts) > 1 and all(p.lower() in _PLACEHOLDER_SURNAMES for p in parts[1:]):
        return parts[0]
    return cleaned


def _roster_firsts(team_roster: Optional[List[Dict[str, Any]]]) -> set:
    return {
        _first_name(r.get("name")).lower() for r in team_roster or [] if _first_name(r.get("name"))
    }


def _local_hhmm(timestamp: Optional[str], offset_hours: int) -> Optional[str]:
    if not timestamp:
        return None
    try:
        dt = datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is not None:
        dt = dt.astimezone(timezone.utc).replace(tzinfo=None) + timedelta(hours=offset_hours)
    return dt.strftime("%H:%M")


def meeting_time_range(meeting: Dict[str, Any], offset_hours: int) -> Optional[str]:
    """``HH:MM–HH:MM`` (local time) from a meeting row, or None when unknown."""
    start = _local_hhmm(meeting.get("actual_start"), offset_hours)
    end = _local_hhmm(meeting.get("actual_end"), offset_hours)
    if start and end:
        return f"{start}–{end}"
    return start


def _classify(value: Any) -> Tuple[str, List[str]]:
    """Classify a field value: ``missing`` (→ ❌), ``none`` (→ –) or items."""
    if value is None:
        return "missing", []
    if isinstance(value, (list, tuple)):
        items = [_clean(x) for x in value if _clean(x)]
        items = [x for x in items if x.lower() not in _MISSING_WORDS]
        if not items:
            return "missing", []
        if all(x.lower() in _NONE_WORDS for x in items):
            return "none", []
        return "items", [x for x in items if x.lower() not in _NONE_WORDS]
    text = _clean(value)
    if text.lower() in _MISSING_WORDS:
        return "missing", []
    if text.lower() in _NONE_WORDS:
        return "none", []
    return "items", [text]


def _field_lines(label: str, value: Any) -> List[str]:
    """One mandatory report line: ``label: value | ❌ | –`` (lists multi-line)."""
    kind, items = _classify(value)
    if kind == "missing":
        return [f"{label}: {MISSING}"]
    if kind == "none":
        return [f"{label}: {NONE_DASH}"]
    if len(items) == 1:
        return [f"{label}: {items[0]}"]
    return [f"{label}:"] + [f"  – {item}" for item in items]


def _optional_field_lines(label: str, value: Any) -> List[str]:
    """Same as :func:`_field_lines` but omitted entirely when there is no value."""
    kind, items = _classify(value)
    if kind != "items":
        return []
    if len(items) == 1:
        return [f"{label}: {items[0]}"]
    return [f"{label}:"] + [f"  – {item}" for item in items]


def _value_or_cross(value: Any) -> str:
    kind, items = _classify(value)
    return "; ".join(items) if kind == "items" else MISSING


def _find_manager(team_roster: Optional[List[Dict[str, Any]]]) -> str:
    for entry in team_roster or []:
        if "руковод" in _clean(entry.get("role")).lower():
            return _first_name(entry.get("name"))
    return "Эмилия"


def render_telegram_report(
    data: Dict[str, Any],
    *,
    meeting_date: Optional[str] = None,
    time_range: Optional[str] = None,
    team_roster: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build the full Telegram report text from the structured analysis JSON.

    ``data`` is the merged report (column fields + extras: ``effectiveness``,
    ``participant_breakdown``, ``manager_reactions``, ``talk_share``, …).
    Sections with no data are dropped whole; the structure never changes.
    """
    manager = _find_manager(team_roster)
    roster_firsts = _roster_firsts(team_roster)
    lines: List[str] = ["📋 Планёрка бухгалтерии"]
    if meeting_date and time_range:
        lines.append(f"{meeting_date}, {time_range}")
    elif meeting_date:
        lines.append(meeting_date)
    lines.append("")

    lines += _score_block(data)
    lines += _attendance_block(data)
    lines += _accountant_blocks(data, team_roster, manager)
    lines += _manager_block(data, manager, roster_firsts)
    lines += _risks_block(data)
    lines += _tasks_block(data, roster_firsts)
    lines += _open_questions_block(data)

    return _finalize(lines)


# --- sections -----------------------------------------------------------------
def _score_block(data: Dict[str, Any]) -> List[str]:
    eff = data.get("effectiveness") or {}
    criteria = eff.get("criteria") or []
    out: List[str] = []
    score = eff.get("score")
    if score:
        max_score = eff.get("max_score") or 10
        # No verdict sentence: the checklist below explains the score itself.
        out.append(f"ОЦЕНКА ВСТРЕЧИ: {int(score)} из {int(max_score)}")
    if criteria:
        # Older stored analyses have 5 checklist items (no followup criterion);
        # render only what was actually assessed instead of inventing a ❌.
        for i, label in enumerate(CRITERIA_LABELS[: len(criteria)]):
            status = _clean((criteria[i] or {}).get("status"))
            icon = _STATUS_ICONS.get(status.lower(), MISSING)
            out.append(f"  {icon} {label}")
    talk = data.get("talk_share") or {}
    manager_pct, accountants_pct = talk.get("manager_pct"), talk.get("accountants_pct")
    if manager_pct or accountants_pct:
        if out:
            out.append("")  # blank line above so the talk-share line stands out
        out.append(
            f"Кто сколько говорил: {manager_pct or 0}% руководитель, "
            f"{accountants_pct or 0}% бухгалтеры"
        )
    if out:
        out.append("")
    return out


def _attendance_block(data: Dict[str, Any]) -> List[str]:
    # «Кто был» / «Не было» lines were dropped per feedback: the per-person
    # blocks below already show attendance (non-participants listed first).
    out: List[str] = []
    if data.get("late_start"):
        minutes = data.get("late_start_minutes") or 0
        out.append(f"🕐 Опоздание: {int(minutes)} мин" if minutes else "🕐 Опоздание")
        out.append("")
    return out


def _accountant_blocks(
    data: Dict[str, Any],
    team_roster: Optional[List[Dict[str, Any]]],
    manager: str,
) -> List[str]:
    entries = [
        e for e in data.get("participant_breakdown") or []
        if _first_name(e.get("name")) and _first_name(e.get("name")).lower() != manager.lower()
    ]
    if not entries:
        return []

    # Non-participants go FIRST so silence is impossible to miss; within each
    # group the order is stable: roster order, then anyone extra as encountered.
    roster_order = {
        _first_name(r.get("name")).lower(): i for i, r in enumerate(team_roster or [])
    }
    entries.sort(
        key=lambda e: (
            bool(e.get("participated")),
            roster_order.get(_first_name(e.get("name")).lower(), len(roster_order)),
        )
    )

    out: List[str] = []
    absent_done = False
    for entry in entries:
        name = _first_name(entry.get("name"))
        if not entry.get("participated"):
            # One line per absentee, listed together at the top of the section.
            out.append(f"👤 {name} - не принимал(а) участия.")
            continue
        if out and not absent_done:
            out.append("")  # close the absentee group with one blank line
        absent_done = True
        out.append(f"👤 {name}")
        out += _field_lines("Отчёт за вчера", entry.get("yesterday"))
        out += _field_lines("План на сегодня", entry.get("today_plan"))
        out += _field_lines("Блокеры", entry.get("blockers"))
        out += _optional_field_lines("Нужна помощь", entry.get("needs_help"))
        out += _optional_field_lines("Вопрос руководителю", entry.get("question_to_manager"))
        out.append("")
    if out and out[-1] != "":
        out.append("")
    return out


def _manager_block(data: Dict[str, Any], manager: str, roster_firsts: set) -> List[str]:
    reactions = data.get("manager_reactions") or []
    grouped: Dict[str, List[str]] = {}
    order: List[str] = []
    for reaction in reactions:
        text = _clean(reaction.get("text"))
        if not text:
            continue
        to_whom = _clean(reaction.get("to_whom")) or "Общее"
        key = "Общее" if to_whom.lower() in {"общее", "все", "всем", "команда", "команде"} \
            else _person(to_whom, roster_firsts)
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(text)
    if not grouped:
        return []

    out = [f"🧭 ЧТО СКАЗАЛА РУКОВОДИТЕЛЬ ({manager.upper()})"]
    # «Общее» first, then per person: the name on its own line, every remark
    # on its own line below (numbered when there is more than one).
    for key in (["Общее"] if "Общее" in grouped else []) + [k for k in order if k != "Общее"]:
        out.append(f"{key}:")
        texts = grouped[key]
        if len(texts) == 1:
            out.append(texts[0])
        else:
            out.extend(f"{i}. {text}" for i, text in enumerate(texts, start=1))
    out.append("")
    return out


def _risks_block(data: Dict[str, Any]) -> List[str]:
    risks = [r for r in data.get("problems_risks") or [] if _clean(r.get("text"))]
    if not risks:
        return []
    # Critical situations first: high -> medium -> low (stable within a level).
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    risks.sort(key=lambda r: severity_rank.get(_clean(r.get("severity")).lower(), 1))
    out = ["⚠️ РИСКИ И СИТУАЦИИ", ""]
    for i, risk in enumerate(risks, start=1):
        severity = _clean(risk.get("severity")).lower()
        icon = _SEVERITY_ICONS.get(severity, "🟡")
        out.append(f"{icon} {i}. {_clean(risk.get('text'))}")
        # ❌ when no decision / next step was discussed on the meeting.
        out.append(f"Что решили: {_value_or_cross(risk.get('decision'))}")
        out.append("")
    return out


def _tasks_block(data: Dict[str, Any], roster_firsts: set) -> List[str]:
    items = [t for t in data.get("action_items") or [] if _clean(t.get("text"))]
    if not items:
        return []
    grouped: Dict[str, List[Dict[str, Any]]] = {}
    order: List[str] = []
    for item in items:
        kind, names = _classify(item.get("assignee"))
        assignee = _person(names[0], roster_firsts) if kind == "items" else "Не указано"
        if assignee not in grouped:
            grouped[assignee] = []
            order.append(assignee)
        grouped[assignee].append(item)

    out = ["✅ ЗАДАЧИ НА КОНТРОЛЕ"]
    for assignee in order:
        out.append(f"👤 {assignee}:")
        for i, task in enumerate(grouped[assignee], start=1):
            text = _clean(task.get("text")).rstrip(".")
            out.append(f"{i}. {text}. Срок: {_value_or_cross(task.get('deadline'))}")
        out.append("")
    return out


def _open_questions_block(data: Dict[str, Any]) -> List[str]:
    questions = [_clean(q) for q in data.get("open_questions") or [] if _clean(q)]
    questions = [q for q in questions if q.lower() not in _MISSING_WORDS]
    if not questions:
        return []
    return ["❓ ОТКРЫТЫЕ ВОПРОСЫ"] + [f"  – {q}" for q in questions] + [""]


def _finalize(lines: List[str]) -> str:
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip() + "\n"
