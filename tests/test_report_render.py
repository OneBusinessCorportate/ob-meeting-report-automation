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
         "owner": "Оля", "deadline": "Не указано", "how_to_track": "Список задолженностей"},
        {"text": "Документы от клиента Гамма задерживаются.", "severity": "low",
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
    assert lines[1] == "📅 2026-06-10, 09:30–10:00"
    assert "📊 ОЦЕНКА ВСТРЕЧИ: 7 из 10" in text
    # Checklist says «Руководитель», partial = yellow circle, 6 items incl. followup.
    assert "  🟡 Все высказались" in text
    assert "  ✅ Руководитель задавала вопросы" in text
    assert "  ❌ Руководитель кого-то похвалила" in text
    assert "  ❌ Руководитель спросила про прошлые задачи" in text
    assert "Эмилия задавала" not in text
    # Talk share is compact and on top (before "Кто был").
    assert "Кто сколько говорил: 70% руководитель, 30% бухгалтеры" in text
    assert text.index("Кто сколько говорил") < text.index("Кто был")


def test_attendance_block_with_visible_absent():
    text = _render()
    assert "Кто был: Эмилия (руководитель), Стелла, Оля" in text
    # Blank line right above «Не было» so the block stands out.
    assert "\n\nНе было: Тагуи" in text
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
    # Тагуи did not participate.
    assert "👤 Тагуи\nНе принимал(а) участия." in text
    # The manager has no personal accountant block.
    assert "👤 Эмилия\n" not in text


def test_manager_block_grouped_numbered_no_poruchila():
    text = _render()
    block = text.split("🧭 ЧТО СКАЗАЛА РУКОВОДИТЕЛЬ (ЭМИЛИЯ)")[1].split("⚠️")[0]
    assert "Общее: Я завтра буду в Арцахе." in block
    assert ("Оля: 1. Отправлю платежное поручение после митинга. "
            "2. Просто предупреди, что если не присылают данные, "
            "то у нас нет информации.") in block
    assert "поручила" not in block
    assert "Спросила про прошлые задачи" not in block
    assert "Кто сколько говорил" not in block


def test_risks_numbered_with_icon_no_severity_words():
    text = _render()
    block = text.split("⚠️ РИСКИ И СИТУАЦИИ")[1].split("✅ ЗАДАЧИ")[0]
    assert "1. Два клиента не платят дольше пяти месяцев." in block
    assert "Риск: 🔴" in block
    assert "Ответственный: Оля" in block
    assert "Срок: ❌" in block
    assert "2. Документы от клиента Гамма задерживаются." in block
    assert "Риск: 🟢" in block
    assert "Ответственный: ❌" in block
    assert "Ситуация" not in block
    assert "высокая" not in block and "Степень риска" not in block
    # Situations are separated by a blank line.
    assert "\n\n2. Документы" in block


def test_tasks_grouped_by_assignee():
    text = _render()
    block = text.split("✅ ЗАДАЧИ НА КОНТРОЛЕ")[1].split("❓")[0]
    assert ("👤 Эмилия:\n1. Уточнить ситуацию с клиентами с долгом более "
            "пяти месяцев. Срок: ❌") in block
    assert "👤 Оля:\n1. Подготовить договор с Бета. Срок: 2026-06-15" in block


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
    assert "Кто был: Эмилия (руководитель), Стелла, Оля" in text
    assert "Не было: Тагуи" in text


def test_full_names_normalized_collectives_and_clients_kept():
    """Model output from real runs uses full names — the report shows first names.

    Collective assignees («Все бухгалтеры») and client names («Гюльчоре
    Балаян») must not be clipped to their first word.
    """
    data = {
        "problems_risks": [
            {"text": "Долг по DG Finance.", "severity": "high",
             "owner": "Эмилия Аванесян", "deadline": "не указан"},
            {"text": "Клиент Гюльчоре Балаян отказывается платить.", "severity": "high",
             "owner": "Гюльчоре Балаян", "deadline": "Не указано"},
        ],
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
    assert "Ответственный: Эмилия\n" in text
    assert "Ответственный: Гюльчоре Балаян" in text  # client name kept whole
    assert "👤 Наира:" in text and "Наира Мхитарян:" not in text
    assert "👤 Все бухгалтеры:\n1. Проверить свои банковские коды. Срок: 2026-03-24" in text
    assert "👤 Тагуи:\n1. Разобраться с делами Алекса. Срок: ❌" in text
    assert "Наира: Узнай про Сианну Алиеву." in text
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
