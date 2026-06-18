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
delivered as a SEPARATE Telegram message) is deliberately compact and dense —
leadership asked for «less text, more info». It leads with per-accountant
workload & engagement and uses tight, scannable lines:

- engagement: one line — «Высказались N/M · говорили X% рук. / Y% бух.»; a
  second «Молчали: …» line names anyone silent (with their miss-streak);
- «👥 НАГРУЗКА · клиенты / задачи»: one short «Имя K / P» line per accountant
  (K clients, P planned tasks), heaviest-load first, with a ↗/↘ when the client
  load changed vs. last time and ⛔/🆘 flags; people with no load are skipped;
- «✅ ЗАДАЧИ»: how many tasks were set today (per assignee on one line) and the
  fair completion of last planёрка's tasks — a per-person one-liner when they
  were reviewed, or just the count + ⚠️ when nothing was;
- «📈 ТРЕНДЫ»: a single line — meeting score / manager talk-share / completion
  over recent stand-ups (each as «a→b→c» with a trend mark);
- «🔁 ПОВТОРЯЕТСЯ»: recurring cross-meeting problems, severity-sorted, one line.

Every fact is the AI's (from the transcript); every NUMBER (counts, percents,
trends, streaks) is computed here, so the analytics can never be invented.
Sections with no data are dropped, so a thin meeting yields a short message.
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
    prior_stats: Optional[List[Dict[str, Any]]] = None,
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


def _dd_mm(iso_date: Any) -> str:
    text = _clean(iso_date)
    parts = text.split("-")
    return f"{parts[2]}.{parts[1]}" if len(parts) == 3 else text


def _fair_pct(done: int, partial: int, assessed: int) -> Optional[int]:
    """Fair completion percent: «частично» is half a point, only DISCUSSED
    tasks count (a task nobody asked about must not lower anyone's score)."""
    if not assessed:
        return None
    return round(100 * (done + 0.5 * partial) / assessed)


def _entry_pct(entry: Dict[str, Any]) -> Optional[int]:
    assessed = entry.get("tasks_assessed", entry.get("tasks_total") or 0)
    return _fair_pct(
        entry.get("tasks_done") or 0, entry.get("tasks_partial") or 0, assessed
    )


def _counts(done: int, partial: int, failed: int) -> str:
    parts = []
    if done:
        parts.append(f"✅ {done}")
    if partial:
        parts.append(f"🟡 {partial}")
    if failed:
        parts.append(f"❌ {failed}")
    return ", ".join(parts)


def _ru_plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural form for ``n`` (1 клиент / 2 клиента / 5 клиентов)."""
    n = abs(int(n))
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


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


def _prior_workload(prior_stats: List[Dict[str, Any]], name: str, roster_firsts: set) -> Optional[int]:
    """Most recent prior client-load for ``name`` (None when never recorded)."""
    for entry in reversed(prior_stats):
        for raw, count in (entry.get("workload") or {}).items():
            if _person(raw, roster_firsts) == name:
                return count
    return None


def _arrow(cur: float, prev: float) -> str:
    """Compact trend mark for inline use (no leading space): ↗ / ↘ / ''."""
    if cur > prev:
        return "↗"
    if cur < prev:
        return "↘"
    return ""


def _today_completion(statuses: List[Dict[str, Any]]) -> Optional[int]:
    """Fair completion percent of last planёрка's tasks (None when none discussed)."""
    if not statuses:
        return None
    st = lambda t: _clean(t.get("status")).lower()  # noqa: E731
    done = sum(1 for s in statuses if st(s) == "выполнено")
    partial = sum(1 for s in statuses if st(s) == "частично")
    unmentioned = sum(1 for s in statuses if st(s) == "не упоминалось")
    return _fair_pct(done, partial, len(statuses) - unmentioned)


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


def _engagement_lines(
    data: Dict[str, Any],
    breakdown: List[Dict[str, Any]],
    prior_stats: List[Dict[str, Any]],
    roster_firsts: set,
) -> List[str]:
    """One/two lines: who spoke, the talk balance, and who stayed silent."""
    if not breakdown:
        return []
    participated = [e for e in breakdown if e.get("participated")]
    talk = data.get("talk_share") or {}
    manager_pct, accountants_pct = talk.get("manager_pct"), talk.get("accountants_pct")
    head = f"Высказались {len(participated)}/{len(breakdown)}"
    if manager_pct or accountants_pct:
        head += f" · говорили {manager_pct or 0}% рук. / {accountants_pct or 0}% бух."
    out = [head]

    silent = [e for e in breakdown if not e.get("participated")]
    if silent:
        misses, total = _attendance_misses(data, prior_stats, roster_firsts)
        names = []
        for entry in silent:
            name = _person(entry.get("name"), roster_firsts)
            streak = misses.get(name, 0)
            # «N-ю подряд» only when we actually have a window to judge it.
            names.append(f"{name} ({streak}-ю подряд)" if total >= 2 and streak >= 2 else name)
        out.append("Молчали: " + ", ".join(names))
    return out


def _workload_lines(
    breakdown: List[Dict[str, Any]],
    prior_stats: List[Dict[str, Any]],
    roster_firsts: set,
) -> List[str]:
    """👥 Compact «name clients / tasks» list, heaviest client load first.

    People who spoke but carried no clients and no tasks are skipped — they add
    no workload signal. A ↗/↘ marks a change in client load vs. last time; ⛔/🆘
    flag a blocker or a help request. Counts come straight from the transcript.
    """
    rows = []
    for entry in breakdown:
        if not entry.get("participated"):
            continue
        cases = _real_count(entry.get("cases"))
        plan = _real_count(entry.get("today_plan"))
        if cases == 0 and plan == 0:
            continue
        rows.append(
            {
                "name": _person(entry.get("name"), roster_firsts),
                "cases": cases,
                "plan": plan,
                "blockers": _real_count(entry.get("blockers")),
                "needs_help": _has_text(entry.get("needs_help")),
            }
        )
    if not rows:
        return []
    rows.sort(key=lambda r: (-r["cases"], -r["plan"], r["name"]))

    out = ["👥 НАГРУЗКА · клиенты / задачи"]
    for r in rows:
        cell = f"{r['cases']}"
        prev = _prior_workload(prior_stats, r["name"], roster_firsts)
        if prev is not None and prev != r["cases"]:
            cell += _arrow(r["cases"], prev)
        line = f"{r['name']} {cell} / {r['plan']}"
        if r["blockers"]:
            line += " ⛔"
        if r["needs_help"]:
            line += " 🆘"
        out.append(line)
    return out


def _tasks_lines(
    data: Dict[str, Any],
    statuses: List[Dict[str, Any]],
    prior_stats: List[Dict[str, Any]],
    roster_firsts: set,
) -> List[str]:
    """✅ Tasks set today + how last planёрка's tasks are progressing (compact)."""
    body: List[str] = []
    actions = [t for t in data.get("action_items") or [] if _clean(t.get("text"))]
    if actions:
        per: Dict[str, int] = {}
        order: List[str] = []
        for item in actions:
            kind, names = _classify(item.get("assignee"))
            who = _person(names[0], roster_firsts) if kind == "items" else "Не указано"
            if who not in per:
                per[who] = 0
                order.append(who)
            per[who] += 1
        detail = ", ".join(f"{who} {per[who]}" for who in order)
        body.append(
            f"Поставлено сегодня: {len(actions)} "
            f"{_ru_plural(len(actions), 'задача', 'задачи', 'задач')} ({detail})"
        )

    if statuses:
        st = lambda t: _clean(t.get("status")).lower()  # noqa: E731
        unmentioned = sum(1 for s in statuses if st(s) == "не упоминалось")
        today_pct = _today_completion(statuses)
        if today_pct is None:
            prev_date = ""
            if prior_stats and prior_stats[-1].get("date"):
                prev_date = f" ({_dd_mm(prior_stats[-1]['date'])})"
            body.append(
                f"С прошлой планёрки{prev_date} не разобрали {unmentioned} "
                f"{_ru_plural(unmentioned, 'задачу', 'задачи', 'задач')} ⚠️"
            )
        else:
            done = sum(1 for s in statuses if st(s) == "выполнено")
            partial = sum(1 for s in statuses if st(s) == "частично")
            assessed = len(statuses) - unmentioned
            failed = assessed - done - partial
            head = f"С прошлой планёрки: {today_pct}% ({_counts(done, partial, failed)} из {assessed})"
            if unmentioned:
                head += f", ❓ {unmentioned} не разобрали"
            body.append(head)
            # Per-person, one compact line; people whose tasks weren't discussed
            # are skipped (no score, no noise).
            grouped: Dict[str, List[Dict[str, Any]]] = {}
            order = []
            for s in statuses:
                kind, names = _classify(s.get("assignee"))
                who = _person(names[0], roster_firsts) if kind == "items" else "Не указано"
                if who not in grouped:
                    grouped[who] = []
                    order.append(who)
                grouped[who].append(s)
            for who in order:
                tasks = grouped[who]
                a_done = sum(1 for t in tasks if st(t) == "выполнено")
                a_partial = sum(1 for t in tasks if st(t) == "частично")
                a_assessed = len(tasks) - sum(1 for t in tasks if st(t) == "не упоминалось")
                pct = _fair_pct(a_done, a_partial, a_assessed)
                if pct is None:
                    continue
                body.append(
                    f"  {who} {pct}% "
                    f"({_counts(a_done, a_partial, a_assessed - a_done - a_partial)})"
                )

    if not body:
        return []
    return ["✅ ЗАДАЧИ"] + body


def _trends_lines(
    data: Dict[str, Any],
    statuses: List[Dict[str, Any]],
    prior_stats: List[Dict[str, Any]],
) -> List[str]:
    """📈 One dense line: score / manager talk-share / completion over time."""
    parts: List[str] = []
    score = (data.get("effectiveness") or {}).get("score")
    prior_scores = [s.get("score") for s in prior_stats if s.get("score")]
    if score and prior_scores:
        chain = "→".join(str(int(s)) for s in prior_scores[-2:] + [score])
        parts.append(f"Оценка {chain}{_arrow(score, prior_scores[-1])}")

    today_talk = (data.get("talk_share") or {}).get("manager_pct")
    prior_talk = [e.get("manager_pct") for e in prior_stats if e.get("manager_pct")]
    if today_talk and prior_talk:
        parts.append(
            f"Доля рук. {int(prior_talk[-1])}→{int(today_talk)}%"
            f"{_arrow(today_talk, prior_talk[-1])}"
        )

    history = [s for s in prior_stats if _entry_pct(s) is not None and s.get("date")]
    today_pct = _today_completion(statuses)
    if history and today_pct is not None:
        pcts = [_entry_pct(e) for e in history] + [today_pct]
        chain = "→".join(str(p) for p in pcts[-3:])
        parts.append(f"Выполнение {chain}%{_arrow(today_pct, pcts[-2])}")

    if not parts:
        return []
    return ["📈 ТРЕНДЫ", " · ".join(parts)]


def _recurring_lines(data: Dict[str, Any]) -> List[str]:
    """🔁 Cross-meeting recurring problems, severity-sorted, one line each."""
    points = [
        p for p in data.get("attention_points") or []
        if isinstance(p, dict) and _has_text(p.get("point")) and p.get("recurring")
    ]
    if not points:
        return []
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    points.sort(key=lambda p: severity_rank.get(_clean(p.get("severity")).lower(), 1))
    out = ["🔁 ПОВТОРЯЕТСЯ"]
    for p in points:
        icon = _SEVERITY_ICONS.get(_clean(p.get("severity")).lower(), "🟡")
        out.append(f"{icon} {_clean(p.get('point'))}")
    return out


def _analytics_block(
    data: Dict[str, Any],
    prior_stats: Optional[List[Dict[str, Any]]],
    roster_firsts: set,
    manager: str = "",
) -> List[str]:
    """Compact analytics (delivered as a separate Telegram message): workload &
    engagement per accountant, tasks, trends and recurring problems.

    Every fact (who carried which clients, task statuses) is the AI's; every
    NUMBER (counts, percents, trends) is computed here, so it can't be invented.
    Sections with no data are dropped, so a thin meeting yields a short message.
    """
    statuses = [
        s for s in data.get("previous_tasks_status") or [] if _clean(s.get("task"))
    ]
    prior_stats = prior_stats or []
    history = [s for s in prior_stats if _entry_pct(s) is not None and s.get("date")]
    breakdown = _roster_breakdown(data, roster_firsts, manager)
    if not statuses and not history and not breakdown:
        return []

    sections = [
        _engagement_lines(data, breakdown, prior_stats, roster_firsts),
        _workload_lines(breakdown, prior_stats, roster_firsts),
        _tasks_lines(data, statuses, prior_stats, roster_firsts),
        _trends_lines(data, statuses, prior_stats),
        _recurring_lines(data),
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
