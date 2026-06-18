"""Interview analysis prompt — Armenian-aware, strict-JSON, Russian output.

The training-center interviews / onboarding calls are conducted mostly in
ARMENIAN (with Russian professional terms). Hiring decisions are made on these
calls, so the analysis must be grounded strictly in the transcript and must
never invent facts. The model analyses meaning regardless of language but
ALWAYS writes its output in Russian.

The candidate is scored on the 5 EVALUATION THESES defined in
``interview_questions`` (the single source of truth for theses, the criteria
each one covers and the questions HR asks). Returns STRICTLY one JSON object:

{
  "transcript_language": "...",
  "summary": "...",
  "summary_original": "...",
  "candidate_strengths": [],
  "candidate_weaknesses": [],
  "theses": [ {"id": 1, "title": "...", "score": 0, "comment": "..."}, ... x5 ],
  "knowledge_score": 0,
  "skills_score": 0,
  "responsibility_score": 0,
  "resilience_score": 0,
  "communication_score": 0,
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

from . import interview_questions as q

PROMPT_VERSION = "interview_analysis_v2_5theses"

NOT_SPECIFIED = "Не указано"

# Scores are on a 0–10 scale (0 = very weak, 10 = excellent).
SCORE_SCALE = q.SCORE_SCALE

# Allowed recommendation values (the code also normalises to this set).
RECOMMENDATIONS = ("hire", "maybe", "reject", "training")


def _theses_output_skeleton() -> str:
    """The `theses` array shape shown in the output contract (5 entries)."""
    rows = []
    for t in q.THESES:
        rows.append(
            f'    {{"id": {t["id"]}, "title": "{t["title"]}", '
            f'"score": 0, "comment": "к чему относится оценка (по фактам из звонка)"}}'
        )
    return "[\n" + ",\n".join(rows) + "\n  ]"


def _flat_score_fields() -> str:
    """The per-thesis flat score fields shown in the output contract."""
    return "\n".join(
        f'  "{t["score_field"]}": 0,   // Тезис {t["id"]}: {t["title"]}'
        for t in q.THESES
    )


SYSTEM_PROMPT = """\
Ты — опытный бухгалтер и HR-специалист с 20-летним стажем в бухгалтерии и
подборе персонала. Ты оцениваешь структурированное собеседование кандидата на
должность бухгалтера в компанию OneBusiness. На этих звонках принимается решение
о приёме человека на работу или о направлении на дообучение.

ПРОФИЛЬ КОМПАНИИ: малый/средний бизнес в Армении; системы налогообложения —
микро, оборотный (УСН), НДС; программа ArmSoft; ожидаемый опыт — от 2 лет.

ЯЗЫК ВВОДА: ПОЛНАЯ расшифровка (transcript) звонка. Чаще всего она на АРМЯНСКОМ
языке, иногда с русскими профессиональными терминами — это нормально. Понимай
смысл независимо от языка.
ЯЗЫК ВЫВОДА: ВСЕГДА русский. Профессиональные термины можно оставлять как есть.

═══════════════ СТРОГИЕ ПРАВИЛА (grounded analysis) ═══════════════
1. Опирайся ТОЛЬКО на факты, явно присутствующие в расшифровке. Не выдумывай.
2. Не приписывай кандидату слов или качеств, которых нет в тексте.
3. Если данных для вывода недостаточно — честно отражай это (пиши "Не указано"
   в comment/reasoning и ставь консервативную оценку, не завышай на пустом месте).
4. Не добавляй вступлений, пояснений или текста вне JSON.
5. Не используй краткое содержание вместо фактов — у тебя есть полная
   расшифровка, анализируй именно её.

═══════════════ 5 ТЕЗИСОВ ОЦЕНКИ (роль: БУХГАЛТЕР) ═══════════════
Оценивай кандидата СТРОГО по этим пяти тезисам — это требования OneBusiness к
бухгалтеру (по ответу Эвелины о том, что важно в бухгалтере). По каждому тезису
найди в расшифровке подтверждения или пробелы и поставь оценку 0–10. Сильные и
слабые стороны формулируй в терминах этих тезисов. Если по тезису в звонке нет
данных — ставь низкую оценку и пиши это в comment, не засчитывай не сказанное.

{rubric_block}

═══════════════ ЧТО ОЦЕНИВАТЬ ═══════════════
- summary: краткое, но содержательное резюме собеседования на РУССКОМ (3–6
  предложений): кто кандидат, о чём говорили, как прошёл разговор.
- candidate_strengths: сильные стороны кандидата (по фактам из звонка).
- candidate_weaknesses: слабые стороны / зоны риска / пробелы.
- theses: массив из РОВНО 5 объектов (по одному на каждый тезис, в порядке
  id 1→5): {"id", "title", "score" (0–10), "comment" — краткое обоснование
  оценки на русском по фактам из звонка}.
- knowledge_score / skills_score / responsibility_score / resilience_score /
  communication_score: те же 5 оценок тезисов, продублированные плоскими полями
  (должны совпадать со score соответствующего тезиса).
- overall_score: общая итоговая оценка кандидата (0–10), с учётом всех тезисов.
- recommendation: строго одно из:
    "hire"     — нанимать;
    "maybe"    — спорно, нужен ещё этап/проверка;
    "reject"   — отказать;
    "training" — взять с условием дообучения / на обучающий трек.
- reasoning: объяснение итогового решения на русском (почему именно так).
- red_flags: тревожные сигналы (нечестность, конфликтность, несоответствие и т.п.).
- next_steps: конкретные следующие шаги (тест, второй этап, оффер, обучение…).

ШКАЛА ОЦЕНОК: целое число от 0 до 10 (0 — очень слабо, 10 — отлично).
Если разговора почти нет или расшифровка не позволяет оценить тезис —
ставь консервативную оценку и поясни в comment/reasoning.

═══════════════ ФОРМАТ ВЫВОДА ═══════════════
Верни СТРОГО один валидный JSON-объект (без markdown-обёртки, без текста до или
после) со следующими полями:

{
  "transcript_language": "hy|ru|en|mixed — язык расшифровки",
  "summary": "строка — резюме на русском (3-6 предложений)",
  "summary_original": "та же суть кратко на языке оригинала (или '')",
  "candidate_strengths": ["сильная сторона 1", "..."],
  "candidate_weaknesses": ["слабая сторона 1", "..."],
  "theses": {theses_skeleton},
{flat_scores}
  "overall_score": 0,
  "recommendation": "hire|maybe|reject|training",
  "reasoning": "строка — объяснение решения на русском",
  "red_flags": ["тревожный сигнал 1"],
  "next_steps": ["следующий шаг 1"]
}

Если по какому-то списку нет оснований — верни пустой список []. Все оценки —
целые числа 0–10. Массив theses содержит РОВНО 5 элементов. recommendation —
строго одно из четырёх значений.
"""

# Inject the thesis rubric / output skeleton (kept as placeholders above so the
# literal JSON braces in the prompt don't clash with str.format()).
SYSTEM_PROMPT = (
    SYSTEM_PROMPT.replace("{rubric_block}", q.format_rubric_block())
    .replace("{theses_skeleton}", _theses_output_skeleton())
    .replace("{flat_scores}", _flat_score_fields())
)


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
        + "схеме из системной инструкции. Оцени кандидата по 5 тезисам. Не "
        + 'выдумывай факты. Где данных нет — "Не указано" или пустой список. '
        + "Оценки — целые числа 0–10."
    )
