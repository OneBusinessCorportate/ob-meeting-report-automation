"""Deterministic Telegram report renderer (structure by script, values by AI).

Per leadership feedback (2026-06-11) the report TEXT is built here, in code,
with a fixed structure — the AI only fills in the values via the structured
JSON fields. Previously the model wrote ``telegram_report_md`` itself and kept
re-inventing the layout: blocks disappeared, stray lines appeared. Now the
layout cannot drift: every run produces the same sections in the same order.

Layout (updated 2026-06-24):

- score + 10-criteria checklist + talk share;
- per-person blocks — each person shows their own tasks (Задачи) and risks
  (Обратить внимание) directly below their block, no separate bottom sections;
- non-participants listed first as one-liners;
- manager reactions grouped (Общее first);
- general Обратить внимание for unassigned risks;
- Margarita errors block;
- general Задачи for unassigned tasks;
- open questions.

The closing analytics message (render_analytics_message) is sent separately.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

MISSING = "❌"  # value was not voiced on the meeting
NONE_DASH = "–"  # the person explicitly said there is nothing

# Known name aliases: maps any variant the AI might output to the canonical
# first name used in the roster.  Both directions are registered so lookup is
# always O(1) regardless of which form the AI writes.
_NAME_ALIASES: Dict[str, str] = {
    "асмик": "асмик",
    "хасмик": "асмик",  # roster uses Асмик; old transcripts may say Хасмик
}

# Canonical checklist labels (order matches the prompt schema).
CRITERIA_LABELS = [
    "Все высказались",
    "Задавала вопросы",
    "Ставила задачи",
    "Делилась новостями",
    "Хвалила команду",
    "Разбирала прошлые задачи",
    "Формат 1 мин. соблюдён",
    "Невыполненные объяснены",
    "Ошибки Маргариты обсудили",
    "Планы на завтра озвучены",
]

_STATUS_ICONS = {"выполнено": "✅", "частично": "\U0001f7e1", "не выполнено": "❌"}
_SEVERITY_ICONS = {"high": "\U0001f534", "medium": "\U0001f7e1", "low": "\U0001f7e2"}

_MISSING_WORDS = {"", "не указано", "не указан", "не указана", "❌", "none", "null"}
_NONE_WORDS = {"нет", "-", "–", "нет блокеров", "блокеров нет", "без блокеров", "ничего"}
_PLACEHOLDER_SURNAMES = {"бухгалтер", "менеджер", "руководитель"}


def _clean(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _first_name(name: Any) -> str:
    cleaned = _clean(name)
    return cleaned.split()[0] if cleaned else ""


def _person(name: Any, roster_firsts: set) -> str:
    cleaned = _clean(name)
    if not cleaned:
        return ""
    parts = cleaned.split()
    if parts[0].lower() in roster_firsts:
        return parts[0]
    if len(parts) > 1 and all(p.lower() in _PLACEHOLDER_SURNAMES for p in parts[1:]):
        return parts[0]
    return cleaned


def _canonical_first(name: Any) -> str:
    """Return the canonical (alias-resolved) lower-case first name."""
    raw = _first_name(name).lower()
    return _NAME_ALIASES.get(raw, raw)


def _roster_firsts(team_roster: Optional[List[Dict[str, Any]]]) -> set:
    return {
        _canonical_first(r.get("name")) for r in team_roster or [] if _first_name(r.get("name"))
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
    start = _local_hhmm(meeting.get("actual_start"), offset_hours)
    end = _local_hhmm(meeting.get("actual_end"), offset_hours)
    if start and end:
        return f"{start}–{end}"
    return start


def _classify(value: Any) -> Tuple[str, List[str]]:
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
    kind, items = _classify(value)
    if kind == "missing":
        return [f"{label}: {MISSING}"]
    if kind == "none":
        return [f"{label}: {NONE_DASH}"]
    if len(items) == 1:
        return [f"{label}: {items[0]}"]
    return [f"{label}:"] + [f"  – {item}" for item in items]


def _optional_field_lines(label: str, value: Any) -> List[str]:
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


def _armsoft_block(activity: List[Dict[str, Any]]) -> List[str]:
    if not activity:
        return []
    date_label = _dd_mm(activity[0].get("date", ""))
    lines: List[str] = [f"\U0001f4ca ФАКТ ПО ARMSOFT ({date_label})", ""]
    for entry in activity:
        name = entry.get("name") or "?"
        assigned = entry.get("assigned", 0)
        active = entry.get("active", 0)
        docs = entry.get("docs", 0)
        invoices = entry.get("invoices", 0)
        tax_docs = entry.get("tax_docs", 0)
        if assigned == 0:
            lines.append(f"  – {name}: нет назначенных клиентов")
        elif active == 0:
            lines.append(f"  ⚠️ {name} — {assigned} кл. | нет активности")
        else:
            parts = [f"{active} компан.", f"{docs} докум."]
            if invoices:
                parts.append(f"{invoices} накл.")
            if tax_docs:
                parts.append(f"{tax_docs} нал. докум.")
            lines.append(f"  ✅ {name} — {assigned} кл. | {', '.join(parts)}")
    lines.append("")
    return lines


def _taxservice_block(activity: List[Dict[str, Any]]) -> List[str]:
    if not activity:
        return []
    date_label = _dd_mm(activity[0].get("date", ""))
    lines: List[str] = [f"\U0001f3db НАЛОГОВАЯ СЛУЖБА ({date_label})", ""]
    for entry in activity:
        name = entry.get("name") or "?"
        assigned = entry.get("assigned", 0)
        active = entry.get("active", 0)
        invoices = entry.get("invoices", 0)
        if assigned == 0:
            lines.append(f"  – {name}: нет назначенных клиентов")
        elif active == 0:
            lines.append(f"  – {name} — {assigned} кл. | нет активности")
        else:
            lines.append(f"  ✅ {name} — {invoices} счёт-фактур, {active}/{assigned} компаний")
    lines.append("")
    return lines


def render_armsoft_message(
    *,
    meeting_date: Optional[str] = None,
    armsoft_activity: Optional[List[Dict[str, Any]]] = None,
    taxservice_activity: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Standalone evening message with ArmSoft + TaxService cross-check data.

    Sent separately from the morning report (at 18:00 Armenia) so bookkeepers
    can review and comment before the next day's planning meeting.
    """
    if not armsoft_activity and not taxservice_activity:
        return ""
    title = "\U0001f4ca ДАННЫЕ ПО БАЗАМ"
    if meeting_date:
        title += f" · {_dd_mm(meeting_date)}"
    lines: List[str] = [title, ""]
    if armsoft_activity:
        lines += _armsoft_block(armsoft_activity)
    if taxservice_activity:
        lines += _taxservice_block(taxservice_activity)
    return _finalize(lines)


def _verifications_compact_block(data: Dict[str, Any]) -> List[str]:
    verifications = [
        v for v in data.get("db_verifications") or []
        if isinstance(v, dict) and _clean(v.get("speaker"))
    ]
    checked = [
        v for v in verifications
        if _clean(v.get("verification_status")).lower() != "no_data"
    ]
    if not checked:
        return []

    date_label = ""
    for v in checked:
        if v.get("verified_date"):
            date_label = f" ({_dd_mm(v['verified_date'])})"
            break

    confirmed = [v for v in checked if _clean(v.get("verification_status")).lower() == "confirmed"]
    discrepant = [v for v in checked if _clean(v.get("verification_status")).lower() in ("partial", "unconfirmed")]

    lines: List[str] = [f"ПРОВЕРКА ПО БАЗЕ{date_label}", ""]

    if confirmed:
        names = ", ".join(_clean(v.get("speaker")) for v in confirmed)
        lines.append(f"✅ {names}")
        if discrepant:
            lines.append("")

    for v in discrepant:
        name = _clean(v.get("speaker"))
        manager_task = _clean(v.get("manager_task"))
        accountant_said = _clean(v.get("accountant_said"))
        db_shows = _clean(v.get("db_shows"))
        lines.append(f"{name}:")
        if manager_task:
            lines.append(f"Эмилия: {manager_task}")
        if accountant_said:
            lines.append(f"Сказали: {accountant_said}")
        if db_shows:
            lines.append(f"База: {db_shows}")
        lines.append("")
    return lines


# ---------------------------------------------------------------------------
# Per-person task and attention helpers
# ---------------------------------------------------------------------------

def _person_tasks(name_canonical: str, data: Dict[str, Any], roster_firsts: set) -> List[str]:
    """Action items assigned to this specific person, shown inside their block."""
    items = [
        t for t in data.get("action_items") or []
        if _clean(t.get("text")) and
        _canonical_first(_clean(t.get("assignee", ""))) == name_canonical
    ]
    if not items:
        return []
    out = ["Задачи:"]
    for i, task in enumerate(items, start=1):
        text = _clean(task.get("text")).rstrip(".")
        deadline = _value_or_cross(task.get("deadline"))
        out.append(f"  {i}. {text}. Срок: {deadline}")
    return out


def _person_attention(name_canonical: str, data: Dict[str, Any]) -> List[str]:
    """Risks where this person is the owner, shown inside their block."""
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    risks = [
        r for r in data.get("problems_risks") or []
        if _clean(r.get("text")) and
        _canonical_first(_clean(r.get("owner", ""))) == name_canonical
    ]
    if not risks:
        return []
    risks.sort(key=lambda r: severity_rank.get(_clean(r.get("severity")).lower(), 1))
    out = ["Обратить внимание:"]
    for risk in risks:
        severity = _clean(risk.get("severity")).lower()
        icon = _SEVERITY_ICONS.get(severity, "\U0001f7e1")
        out.append(f"  {icon} {_clean(risk.get('text'))}")
        out.append(f"  Решение: {_value_or_cross(risk.get('decision'))}")
    return out


def _general_attention_block(data: Dict[str, Any], roster_firsts: set) -> List[str]:
    """All risks/situations — every entry shown, owner displayed inline."""
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    risks = [r for r in data.get("problems_risks") or [] if _clean(r.get("text"))]
    if not risks:
        return []
    risks.sort(key=lambda r: severity_rank.get(_clean(r.get("severity")).lower(), 1))
    out = ["⚠️ СИТУАЦИИ И РИСКИ", ""]
    for i, risk in enumerate(risks, start=1):
        severity = _clean(risk.get("severity")).lower()
        icon = _SEVERITY_ICONS.get(severity, "\U0001f7e1")
        owner = _clean(risk.get("owner"))
        owner_part = f" ({owner})" if owner and owner.lower() not in _MISSING_WORDS else ""
        out.append(f"  {icon} {i}. {_clean(risk.get('text'))}{owner_part}")
        out.append(f"  Решение: {_value_or_cross(risk.get('decision'))}")
        out.append("")
    return out


def _general_tasks_block(data: Dict[str, Any], roster_firsts: set) -> List[str]:
    """All action items — every task shown, grouped by assignee."""
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
    out = ["\U0001f4cc ЗАДАЧИ"]
    for assignee in order:
        tasks = grouped[assignee]
        if len(tasks) == 1:
            text = _clean(tasks[0].get("text")).rstrip(".")
            deadline = _value_or_cross(tasks[0].get("deadline"))
            out.append(f"  {assignee}: {text}. Срок: {deadline}")
        else:
            out.append(f"  {assignee}:")
            for i, task in enumerate(tasks, start=1):
                text = _clean(task.get("text")).rstrip(".")
                deadline = _value_or_cross(task.get("deadline"))
                out.append(f"    {i}. {text}. Срок: {deadline}")
    out.append("")
    return out


# ---------------------------------------------------------------------------
# Main render functions
# ---------------------------------------------------------------------------

def render_telegram_report(
    data: Dict[str, Any],
    *,
    meeting_date: Optional[str] = None,
    time_range: Optional[str] = None,
    team_roster: Optional[List[Dict[str, Any]]] = None,
    prior_stats: Optional[List[Dict[str, Any]]] = None,
    armsoft_activity: Optional[List[Dict[str, Any]]] = None,  # unused here; see render_armsoft_message()
    taxservice_activity: Optional[List[Dict[str, Any]]] = None,  # unused here; see render_armsoft_message()
    include_analytics: bool = True,
) -> str:
    manager = _find_manager(team_roster)
    roster_firsts = _roster_firsts(team_roster)
    lines: List[str] = ["\U0001f4cb Планёрка бухгалтерии"]
    if meeting_date and time_range:
        lines.append(f"{meeting_date}, {time_range}")
    elif meeting_date:
        lines.append(meeting_date)
    lines.append("")

    lines += _score_block(data)
    lines += _attendance_block(data)
    lines += _accountant_blocks(data, team_roster, manager)
    lines += _manager_block(data, manager, roster_firsts)
    lines += _general_attention_block(data, roster_firsts)
    lines += _critical_errors_block(data)
    lines += _general_tasks_block(data, roster_firsts)
    lines += _open_questions_block(data)
    if include_analytics:
        block = _analytics_block(data, prior_stats, roster_firsts, manager)
        if block:
            lines += ["\U0001f4c8 АНАЛИТИКА", ""] + block

    return _finalize(lines)


def render_analytics_message(
    data: Dict[str, Any],
    *,
    meeting_date: Optional[str] = None,
    team_roster: Optional[List[Dict[str, Any]]] = None,
    prior_stats: Optional[List[Dict[str, Any]]] = None,
) -> str:
    roster_firsts = _roster_firsts(team_roster)
    manager = _find_manager(team_roster)
    block = _analytics_block(data, prior_stats, roster_firsts, manager)
    verif = _verifications_compact_block(data)
    if not block and not verif:
        return ""
    title = "\U0001f4ca Аналитика планёрки"
    if meeting_date:
        title += f" · {_dd_mm(meeting_date)}"
    content = [title, ""] + block
    if verif:
        if block:
            content.append("")
        content += verif
    return _finalize(content)


# --- sections -----------------------------------------------------------------

def _score_block(data: Dict[str, Any]) -> List[str]:
    eff = data.get("effectiveness") or {}
    criteria = eff.get("criteria") or []
    out: List[str] = []
    score = eff.get("score")
    if score:
        max_score = eff.get("max_score") or 10
        out.append(f"Оценка: {int(score)}/{int(max_score)}")
    if criteria:
        for i, label in enumerate(CRITERIA_LABELS[: len(criteria)]):
            status = _clean((criteria[i] or {}).get("status"))
            icon = _STATUS_ICONS.get(status.lower(), MISSING)
            out.append(f"  {icon} {label}")
    talk = data.get("talk_share") or {}
    manager_pct, accountants_pct = talk.get("manager_pct"), talk.get("accountants_pct")
    if manager_pct or accountants_pct:
        if out:
            out.append("")
        out.append(
            f"Говорят: {manager_pct or 0}% руководитель · {accountants_pct or 0}% бухгалтеры"
        )
    if out:
        out.append("")
    return out


def _attendance_block(data: Dict[str, Any]) -> List[str]:
    out: List[str] = []
    if data.get("late_start"):
        minutes = data.get("late_start_minutes") or 0
        out.append(f"Опоздание: {int(minutes)} мин" if minutes else "Опоздание")
        out.append("")
    return out


def _armsoft_line(canonical: str, armsoft_lookup: Dict[str, Any]) -> List[str]:
    """One-line Armsoft fact for a single person, shown inside their block."""
    arm = armsoft_lookup.get(canonical)
    if arm is None:
        return []
    assigned = arm.get("assigned", 0)
    active = arm.get("active", 0)
    docs = arm.get("docs", 0)
    invoices = arm.get("invoices", 0)
    tax_docs = arm.get("tax_docs", 0)
    date_label = _dd_mm(arm.get("date", ""))
    label = f"Armsoft ({date_label})"
    if assigned == 0:
        return [f"{label}: нет назначенных клиентов"]
    if active == 0:
        return [f"{label}: ⚠️ нет активности ({assigned} клиентов)"]
    parts = [f"{active}/{assigned} клиентов", f"{docs} докум."]
    if invoices:
        parts.append(f"{invoices} накл.")
    if tax_docs:
        parts.append(f"{tax_docs} нал. докум.")
    return [f"{label}: ✅ {', '.join(parts)}"]


def _taxservice_line(canonical: str, taxservice_lookup: Dict[str, Any]) -> List[str]:
    """One-line tax portal fact for a single person, shown inside their block."""
    ts = taxservice_lookup.get(canonical)
    if ts is None:
        return []
    assigned = ts.get("assigned", 0)
    active = ts.get("active", 0)
    invoices = ts.get("invoices", 0)
    date_label = _dd_mm(ts.get("date", ""))
    label = f"Налоговая ({date_label})"
    if assigned == 0:
        return [f"{label}: нет назначенных клиентов"]
    if active == 0:
        return [f"{label}: – нет активности"]
    parts = [f"{invoices} счёт-фактур", f"{active}/{assigned} компаний"]
    return [f"{label}: ✅ {', '.join(parts)}"]


def _accountant_blocks(
    data: Dict[str, Any],
    team_roster: Optional[List[Dict[str, Any]]],
    manager: str,
) -> List[str]:
    roster_firsts = _roster_firsts(team_roster)
    entries = [
        e for e in data.get("participant_breakdown") or []
        if _first_name(e.get("name")) and _canonical_first(e.get("name")) != manager.lower()
    ]
    if roster_firsts:
        entries = [
            e for e in entries if _canonical_first(e.get("name")) in roster_firsts
        ]

    # Add absent entries for roster members the AI omitted entirely from the
    # breakdown — they must still appear in the report so attendance is complete.
    breakdown_canonical = {_canonical_first(e.get("name")) for e in entries}
    for roster_member in (team_roster or []):
        canonical = _canonical_first(roster_member.get("name"))
        if canonical and canonical != manager.lower() and canonical not in breakdown_canonical:
            entries.append({"name": _first_name(roster_member.get("name")), "participated": False})

    if not entries:
        return []

    roster_order = {
        _canonical_first(r.get("name")): i for i, r in enumerate(team_roster or [])
    }
    entries.sort(
        key=lambda e: (
            bool(e.get("participated")),
            roster_order.get(_canonical_first(e.get("name")), len(roster_order)),
        )
    )

    out: List[str] = []
    absent_done = False
    for entry in entries:
        name = _first_name(entry.get("name"))
        if not entry.get("participated"):
            out.append(f"\U0001f464 {name} — не участвовал(а)")
            continue
        if out and not absent_done:
            out.append("")
        absent_done = True
        out.append(f"\U0001f464 {name}")
        out += ["  " + l for l in _field_lines("Вчера", entry.get("yesterday"))]
        out += ["  " + l for l in _field_lines("Сегодня", entry.get("today_plan"))]
        out += ["  " + l for l in _field_lines("Завтра", entry.get("tomorrow_plan"))]
        out += ["  " + l for l in _field_lines("Блокеры", entry.get("blockers"))]
        out += ["  " + l for l in _optional_field_lines("Помощь", entry.get("needs_help"))]
        out += ["  " + l for l in _optional_field_lines("Вопрос", entry.get("question_to_manager"))]
        out += ["  " + l for l in _optional_field_lines("Не выполнено", entry.get("task_not_done_reason"))]
        out.append("")
    if out and out[-1] != "":
        out.append("")
    return out


def _manager_block(data: Dict[str, Any], manager: str, roster_firsts: set) -> List[str]:
    reactions = data.get("manager_reactions") or []
    absent_firsts = {
        _canonical_first(e.get("name"))
        for e in (data.get("participant_breakdown") or [])
        if not e.get("participated") and _first_name(e.get("name"))
    }
    grouped: Dict[str, List[str]] = {}
    order: List[str] = []
    for reaction in reactions:
        text = _clean(reaction.get("text"))
        if not text:
            continue
        to_whom = _clean(reaction.get("to_whom")) or "Общее"
        key = "Общее" if to_whom.lower() in {"общее", "все", "всем", "команда", "команде"} \
            else _person(to_whom, roster_firsts)
        if key.lower() in absent_firsts:
            continue
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(text)
    if not grouped:
        return []

    out = [f"\U0001f5e3 {manager.upper()}"]
    for key in (["Общее"] if "Общее" in grouped else []) + [k for k in order if k != "Общее"]:
        texts = grouped[key]
        if len(texts) == 1:
            out.append(f"  {key}: {texts[0]}")
        else:
            out.append(f"  {key}:")
            out.extend(f"  {i}. {text}" for i, text in enumerate(texts, start=1))
    out.append("")
    return out


def _critical_errors_block(data: Dict[str, Any]) -> List[str]:
    errors = [e for e in data.get("critical_errors_margarita") or [] if _clean(e.get("error"))]
    if not errors:
        return []
    out = ["\U0001f4cb ОШИБКИ ПО ОТЧЁТУ МАРГАРИТЫ", ""]
    for i, err in enumerate(errors, start=1):
        accountant = _clean(err.get("accountant")) or "Не указано"
        discussed = err.get("was_discussed", False)
        explanation = _clean(err.get("explanation_given"))
        root_cause = _clean(err.get("root_cause"))
        out.append(f"  {i}. {_clean(err.get('error'))}")
        out.append(f"  Сотрудник: {accountant}")
        out.append(f"  Обсуждалось: {'да' if discussed else 'нет'}")
        if explanation:
            out.append(f"  Объяснение: {explanation}")
        else:
            out.append(f"  Объяснение: {MISSING}")
        if root_cause and root_cause.lower() not in _MISSING_WORDS:
            out.append(f"  Причина: {root_cause}")
        out.append("")
    return out


def _open_questions_block(data: Dict[str, Any]) -> List[str]:
    questions = [_clean(q) for q in data.get("open_questions") or [] if _clean(q)]
    questions = [q for q in questions if q.lower() not in _MISSING_WORDS]
    if not questions:
        return []
    return ["❓ ВОПРОСЫ"] + [f"  – {q}" for q in questions] + [""]


def _dd_mm(iso_date: Any) -> str:
    text = _clean(iso_date)
    parts = text.split("-")
    return f"{parts[2]}.{parts[1]}" if len(parts) == 3 else text


def _real_count(value: Any) -> int:
    kind, items = _classify(value)
    return len(items) if kind == "items" else 0


def _has_text(value: Any) -> bool:
    text = _clean(value)
    return bool(text) and text.lower() not in _MISSING_WORDS and text.lower() not in _NONE_WORDS


def _roster_breakdown(
    data: Dict[str, Any], roster_firsts: set, manager: str
) -> List[Dict[str, Any]]:
    out = []
    for entry in data.get("participant_breakdown") or []:
        first = _first_name(entry.get("name"))
        if not first or _canonical_first(entry.get("name")) == manager.lower():
            continue
        if roster_firsts and _canonical_first(entry.get("name")) not in roster_firsts:
            continue
        out.append(entry)
    return out


def _arrow(cur: float, prev: float) -> str:
    if cur > prev:
        return "↗"
    if cur < prev:
        return "↘"
    return ""


def _attendance_misses(
    data: Dict[str, Any], prior_stats: List[Dict[str, Any]], roster_firsts: set
) -> Tuple[Dict[str, int], int]:
    attendance = [e for e in prior_stats if e.get("has_participation")]
    today_breakdown = [
        p for p in data.get("participant_breakdown") or [] if _first_name(p.get("name"))
    ]
    total = len(attendance) + (1 if today_breakdown else 0)

    def _in_roster(name: str) -> bool:
        return not roster_firsts or name.split()[0].lower() in roster_firsts

    misses: Dict[str, int] = {}
    for entry in attendance:
        for raw in entry.get("absent") or []:
            name = _person(raw, roster_firsts)
            if _in_roster(name):
                misses[name] = misses.get(name, 0) + 1
    for participant in today_breakdown:
        if not participant.get("participated"):
            name = _person(participant.get("name"), roster_firsts)
            if _in_roster(name):
                misses[name] = misses.get(name, 0) + 1
    return misses, total


def _score_line(data: Dict[str, Any], prior_stats: List[Dict[str, Any]]) -> List[str]:
    score = (data.get("effectiveness") or {}).get("score")
    if not score:
        return []
    line = f"Оценка встречи: {int(score)}/10"
    prior_scores = [s.get("score") for s in prior_stats if s.get("score")]
    if prior_scores:
        chain = "→".join(str(int(s)) for s in prior_scores[-2:] + [score])
        line += f" ({chain}{_arrow(score, prior_scores[-1])})"
    out = [line]
    if data.get("late_start"):
        minutes = data.get("late_start_minutes") or 0
        out.append(f"Начали с опозданием на {int(minutes)} мин" if minutes
                   else "Начали с опозданием")
    return out


_MANAGER_CONDUCT = [
    (1, "Задаёт вопросы"),
    (2, "Ставит задачи команде"),
    (5, "Разбирает прошлые задачи"),
    (4, "Хвалит / отмечает работу"),
    (3, "Делится новостями"),
]


def _criterion_status(data: Dict[str, Any], idx: int) -> Optional[str]:
    criteria = (data.get("effectiveness") or {}).get("criteria") or []
    if idx < len(criteria) and isinstance(criteria[idx], dict):
        return _clean(criteria[idx].get("status")).lower() or None
    return None


def _criterion_detail(data: Dict[str, Any], idx: int) -> str:
    criteria = (data.get("effectiveness") or {}).get("criteria") or []
    if idx < len(criteria) and isinstance(criteria[idx], dict):
        return _clean(criteria[idx].get("detail"))
    return ""


def _short(text: Any, limit: int = 80) -> str:
    text = _clean(text)
    if not text or text.lower() in _MISSING_WORDS:
        return ""
    head, sep, _ = text.partition(". ")
    if sep and len(head) >= 20:
        text = head
    if len(text) > limit:
        return text[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return text.rstrip(" .")


def _conduct_streak(idx: int, prior_stats: List[Dict[str, Any]]) -> int:
    streak = 1
    for entry in reversed(prior_stats):
        crit = entry.get("criteria") or []
        if idx >= len(crit) or not crit[idx]:
            break
        if _clean(crit[idx]).lower() == "выполнено":
            break
        streak += 1
    return streak


def _manager_conduct_lines(
    data: Dict[str, Any], prior_stats: List[Dict[str, Any]]
) -> List[str]:
    body: List[str] = []
    for idx, label in _MANAGER_CONDUCT:
        status = _criterion_status(data, idx)
        if status is None:
            continue
        icon = _STATUS_ICONS.get(status, MISSING)
        line = f"{icon} {label}"
        if status != "выполнено":
            extras = []
            detail = _short(_criterion_detail(data, idx))
            if detail:
                extras.append(detail)
            streak = _conduct_streak(idx, prior_stats)
            if streak >= 2:
                extras.append(f"{streak}-ю планёрку подряд")
            if extras:
                line += " — " + " · ".join(extras)
        body.append(line)

    talk = data.get("talk_share") or {}
    manager_pct = talk.get("manager_pct")
    if manager_pct:
        note = " — говорит много, дайте бухгалтерам слово" if manager_pct >= 70 else ""
        body.append(f"Говорит {int(manager_pct)}% времени{note}")

    if not body:
        return []
    return ["\U0001f9ed КАК ВЕЛА ВСТРЕЧУ"] + ["  " + l for l in body]


def _accountant_tasks_lines(
    data: Dict[str, Any], breakdown: List[Dict[str, Any]], roster_firsts: set
) -> List[str]:
    if not breakdown:
        return []
    participated = [e for e in breakdown if e.get("participated")]
    if not participated:
        return []
    no_plan = [
        _person(e.get("name"), roster_firsts)
        for e in participated
        if _real_count(e.get("today_plan")) == 0
    ]
    no_tomorrow = [
        _person(e.get("name"), roster_firsts)
        for e in participated
        if _real_count(e.get("tomorrow_plan")) == 0
    ]
    silent = [_person(e.get("name"), roster_firsts) for e in breakdown if not e.get("participated")]

    body: List[str] = []
    body.append(f"Озвучили план: {len(participated) - len(no_plan)} из {len(participated)}")
    if no_plan:
        body.append(f"Без плана: {', '.join(no_plan)}")
    if no_tomorrow:
        body.append(f"Без плана на завтра: {', '.join(no_tomorrow)}")

    actions = [t for t in data.get("action_items") or [] if _clean(t.get("text"))]
    if actions:
        no_deadline = sum(1 for t in actions if not _has_text(t.get("deadline")))
        no_owner = sum(1 for t in actions if not _has_text(t.get("assignee")))
        if no_deadline:
            body.append(f"Задачи без срока: {no_deadline} из {len(actions)}")
        if no_owner:
            body.append(f"Задачи без ответственного: {no_owner} из {len(actions)}")

    askers = [
        _person(e.get("name"), roster_firsts)
        for e in participated
        if _has_text(e.get("question_to_manager"))
    ]
    if askers:
        body.append(f"Вопросы руководителю: {len(askers)} ({', '.join(askers)})")

    need_help = [
        _person(e.get("name"), roster_firsts)
        for e in participated
        if _has_text(e.get("needs_help")) or _real_count(e.get("blockers"))
    ]
    if need_help:
        body.append(f"Помощь нужна: {', '.join(need_help)}")

    if silent:
        body.append(f"Промолчали: {', '.join(silent)}")
    return ["\U0001f9d1‍\U0001f4bc БУХГАЛТЕРЫ"] + ["  " + l for l in body]


def _improve_lines(
    data: Dict[str, Any],
    breakdown: List[Dict[str, Any]],
    prior_stats: List[Dict[str, Any]],
    roster_firsts: set,
) -> List[str]:
    tips: List[str] = []

    if _criterion_status(data, 5) not in (None, "выполнено"):
        streak = _conduct_streak(5, prior_stats)
        suffix = f" ({streak}-ю планёрку подряд)" if streak >= 2 else ""
        tips.append(f"Разобрать статус прошлых задач{suffix}")

    actions = [t for t in data.get("action_items") or [] if _clean(t.get("text"))]
    no_deadline = sum(1 for t in actions if not _has_text(t.get("deadline")))
    if actions and no_deadline >= max(2, len(actions) // 3):
        tips.append(f"Проставлять задачам сроки ({no_deadline} из {len(actions)} без даты)")

    no_plan = [
        _person(e.get("name"), roster_firsts)
        for e in breakdown
        if e.get("participated") and _real_count(e.get("today_plan")) == 0
    ]
    if no_plan:
        tips.append(f"Попросить озвучивать план на день: {', '.join(no_plan)}")

    format_violators = [
        _person(e.get("name"), roster_firsts)
        for e in breakdown
        if e.get("participated") and _clean(e.get("format_compliance")).lower() == "нарушен"
    ]
    if format_violators:
        tips.append(f"Соблюдать формат 1 минуты: {', '.join(format_violators)}")

    silent = [e for e in breakdown if not e.get("participated")]
    if silent:
        misses, total = _attendance_misses(data, prior_stats, roster_firsts)
        chronic = [
            _person(e.get("name"), roster_firsts)
            for e in silent
            if total >= 2 and misses.get(_person(e.get("name"), roster_firsts), 0) >= 2
        ]
        names = chronic or [_person(e.get("name"), roster_firsts) for e in silent]
        tips.append(f"Вовлечь в обсуждение: {', '.join(names)}")

    manager_pct = (data.get("talk_share") or {}).get("manager_pct")
    if manager_pct and manager_pct >= 70:
        tips.append(f"Дать бухгалтерам больше говорить (руководитель {int(manager_pct)}%)")

    if _criterion_status(data, 4) == "не выполнено":
        tips.append("Отметить хорошую работу кого-то из команды")

    if not tips:
        return []
    return ["\U0001f4a1 НА СЛЕДУЮЩЕЙ"] + [f"  – {tip}" for tip in tips]


def _analytics_block(
    data: Dict[str, Any],
    prior_stats: Optional[List[Dict[str, Any]]],
    roster_firsts: set,
    manager: str = "",
) -> List[str]:
    prior_stats = prior_stats or []
    breakdown = _roster_breakdown(data, roster_firsts, manager)
    has_criteria = bool((data.get("effectiveness") or {}).get("criteria"))
    if not has_criteria and not breakdown:
        return []

    sections = [
        _score_line(data, prior_stats),
        _manager_conduct_lines(data, prior_stats),
        _accountant_tasks_lines(data, breakdown, roster_firsts),
        _improve_lines(data, breakdown, prior_stats, roster_firsts),
    ]
    out: List[str] = []
    for section in sections:
        if section:
            if out:
                out.append("")
            out += section
    return out


def _finalize(lines: List[str]) -> str:
    text = "\n".join(lines)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip() + "\n"
