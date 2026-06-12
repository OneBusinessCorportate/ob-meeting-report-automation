"""Tests for the deterministic Telegram report renderer.

The structure is built by script (rigid), the AI only supplies values — these
tests pin down the approved layout so it cannot drift again.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_pipeline.report_render import render_telegram_report  # noqa: E402

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


def test_analytics_block_with_completion_rates_and_trends():
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
         "per_assignee": {"Оля": {"done": 0, "total": 2}},
         "has_participation": True, "absent": ["Аваг", "Артак"], "manager_pct": 80},
        {"date": "2026-06-11", "score": 6, "tasks_done": 0, "tasks_total": 3,
         "per_assignee": {"Оля": {"done": 0, "total": 1},
                          "Наира Мхитарян": {"done": 0, "total": 2}},
         "has_participation": True, "absent": ["Аваг"], "manager_pct": 75},
    ]
    roster = ROSTER + [{"name": "Наира Мхитарян", "role": "бухгалтер"}]
    data["talk_share"] = {"manager_pct": 70, "accountants_pct": 30}
    data["participant_breakdown"] = [
        {"name": "Аваг", "participated": False},
        {"name": "Оля", "participated": True},
    ]
    text = render_telegram_report(
        data, meeting_date="2026-06-12", team_roster=roster, prior_stats=prior_stats
    )
    assert "📈 АНАЛИТИКА" in text
    # Analytics close the report (after the open questions).
    assert text.index("📈 АНАЛИТИКА") > text.index("❓ ОТКРЫТЫЕ ВОПРОСЫ")
    # Team and per-accountant completion rates are computed by script.
    assert "Задачи с прошлой планёрки: выполнено 1 из 4 (25%)" in text
    # Personal trend against the person's own previous result.
    assert "👤 Оля: выполнено 1 из 3 (33%), прошлая планёрка 0% ↗️" in text
    assert "  ✅ Отправить платежное поручение. — сказала, что отправила" in text
    assert "  ❌ Заключить договор с Бета." in text
    assert "  🟡 Проверить банковские коды." in text
    assert "👤 Наира: выполнено 0 из 1 (0%), прошлая планёрка 0% ➡️" in text
    assert "  ❓ Подготовить список клиентов с оборотом 200 млн." in text
    # Completion trend across stand-ups + the script-computed average.
    assert "Динамика выполнения задач:\n  10.06: 25%\n  11.06: 0%\n  сегодня: 25% ↗️" in text
    assert "среднее за 3 планёрки(ок): 17%" in text
    # Attendance: misses over the window (2 prior + today).
    assert "Пропуски за последние 3 планёрки(ок):" in text
    assert "  Аваг: 3 из 3" in text
    assert "  Артак: 1 из 3" in text
    # Manager talk-share trend and score chain.
    assert "Доля руководителя в разговоре: 75% → 70% ↘️" in text
    assert "Оценка встречи: 5 → 6 → 7 из 10 ↗️" in text
    # Script-detected signals.
    assert "❗ СИГНАЛЫ" in text
    assert "  – Наира: 0% выполнения вторую планёрку подряд." in text
    assert "  – Задач с прошлой планёрки, про которые никто не вспомнил: 1." in text
    assert "  – Аваг: не было ни на одной из последних 3 планёрок." in text


def test_analytics_block_skipped_without_data():
    text = _render()  # FULL_DATA has no previous_tasks_status / prior stats
    assert "АНАЛИТИКА" not in text


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
