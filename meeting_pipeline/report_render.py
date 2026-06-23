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

The closing «📊 Аналитика планёрки» (built by :func:`render_analytics_message`,
delivered as a SEPARATE Telegram message) has ONE goal: make the planёрка
itself run better (not manage the whole company). So it shows only meeting
mechanics, compact and scannable:

- the meeting effectiveness score (+ trend) and a late-start discipline note;
- «🧭 РУКОВОДИТЕЛЬ ВЕДЁТ ВСТРЕЧУ»: the facilitation checklist — does the
  manager ask questions, set tasks, review past tasks, praise, share news — each
  ✅/🟡/❌; a gap carries the AI's short «why» and «N-ю планёрку подряд» when it
  is chronic; plus talk share;
- «🧑‍💼 БУХГАЛТЕРЫ СТАВЯТ ЗАДАЧИ»: did each accountant voice a plan for the day,
  how concretely (tasks без срока / без ответственного = «поставлено не так»),
  what questions they put to the manager, who asked for help, and who was silent;
- «💡 УЛУЧШИТЬ НА СЛЕДУЮЩЕЙ»: concrete, meeting-level fixes derived from the gaps.

Client-workload and company-level recurring problems were dropped on purpose —
they are about running the company, not the meeting. Every fact is the AI's;
every NUMBER (counts, streaks, trends) is computed here. Empty sections drop.
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


def _armsoft_block(activity: List[Dict[str, Any]]) -> List[str]:
    """Armsoft portfolio activity cross-check section."""
    if not activity:
        return []
    date_label = activity[0].get("date", "")
    lines: List[str] = [f"📊 Активность в Armsoft ({date_label})", ""]
    for entry in activity:
        name = entry.get("name") or "?"
        assigned = entry.get("assigned", 0)
        active = entry.get("active", 0)
        docs = entry.get("docs", 0)
        if assigned == 0:
            lines.append(f"• {name} — нет назначенных клиентов")
        elif active == 0:
            lines.append(f"• {name} — {assigned} кл. | вчера: ⚠️ нет активности")
        else:
            lines.append(
                f"• {name} — {assigned} кл. | вчера: {active} компан., {docs} докум."
            )
    lines.append("")
    return lines


def render_telegram_report(
    data: Dict[str, Any],
    *,
    meeting_date: Optional[str] = None,
    time_range: Optional[str] = None,
    team_roster: Optional[List[Dict[str, Any]]] = None,
    prior_stats: Optional[List[Dict[str, Any]]] = None,
    armsoft_activity: Optional[List[Dict[str, Any]]] = None,
    include_analytics: bool = True,
) -> str:
    """Build the full Telegram report text from the structured analysis JSON.

    ``data`` is the merged report (column fields + extras: ``effectiveness``,
    ``participant_breakdown``, ``manager_reactions``, ``talk_share``, …).
    ``prior_stats`` (oldest first, from ``get_prior_meeting_stats``) powers the
    trend lines of the analytics block. Sections with no data are dropped
    whole; the structure never changes.

    Set ``include_analytics=False`` to leave out the closing analytics block —
    delivery sends it as a separate Telegram message via
    :func:`render_analytics_message`.
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
    if armsoft_activity:
        lines += _armsoft_block(armsoft_activity)
    if include_analytics:
        block = _analytics_block(data, prior_stats, roster_firsts, manager)
        if block:
            lines += ["📈 АНАЛИТИКА", ""] + block

    return _finalize(lines)


def render_analytics_message(
    data: Dict[str, Any],
    *,
    meeting_date: Optional[str] = None,
    team_roster: Optional[List[Dict[str, Any]]] = None,
    prior_stats: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """Build the standalone analytics message (sent right after the report).

    Returns an empty string when there is no analytics to show (e.g. the first
    stand-up, or an analysis stored before the dynamics feature existed), so
    the caller can simply skip the second message.
    """
    roster_firsts = _roster_firsts(team_roster)
    manager = _find_manager(team_roster)
    block = _analytics_block(data, prior_stats, roster_firsts, manager)
    if not block:
        return ""
    title = "📊 Аналитика планёрки"
    if meeting_date:
        title += f" · {_dd_mm(meeting_date)}"
    return _finalize([title, ""] + block)


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
    roster_firsts = _roster_firsts(team_roster)
    entries = [
        e for e in data.get("participant_breakdown") or []
        if _first_name(e.get("name")) and _first_name(e.get("name")).lower() != manager.lower()
    ]
    if roster_firsts:
        # The roster is the source of truth for the stand-up: people who are
        # no longer part of it (e.g. Гор-менеджер in analyses stored before
        # the roster was cleaned up) are not shown.
        entries = [
            e for e in entries if _first_name(e.get("name")).lower() in roster_firsts
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
    # First names of participants who were NOT at the meeting — manager comments
    # addressed to absent people are skipped (they couldn't hear the feedback).
    absent_firsts = {
        _first_name(e.get("name")).lower()
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


def _dd_mm(iso_date: Any) -> str:
    text = _clean(iso_date)
    parts = text.split("-")
    return f"{parts[2]}.{parts[1]}" if len(parts) == 3 else text


def _real_count(value: Any) -> int:
    """How many real items a field holds (drops ❌/«Не указано»/«нет» noise)."""
    kind, items = _classify(value)
    return len(items) if kind == "items" else 0


def _has_text(value: Any) -> bool:
    text = _clean(value)
    return bool(text) and text.lower() not in _MISSING_WORDS and text.lower() not in _NONE_WORDS


def _roster_breakdown(
    data: Dict[str, Any], roster_firsts: set, manager: str
) -> List[Dict[str, Any]]:
    """Today's participant entries limited to current-roster accountants."""
    out = []
    for entry in data.get("participant_breakdown") or []:
        first = _first_name(entry.get("name"))
        if not first or first.lower() == manager.lower():
            continue
        if roster_firsts and first.lower() not in roster_firsts:
            continue
        out.append(entry)
    return out


def _arrow(cur: float, prev: float) -> str:
    """Compact trend mark for inline use (no leading space): ↗ / ↘ / ''."""
    if cur > prev:
        return "↗"
    if cur < prev:
        return "↘"
    return ""


def _attendance_misses(
    data: Dict[str, Any], prior_stats: List[Dict[str, Any]], roster_firsts: set
) -> Tuple[Dict[str, int], int]:
    """How many of the recent stand-ups (incl. today) each person missed."""
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
    """Headline: the meeting effectiveness score (+ trend over recent stand-ups)."""
    score = (data.get("effectiveness") or {}).get("score")
    if not score:
        return []
    line = f"Оценка встречи: {int(score)}/10"
    prior_scores = [s.get("score") for s in prior_stats if s.get("score")]
    if prior_scores:
        chain = "→".join(str(int(s)) for s in prior_scores[-2:] + [score])
        line += f" ({chain}{_arrow(score, prior_scores[-1])})"
    out = [line]
    # Discipline: a late start is a meeting-effectiveness signal.
    if data.get("late_start"):
        minutes = data.get("late_start_minutes") or 0
        out.append(f"🕐 Начали с опозданием на {int(minutes)} мин" if minutes
                   else "🕐 Начали с опозданием")
    return out


# Manager-facilitation behaviours, by their index in the effectiveness checklist
# (criterion 0 «Все высказались» is about the team, shown in the accountants
# section). Ordered most-important-first for the «как ведёт встречу» view.
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
    """First sentence of ``text``, trimmed to ``limit`` chars on a word boundary.

    Used to surface the AI's short «why» for a checklist gap without bloating the
    line. Returns '' for empty / «Не указано» values.
    """
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
    """Consecutive recent stand-ups (most recent first) where the manager did
    NOT fully satisfy criterion ``idx`` — stops at the first «выполнено»."""
    streak = 1  # today already counts as a miss when this is called
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
    """🧭 How the manager ran the meeting: the facilitation checklist + talk share."""
    body: List[str] = []
    for idx, label in _MANAGER_CONDUCT:
        status = _criterion_status(data, idx)
        if status is None:
            continue  # older analyses had only 5 criteria — skip the missing one
        icon = _STATUS_ICONS.get(status, MISSING)
        line = f"{icon} {label}"
        if status != "выполнено":
            # Surface the AI's short «why» + the chronic-gap streak, so a 🟡/❌
            # is actionable instead of a bare icon.
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

    if not body:  # nothing assessable about facilitation -> no empty header
        return []
    return ["🧭 РУКОВОДИТЕЛЬ ВЕДЁТ ВСТРЕЧУ"] + body


def _accountant_tasks_lines(
    data: Dict[str, Any], breakdown: List[Dict[str, Any]], roster_firsts: set
) -> List[str]:
    """🧑‍💼 Whether accountants set themselves tasks — and how concretely."""
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
    silent = [_person(e.get("name"), roster_firsts) for e in breakdown if not e.get("participated")]

    out = ["🧑‍💼 БУХГАЛТЕРЫ СТАВЯТ ЗАДАЧИ"]
    out.append(f"План на сегодня озвучили: {len(participated) - len(no_plan)} из {len(participated)}")
    if no_plan:
        out.append(f"Не озвучили план: {', '.join(no_plan)}")

    # Concreteness of the tasks set today: a task without a deadline (or without
    # an owner) is «поставлена не так» — нельзя проконтролировать.
    actions = [t for t in data.get("action_items") or [] if _clean(t.get("text"))]
    if actions:
        no_deadline = sum(1 for t in actions if not _has_text(t.get("deadline")))
        no_owner = sum(1 for t in actions if not _has_text(t.get("assignee")))
        if no_deadline:
            out.append(f"Задачи без срока: {no_deadline} из {len(actions)} ⚠️")
        if no_owner:
            out.append(f"Задачи без ответственного: {no_owner} из {len(actions)} ⚠️")

    # Questions accountants put to the manager — they need an answer ON the call.
    askers = [
        _person(e.get("name"), roster_firsts)
        for e in participated
        if _has_text(e.get("question_to_manager"))
    ]
    if askers:
        out.append(f"Вопросы руководителю: {len(askers)} ({', '.join(askers)})")

    # Open help requests / blockers raised — the meeting should unblock them.
    need_help = [
        _person(e.get("name"), roster_firsts)
        for e in participated
        if _has_text(e.get("needs_help")) or _real_count(e.get("blockers"))
    ]
    if need_help:
        out.append(f"🆘 Нужна помощь / блокеры: {', '.join(need_help)}")

    if silent:
        out.append(f"Промолчали: {', '.join(silent)}")
    return out


def _improve_lines(
    data: Dict[str, Any],
    breakdown: List[Dict[str, Any]],
    prior_stats: List[Dict[str, Any]],
    roster_firsts: set,
) -> List[str]:
    """💡 Concrete, meeting-level fixes for the next planёрка (derived from gaps)."""
    tips: List[str] = []

    # Manager didn't review last planёрка's tasks.
    if _criterion_status(data, 5) not in (None, "выполнено"):
        streak = _conduct_streak(5, prior_stats)
        suffix = f" ({streak}-ю планёрку подряд)" if streak >= 2 else ""
        tips.append(f"Разобрать статус прошлых задач{suffix}")

    # Tasks set without a deadline → нельзя проконтролировать.
    actions = [t for t in data.get("action_items") or [] if _clean(t.get("text"))]
    no_deadline = sum(1 for t in actions if not _has_text(t.get("deadline")))
    if actions and no_deadline >= max(2, len(actions) // 3):
        tips.append(f"Проставлять задачам сроки ({no_deadline} из {len(actions)} без даты)")

    # Accountants who didn't voice a plan for the day.
    no_plan = [
        _person(e.get("name"), roster_firsts)
        for e in breakdown
        if e.get("participated") and _real_count(e.get("today_plan")) == 0
    ]
    if no_plan:
        tips.append(f"Попросить озвучивать план на день: {', '.join(no_plan)}")

    # People who keep staying silent.
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

    # Manager dominates the conversation.
    manager_pct = (data.get("talk_share") or {}).get("manager_pct")
    if manager_pct and manager_pct >= 70:
        tips.append(f"Дать бухгалтерам больше говорить (руководитель {int(manager_pct)}%)")

    # No praise at all — motivation matters.
    if _criterion_status(data, 4) == "не выполнено":
        tips.append("Отметить хорошую работу кого-то из команды")

    if not tips:
        return []
    return ["💡 УЛУЧШИТЬ НА СЛЕДУЮЩЕЙ"] + [f"– {tip}" for tip in tips]


def _analytics_block(
    data: Dict[str, Any],
    prior_stats: Optional[List[Dict[str, Any]]],
    roster_firsts: set,
    manager: str = "",
) -> List[str]:
    """Compact meeting-effectiveness analytics (separate Telegram message).

    Its only goal is to make the planёрка itself run better, so it shows three
    things and nothing else: how the manager facilitates the meeting, whether
    the accountants set themselves tasks (and how concretely), and concrete
    fixes for next time. Workload-per-client and company-level problems were
    dropped on purpose — they are about running the company, not the meeting.

    Every fact is the AI's (from the transcript); every NUMBER (counts, streaks,
    trends) is computed here. Sections with no data are dropped.
    """
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
                out.append("")  # one blank line between sections
            out += section
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
