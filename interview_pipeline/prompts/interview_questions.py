"""Structured accountant interview — 5 evaluation theses, criteria & questions.

Single source of truth for the OneBusiness accountant interview. It binds three
things together so they never drift apart:

  * THE 5 THESES the candidate is scored on (тезисы оценки);
  * which of Evelina's requirements (проф. компетенции + личные качества) each
    thesis covers — the grounding rubric;
  * the interview QUESTIONS HR asks to surface evidence for each thesis.

Both the analysis prompt (``interview_analysis_v1``) and the Telegram report
import from here, so the rubric, the questions and the score fields stay in sync.

Company profile baked into the questions: small/medium business in Armenia,
tax regimes — micro / turnover (УСН) / VAT (НДС), ArmSoft, 2+ years experience.

Each thesis exposes a stable ``score_field`` — the key used in the AI JSON, in
``InterviewAnalysisResult`` and in the ``intv_scores`` table — so a score is
always traceable back to the thesis and its questions.
"""
from __future__ import annotations

from typing import Dict, List

# Scores are integers on a 0–10 scale (0 = very weak, 10 = excellent).
SCORE_SCALE = "0-10"

# The five evaluation theses, in the order they are shown everywhere.
THESES: List[Dict[str, object]] = [
    {
        "id": 1,
        "key": "knowledge",
        "score_field": "knowledge_score",
        "title": "Профессиональные знания",
        "criteria": [
            "Знание налогового и трудового законодательства РА",
            "Системы налогообложения: микро, оборотный (УСН), НДС",
            "Знание ՀՀՄՍ (стандарты РА) и ՖՀՄՍ (МСФО / IFRS)",
            "Навыки налогового планирования",
            "Знание основ финансового анализа",
        ],
        "questions": [
            "С какими системами налогообложения в Армении вы работали "
            "(микро, оборотный/УСН, НДС)? В чём ключевые отличия и пороги перехода?",
            "Какие налоги и отчёты и в какие сроки сдаёт ООО на НДС "
            "(НДС, налог на прибыль, подоходный, социальные платежи)?",
            "В чём разница между ՀՀՄՍ (нац. стандарты РА) и ՖՀՄՍ (МСФО) и когда "
            "применяется каждый?",
            "Как вы рассчитываете зарплату, подоходный налог и социальные "
            "отчисления? Какие тонкости трудового законодательства учитываете?",
            "Какие показатели вы смотрите по балансу и отчёту о прибылях/убытках, "
            "чтобы оценить состояние бизнеса?",
        ],
    },
    {
        "id": 2,
        "key": "skills",
        "score_field": "skills_score",
        "title": "Практический опыт и инструменты (ArmSoft)",
        "criteria": [
            "Навыки ведения бухгалтерского учёта и отчётности",
            "Опыт работы с первичными документами",
            "Хорошее владение программой ArmSoft",
            "Способность соблюдать сроки сдачи отчётности",
            "Способность работать с большими объёмами данных",
            "Грамотное ведение архива документов",
        ],
        "questions": [
            "Сколько лет и в каких модулях ArmSoft вы работали? Что умеете делать "
            "в программе самостоятельно, без помощи?",
            "Опишите ваш типичный месячный цикл: от приёма первички до сдачи "
            "отчётности.",
            "Как вы организуете работу с первичными документами — приём, проверка, "
            "проведение?",
            "Сколько компаний или какой объём документов вы вели одновременно? "
            "Как справлялись с пиковой нагрузкой в конце периода?",
            "Как вы ведёте архив документов (бумажный и электронный) и как быстро "
            "находите нужный документ за прошлый год?",
            "Был ли у вас случай, когда срывался срок отчётности? Что вы делаете, "
            "чтобы сроки соблюдались?",
        ],
    },
    {
        "id": 3,
        "key": "responsibility",
        "score_field": "responsibility_score",
        "title": "Ответственность и внимательность",
        "criteria": [
            "Высокое чувство ответственности",
            "Внимание к мелочам",
            "Дисциплинированность",
            "Способность хранить конфиденциальную информацию",
        ],
        "questions": [
            "Расскажите об ошибке в учёте, которую допустили вы или коллега. Как "
            "вы её обнаружили и исправили?",
            "Как вы перепроверяете себя перед сдачей отчёта, чтобы не пропустить "
            "ошибку?",
            "Как вы обращаетесь с конфиденциальной информацией клиентов "
            "(зарплаты, обороты, договоры)?",
            "Что вы делаете, если понимаете, что не успеваете выполнить задачу к "
            "сроку?",
        ],
    },
    {
        "id": 4,
        "key": "resilience",
        "score_field": "resilience_score",
        "title": "Стрессоустойчивость, честность, последовательность",
        "criteria": [
            "Честность",
            "Стрессоустойчивость",
            "Терпеливость и последовательность",
        ],
        "questions": [
            "Опишите самый напряжённый рабочий период (закрытие года, проверка). "
            "Как вы справлялись с нагрузкой?",
            "Что вы сделаете, если руководитель или клиент попросит «оптимизировать» "
            "налоги способом, который вы считаете незаконным?",
            "Если налоговая выявила ошибку по вашей вине — какими будут ваши "
            "действия?",
            "Как вы относитесь к монотонной, рутинной работе изо дня в день?",
        ],
    },
    {
        "id": 5,
        "key": "communication",
        "score_field": "communication_score",
        "title": "Коммуникация и готовность учиться",
        "criteria": [
            "Коммуникативные навыки",
            "Готовность учиться и развиваться",
        ],
        "questions": [
            "Как вы объясните клиенту без бухгалтерского образования, почему он "
            "должен заплатить тот или иной налог?",
            "Как вы следите за изменениями в законодательстве РА?",
            "Чему вы научились за последний год? Готовы ли осваивать новое ПО или "
            "новый участок учёта?",
            "Как вы строите общение с коллегами и клиентами при разногласиях?",
        ],
    },
]

# Stable list of the five score fields, in thesis order (id 1..5).
SCORE_FIELDS: List[str] = [t["score_field"] for t in THESES]  # type: ignore[index]


def thesis_by_score_field() -> Dict[str, Dict[str, object]]:
    return {t["score_field"]: t for t in THESES}  # type: ignore[index]


def format_rubric_block() -> str:
    """Render the 5 theses + the criteria each one covers (for the AI prompt)."""
    lines: List[str] = []
    for t in THESES:
        crit = "; ".join(t["criteria"])  # type: ignore[arg-type]
        lines.append(
            f"Тезис {t['id']} — {t['title']} (поле \"{t['score_field']}\"):\n"
            f"    охватывает: {crit}."
        )
    return "\n".join(lines)


def format_questionnaire() -> str:
    """Render the full question bank grouped by thesis (for HR / docs)."""
    blocks: List[str] = []
    for t in THESES:
        qs = "\n".join(f"  {i}. {q}" for i, q in enumerate(t["questions"], 1))  # type: ignore[arg-type]
        blocks.append(f"Тезис {t['id']}. {t['title']}\n{qs}")
    return "\n\n".join(blocks)
