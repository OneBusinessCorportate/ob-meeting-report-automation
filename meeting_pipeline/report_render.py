"""Deterministic Telegram report renderer (structure by script, values by AI).

Per leadership feedback (2026-06-11) the report TEXT is built here, in code,
with a fixed structure — the AI only fills in the values via the structured
JSON fields. Previously the model wrote ``telegram_report_md`` itself and kept
re-inventing the layout («просто это стал другой отчет»): blocks disappeared,
stray lines like «Настроение: продуктивное» appeared. Now the layout cannot
drift: every run produces the same sections in the same order.

The layout is the approved report with the requested fixes applied:

- checklist «Что было на встрече» with «Руководитель» and ✅/🟡/❌ icons,
  including «спросила про прошлые задачи»;
- compact «Кто сколько говорил» line in the top score block;
- a blank line before «Не было» so absentees stand out;
- mandatory «Отчёт за вчера / План на сегодня / Блокеры» per accountant:
  ❌ when not voiced, «–» when the person explicitly said there is nothing;
- manager remarks grouped: one «Общее» line, numbered items per person,
  no «поручила»;
- risks numbered «1.» (no word «Ситуация»), separated by blank lines, with
  «Риск: 🔴/🟡/🟢» instead of «Степень риска: высокая»;
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


def _numbered_inline(texts: List[str]) -> str:
    if len(texts) == 1:
        return texts[0]
    return " ".join(f"{i}. {t}" for i, t in enumerate(texts, start=1))


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
        lines.append(f"📅 {meeting_date}, {time_range}")
    elif meeting_date:
        lines.append(f"📅 {meeting_date}")
    lines.append("")

    lines += _score_block(data)
    lines += _attendance_block(data, team_roster, manager)
    lines += _accountant_blocks(data, team_roster, manager)
    lines += _manager_block(data, manager, roster_firsts)
    lines += _risks_block(data, roster_firsts)
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
        out.append(f"📊 ОЦЕНКА ВСТРЕЧИ: {int(score)} из {int(max_score)}")
        verdict = _clean(eff.get("verdict"))
        if verdict:
            out.append(verdict)
    if criteria:
        out.append("Что было на встрече:")
        # Older stored analyses have 5 checklist items (no followup criterion);
        # render only what was actually assessed instead of inventing a ❌.
        for i, label in enumerate(CRITERIA_LABELS[: len(criteria)]):
            status = _clean((criteria[i] or {}).get("status"))
            icon = _STATUS_ICONS.get(status.lower(), MISSING)
            out.append(f"  {icon} {label}")
    talk = data.get("talk_share") or {}
    manager_pct, accountants_pct = talk.get("manager_pct"), talk.get("accountants_pct")
    if manager_pct or accountants_pct:
        out.append(
            f"Кто сколько говорил: {manager_pct or 0}% руководитель, "
            f"{accountants_pct or 0}% бухгалтеры"
        )
    if out:
        out.append("")
    return out


def _participation(data: Dict[str, Any]) -> Dict[str, bool]:
    """first-name (lower) → participated, from participant_breakdown."""
    result: Dict[str, bool] = {}
    for entry in data.get("participant_breakdown") or []:
        name = _first_name(entry.get("name"))
        if name:
            result[name.lower()] = bool(entry.get("participated"))
    return result


def _attendance_block(
    data: Dict[str, Any],
    team_roster: Optional[List[Dict[str, Any]]],
    manager: str,
) -> List[str]:
    participated = _participation(data)
    roster_names = [_first_name(r.get("name")) for r in team_roster or []]
    roster_names = [n for n in roster_names if n]

    if roster_names:
        # The manager runs the meeting: assume present unless explicitly absent.
        manager_present = participated.get(manager.lower(), True)
        accountants = [n for n in roster_names if n.lower() != manager.lower()]
        present = [n for n in accountants if participated.get(n.lower(), False)]
        absent = [n for n in accountants if not participated.get(n.lower(), False)]
        if not manager_present:
            absent.insert(0, manager)
    else:
        manager_present = participated.get(manager.lower(), True)
        present = [
            _first_name(p.get("name"))
            for p in data.get("participant_breakdown") or []
            if p.get("participated") and _first_name(p.get("name")).lower() != manager.lower()
        ]
        absent = [
            _first_name(p.get("name"))
            for p in data.get("participant_breakdown") or []
            if not p.get("participated")
        ]

    out: List[str] = []
    who = ([f"{manager} (руководитель)"] if manager_present else []) + present
    if who:
        out.append("Кто был: " + ", ".join(who))
    if absent:
        # Blank line above so the absentee block is clearly visible.
        out.append("")
        out.append("Не было: " + ", ".join(absent))
    if data.get("late_start"):
        minutes = data.get("late_start_minutes") or 0
        out.append(f"🕐 Опоздание: {int(minutes)} мин" if minutes else "🕐 Опоздание")
    if out:
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

    # Stable order: roster order first, then anyone extra in encounter order.
    roster_order = {
        _first_name(r.get("name")).lower(): i for i, r in enumerate(team_roster or [])
    }
    entries.sort(key=lambda e: roster_order.get(_first_name(e.get("name")).lower(), len(roster_order)))

    out: List[str] = []
    for entry in entries:
        out.append(f"👤 {_first_name(entry.get('name'))}")
        if not entry.get("participated"):
            out.append("Не принимал(а) участия.")
            out.append("")
            continue
        out += _field_lines("Отчёт за вчера", entry.get("yesterday"))
        out += _field_lines("План на сегодня", entry.get("today_plan"))
        out += _field_lines("Блокеры", entry.get("blockers"))
        out += _optional_field_lines("Нужна помощь", entry.get("needs_help"))
        out += _optional_field_lines("Вопрос руководителю", entry.get("question_to_manager"))
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
    # «Общее» first, then per-person lines; tasks per person on one line.
    for key in (["Общее"] if "Общее" in grouped else []) + [k for k in order if k != "Общее"]:
        out.append(f"{key}: {_numbered_inline(grouped[key])}")
    out.append("")
    return out


def _person_or_cross(value: Any, roster_firsts: set) -> str:
    kind, items = _classify(value)
    if kind != "items":
        return MISSING
    return "; ".join(_person(item, roster_firsts) for item in items)


def _risks_block(data: Dict[str, Any], roster_firsts: set) -> List[str]:
    risks = [r for r in data.get("problems_risks") or [] if _clean(r.get("text"))]
    if not risks:
        return []
    out = ["⚠️ РИСКИ И СИТУАЦИИ", ""]
    for i, risk in enumerate(risks, start=1):
        severity = _clean(risk.get("severity")).lower()
        out.append(f"{i}. {_clean(risk.get('text'))}")
        out.append(f"Риск: {_SEVERITY_ICONS.get(severity, '🟡')}")
        out.append(f"Ответственный: {_person_or_cross(risk.get('owner'), roster_firsts)}")
        out.append(f"Срок: {_value_or_cross(risk.get('deadline'))}")
        how = _clean(risk.get("how_to_track"))
        if how and how.lower() not in _MISSING_WORDS:
            out.append(f"Как контролируем: {how}")
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
