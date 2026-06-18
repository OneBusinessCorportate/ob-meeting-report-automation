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
delivered as a SEPARATE Telegram message) was redesigned to lead with what
leadership actually asked for — per-accountant workload & engagement — instead
of only task dynamics:

- «👥 ЗАГРУЗКА И ВОВЛЕЧЁННОСТЬ»: who spoke vs who stayed silent, talk balance,
  and one line per accountant — how many clients/cases they carried (with a
  trend arrow vs their previous load) and how many tasks they planned, sorted
  heaviest-load first, plus ⛔ blockers / 🆘 needs-help / ❓ question flags;
- «Новых задач поставлено сегодня»: count of fresh action items, per assignee;
- task dynamics: each previous task graded ✅/🟡/❌/❓ with a fair per-accountant
  completion rate; when nothing was reviewed, the untouched tasks are listed
  once (compactly) instead of a per-person «не обсуждались» wall;
- trends (completion %, attendance, talk-share, meeting score) over recent
  stand-ups; «🔁 ПОВТОРЯЮЩИЕСЯ ПРОБЛЕМЫ» from recurring attention points;
- «🏆 ПРОГРЕСС» / «❗ СИГНАЛЫ» — wins first, then softly-worded signals.

Every fact is the AI's (from the transcript); every NUMBER (counts, percents,
trends, attendance) is computed here, so the analytics can never be invented.
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
        lines += _analytics_block(data, prior_stats, roster_firsts, manager)

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
    header: List[str] = ["📊 Аналитика планёрки"]
    if meeting_date:
        header.append(meeting_date)
    header.append("")
    return _finalize(header + block)


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


_PREV_TASK_ICONS = {
    "выполнено": "✅",
    "частично": "🟡",
    "не выполнено": "❌",
    "не упоминалось": "❓",
}


def _dd_mm(iso_date: Any) -> str:
    text = _clean(iso_date)
    parts = text.split("-")
    return f"{parts[2]}.{parts[1]}" if len(parts) == 3 else text


def _trend_arrow(current: float, previous: float) -> str:
    if current > previous:
        return " ↗️"
    if current < previous:
        return " ↘️"
    return " ➡️"


def _fair_pct(done: int, partial: int, assessed: int) -> Optional[int]:
    """Fair completion percent: «частично» is half a point, only DISCUSSED
    tasks count (a task nobody asked about must not lower anyone's score)."""
    if not assessed:
        return None
    return round(100 * (done + 0.5 * partial) / assessed)


def _bucket_pct(bucket: Dict[str, Any]) -> Optional[int]:
    assessed = bucket.get("assessed", bucket.get("total") or 0)
    return _fair_pct(bucket.get("done") or 0, bucket.get("partial") or 0, assessed)


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


def _assignee_history(
    prior_stats: List[Dict[str, Any]], name: str, roster_firsts: set
) -> List[Dict[str, int]]:
    """Per-meeting stats for one person across prior stand-ups (oldest first)."""
    history = []
    for entry in prior_stats:
        for raw, bucket in (entry.get("per_assignee") or {}).items():
            if _person(raw, roster_firsts) == name and _bucket_pct(bucket) is not None:
                history.append(bucket)
                break
    return history


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


def _workload_section(
    data: Dict[str, Any],
    prior_stats: List[Dict[str, Any]],
    roster_firsts: set,
    manager: str,
    signals: List[str],
) -> List[str]:
    """👥 Загрузка и вовлечённость: client load + involvement per accountant.

    Each line shows how many clients/cases the person carried and how many
    tasks they planned today, sorted heaviest-load first — so it is obvious at
    a glance who carries the most and who said nothing. All counts come from the
    transcript-grounded ``participant_breakdown``; nothing is invented here.
    """
    entries = _roster_breakdown(data, roster_firsts, manager)
    if not entries:
        return []

    participated = [e for e in entries if e.get("participated")]
    silent = [_person(e.get("name"), roster_firsts) for e in entries if not e.get("participated")]

    out: List[str] = ["👥 ЗАГРУЗКА И ВОВЛЕЧЁННОСТЬ"]
    line = f"Высказались: {len(participated)} из {len(entries)}"
    if silent:
        line += f" (молчали: {', '.join(silent)})"
    out.append(line)

    # Talk balance, when the model estimated it (engagement signal).
    talk = data.get("talk_share") or {}
    manager_pct, accountants_pct = talk.get("manager_pct"), talk.get("accountants_pct")
    if manager_pct or accountants_pct:
        out.append(
            f"Кто сколько говорил: {manager_pct or 0}% руководитель, "
            f"{accountants_pct or 0}% бухгалтеры"
        )

    rows = []
    for entry in participated:
        name = _person(entry.get("name"), roster_firsts)
        cases = _real_count(entry.get("cases"))
        plan = _real_count(entry.get("today_plan"))
        blockers = _real_count(entry.get("blockers"))
        rows.append(
            {
                "name": name,
                "cases": cases,
                "plan": plan,
                "blockers": blockers,
                "needs_help": _has_text(entry.get("needs_help")),
                "question": _has_text(entry.get("question_to_manager")),
            }
        )
    # Heaviest client load first; ties broken by planned tasks, then by name.
    rows.sort(key=lambda r: (-r["cases"], -r["plan"], r["name"]))

    for r in rows:
        parts = [f"{r['cases']} {_ru_plural(r['cases'], 'клиент', 'клиента', 'клиентов')}"]
        prev = _prior_workload(prior_stats, r["name"], roster_firsts)
        if prev is not None and prev != r["cases"]:
            parts[0] += _trend_arrow(r["cases"], prev)
        parts.append(f"{r['plan']} {_ru_plural(r['plan'], 'задача', 'задачи', 'задач')} на сегодня")
        if r["blockers"]:
            parts.append(f"⛔ {r['blockers']} {_ru_plural(r['blockers'], 'блокер', 'блокера', 'блокеров')}")
        if r["needs_help"]:
            parts.append("🆘 нужна помощь")
        if r["question"]:
            parts.append("❓ вопрос руководителю")
        out.append(f"👤 {r['name']} — " + ", ".join(parts))

    # Workload concentration: flag a clear outlier so it is easy to rebalance.
    loaded = [r for r in rows if r["cases"] > 0]
    if len(loaded) >= 3:
        top = loaded[0]
        others_avg = sum(r["cases"] for r in loaded[1:]) / (len(loaded) - 1)
        if top["cases"] >= 5 and top["cases"] >= 2 * others_avg:
            signals.append(
                f"{top['name']} ведёт больше всех клиентов ({top['cases']}) — "
                "стоит проверить загрузку и при необходимости перераспределить."
            )

    out.append("")
    return out


def _new_tasks_section(data: Dict[str, Any], roster_firsts: set) -> List[str]:
    """✅ how many fresh tasks were set today and to whom (from action_items)."""
    items = [t for t in data.get("action_items") or [] if _clean(t.get("text"))]
    if not items:
        return []
    per_person: Dict[str, int] = {}
    order: List[str] = []
    for item in items:
        kind, names = _classify(item.get("assignee"))
        who = _person(names[0], roster_firsts) if kind == "items" else "Не указано"
        if who not in per_person:
            per_person[who] = 0
            order.append(who)
        per_person[who] += 1
    total = len(items)
    out = [f"Новых задач поставлено сегодня: {total}"]
    breakdown = "; ".join(f"{who} — {per_person[who]}" for who in order)
    out.append(f"  {breakdown}")
    out.append("")
    return out


def _recurring_section(data: Dict[str, Any]) -> List[str]:
    """🔁 Cross-meeting recurring problems (from attention_points.recurring)."""
    points = [
        p for p in data.get("attention_points") or []
        if isinstance(p, dict) and _has_text(p.get("point")) and p.get("recurring")
    ]
    if not points:
        return []
    severity_rank = {"high": 0, "medium": 1, "low": 2}
    points.sort(key=lambda p: severity_rank.get(_clean(p.get("severity")).lower(), 1))
    out = ["🔁 ПОВТОРЯЮЩИЕСЯ ПРОБЛЕМЫ"]
    for p in points:
        icon = _SEVERITY_ICONS.get(_clean(p.get("severity")).lower(), "🟡")
        out.append(f"{icon} {_clean(p.get('point'))}")
        follow = _clean(p.get("suggested_follow_up"))
        if follow and follow.lower() not in _MISSING_WORDS:
            out.append(f"  Что сделать: {follow}")
    out.append("")
    return out


def _analytics_block(
    data: Dict[str, Any],
    prior_stats: Optional[List[Dict[str, Any]]],
    roster_firsts: set,
    manager: str = "",
) -> List[str]:
    """Аналитика (отдельным сообщением после отчёта): загрузка, вовлечённость и
    задачи по каждому бухгалтеру, плюс динамика и повторяющиеся проблемы.

    Все факты (кто что вёл, статусы задач) берёт ИИ из расшифровки, но ВСЕ цифры
    (счётчики, проценты, тренды, посещаемость, сигналы) считает этот скрипт —
    они не могут быть выдуманы.
    """
    statuses = [
        s for s in data.get("previous_tasks_status") or [] if _clean(s.get("task"))
    ]
    prior_stats = prior_stats or []
    history = [
        s for s in prior_stats if _entry_pct(s) is not None and s.get("date")
    ]
    breakdown = _roster_breakdown(data, roster_firsts, manager)
    if not statuses and not history and not breakdown:
        return []

    out = ["📈 АНАЛИТИКА"]
    progress: List[str] = []
    signals: List[str] = []

    # 1) Workload & engagement per accountant (the headline of the analytics).
    out += _workload_section(data, prior_stats, roster_firsts, manager, signals)

    def _status_of(task: Dict[str, Any]) -> str:
        return _clean(task.get("status")).lower()

    # 2) Tasks: what was newly set today + how last planёрка's tasks are going.
    out += _new_tasks_section(data, roster_firsts)

    today_pct: Optional[int] = None
    if statuses:
        done = sum(1 for s in statuses if _status_of(s) == "выполнено")
        partial = sum(1 for s in statuses if _status_of(s) == "частично")
        unmentioned = sum(1 for s in statuses if _status_of(s) == "не упоминалось")
        assessed = len(statuses) - unmentioned
        failed = assessed - done - partial
        today_pct = _fair_pct(done, partial, assessed)
        if today_pct is None:
            # Nothing from the last planёрка was touched today: don't spam a
            # per-person breakdown of «не обсуждались» — list the pending tasks
            # once, compactly, so leadership sees exactly what slipped.
            prev_date = ""
            if prior_stats and prior_stats[-1].get("date"):
                prev_date = f" ({_dd_mm(prior_stats[-1]['date'])})"
            out.append(
                f"Задачи с прошлой планёрки{prev_date} сегодня не разбирали "
                f"(❓ {unmentioned}) — стоит свериться по ним:"
            )
            for status in statuses:
                kind, names = _classify(status.get("assignee"))
                who = _person(names[0], roster_firsts) if kind == "items" else "Не указано"
                out.append(f"  ❓ {_clean(status.get('task'))} ({who})")
            out.append("")
            signals.append(
                f"Ни одна из {unmentioned} задач прошлой планёрки сегодня не "
                "прозвучала — пройдитесь по ним на следующей встрече."
            )
        else:
            line = (
                f"Задачи с прошлой планёрки: {today_pct}% "
                f"({_counts(done, partial, failed)} из {assessed} обсуждённых"
            )
            line += f"; ❓ {unmentioned} не обсуждались)" if unmentioned else ")"
            out.append(line)
            out.append("")

            grouped: Dict[str, List[Dict[str, Any]]] = {}
            order: List[str] = []
            for status in statuses:
                kind, names = _classify(status.get("assignee"))
                assignee = _person(names[0], roster_firsts) if kind == "items" else "Не указано"
                if assignee not in grouped:
                    grouped[assignee] = []
                    order.append(assignee)
                grouped[assignee].append(status)
            for assignee in order:
                tasks = grouped[assignee]
                a_done = sum(1 for t in tasks if _status_of(t) == "выполнено")
                a_partial = sum(1 for t in tasks if _status_of(t) == "частично")
                a_unmentioned = sum(1 for t in tasks if _status_of(t) == "не упоминалось")
                a_assessed = len(tasks) - a_unmentioned
                pct = _fair_pct(a_done, a_partial, a_assessed)
                if pct is None:
                    # No task was discussed: this is NOT the person's failure —
                    # show it neutrally, without a score.
                    header = f"👤 {assignee}: задачи на встрече не обсуждались"
                else:
                    header = (
                        f"👤 {assignee}: {pct}% "
                        f"({_counts(a_done, a_partial, a_assessed - a_done - a_partial)}"
                        f" из {a_assessed})"
                    )
                    person_history = _assignee_history(prior_stats, assignee, roster_firsts)
                    if person_history:
                        prev_pct = _bucket_pct(person_history[-1])
                        if prev_pct is not None:
                            header += (
                                f", прошлая планёрка {prev_pct}%"
                                f"{_trend_arrow(pct, prev_pct)}"
                            )
                            if pct > prev_pct:
                                progress.append(
                                    f"{assignee}: рост с {prev_pct}% до {pct}% 📈"
                                )
                            elif pct == 0 and prev_pct == 0:
                                signals.append(
                                    f"У {assignee} вторую планёрку подряд не получается "
                                    "закрыть задачи — возможно, нужна помощь или "
                                    "пересмотр приоритетов."
                                )
                    if pct == 100:
                        progress.append(f"{assignee}: все обсуждённые задачи закрыты 👏")
                out.append(header)
                for task in tasks:
                    icon = _PREV_TASK_ICONS.get(_status_of(task), "❓")
                    line = f"  {icon} {_clean(task.get('task'))}"
                    evidence = _clean(task.get("evidence"))
                    if evidence and evidence.lower() not in _MISSING_WORDS:
                        line += f" — {evidence}"
                    out.append(line)
                out.append("")

            if unmentioned:
                signals.append(
                    f"Задачи без статуса (❓ {unmentioned}) — о них никто не спросил "
                    "на встрече; стоит пройтись по ним на следующей планёрке."
                )

    # Completion-rate trend across the recent stand-ups (script-computed).
    if history and (today_pct is not None or len(history) >= 2):
        out.append("Динамика выполнения задач:")
        pcts = [_entry_pct(e) for e in history]
        for entry, pct in zip(history, pcts):
            out.append(f"  {_dd_mm(entry['date'])}: {pct}%")
        if today_pct is not None:
            out.append(f"  сегодня: {today_pct}%{_trend_arrow(today_pct, pcts[-1])}")
            if today_pct > pcts[-1]:
                progress.append(
                    f"Команда: выполнение задач выросло с {pcts[-1]}% до {today_pct}%"
                )
            pcts.append(today_pct)
        out.append(f"  среднее за {len(pcts)} планёрки(ок): {round(sum(pcts) / len(pcts))}%")
        out.append("")

    # Attendance over the recent stand-ups (+ today), misses only.
    attendance = [e for e in prior_stats if e.get("has_participation")]
    today_breakdown = [
        p for p in data.get("participant_breakdown") or [] if _first_name(p.get("name"))
    ]
    total_meetings = len(attendance) + (1 if today_breakdown else 0)
    if total_meetings >= 2:
        def _counts_for_roster(name: str) -> bool:
            # Only people from the current roster: ex-members of analyses
            # stored with an older roster must not pollute the stats.
            return not roster_firsts or name.split()[0].lower() in roster_firsts

        misses: Dict[str, int] = {}
        for entry in attendance:
            for raw in entry.get("absent") or []:
                name = _person(raw, roster_firsts)
                if _counts_for_roster(name):
                    misses[name] = misses.get(name, 0) + 1
        for participant in today_breakdown:
            if not participant.get("participated"):
                name = _person(participant.get("name"), roster_firsts)
                if _counts_for_roster(name):
                    misses[name] = misses.get(name, 0) + 1
        if misses:
            out.append(f"Пропуски за последние {total_meetings} планёрки(ок):")
            for name, count in sorted(misses.items(), key=lambda kv: -kv[1]):
                out.append(f"  {name}: {count} из {total_meetings}")
                if count == total_meetings:
                    signals.append(
                        f"{name} не участвует в планёрках ({count} из "
                        f"{total_meetings}) — стоит уточнить причину (возможно, "
                        "отпуск или другой график)."
                    )
            out.append("")

    # Manager talk-share trend (how much of the meeting the manager speaks).
    today_talk = (data.get("talk_share") or {}).get("manager_pct")
    prior_talk = [e.get("manager_pct") for e in prior_stats if e.get("manager_pct")]
    if today_talk and prior_talk:
        out.append(
            f"Доля руководителя в разговоре: {int(prior_talk[-1])}% → "
            f"{int(today_talk)}%{_trend_arrow(float(today_talk), float(prior_talk[-1]))}"
        )
        out.append("")

    # Meeting-score trend: the recent scores plus today's, left to right.
    score = (data.get("effectiveness") or {}).get("score")
    prior_scores = [s.get("score") for s in prior_stats if s.get("score")]
    if score and prior_scores:
        chain = " → ".join(str(int(s)) for s in prior_scores[-2:] + [score])
        out.append(
            f"Оценка встречи: {chain} из 10"
            f"{_trend_arrow(float(score), float(prior_scores[-1]))}"
        )
        out.append("")

    # Recurring, cross-meeting problems the leadership should not lose sight of.
    out += _recurring_section(data)

    # Good news first: people should see progress, not only problems.
    if progress:
        out.append("🏆 ПРОГРЕСС")
        out.extend(f"  – {item}" for item in progress)
        out.append("")

    if signals:
        out.append("❗ СИГНАЛЫ")
        out.extend(f"  – {signal}" for signal in signals)
        out.append("")

    return out if len(out) > 1 else []


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
