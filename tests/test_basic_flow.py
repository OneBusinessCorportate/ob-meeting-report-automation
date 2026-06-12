"""Offline tests for the meeting pipeline.

These tests use in-memory fakes for Supabase, Anthropic and Telegram, so they
require NO network access and NO real credentials. Run with:

    python -m pytest tests/ -v
    # or, without pytest installed:
    python tests/test_basic_flow.py
"""
from __future__ import annotations

import copy
import json
import os
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_pipeline.ai_client import AIClient
from meeting_pipeline.gemini_client import GeminiClient
from meeting_pipeline.analyze import analyze_meeting, extract_full_transcript
from meeting_pipeline.config import Config
from meeting_pipeline.deliver import (
    MISSING_REPORT_MENTIONS,
    MISSING_REPORT_MESSAGE,
    deliver_today,
)
from meeting_pipeline.ingest import (
    build_raw_transcript,
    ingest_from_file,
    ingest_from_timeless,
    ingest_meeting,
)
from meeting_pipeline.supabase_repo import SupabaseRepo
from meeting_pipeline.telegram_client import TelegramClient
from meeting_pipeline.timeless_client import TimelessClient
from meeting_pipeline.utils import (
    extract_json,
    split_telegram_message,
    to_telegram_markdown,
)


# --------------------------------------------------------------------------- #
# In-memory fakes
# --------------------------------------------------------------------------- #
class _Result:
    def __init__(self, data):
        self.data = data


class FakeTable:
    """A tiny chainable query builder over a list of dict rows."""

    def __init__(self, store, name):
        self._store = store
        self._name = name
        self._rows = store.setdefault(name, [])
        self._op = "select"
        self._payload = None
        self._on_conflict = None
        self._filters = []  # (op, col, value)
        self._order = None
        self._desc = False
        self._limit = None

    # -- builders --
    def select(self, *_args, **_kwargs):
        self._op = "select"
        return self

    def insert(self, payload):
        self._op = "insert"
        self._payload = payload
        return self

    def upsert(self, payload, on_conflict=None, ignore_duplicates=False):
        self._op = "upsert"
        self._payload = payload
        self._on_conflict = on_conflict
        self._ignore_duplicates = ignore_duplicates
        return self

    def update(self, payload):
        self._op = "update"
        self._payload = payload
        return self

    def delete(self):
        self._op = "delete"
        return self

    def eq(self, col, value):
        self._filters.append(("eq", col, value))
        return self

    def gte(self, col, value):
        self._filters.append(("gte", col, value))
        return self

    def lt(self, col, value):
        self._filters.append(("lt", col, value))
        return self

    def order(self, col, desc=False):
        self._order = col
        self._desc = desc
        return self

    def limit(self, n):
        self._limit = n
        return self

    # -- execution --
    def _match(self, row):
        for op, col, value in self._filters:
            cell = row.get(col)
            if op == "eq" and cell != value:
                return False
            if op == "gte" and not (cell is not None and str(cell) >= str(value)):
                return False
            if op == "lt" and not (cell is not None and str(cell) < str(value)):
                return False
        return True

    def execute(self):
        if self._op in ("insert", "upsert"):
            payloads = self._payload if isinstance(self._payload, list) else [self._payload]
            inserted = []
            for payload in payloads:
                row = copy.deepcopy(payload)
                if self._op == "upsert" and self._on_conflict:
                    keys = [k.strip() for k in self._on_conflict.split(",")]
                    existing = next(
                        (
                            r
                            for r in self._rows
                            if all(r.get(k) == row.get(k) for k in keys)
                        ),
                        None,
                    )
                    if existing:
                        if getattr(self, "_ignore_duplicates", False):
                            continue  # PostgREST returns no row for ignored dupes
                        existing.update(row)
                        inserted.append(copy.deepcopy(existing))
                        continue
                row.setdefault("id", str(uuid.uuid4()))
                self._rows.append(row)
                inserted.append(copy.deepcopy(row))
            return _Result(inserted)

        if self._op == "delete":
            deleted = [copy.deepcopy(r) for r in self._rows if self._match(r)]
            self._rows[:] = [r for r in self._rows if not self._match(r)]
            return _Result(deleted)

        if self._op == "update":
            updated = []
            for row in self._rows:
                if self._match(row):
                    row.update(self._payload)
                    updated.append(copy.deepcopy(row))
            return _Result(updated)

        # select
        rows = [copy.deepcopy(r) for r in self._rows if self._match(r)]
        if self._order:
            rows.sort(key=lambda r: r.get(self._order) or 0, reverse=self._desc)
        if self._limit is not None:
            rows = rows[: self._limit]
        return _Result(rows)


class FakeSupabaseClient:
    def __init__(self):
        self.store = {}

    def table(self, name):
        return FakeTable(self.store, name)


class FakeAnthropic:
    """Returns a canned JSON report (or raises) based on config."""

    def __init__(self, report=None, raise_exc=False, bad_json=False):
        self._report = report
        self._raise = raise_exc
        self._bad_json = bad_json
        self.messages = self

    def create(self, **kwargs):
        if self._raise:
            raise RuntimeError("simulated API failure")
        text = "not json at all" if self._bad_json else json.dumps(self._report)

        class _Block:
            def __init__(self, t):
                self.text = t

        class _Usage:
            input_tokens = 100
            output_tokens = 50

        class _Resp:
            content = [_Block(text)]
            usage = _Usage()

        return _Resp()


class FakeGenaiModels:
    """Mimics ``genai.Client().models`` for the Gemini adapter.

    ``fail_times`` makes the first N calls raise a transient error so retry
    behaviour can be exercised.
    """

    def __init__(self, report, fail_times=0, error="503 UNAVAILABLE"):
        self._report = report
        self.last_call = None
        self.calls = 0
        self._fail_times = fail_times
        self._error = error

    def generate_content(self, *, model, contents, config):
        self.calls += 1
        self.last_call = {"model": model, "contents": contents, "config": config}
        if self._fail_times and self.calls <= self._fail_times:
            raise RuntimeError(self._error)

        class _UsageMeta:
            prompt_token_count = 100
            candidates_token_count = 50

        class _Resp:
            text = json.dumps(self._report)
            usage_metadata = _UsageMeta()

        return _Resp()


class FakeGenaiClient:
    def __init__(self, report, fail_times=0, error="503 UNAVAILABLE"):
        self.models = FakeGenaiModels(report, fail_times=fail_times, error=error)


class FakeResponse:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload or {"ok": True, "result": {"message_id": 1}}

    def json(self):
        return self._payload


class FakeTelegramSession:
    def __init__(self, fail_markdown=False):
        self.calls = []
        self._fail_markdown = fail_markdown

    def post(self, url, json=None, timeout=None):
        self.calls.append(json)
        # Simulate Telegram rejecting bad Markdown with HTTP 400 once.
        if self._fail_markdown and "parse_mode" in json:
            return FakeResponse(
                status_code=400,
                payload={"ok": False, "description": "can't parse entities"},
            )
        return FakeResponse()


def _config():
    return Config(
        supabase_url="http://fake",
        supabase_service_role_key="fake",
        anthropic_api_key="fake",
        telegram_bot_token="fake",
        telegram_management_chat_id="123",
        timeless_api_token=None,
    )


SAMPLE_REPORT = {
    "summary": "Краткое содержание планёрки.",
    "effectiveness": {
        "score": 8,
        "max_score": 10,
        "verdict": "Хорошая, рабочая встреча.",
        "criteria": [
            {"criterion": "Все сотрудники высказались", "status": "выполнено", "detail": "все"},
            {"criterion": "Руководитель задавала вопросы", "status": "выполнено", "detail": "по кейсам"},
            {"criterion": "Руководитель поставила задачи", "status": "выполнено", "detail": "Лилит"},
            {"criterion": "Руководитель поделилась новостями", "status": "частично", "detail": "коротко"},
            {"criterion": "Руководитель кого-то похвалила", "status": "выполнено", "detail": "Лилит"},
            {"criterion": "Руководитель спросила про прошлые задачи", "status": "не выполнено", "detail": ""},
        ],
    },
    "topics": [{"topic": "Налоги", "key_points": ["12/15 сдано"], "duration_pct": 30}],
    "decisions": [
        {"decision": "Эскалировать долг Mega Build", "context": "задолженности", "owner": "Гор"}
    ],
    "action_items": [
        {
            "text": "Закрыть расхождение",
            "assignee": "Лилит",
            "deadline": "Не указано",
            "status": "open",
            "priority": "high",
        }
    ],
    "open_questions": ["Когда придут документы?"],
    "people_mentioned": [
        {"name": "Лилит", "spoke": True, "context": "ответственная", "sentiment": "neutral"}
    ],
    "praised": [{"name": "Лилит", "reason": "закрыла отчётность вовремя"}],
    "criticized": [{"name": "Армен Строй", "reason": "не передаёт документы"}],
    "problems_risks": [{"text": "Долг клиента", "severity": "high"}],
    "attention_points": [
        {
            "point": "Повторно обсуждали подготовку CSV/QR",
            "reason": "Тема всплывала несколько раз, владелец неясен",
            "severity": "medium",
            "recurring": False,
            "suggested_follow_up": "Назначить одного ответственного и срок",
        }
    ],
    "sentiment": "neutral",
    "meeting_mood": {"overall": "продуктивное", "energy": "medium"},
    "late_start": True,
    "late_start_minutes": 5,
    "mgmt_recommendations": {
        "for_whom": "Эмилия (руководитель)",
        "what_went_well": ["Все высказались по своим кейсам"],
        "what_to_improve": ["Чаще делиться новостями компании"],
        "recommendations": ["Заранее проговаривать новости в начале встречи"],
    },
    "telegram_report_md": "📋 **Планёрка**\n\n**Кратко**\nВсё ок.",
}


# --------------------------------------------------------------------------- #
# Unit tests
# --------------------------------------------------------------------------- #
def test_split_short_message():
    assert split_telegram_message("hello") == ["hello"]


def test_split_long_message():
    text = "\n".join(f"line {i}" for i in range(2000))
    parts = split_telegram_message(text, max_len=500)
    assert len(parts) > 1
    assert all(len(p) <= 500 for p in parts)
    # No content lost (ignoring whitespace differences).
    assert "line 1999" in parts[-1]


def test_split_single_oversized_line():
    parts = split_telegram_message("x" * 9000, max_len=4000)
    assert len(parts) == 3
    assert sum(len(p) for p in parts) == 9000


def test_extract_json_plain():
    assert extract_json('{"a": 1}') == {"a": 1}


def test_extract_json_fenced():
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}


def test_extract_json_with_prose():
    assert extract_json('Here is the result:\n{"a": 1}\nThanks!') == {"a": 1}


def test_extract_json_invalid():
    assert extract_json("totally not json") is None


def test_build_raw_transcript_shape():
    raw = build_raw_transcript("hello", language="hy", source="Timeless")
    assert raw["type"] == "full_transcript"
    assert raw["language"] == "hy"
    assert raw["text"] == "hello"
    assert raw["segments"] == []


def test_extract_full_transcript():
    meeting = {"raw_transcript": {"type": "full_transcript", "text": "T"}}
    assert extract_full_transcript(meeting) == "T"
    assert extract_full_transcript({"raw_transcript": {"text": "  "}}) is None
    assert extract_full_transcript({"raw_transcript": None}) is None


def test_to_telegram_markdown_converts_bold():
    assert to_telegram_markdown("**Привет** мир") == "*Привет* мир"
    assert to_telegram_markdown("a **b** c **d**") == "a *b* c *d*"
    assert to_telegram_markdown("no bold here") == "no bold here"


def test_timeless_not_configured_returns_blocker():
    client = TimelessClient(_config())
    from datetime import date

    result = client.list_today_meetings(date(2026, 3, 26))
    assert result.ok is False
    assert "Timeless API" in (result.error or "")


# --------------------------------------------------------------------------- #
# Integration-ish tests with fakes
# --------------------------------------------------------------------------- #
def _repo():
    return SupabaseRepo(_config(), client=FakeSupabaseClient())


def test_ensure_source_idempotent():
    repo = _repo()
    s1 = repo.ensure_source("timeless")
    s2 = repo.ensure_source("timeless")
    assert s1["id"] == s2["id"]
    assert s1["display_name"] == "Timeless"


def test_ingest_from_file(tmp_path=None):
    import tempfile

    repo = _repo()
    config = _config()
    d = tempfile.mkdtemp()
    f = Path(d) / "meeting.txt"
    f.write_text("Speaker 1: Привет\nSpeaker 2: Начнём.", encoding="utf-8")

    from datetime import date

    result = ingest_from_file(
        repo,
        config,
        file_path=str(f),
        title="Планёрка",
        on_date=date(2026, 3, 26),
        language="hy",
        source_meeting_id="manual_2026_03_26_test",
    )
    assert result["ok"] is True
    assert result["status"] == "ingested"
    meeting = result["meeting"]
    assert meeting["raw_transcript"]["type"] == "full_transcript"
    assert "Привет" in meeting["raw_transcript"]["text"]
    assert meeting["status"] == "completed"


def test_ingest_missing_file():
    repo = _repo()
    config = _config()
    result = ingest_from_file(
        repo, config, file_path="/no/such/file_xyz.txt", title="x"
    )
    assert result["ok"] is False
    assert result["status"] == "transcript_not_found"


def test_create_analysis_version_increment():
    repo = _repo()
    meeting_id = str(uuid.uuid4())
    a1 = repo.create_analysis(meeting_id=meeting_id, status="completed", summary="v1")
    a2 = repo.create_analysis(meeting_id=meeting_id, status="completed", summary="v2")
    assert a1["version"] == 1
    assert a2["version"] == 2


def test_analyze_meeting_success():
    repo = _repo()
    ai = AIClient(_config(), client=FakeAnthropic(report=SAMPLE_REPORT))
    meeting = {
        "id": str(uuid.uuid4()),
        "title": "Планёрка",
        "transcript_language": "hy",
        "actual_start": "2026-03-26T05:00:00+00:00",
        "raw_transcript": {"type": "full_transcript", "text": "long transcript text"},
    }
    result = analyze_meeting(repo, ai, meeting)
    assert result["ok"] is True
    analysis = result["analysis"]
    assert analysis["status"] == "completed"
    assert analysis["summary"] == SAMPLE_REPORT["summary"]
    assert analysis["telegram_report_md"]
    assert analysis["sentiment"] == "neutral"
    # Manager briefing is stored in its column, now addressed to Эмилия.
    assert "recommendations" in analysis["mgmt_recommendations"]
    # Extra grounded fields (effectiveness score, decision log, praise) are preserved.
    extras = analysis["ai_metadata"]["report_extras"]
    assert extras["effectiveness"]["score"] == 8
    assert extras["attention_points"][0]["severity"] == "medium"
    assert extras["decisions"][0]["owner"] == "Гор"
    assert extras["praised"][0]["name"] == "Лилит"
    assert extras["criticized"][0]["name"] == "Армен Строй"


def test_get_recent_meeting_context_pulls_attention_points():
    """Prior completed reports surface their summary + attention_points."""
    repo = _repo()
    meeting_id = str(uuid.uuid4())
    # Seed a past meeting and its current completed L2 (with attention_points).
    repo.client.table("mtg_meetings").insert(
        {"id": meeting_id, "title": "Прошлая планёрка", "actual_start": "2026-03-20T05:00:00+00:00"}
    ).execute()
    repo.create_analysis(
        meeting_id=meeting_id,
        status="completed",
        summary="Прошлая встреча",
        ai_metadata={"report_extras": {"attention_points": [{"point": "Снова Mega Build"}]}},
    )

    context = repo.get_recent_meeting_context("2026-03-26T05:00:00+00:00", limit=5)
    assert len(context) == 1
    assert context[0]["summary"] == "Прошлая встреча"
    assert context[0]["attention_points"][0]["point"] == "Снова Mega Build"


def test_analyze_retries_on_invalid_json_with_bigger_budget():
    """Invalid/truncated JSON triggers one retry with a doubled token budget."""

    class _FlakyClient:
        def __init__(self, report):
            self._report = report
            self.calls = []
            self.messages = self

        def create(self, **kwargs):
            self.calls.append(kwargs["max_tokens"])
            text = "{ truncated json..." if len(self.calls) == 1 else json.dumps(self._report)

            class _Block:
                def __init__(self, t):
                    self.text = t

            class _Usage:
                input_tokens = 100
                output_tokens = 50

            class _Resp:
                content = [_Block(text)]
                usage = _Usage()

            return _Resp()

    client = _FlakyClient(SAMPLE_REPORT)
    ai = AIClient(_config(), client=client)
    result = ai.analyze("long transcript", max_tokens=16384)
    assert result.ok is True
    assert result.report["summary"] == SAMPLE_REPORT["summary"]
    # First attempt at the base budget, retry at double (capped at 32768).
    assert client.calls == [16384, 32768]


def test_build_user_prompt_embeds_prior_context():
    """build_user_prompt carries prior_context into the model input."""
    from meeting_pipeline.prompts.meeting_analysis_v1 import build_user_prompt

    prompt = build_user_prompt(
        "transcript",
        prior_context=[{"date": "2026-03-25", "attention_points": [{"point": "Снова Mega Build"}]}],
    )
    assert "prior_context" in prompt
    assert "Снова Mega Build" in prompt


def test_analyze_pending_range_processes_all_days():
    """Range mode analyzes pending meetings across multiple days in one call."""
    from datetime import date

    from meeting_pipeline.analyze import analyze_pending

    repo = _repo()
    config = _config()
    source = repo.ensure_source("timeless")
    # Two completed meetings on different days, neither analyzed yet.
    for day, smid in (("2026-03-24", "timeless_a"), ("2026-03-26", "timeless_b")):
        repo.upsert_meeting(
            source_id=source["id"],
            source_meeting_id=smid,
            title="Планёрка",
            status="completed",
            actual_start=f"{day}T07:00:00+00:00",
            raw_transcript={"type": "full_transcript", "text": "transcript text"},
        )
    ai = AIClient(config, client=FakeAnthropic(report=SAMPLE_REPORT))
    result = analyze_pending(
        repo=repo,
        config=config,
        ai=ai,
        start_date_str="2026-03-24",
        end_date_str="2026-03-26",
    )
    assert result["analyzed"] == 2
    assert result["completed"] == 2
    assert result["failed"] == 0


def test_analyze_meeting_idempotent_skip():
    repo = _repo()
    ai = AIClient(_config(), client=FakeAnthropic(report=SAMPLE_REPORT))
    meeting = {
        "id": str(uuid.uuid4()),
        "raw_transcript": {"type": "full_transcript", "text": "long transcript text"},
    }
    first = analyze_meeting(repo, ai, meeting)
    assert first["status"] == "completed"
    # Re-running must NOT create a duplicate version.
    second = analyze_meeting(repo, ai, meeting)
    assert second["status"] == "skipped"
    rows = repo.client.store["mtg_analyses"]
    assert len([r for r in rows if r["meeting_id"] == meeting["id"]]) == 1
    # ...unless forced.
    third = analyze_meeting(repo, ai, meeting, force=True)
    assert third["status"] == "completed"
    assert third["analysis"]["version"] == 2


def test_record_failed_analysis_does_not_pile_up():
    """Repeated failures update one row instead of creating many versions."""
    repo = _repo()
    meeting_id = str(uuid.uuid4())
    a1 = repo.record_failed_analysis(meeting_id=meeting_id, error_message="429 quota")
    a2 = repo.record_failed_analysis(meeting_id=meeting_id, error_message="503 busy")
    rows = repo.client.store["mtg_analyses"]
    failed_rows = [r for r in rows if r["meeting_id"] == meeting_id]
    assert len(failed_rows) == 1  # updated in place, not piled up
    assert a1["id"] == a2["id"]
    assert failed_rows[0]["error_message"] == "503 busy"


def test_record_failed_analysis_never_clobbers_completed():
    """A failure must not overwrite an existing good report."""
    repo = _repo()
    meeting_id = str(uuid.uuid4())
    repo.create_analysis(meeting_id=meeting_id, status="completed", summary="good")
    result = repo.record_failed_analysis(meeting_id=meeting_id, error_message="429 quota")
    assert result == {}  # not stored
    assert repo.has_current_completed_analysis(meeting_id) is True
    rows = [r for r in repo.client.store["mtg_analyses"] if r["meeting_id"] == meeting_id]
    assert len(rows) == 1 and rows[0]["status"] == "completed"


def test_analyze_meeting_missing_required_field_fails():
    repo = _repo()
    bad = {"summary": "", "topics": []}  # no summary
    ai = AIClient(_config(), client=FakeAnthropic(report=bad))
    meeting = {
        "id": str(uuid.uuid4()),
        "raw_transcript": {"type": "full_transcript", "text": "text"},
    }
    result = analyze_meeting(repo, ai, meeting)
    assert result["ok"] is False
    assert result["analysis"]["status"] == "failed"
    assert "required field" in result["analysis"]["error_message"]


def test_analyze_meeting_ai_failure_saves_failed():
    repo = _repo()
    ai = AIClient(_config(), client=FakeAnthropic(raise_exc=True))
    meeting = {
        "id": str(uuid.uuid4()),
        "raw_transcript": {"type": "full_transcript", "text": "text"},
    }
    result = analyze_meeting(repo, ai, meeting)
    assert result["ok"] is False
    assert result["analysis"]["status"] == "failed"
    assert result["analysis"]["error_message"]


def test_analyze_meeting_bad_json_saves_failed():
    repo = _repo()
    ai = AIClient(_config(), client=FakeAnthropic(bad_json=True))
    meeting = {
        "id": str(uuid.uuid4()),
        "raw_transcript": {"type": "full_transcript", "text": "text"},
    }
    result = analyze_meeting(repo, ai, meeting)
    assert result["ok"] is False
    assert result["analysis"]["status"] == "failed"
    assert "JSON" in result["analysis"]["error_message"]


def test_analyze_meeting_no_transcript_saves_failed():
    repo = _repo()
    ai = AIClient(_config(), client=FakeAnthropic(report=SAMPLE_REPORT))
    meeting = {"id": str(uuid.uuid4()), "raw_transcript": {"text": ""}}
    result = analyze_meeting(repo, ai, meeting)
    assert result["ok"] is False
    assert result["analysis"]["status"] == "failed"
    assert "transcript_not_found" in result["analysis"]["error_message"]


def test_transcript_corrections_fix_known_asr_mistakes():
    """«Արցախում» (Artsakh) is a confirmed ASR mishear of «արձակուրդում» (vacation)."""
    from meeting_pipeline.analyze import apply_transcript_corrections

    fixed = apply_transcript_corrections("Էմ հիշացնում եմ, որ ես վաղը Արցախում եմ։")
    assert "Արցախ" not in fixed
    assert "արձակուրդում" in fixed
    # Env-provided corrections extend the built-in map.
    config = Config(transcript_corrections_raw="ԴԳ Ֆինանս=>DG Finance")
    fixed = apply_transcript_corrections(
        "ԴԳ Ֆինանս և Արցախ", config.transcript_corrections
    )
    assert fixed == "DG Finance և արձակուրդ"


def test_deliver_today_sends_report():
    from datetime import date

    config = _config()
    repo = _repo()
    source = repo.ensure_source("timeless")
    meeting = repo.upsert_meeting(
        source_id=source["id"],
        source_meeting_id="manual_2026_03_26",
        title="Планёрка",
        status="completed",
        actual_start="2026-03-26T05:00:00+00:00",
        raw_transcript={"type": "full_transcript", "text": "t"},
    )
    repo.create_analysis(
        meeting_id=meeting["id"],
        status="completed",
        telegram_report_md="📋 **Планёрка**\nГотово.",
    )
    session = FakeTelegramSession()
    telegram = TelegramClient(config, session=session)
    result = deliver_today(config, date_str="2026-03-26", repo=repo, telegram=telegram)
    assert result["delivered"] is True
    assert result["status"] == "delivered"
    assert len(session.calls) == 1
    assert "Планёрка" in session.calls[0]["text"]


def test_deliver_rerenders_stored_report_with_current_template():
    """Old stored analyses are re-rendered with the current rigid template.

    The 2026-03-24 report went out in the legacy model-written format because
    delivery used the stored text as-is. Now delivery re-renders from the
    structured extras, so template fixes apply without re-running the AI.
    """
    config = _config()
    repo = _repo()
    repo.client.store["mtg_participants"] = [
        {"full_name": "Эмилия Аванесян", "is_internal": True,
         "metadata": {"role": "руководитель"}},
        {"full_name": "Стелла Бухгалтер", "is_internal": True,
         "metadata": {"role": "бухгалтер"}},
    ]
    source = repo.ensure_source("timeless")
    meeting = repo.upsert_meeting(
        source_id=source["id"],
        source_meeting_id="manual_2026_03_24",
        title="Планёрка",
        status="completed",
        actual_start="2026-03-24T05:00:00+00:00",
        raw_transcript={"type": "full_transcript", "text": "t"},
    )
    legacy_md = "⚠️ Ситуация 1. Старый формат.\nСтепень риска: высокая"
    repo.create_analysis(
        meeting_id=meeting["id"],
        status="completed",
        telegram_report_md=legacy_md,
        problems_risks=[{"text": "Долг клиента.", "severity": "high",
                         "owner": "Эмилия Аванесян", "deadline": "не указан"}],
        ai_metadata={"report_extras": {
            "effectiveness": {
                "score": 6, "verdict": "Нормально.",
                "criteria": [{"criterion": "Все сотрудники высказались",
                              "status": "частично"}],
            },
            "participant_breakdown": [
                {"name": "Стелла", "participated": True,
                 "yesterday": "Указала должников.", "today_plan": [],
                 "blockers": ["нет"]},
            ],
        }},
    )
    session = FakeTelegramSession()
    telegram = TelegramClient(config, session=session)
    result = deliver_today(config, date_str="2026-03-24", repo=repo, telegram=telegram)
    assert result["delivered"] is True
    text = session.calls[0]["text"]
    # Current template, not the stored legacy text.
    assert "Степень риска" not in text and "Ситуация 1" not in text
    assert "🟡 Все высказались" in text
    assert "🔴 1. Долг клиента." in text
    assert "Что решили: ❌" in text  # legacy analysis has no decision field
    assert "👤 Стелла" in text and "План на сегодня: ❌" in text and "Блокеры: –" in text


def test_telegram_sends_converted_bold():
    config = _config()
    session = FakeTelegramSession()
    telegram = TelegramClient(config, session=session)
    result = telegram.send_message("📋 **Планёрка**\nГотово.", parse_mode="Markdown")
    assert result.ok is True
    # GFM **bold** is converted to Telegram legacy *bold* on the wire.
    assert session.calls[0]["text"] == "📋 *Планёрка*\nГотово."


class FlakyTelegramSession:
    """Fails (network error or transient HTTP) a number of times, then succeeds."""

    def __init__(self, fail_times=0, mode="raise", status_code=503):
        self.fail_times = fail_times
        self.mode = mode  # "raise" (timeout/connection) or "http"
        self.status_code = status_code
        self.calls = 0

    def post(self, url, json=None, timeout=None):
        self.calls += 1
        if self.calls <= self.fail_times:
            if self.mode == "raise":
                raise ConnectionError("simulated network timeout")
            return FakeResponse(
                status_code=self.status_code,
                payload={"ok": False, "description": "try again later"},
            )
        return FakeResponse()


def test_telegram_retries_network_error_then_succeeds():
    """A single timeout must NOT lose the report — it retries and lands."""
    config = _config()
    config.telegram_max_retries = 4
    sleeps = []
    session = FlakyTelegramSession(fail_times=2, mode="raise")
    telegram = TelegramClient(config, session=session, sleep=sleeps.append)
    result = telegram.send_message("hello", parse_mode=None)
    assert result.ok is True
    assert session.calls == 3  # 2 failures + 1 success
    assert sleeps == [2, 4]  # exponential backoff between retries


def test_telegram_retries_transient_http_then_succeeds():
    config = _config()
    config.telegram_max_retries = 4
    session = FlakyTelegramSession(fail_times=1, mode="http", status_code=503)
    telegram = TelegramClient(config, session=session, sleep=lambda s: None)
    result = telegram.send_message("hello", parse_mode=None)
    assert result.ok is True
    assert session.calls == 2


def test_telegram_gives_up_after_max_retries():
    config = _config()
    config.telegram_max_retries = 2
    session = FlakyTelegramSession(fail_times=99, mode="raise")
    telegram = TelegramClient(config, session=session, sleep=lambda s: None)
    result = telegram.send_message("hello", parse_mode=None)
    assert result.ok is False
    assert session.calls == 3  # 1 initial + 2 retries, then give up
    assert "failed" in (result.error or "").lower()


def test_get_team_roster_reads_internal_participants():
    """Roster (and thus absentee detection) is driven by mtg_participants."""
    repo = _repo()
    store = repo.client.store
    store["mtg_participants"] = [
        {"full_name": "Эмилия Аванесян", "is_internal": True,
         "metadata": {"role": "руководитель"}},
        {"full_name": "Оля Бухгалтер", "is_internal": True,
         "metadata": {"role": "бухгалтер"}},
        {"full_name": "Гор Менеджер", "is_internal": True,
         "metadata": {"role": "менеджер"}},
        {"full_name": "Внешний Клиент", "is_internal": False, "metadata": None},
    ]
    roster = repo.get_team_roster()
    names = {r["name"] for r in roster}
    # Internal only, meeting roles only (Гор-менеджер не из планёрки),
    # placeholder-фамилия «Бухгалтер» срезается, настоящая остаётся.
    assert names == {"Эмилия Аванесян", "Оля"}
    by_name = {r["name"]: r["role"] for r in roster}
    assert by_name["Эмилия Аванесян"] == "руководитель"


class _TimelessResp:
    def __init__(self, status_code=200, payload=None):
        self.status_code = status_code
        self._payload = payload
        self.headers = {}

    def json(self):
        return self._payload


class _TimelessSeqSession:
    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "params": params})
        return self._responses.pop(0)


def test_ingest_from_timeless_backfill_range():
    """End-to-end: list a range from Timeless and upsert each meeting + transcript."""
    from datetime import date

    from meeting_pipeline.timeless_client import TimelessClient

    config = Config(
        supabase_url="http://fake",
        supabase_service_role_key="fake",
        timeless_api_token="tok",
        timeless_api_base_url="https://api.timeless.test/v1",
    )
    repo = SupabaseRepo(config, client=FakeSupabaseClient())
    session = _TimelessSeqSession(
        [
            # listing (one page, no more)
            _TimelessResp(
                200,
                {
                    "data": [
                        {
                            "id": "mtg_1",
                            "title": "Планёрка 1",
                            "start_time": "2026-06-01T07:00:00+00:00",
                            "duration": 600,
                        }
                    ],
                    "has_more": False,
                    "next_cursor": None,
                },
            ),
            # transcript for mtg_1 (first template path hits)
            _TimelessResp(
                200,
                {
                    "meeting_id": "mtg_1",
                    "language": "hy",
                    "speakers": [{"id": "s1", "name": "Гор"}],
                    "segments": [{"speaker_id": "s1", "text": "Начнём."}],
                },
            ),
        ]
    )
    timeless = TimelessClient(config, session=session, sleep=lambda _s: None)

    result = ingest_from_timeless(
        repo, config, timeless, start_date=date(2026, 5, 25), end_date=date(2026, 6, 8)
    )
    assert result["ok"] is True
    assert result["saved"] == 1
    stored = repo.client.store["mtg_meetings"]
    assert len(stored) == 1
    row = stored[0]
    assert row["status"] == "completed"
    assert row["source_meeting_id"] == "timeless_mtg_1"
    assert row["duration_seconds"] == 600
    assert row["transcript_language"] == "hy"
    assert "Гор: Начнём." in row["raw_transcript"]["text"]
    # The listing used the documented date-range params.
    assert session.calls[0]["params"]["start_date"] == "2026-05-25"


def test_ingest_no_file_no_timeless_returns_recording_not_found():
    config = _config()  # timeless_api_token is None
    repo = _repo()
    timeless = TimelessClient(config)
    result = ingest_meeting(
        config, file_path=None, repo=repo, timeless=timeless
    )
    assert result["ok"] is False
    assert result["status"] == "recording_not_found"
    assert "fallback" in (result.get("detail") or "")


def test_telegram_markdown_fallback_to_plain():
    config = _config()
    session = FakeTelegramSession(fail_markdown=True)
    telegram = TelegramClient(config, session=session)
    result = telegram.send_message("📋 **bad _markdown", parse_mode="Markdown")
    assert result.ok is True
    # First attempt with parse_mode (rejected), retry without it (accepted).
    assert len(session.calls) == 2
    assert "parse_mode" in session.calls[0]
    assert "parse_mode" not in session.calls[1]


def test_deliver_today_missing_report_notifies():
    config = _config()
    repo = _repo()
    session = FakeTelegramSession()
    telegram = TelegramClient(config, session=session)
    result = deliver_today(config, date_str="2026-03-26", repo=repo, telegram=telegram)
    assert result["status"] == "report_not_found"
    assert len(session.calls) == 1
    text = session.calls[0]["text"]
    assert MISSING_REPORT_MESSAGE in text
    # Lilit and Emiliya are @-tagged so they're alerted on a no-report day.
    assert MISSING_REPORT_MENTIONS in text
    assert "@saakyans_21" in text and "@emilyaavanesyan" in text
    # Sent as plain text so the "_" in the username isn't parsed as Markdown.
    assert session.calls[0].get("parse_mode") is None


def test_deliver_missing_report_notice_sent_once_per_day():
    """Re-runs the same day must NOT spam the chat with repeat notices."""
    config = _config()
    repo = _repo()
    session = FakeTelegramSession()
    telegram = TelegramClient(config, session=session)

    first = deliver_today(config, date_str="2026-06-12", repo=repo, telegram=telegram)
    assert first["status"] == "report_not_found"
    assert len(session.calls) == 1

    # Cron fires again (e.g. every 15 minutes): nothing is sent again.
    second = deliver_today(config, date_str="2026-06-12", repo=repo, telegram=telegram)
    assert second["status"] == "notice_already_sent"
    assert len(session.calls) == 1

    # A new day gets its own notice.
    third = deliver_today(config, date_str="2026-06-13", repo=repo, telegram=telegram)
    assert third["status"] == "report_not_found"
    assert len(session.calls) == 2


def test_deliver_missing_report_notice_retried_after_failed_send():
    """A failed Telegram send releases the daily slot so the next run retries."""
    config = _config()
    config.telegram_max_retries = 1
    repo = _repo()

    broken = FlakyTelegramSession(fail_times=99, mode="raise")
    telegram = TelegramClient(config, session=broken, sleep=lambda s: None)
    first = deliver_today(config, date_str="2026-06-12", repo=repo, telegram=telegram)
    assert first["delivered"] is False

    session = FakeTelegramSession()
    telegram = TelegramClient(config, session=session)
    second = deliver_today(config, date_str="2026-06-12", repo=repo, telegram=telegram)
    assert second["status"] == "report_not_found"
    assert second["delivered"] is True
    assert len(session.calls) == 1


def test_deliver_report_not_resent_on_rerun():
    """A delivered report must not be re-sent when the cron fires again."""
    config = _config()
    repo = _repo()
    source = repo.ensure_source("timeless")
    meeting = repo.upsert_meeting(
        source_id=source["id"],
        source_meeting_id="manual_2026_03_26",
        title="Планёрка",
        status="completed",
        actual_start="2026-03-26T05:00:00+00:00",
        raw_transcript={"type": "full_transcript", "text": "t"},
    )
    repo.create_analysis(
        meeting_id=meeting["id"],
        status="completed",
        telegram_report_md="📋 **Планёрка**\nГотово.",
    )
    session = FakeTelegramSession()
    telegram = TelegramClient(config, session=session)

    first = deliver_today(config, date_str="2026-03-26", repo=repo, telegram=telegram)
    assert first["status"] == "delivered"
    assert len(session.calls) == 1

    second = deliver_today(config, date_str="2026-03-26", repo=repo, telegram=telegram)
    assert second["status"] == "already_delivered"
    assert second["delivered"] is True
    assert len(session.calls) == 1  # still only the original send

    # A deliberate manual re-send works with force=True.
    third = deliver_today(
        config, date_str="2026-03-26", force=True, repo=repo, telegram=telegram
    )
    assert third["status"] == "delivered"
    assert len(session.calls) == 2

    # The forced send does not unlock automatic re-sends afterwards.
    fourth = deliver_today(config, date_str="2026-03-26", repo=repo, telegram=telegram)
    assert fourth["status"] == "already_delivered"
    assert len(session.calls) == 2


def test_deliver_new_analysis_version_still_goes_out_same_day():
    """A forced re-analysis (new version) may be delivered the same day."""
    config = _config()
    repo = _repo()
    source = repo.ensure_source("timeless")
    meeting = repo.upsert_meeting(
        source_id=source["id"],
        source_meeting_id="manual_2026_03_26",
        title="Планёрка",
        status="completed",
        actual_start="2026-03-26T05:00:00+00:00",
        raw_transcript={"type": "full_transcript", "text": "t"},
    )
    repo.create_analysis(
        meeting_id=meeting["id"],
        status="completed",
        telegram_report_md="📋 v1",
    )
    session = FakeTelegramSession()
    telegram = TelegramClient(config, session=session)
    deliver_today(config, date_str="2026-03-26", repo=repo, telegram=telegram)
    assert len(session.calls) == 1

    # Supersede with a corrected version (the fake has no DB trigger, so
    # demote the old row by hand) — the new analysis id gets its own slot.
    for row in repo.client.store["mtg_analyses"]:
        row["is_current"] = False
        row["status"] = "superseded"
    repo.create_analysis(
        meeting_id=meeting["id"],
        status="completed",
        telegram_report_md="📋 v2 (исправленный)",
    )
    result = deliver_today(config, date_str="2026-03-26", repo=repo, telegram=telegram)
    assert result["status"] == "delivered"
    assert len(session.calls) == 2
    assert "v2" in session.calls[1]["text"]


def _gemini_config():
    return Config(
        supabase_url="http://fake",
        supabase_service_role_key="fake",
        ai_provider="gemini",
        gemini_api_key="fake",
        telegram_bot_token="fake",
        telegram_management_chat_id="123",
        timeless_api_token=None,
    )


def test_config_resolves_default_model_per_provider():
    assert _config().ai_model_id == "claude-sonnet-4-20250514"
    assert _gemini_config().ai_model_id == "gemini-2.5-pro"
    # An explicit AI_MODEL_ID always wins over the per-provider default.
    explicit = Config(ai_provider="gemini", ai_model_id="gemini-2.5-flash")
    assert explicit.ai_model_id == "gemini-2.5-flash"


def test_config_provider_validation():
    cfg = _gemini_config()
    assert cfg.has_ai is True
    cfg.require_ai()  # must not raise
    no_key = Config(ai_provider="gemini")
    assert no_key.has_ai is False
    try:
        no_key.require_ai()
        assert False, "expected RuntimeError"
    except RuntimeError as exc:
        assert "GEMINI_API_KEY" in str(exc)


def test_gemini_client_adapts_response_shape():
    """The Gemini wrapper must expose the Anthropic-style response contract."""
    client = GeminiClient(genai_client=FakeGenaiClient(SAMPLE_REPORT))
    resp = client.messages.create(
        model="gemini-2.5-pro",
        max_tokens=4096,
        system="be grounded",
        messages=[{"role": "user", "content": "transcript text"}],
    )
    assert resp.content[0].text  # .content[].text is read by AIClient
    assert resp.usage.input_tokens == 100
    assert resp.usage.output_tokens == 50


def test_gemini_client_disables_thinking_for_25_models():
    """2.5 models must run with thinking disabled so JSON isn't truncated."""
    fake = FakeGenaiClient(SAMPLE_REPORT)
    client = GeminiClient(genai_client=fake)
    client.messages.create(
        model="gemini-2.5-flash",
        max_tokens=8192,
        messages=[{"role": "user", "content": "t"}],
    )
    cfg = fake.models.last_call["config"]
    assert cfg["thinking_config"] == {"thinking_budget": 0}
    assert cfg["max_output_tokens"] == 8192


def test_gemini_client_retries_transient_then_succeeds():
    """503/429 errors are retried with backoff (sleep injected as no-op)."""
    slept = []
    fake = FakeGenaiClient(SAMPLE_REPORT, fail_times=2, error="503 UNAVAILABLE high demand")
    client = GeminiClient(genai_client=fake, sleep=slept.append)
    resp = client.messages.create(
        model="gemini-2.5-flash",
        max_tokens=8192,
        messages=[{"role": "user", "content": "t"}],
    )
    assert json.loads(resp.content[0].text)["summary"] == SAMPLE_REPORT["summary"]
    assert fake.models.calls == 3  # 2 failures + 1 success
    assert len(slept) == 2


def test_gemini_client_hard_quota_not_retried():
    """A 'limit: 0' quota (model not on tier) must fail fast, not retry."""
    slept = []
    fake = FakeGenaiClient(
        SAMPLE_REPORT, fail_times=99, error="429 RESOURCE_EXHAUSTED limit: 0"
    )
    client = GeminiClient(genai_client=fake, sleep=slept.append)
    try:
        client.messages.create(
            model="gemini-2.5-pro", max_tokens=8192, messages=[{"role": "user", "content": "t"}]
        )
        assert False, "expected the hard-quota error to propagate"
    except RuntimeError:
        pass
    assert fake.models.calls == 1  # no retries
    assert slept == []


def test_gemini_client_daily_quota_not_retried():
    """A per-day free-tier cap must fail fast so it doesn't burn the budget."""
    slept = []
    fake = FakeGenaiClient(
        SAMPLE_REPORT,
        fail_times=99,
        error="429 RESOURCE_EXHAUSTED ... GenerateRequestsPerDayPerProjectPerModel-FreeTier limit: 20",
    )
    client = GeminiClient(genai_client=fake, sleep=slept.append)
    try:
        client.messages.create(
            model="gemini-2.5-flash", max_tokens=8192, messages=[{"role": "user", "content": "t"}]
        )
        assert False, "expected the daily-quota error to propagate"
    except RuntimeError:
        pass
    assert fake.models.calls == 1  # no retries
    assert slept == []


def test_analyze_meeting_success_with_gemini():
    """End-to-end analyze using the Gemini adapter over a fake genai client."""
    repo = _repo()
    fake_genai = FakeGenaiClient(SAMPLE_REPORT)
    ai = AIClient(_gemini_config(), client=GeminiClient(genai_client=fake_genai))
    meeting = {
        "id": str(uuid.uuid4()),
        "title": "Планёрка",
        "raw_transcript": {"type": "full_transcript", "text": "long transcript text"},
    }
    result = analyze_meeting(repo, ai, meeting)
    assert result["ok"] is True
    assert result["analysis"]["summary"] == SAMPLE_REPORT["summary"]
    # System prompt + JSON mime hint must reach Gemini's request config.
    cfg = fake_genai.models.last_call["config"]
    assert cfg["system_instruction"]
    assert cfg["response_mime_type"] == "application/json"
    # Output budget comes from config (default 16384) so large reports don't
    # get truncated into invalid JSON on longer transcripts.
    assert cfg["max_output_tokens"] == 16384


# --------------------------------------------------------------------------- #
# Manual runner (no pytest required)
# --------------------------------------------------------------------------- #
def _run_all():
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
    ]
    passed = 0
    failed = 0
    for name, fn in tests:
        try:
            fn()
            print(f"PASS  {name}")
            passed += 1
        except Exception as exc:  # noqa: BLE001
            print(f"FAIL  {name}: {exc!r}")
            failed += 1
    print(f"\n{passed} passed, {failed} failed, {len(tests)} total")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(_run_all())
