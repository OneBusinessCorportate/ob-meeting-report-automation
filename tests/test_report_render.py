"""Tests for the deterministic Telegram report renderer.

The structure is built by script (rigid), the AI only supplies values — these
tests pin down the approved layout so it cannot drift again.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_pipeline.report_render import (  # noqa: E402
    render_analytics_message,
    render_telegram_report,
)

ROSTER = [
    {"name": "Эмилия Аванесян", "role": "руководитель"},
    {"name": "Стелла", "role": "бухгалтер"},
    {"name": "Оля", "role": "бухгалтер"},
    {"name": "Тагуи", "role": "бухгалтер"},
]

FULL_DATA = {
    "effectiveness": {
        "score": 7,
        "max_score": 10,
        "verdict": "Рабочая встреча, но без похвалы.",
        "criteria": [
            {"criterion": "Все сотрудники высказались", "status": "частично"},
            {"criterion": "Руководитель задавала вопросы", "status": "выполнено"},
            {"criterion": "Руководитель поставила задачи", "status": "выполнено"},
            {"criterion": "Руководитель поделилась новостями", "status": "выполнено"},
            {"criterion": "Руководитель кого-то похвалила", "status": "не выполнено"},
            {"criterion": "Руководитель спросила про прошлые задачи", "status": "не выполнено"},
        ],
    },
    "talk_share": {"manager_pct": 70, "accountants_pct": 30},
    "participant_breakdown": [
        {"name": "Эмилия", "participated": True},
        {
            "name": "Стелла",
            "participated": True,
            "yesterday": "Указала в чате двух клиентов с пятимесячной задолженностью.",
            "today_plan": [],
            "blockers": ["нет"],
        },
        {
            "name": "Оля",
            "participated": True,
            "yesterday": "Не указано",
            "today_plan": ["Регистрация зарплат по клиенту Альфа", "Заключение договора с Бета"],
            "blockers": ["Список задолженностей не обновляется"],
            "needs_help": "Не указано",
            "question_to_manager": "",
        },
        {"name": "Тагуи", "participated": False},
    ],
    "manager_reactions": [
        {"to_whom": "Общее", "type": "новость", "text": "Я завтра буду в Арцахе."},
        {"to_whom": "Оля", "type": "задача", "text": "Отправлю платежное поручение после митинга."},
        {"to_whom": "Оля", "type": "рекомендация",
         "text": "Просто предупреди, что если не присылают данные, то у нас нет информации."},
    ],
    "problems_risks": [
        {"text": "Два клиента не платят дольше пяти месяцев.", "severity": "high",
         "decision": "Эмилия уточнит ситуацию и решит, работаем ли дальше. В Задачи добавили.",
         "owner": "Оля", "deadline": "Не указано", "how_to_track": "Список задолженностей"},
        {"text": "Документы от клиента Гамма задерживаются.", "severity": "low",
         "decision": "Не указано",
         "owner": "Не указано", "deadline": "Не указано", "how_to_track": "Не указано"},
    ],
    "action_items": [
        {"text": "Уточнить ситуацию с клиентами с долгом более пяти месяцев.",
         "assignee": "Эмилия", "deadline": "Не указано"},
        {"text": "Подготовить договор с Бета", "assignee": "Оля", "deadline": "2026-06-15"},
    ],
    "open_questions": ["Работаем ли дальше с должниками?"],
    "late_start": True,
    "late_start_minutes": 5,
    "summary": "не должно попасть в отчёт",
    "meeting_mood": {"overall": "продуктивное"},
    "attention_points": [{"point": "не должно попасть в отчёт"}],
}


def _render():
    return render_telegram_report(
        FULL_DATA, meeting_date="2026-06-10", time_range="09:30–10:00", team_roster=ROSTER
    )


def test_header_and_score_block():
    text = _render()
    lines = text.splitlines()
    assert lines[0] == "📋 Планёрка бухгалтерии"
    assert lines[1] == "2026-06-10, 09:30–10:00"  # no 📅 emoji
    # Score line without the 📊 emoji and without a verdict sentence;
    # the checklist follows directly — no «Что было на встрече:» label.
    assert "ОЦЕНКА ВСТРЕЧИ: 7 из 10" in text
    assert "📊" not in text
    assert "Рабочая встреча, но без похвалы." not in text
    assert "Что было на встрече" not in text
    # Checklist says «Руководитель», partial = yellow circle, 6 items incl. followup.
    assert "  🟡 Все высказались" in text
    assert "  ✅ Руководитель задавала вопросы" in text
    assert "  ❌ Руководитель кого-то похвалила" in text
    assert "  ❌ Руководитель спросила про прошлые задачи" in text
    assert "Эмилия задавала" not in text
    # Talk share is compact, separated by a blank line above, before the people.
    assert "\n\nКто сколько говорил: 70% руководитель, 30% бухгалтеры" in text
    assert text.index("Кто сколько говорил") < text.index("👤")


def test_attendance_lines_dropped_late_start_kept():
    text = _render()
    # «Кто был» / «Не было» dropped: per-person blocks already show attendance.
    assert "Кто был" not in text
    assert "Не было" not in text
    assert "🕐 Опоздание: 5 мин" in text


def test_accountant_blocks_cross_and_dash_semantics():
    text = _render()
    # Стелла: voiced "no blockers" → dash; plan not voiced → cross.
    stella = text.split("👤 Стелла")[1].split("👤")[0]
    assert "Отчёт за вчера: Указала в чате двух клиентов" in stella
    assert "План на сегодня: ❌" in stella
    assert "Блокеры: –" in stella
    # Оля: yesterday not voiced → cross; two plan items → list; real blocker shown.
    olya = text.split("👤 Оля")[1].split("👤")[0]
    assert "Отчёт за вчера: ❌" in olya
    assert "  – Регистрация зарплат по клиенту Альфа" in olya
    assert "Блокеры: Список задолженностей не обновляется" in olya
    assert "Нужна помощь" not in olya  # optional line omitted when empty
    # Тагуи did not participate: a single line, listed FIRST.
    assert "👤 Тагуи - не принимал(а) участия." in text
    assert text.index("👤 Тагуи") < text.index("👤 Стелла") < text.index("👤 Оля")
    # The manager has no personal accountant block.
    assert "👤 Эмилия\n" not in text


def test_manager_block_name_line_then_numbered_lines():
    text = _render()
    block = text.split("🧭 ЧТО СКАЗАЛА РУКОВОДИТЕЛЬ (ЭМИЛИЯ)")[1].split("⚠️")[0]
    # Name on its own line; single remark unnumbered, multiple remarks each
    # on their own numbered line.
    assert "Общее:\nЯ завтра буду в Арцахе." in block
    assert ("Оля:\n"
            "1. Отправлю платежное поручение после митинга.\n"
            "2. Просто предупреди, что если не присылают данные, "
            "то у нас нет информации.") in block
    assert "поручила" not in block
    assert "Спросила про прошлые задачи" not in block
    assert "Кто сколько говорил" not in block


def test_risks_icon_before_number_with_decision_line():
    text = _render()
    block = text.split("⚠️ РИСКИ И СИТУАЦИИ")[1].split("✅ ЗАДАЧИ")[0]
    # Severity icon goes BEFORE the number; no «Риск:» line.
    assert "🔴 1. Два клиента не платят дольше пяти месяцев." in block
    assert "🟢 2. Документы от клиента Гамма задерживаются." in block
    assert "Риск:" not in block
    # «Что решили» follows each situation; ❌ when no next step was discussed.
    assert ("Что решили: Эмилия уточнит ситуацию и решит, работаем ли дальше. "
            "В Задачи добавили.") in block
    assert "Что решили: ❌" in block
    # Ответственный/Срок/Как контролируем are no longer shown here.
    assert "Ответственный" not in block
    assert "Срок" not in block
    assert "Как контролируем" not in block
    assert "Ситуация" not in block
    assert "высокая" not in block and "Степень риска" not in block
    # Situations are separated by a blank line.
    assert "\n\n🟢 2. Документы" in block


def test_risks_sorted_critical_first():
    data = {
        "problems_risks": [
            {"text": "Мелкая задержка документов.", "severity": "low"},
            {"text": "Клиент не платит полгода.", "severity": "high"},
            {"text": "Список не обновляется.", "severity": "medium"},
            {"text": "Второй клиент не платит.", "severity": "high"},
        ]
    }
    text = render_telegram_report(data, meeting_date="2026-06-12", team_roster=ROSTER)
    # high -> medium -> low, stable order within the same severity.
    assert "🔴 1. Клиент не платит полгода." in text
    assert "🔴 2. Второй клиент не платит." in text
    assert "🟡 3. Список не обновляется." in text
    assert "🟢 4. Мелкая задержка документов." in text


def test_tasks_grouped_by_assignee():
    text = _render()
    block = text.split("✅ ЗАДАЧИ НА КОНТРОЛЕ")[1].split("❓")[0]
    assert ("👤 Эмилия:\n1. Уточнить ситуацию с клиентами с долгом более "
            "пяти месяцев. Срок: ❌") in block
    assert "👤 Оля:\n1. Подготовить договор с Бета. Срок: 2026-06-15" in block


def test_analytics_tasks_completion_and_trends():
    data = {
        "effectiveness": {"score": 7, "criteria": []},
        "previous_tasks_status": [
            {"task": "Отправить платежное поручение.", "assignee": "Оля Бухгалтер",
             "status": "выполнено", "evidence": "сказала, что отправила"},
            {"task": "Заключить договор с Бета.", "assignee": "Оля",
             "status": "не выполнено", "evidence": ""},
            {"task": "Проверить банковские коды.", "assignee": "Оля",
             "status": "частично", "evidence": ""},
            {"task": "Подготовить список клиентов с оборотом 200 млн.",
             "assignee": "Наира Мхитарян", "status": "не упоминалось", "evidence": ""},
        ],
        "open_questions": ["Вопрос?"],
    }
    prior_stats = [
        {"date": "2026-06-10", "score": 5, "tasks_done": 1, "tasks_total": 4,
         "per_assignee": {}, "has_participation": True,
         "absent": ["Аваг", "Артак"], "manager_pct": 80},
        {"date": "2026-06-11", "score": 6, "tasks_done": 0, "tasks_total": 3,
         "per_assignee": {}, "has_participation": True, "absent": ["Аваг"],
         "manager_pct": 75},
    ]
    roster = ROSTER + [
        {"name": "Наира Мхитарян", "role": "бухгалтер"},
        {"name": "Аваг", "role": "бухгалтер"},
        {"name": "Артак", "role": "бухгалтер"},
    ]
    data["talk_share"] = {"manager_pct": 70, "accountants_pct": 30}
    data["participant_breakdown"] = [
        {"name": "Аваг", "participated": False},
        {"name": "Оля", "participated": True, "cases": ["X"], "today_plan": ["y"]},
    ]
    text = render_telegram_report(
        data, meeting_date="2026-06-12", team_roster=roster, prior_stats=prior_stats
    )
    assert "📈 АНАЛИТИКА" in text
    # Analytics close the report (after the open questions).
    assert text.index("📈 АНАЛИТИКА") > text.index("❓ ОТКРЫТЫЕ ВОПРОСЫ")
    # Fair team score: «частично» = half a point, the unmentioned task is NOT
    # counted against anyone — 3 assessed: 1 done + 0.5 partial -> 50%.
    assert "С прошлой планёрки: 50% (✅ 1, 🟡 1, ❌ 1 из 3), ❓ 1 не разобрали" in text
    # Per-person, one compact line (no per-task wall); Наира's task wasn't
    # discussed, so she is omitted entirely.
    assert "  Оля 50% (✅ 1, 🟡 1, ❌ 1)" in text
    assert "Наира" not in text.split("✅ ЗАДАЧИ")[1].split("📈 ТРЕНДЫ")[0]
    # One dense trends line: score chain, manager talk-share, completion.
    assert "📈 ТРЕНДЫ" in text
    assert "Оценка 5→6→7↗" in text
    assert "Доля рук. 75→70%↘" in text
    assert "Выполнение 25→0→50%↗" in text


def test_analytics_block_skipped_without_data():
    # No participant breakdown, no previous-task statuses, no prior stats ->
    # there is nothing to analyze, so the block is dropped entirely.
    text = render_telegram_report(
        {"effectiveness": FULL_DATA["effectiveness"], "open_questions": ["Вопрос?"]},
        meeting_date="2026-06-10",
        team_roster=ROSTER,
    )
    assert "АНАЛИТИКА" not in text


def test_analytics_engagement_line_and_silent_streak():
    data = {
        "talk_share": {"manager_pct": 55, "accountants_pct": 45},
        "participant_breakdown": [
            {"name": "Оля", "participated": True, "cases": ["A"], "today_plan": []},
            {"name": "Стелла", "participated": True, "cases": ["A"], "today_plan": []},
            {"name": "Аваг", "participated": False},
        ],
    }
    prior = [{"date": "2026-06-11", "score": 6, "has_participation": True,
              "absent": ["Аваг"], "manager_pct": 60}]
    roster = ROSTER + [{"name": "Аваг", "role": "бухгалтер"}]
    text = render_analytics_message(
        data, meeting_date="2026-06-12", team_roster=roster, prior_stats=prior
    )
    # Compact title with dd.mm; one engagement line; silent person with streak.
    assert text.startswith("📊 Аналитика планёрки · 12.06")
    assert "Высказались 2/3 · говорили 55% рук. / 45% бух." in text
    assert "Молчали: Аваг (2-ю подряд)" in text  # absent prior + today


def test_analytics_workload_compact_sorted_with_plurals_and_trend():
    data = {
        "participant_breakdown": [
            {"name": "Оля", "participated": True,
             "cases": ["A", "B", "C", "D", "E", "F"], "today_plan": ["x"]},
            {"name": "Стелла", "participated": True, "cases": ["A"], "today_plan": []},
            {"name": "Тагуи", "participated": True, "cases": ["A", "B"],
             "today_plan": ["x", "y"], "blockers": ["завис банк"], "needs_help": "нужна рука"},
            {"name": "Эмилия", "participated": True, "cases": []},  # manager, excluded
            {"name": "Лилит", "participated": True, "cases": [], "today_plan": []},  # no load, skipped
        ],
    }
    prior = [{"date": "2026-06-11", "has_participation": True, "absent": [],
              "workload": {"Оля": 4, "Стелла": 1}, "manager_pct": 60}]
    text = render_analytics_message(
        data, meeting_date="2026-06-12", team_roster=ROSTER, prior_stats=prior
    )
    block = text.split("👥 НАГРУЗКА · клиенты / задачи")[1]
    # «name clients / tasks», heaviest first; ↗ when load grew; ⛔/🆘 flags.
    assert "Оля 6↗ / 1" in block
    assert "Тагуи 2 / 2 ⛔ 🆘" in block
    assert "Стелла 1 / 0" in block  # load unchanged (1 -> 1) -> no arrow
    assert "Эмилия" not in block  # manager excluded
    assert "Лилит" not in block   # spoke but no clients/tasks -> skipped
    assert text.index("Оля 6") < text.index("Тагуи 2") < text.index("Стелла 1")


def test_analytics_new_tasks_one_line():
    data = {
        "participant_breakdown": [{"name": "Оля", "participated": True, "cases": ["A"]}],
        "action_items": [
            {"text": "Задача 1", "assignee": "Оля"},
            {"text": "Задача 2", "assignee": "Оля"},
            {"text": "Задача 3", "assignee": "Наира Мхитарян"},
        ],
    }
    roster = ROSTER + [{"name": "Наира Мхитарян", "role": "бухгалтер"}]
    text = render_analytics_message(data, meeting_date="2026-06-12", team_roster=roster)
    assert "Поставлено сегодня: 3 задачи (Оля 2, Наира 1)" in text


def test_analytics_unmentioned_prev_tasks_one_line():
    data = {
        "participant_breakdown": [{"name": "Оля", "participated": True, "cases": ["A"]}],
        "previous_tasks_status": [
            {"task": "Закрыть Golden Trade", "assignee": "Лилит", "status": "не упоминалось"},
            {"task": "Письмо Армен Строй", "assignee": "Тагуи", "status": "не упоминалось"},
        ],
    }
    prior = [{"date": "2026-03-26", "score": 5, "has_participation": True, "absent": []}]
    roster = ROSTER + [{"name": "Лилит", "role": "бухгалтер"}]
    text = render_analytics_message(
        data, meeting_date="2026-06-12", team_roster=roster, prior_stats=prior
    )
    # Just the count + a ⚠️ marker — no per-task wall, no per-person spam.
    assert "С прошлой планёрки (26.03) не разобрали 2 задачи ⚠️" in text
    assert "Golden Trade" not in text
    assert "задачи на встрече не обсуждались" not in text


def test_analytics_recurring_problems_section():
    data = {
        "participant_breakdown": [{"name": "Оля", "participated": True, "cases": ["A"]}],
        "attention_points": [
            {"point": "Налоговая ответственность не ясна", "severity": "high",
             "recurring": True, "suggested_follow_up": "Позвать юриста"},
            {"point": "Передача дел", "severity": "medium", "recurring": True},
            {"point": "Разовая проблема", "severity": "high", "recurring": False},
        ],
    }
    text = render_analytics_message(data, meeting_date="2026-06-12", team_roster=ROSTER)
    block = text.split("🔁 ПОВТОРЯЕТСЯ")[1]
    # Only recurring points, severity-sorted, one tight line each (no follow-up wall).
    assert "🔴 Налоговая ответственность не ясна" in block
    assert "Что сделать" not in block
    assert "🟡 Передача дел" in block
    assert "Разовая проблема" not in block  # recurring=false is excluded
    assert block.index("🔴") < block.index("🟡")


def test_include_analytics_false_drops_block_for_separate_message():
    data = {
        "effectiveness": {"score": 7, "criteria": []},
        "previous_tasks_status": [
            {"task": "Отправить платежное поручение.", "assignee": "Оля",
             "status": "выполнено", "evidence": ""},
        ],
    }
    report = render_telegram_report(
        data, meeting_date="2026-06-12", team_roster=ROSTER, include_analytics=False
    )
    assert "📈 АНАЛИТИКА" not in report
    # The standalone analytics message carries the same block under its title.
    analytics = render_analytics_message(
        data, meeting_date="2026-06-12", team_roster=ROSTER
    )
    assert analytics.startswith("📊 Аналитика планёрки · 12.06")
    assert "✅ ЗАДАЧИ" in analytics
    assert "С прошлой планёрки: 100% (✅ 1 из 1)" in analytics
    assert "  Оля 100% (✅ 1)" in analytics


def test_analytics_message_empty_when_no_dynamics():
    # No participant breakdown, no previous-task statuses, no prior stats ->
    # nothing to send as a separate analytics message.
    assert render_analytics_message(
        {"effectiveness": {"score": 7}, "open_questions": ["q"]}, team_roster=ROSTER
    ) == ""


def test_open_questions_present_and_noise_absent():
    text = _render()
    assert "❓ ОТКРЫТЫЕ ВОПРОСЫ\n  – Работаем ли дальше с должниками?" in text
    # Dropped per feedback: no summary line, no mood, no attention block.
    assert "не должно попасть в отчёт" not in text
    assert "Настроение" not in text and "продуктивное" not in text
    assert "ОБРАТИТЬ ВНИМАНИЕ" not in text
    # No markdown asterisks anywhere.
    assert "*" not in text


def test_sections_without_data_are_dropped():
    text = render_telegram_report(
        {"effectiveness": FULL_DATA["effectiveness"]},
        meeting_date="2026-06-10",
        team_roster=ROSTER,
    )
    assert "⚠️ РИСКИ И СИТУАЦИИ" not in text
    assert "✅ ЗАДАЧИ НА КОНТРОЛЕ" not in text
    assert "❓ ОТКРЫТЫЕ ВОПРОСЫ" not in text
    assert "🧭" not in text
    assert "🕐" not in text


def test_renders_without_roster():
    text = render_telegram_report(FULL_DATA, meeting_date="2026-06-10")
    assert "👤 Тагуи - не принимал(а) участия." in text
    assert "👤 Стелла" in text and "👤 Оля" in text


def test_ex_roster_members_hidden_when_roster_known():
    """Old stored analyses may contain people removed from the roster (Гор)."""
    data = {
        "participant_breakdown": [
            {"name": "Гор Менеджер", "participated": False},
            {"name": "Стелла Бухгалтер", "participated": True,
             "yesterday": "Сдала отчёт.", "today_plan": [], "blockers": []},
        ]
    }
    text = render_telegram_report(data, meeting_date="2026-03-24", team_roster=ROSTER)
    assert "Гор" not in text
    assert "👤 Стелла" in text


def test_full_names_normalized_collectives_kept():
    """Model output from real runs uses full names — the report shows first names.

    Collective assignees («Все бухгалтеры») must not be clipped to their
    first word.
    """
    data = {
        "action_items": [
            {"text": "Наире подготовить информацию о клиентах с оборотом более 200 млн",
             "assignee": "Наира Мхитарян", "deadline": "не указан"},
            {"text": "Проверить свои банковские коды",
             "assignee": "Все бухгалтеры", "deadline": "2026-03-24"},
            {"text": "Разобраться с делами Алекса",
             "assignee": "Тагуи Бухгалтер", "deadline": "Не указано"},
        ],
        "manager_reactions": [
            {"to_whom": "Наира Мхитарян", "type": "задача", "text": "Узнай про Сианну Алиеву."},
        ],
    }
    roster = ROSTER + [{"name": "Наира Мхитарян", "role": "бухгалтер"}]
    text = render_telegram_report(data, meeting_date="2026-03-24", team_roster=roster)
    assert "👤 Наира:" in text and "Наира Мхитарян:" not in text
    assert "👤 Все бухгалтеры:\n1. Проверить свои банковские коды. Срок: 2026-03-24" in text
    assert "👤 Тагуи:\n1. Разобраться с делами Алекса. Срок: ❌" in text
    assert "Наира:\nУзнай про Сианну Алиеву." in text
    assert "не указан" not in text  # all missing deadlines became ❌


def test_old_analysis_with_five_criteria_renders_five_lines():
    data = {
        "effectiveness": {
            "score": 6,
            "verdict": "Нормальная встреча.",
            "criteria": [
                {"criterion": "Все сотрудники высказались", "status": "частично"},
                {"criterion": "Эмилия задавала вопросы", "status": "выполнено"},
                {"criterion": "Эмилия поставила задачи", "status": "выполнено"},
                {"criterion": "Эмилия поделилась новостями", "status": "частично"},
                {"criterion": "Эмилия кого-то похвалила", "status": "не выполнено"},
            ],
        }
    }
    text = render_telegram_report(data, meeting_date="2026-03-24", team_roster=ROSTER)
    # Canonical «Руководитель» labels even for old stored criteria…
    assert "  🟡 Все высказались" in text
    assert "  ✅ Руководитель задавала вопросы" in text
    # …but no invented 6th item: it was not assessed back then.
    assert "Руководитель спросила про прошлые задачи" not in text
