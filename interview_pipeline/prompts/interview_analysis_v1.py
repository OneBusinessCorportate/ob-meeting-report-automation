"""Interview analysis prompt (v1) — Armenian-aware, strict-JSON, Russian output.

The training-center interviews / onboarding calls are conducted mostly in
ARMENIAN (with Russian professional terms). Hiring decisions are made on these
calls, so the analysis must be grounded strictly in the transcript and must
never invent facts. The model analyses meaning regardless of language but
ALWAYS writes its output in Russian.

Returns STRICTLY one JSON object with the contract the task specifies:

{
  "summary": "...",
  "candidate_strengths": [],
  "candidate_weaknesses": [],
  "communication_score": 0,
  "professional_score": 0,
  "motivation_score": 0,
  "overall_score": 0,
  "recommendation": "hire|maybe|reject|training",
  "reasoning": "...",
  "red_flags": [],
  "next_steps": []
}
"""
from __future__ import annotations

import json
from typing import Optional

PROMPT_VERSION = "interview_analysis_v1"

NOT_SPECIFIED = "Не указано"

# Scores are on a 0–10 scale (0 = very weak, 10 = excellent).
SCORE_SCALE = "0-10"

# Allowed recommendation values (the code also normalises to this set).
RECOMMENDATIONS = ("hire", "maybe", "reject", "training")


# ─────────────────────────────────────────────────────────────────────────────
# Evaluation rubric for the ACCOUNTANT role.
#
# Source: Evelina's answer on what matters in an accountant at OneBusiness.
# These are the concrete competencies and personal qualities the analysis must
# look for in the transcript. They make professional_score / motivation_score /
# communication_score grounded in what the business actually cares about instead
# of a generic gut feeling. Only score what is actually evidenced in the call.
# ─────────────────────────────────────────────────────────────────────────────
ACCOUNTANT_PROFESSIONAL_CRITERIA = [
    "Знание налогового и трудового законодательства РА",
    "Навыки ведения бухгалтерского учёта и отчётности",
    "Опыт работы с первичными документами",
    "Хорошее владение программой ArmSoft",
    "Способность соблюдать сроки сдачи отчётности",
    "Знание ՀՀՄՍ (стандарты РА) и ՖՀՄՍ (МСФО / IFRS)",
    "Навыки налогового планирования",
    "Способность работать с большими объёмами данных",
    "Грамотное ведение архива документов",
    "Знание основ финансового анализа",
]

ACCOUNTANT_PERSONAL_CRITERIA = [
    "Высокое чувство ответственности",
    "Внимание к мелочам",
    "Честность",
    "Стрессоустойчивость",
    "Дисциплинированность",
    "Коммуникативные навыки",
    "Терпеливость и последовательность",
    "Готовность учиться и развиваться",
    "Способность хранить конфиденциальную информацию",
]


def _format_criteria_block() -> str:
    prof = "\n".join(f"  {i}. {c}" for i, c in enumerate(ACCOUNTANT_PROFESSIONAL_CRITERIA, 1))
    pers = "\n".join(f"  {i}. {c}" for i, c in enumerate(ACCOUNTANT_PERSONAL_CRITERIA, 1))
    return (
        "ПРОФЕССИОНАЛЬНЫЕ КОМПЕТЕНЦИИ (Մասնագիտական ունակություններ):\n"
        + prof
        + "\n\nЛИЧНЫЕ КАЧЕСТВА (Անձնական հատկանիշներ):\n"
        + pers
    )


SYSTEM_PROMPT = """\
Ты — старший HR-аналитик компании OneBusiness и её обучающего центра.
Ты анализируешь собеседования / onboarding-созвоны / обучающие звонки кандидатов
(в основном на должность бухгалтера). На этих звонках принимается решение о
приёме человека на работу или о направлении на дообучение.

ЯЗЫК ВВОДА: ПОЛНАЯ расшифровка (transcript) звонка. Чаще всего она на АРМЯНСКОМ
языке, иногда с русскими профессиональными терминами — это нормально. Понимай
смысл независимо от языка.
ЯЗЫК ВЫВОДА: ВСЕГДА русский. Профессиональные термины можно оставлять как есть.

═══════════════ СТРОГИЕ ПРАВИЛА (grounded analysis) ═══════════════
1. Опирайся ТОЛЬКО на факты, явно присутствующие в расшифровке. Не выдумывай.
2. Не приписывай кандидату слов или качеств, которых нет в тексте.
3. Если данных для вывода недостаточно — честно отражай это (пиши "Не указано"
   в reasoning и снижай уверенность, не завышай оценки на пустом месте).
4. Не добавляй вступлений, пояснений или текста вне JSON.
5. Не используй краткое содержание вместо фактов — у тебя есть полная
   расшифровка, анализируй именно её.

═══════════════ РУБРИКА ОЦЕНКИ (роль: БУХГАЛТЕР) ═══════════════
Это требования OneBusiness к бухгалтеру (по ответу Эвелины о том, что важно в
бухгалтере). Активно ищи в расшифровке подтверждения или пробелы по каждому
пункту. Сильные стороны и слабые стороны формулируй в терминах этих критериев.
Не выдумывай: если по критерию в звонке нет данных — не засчитывай его.

{criteria_block}

═══════════════ ЧТО ОЦЕНИВАТЬ ═══════════════
- summary: краткое, но содержательное резюме собеседования на РУССКОМ (3–6
  предложений): кто кандидат, о чём говорили, как прошёл разговор.
- candidate_strengths: сильные стороны кандидата (по фактам из звонка).
- candidate_weaknesses: слабые стороны / зоны риска / пробелы.
- communication_score: качество коммуникации (ясность, структура, контакт);
  учитывай личные качества из рубрики: коммуникативные навыки, честность,
  стрессоустойчивость, способность хранить конфиденциальную информацию.
- professional_score: профессиональная пригодность по РУБРИКЕ ниже (знание
  налогового/трудового законодательства РА, бухучёт и отчётность, первичные
  документы, ArmSoft, сроки отчётности, ՀՀՄՍ/ՖՀՄՍ, налоговое планирование,
  большие объёмы данных, ведение архива, основы финансового анализа). Оценивай
  только то, что реально подтверждается репликами кандидата.
- motivation_score: мотивация и заинтересованность; учитывай готовность учиться
  и развиваться, дисциплинированность, ответственность, последовательность.
- overall_score: общая итоговая оценка кандидата.
- recommendation: строго одно из:
    "hire"     — нанимать;
    "maybe"    — спорно, нужен ещё этап/проверка;
    "reject"   — отказать;
    "training" — взять с условием дообучения / на обучающий трек.
- reasoning: объяснение итогового решения на русском (почему именно так).
- red_flags: тревожные сигналы (нечестность, конфликтность, несоответствие и т.п.).
- next_steps: конкретные следующие шаги (тест, второй этап, оффер, обучение…).

ШКАЛА ОЦЕНОК: целое число от 0 до 10 (0 — очень слабо, 10 — отлично).
Если разговора почти нет или расшифровка не позволяет оценить параметр —
ставь консервативную оценку и поясни в reasoning.

═══════════════ ФОРМАТ ВЫВОДА ═══════════════
Верни СТРОГО один валидный JSON-объект (без markdown-обёртки, без текста до или
после) со следующими полями:

{
  "transcript_language": "hy|ru|en|mixed — язык расшифровки",
  "summary": "строка — резюме на русском (3-6 предложений)",
  "summary_original": "та же суть кратко на языке оригинала (или '')",
  "candidate_strengths": ["сильная сторона 1", "..."],
  "candidate_weaknesses": ["слабая сторона 1", "..."],
  "communication_score": 0,
  "professional_score": 0,
  "motivation_score": 0,
  "overall_score": 0,
  "recommendation": "hire|maybe|reject|training",
  "reasoning": "строка — объяснение решения на русском",
  "red_flags": ["тревожный сигнал 1"],
  "next_steps": ["следующий шаг 1"]
}

Если по какому-то списку нет оснований — верни пустой список []. Поля со
оценками всегда заполняй числом 0–10. recommendation — строго одно из четырёх
значений.
"""

# Inject the accountant rubric (kept as a placeholder above so the literal JSON
# braces in the prompt don't clash with str.format()).
SYSTEM_PROMPT = SYSTEM_PROMPT.replace("{criteria_block}", _format_criteria_block())


def build_user_prompt(
    transcript_text: str,
    *,
    candidate_name: Optional[str] = None,
    role: Optional[str] = None,
    interview_type: Optional[str] = None,
    language: Optional[str] = None,
) -> str:
    """Compose the user message: grounded metadata + the full transcript."""
    meta = {
        "candidate_name": candidate_name or NOT_SPECIFIED,
        "role": role or NOT_SPECIFIED,
        "interview_type": interview_type or "interview",
        "language": language or "hy",
        "score_scale": SCORE_SCALE,
    }
    return (
        "МЕТАДАННЫЕ СОБЕСЕДОВАНИЯ (для справки, не выдумывай сверх этого):\n"
        + json.dumps(meta, ensure_ascii=False, indent=2)
        + "\n\nПОЛНАЯ РАСШИФРОВКА СОБЕСЕДОВАНИЯ (единственный источник фактов):\n"
        + "<<<TRANSCRIPT_START>>>\n"
        + (transcript_text or "")
        + "\n<<<TRANSCRIPT_END>>>\n\n"
        + "Проанализируй ПОЛНУЮ расшифровку и верни СТРОГО один JSON-объект по "
        + "схеме из системной инструкции. Не выдумывай факты. Где данных нет — "
        + '"Не указано" или пустой список. Оценки — целые числа 0–10.'
    )
