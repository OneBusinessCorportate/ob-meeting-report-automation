"""Offline tests for the interview-analysis pipeline (intv_* schema).

No network, no real secrets — uses in-memory fakes. Run with:
    python -m pytest tests/test_interview_analysis_flow.py -v
    python tests/test_interview_analysis_flow.py
"""
from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import FakeSupabaseClient  # noqa: E402

from interview_pipeline.analysis_store import (  # noqa: E402
    STATUS_ANALYSIS_DONE,
    STATUS_ERROR,
    STATUS_LINK_MISSING,
    InterviewStore,
)
from interview_pipeline.analyze import (  # noqa: E402
    InterviewAnalyzer,
    _clamp_score,
    _normalize_recommendation,
)
from interview_pipeline.pipeline import resolve_source_call_id, sync_interviews  # noqa: E402
from interview_pipeline.sheet_source import SheetCandidate, from_csv_text  # noqa: E402
from interview_pipeline.text_clean import clean_transcript, normalize_segments  # noqa: E402
from interview_pipeline.transcript_resolver import (  # noqa: E402
    TranscriptResolver,
    google_doc_id,
    is_google_doc_link,
)
from meeting_pipeline.config import Config  # noqa: E402


# --- fakes --------------------------------------------------------------------
class _Block:
    def __init__(self, text):
        self.text = text


class _Usage:
    input_tokens = 10
    output_tokens = 20


class _Resp:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = _Usage()


class _Messages:
    def __init__(self, payload):
        self._payload = payload

    def create(self, **_kw):
        return _Resp(self._payload)


class FakeAI:
    """Stand-in for the provider client; returns a fixed JSON body."""

    def __init__(self, payload):
        self.messages = _Messages(payload)


def _config():
    return Config(
        supabase_url="http://fake",
        supabase_service_role_key="fake",
        timeless_api_token=None,  # force non-Timeless paths
        default_language="hy",
    )


_GOOD_ANALYSIS = json.dumps(
    {
        "transcript_language": "hy",
        "summary": "Кандидат уверенно отвечал по бухгалтерии.",
        "summary_original": "...",
        "candidate_strengths": ["опыт в 1С", "ясная речь"],
        "candidate_weaknesses": ["слабый английский"],
        "theses": [
            {"id": 1, "title": "Профессиональные знания", "score": 7, "comment": "знает НДС"},
            {"id": 2, "title": "Практический опыт", "score": 6, "comment": "ArmSoft базово"},
            {"id": 3, "title": "Ответственность", "score": 8, "comment": "перепроверяет"},
            {"id": 4, "title": "Стрессоустойчивость", "score": 9, "comment": "спокойна"},
            {"id": 5, "title": "Коммуникация", "score": 8, "comment": "ясная речь"},
        ],
        "knowledge_score": 7,
        "skills_score": 6,
        "responsibility_score": 8,
        "resilience_score": 9,
        "communication_score": 8,
        "overall_score": 8,
        "recommendation": "hire",
        "reasoning": "Хорошие знания и мотивация.",
        "red_flags": [],
        "next_steps": ["отправить оффер"],
    },
    ensure_ascii=False,
)


# --- normalization helpers ----------------------------------------------------
def test_clamp_score_bounds():
    assert _clamp_score(8) == 8
    assert _clamp_score(99) == 10
    assert _clamp_score(-3) == 0
    assert _clamp_score("7") == 7
    assert _clamp_score(None) is None
    assert _clamp_score("abc") is None


def test_normalize_recommendation_aliases():
    assert _normalize_recommendation("hire") == "hire"
    assert _normalize_recommendation("Нанять") == "hire"
    assert _normalize_recommendation("needs more training") == "training"
    assert _normalize_recommendation("REJECT") == "reject"
    assert _normalize_recommendation("возможно") == "maybe"
    assert _normalize_recommendation("garbage") is None


# --- google doc link helpers --------------------------------------------------
def test_google_doc_id_and_detection():
    url = "https://docs.google.com/document/d/1djkmPjekLwS0wzVLQGX79Do4foIQnfQgow6bbj5883o/edit?tab=t.0"
    assert is_google_doc_link(url)
    assert google_doc_id(url) == "1djkmPjekLwS0wzVLQGX79Do4foIQnfQgow6bbj5883o"
    assert not is_google_doc_link("https://app.timeless.day/meetings/abc")


def test_resolve_source_call_id_variants():
    gdoc = SheetCandidate(full_name="Роберт", call_url="https://docs.google.com/document/d/DOCID/edit")
    assert resolve_source_call_id(gdoc) == "gdoc_DOCID"
    tl = SheetCandidate(full_name="Иван", call_url="https://app.timeless.day/meetings/m1")
    assert resolve_source_call_id(tl) == "m1"
    nolink = SheetCandidate(full_name="Аноним", track="buh")
    assert resolve_source_call_id(nolink).startswith("nolink_buh_")


# --- sheet parsing ------------------------------------------------------------
def test_sheet_parsing_extracts_link_phone_and_status():
    csv_text = (
        "Претенденты,Резюме / Краткий коммент,Статус,Дата отправки теста,"
        "Результаты теста,Первичн. собес. (ссылка на транскриб)\n"
        "Роберт Тарланян,коммент,оффер отправлен,2026-05-18,,"
        "https://docs.google.com/document/d/DOCID/edit?tab=t.0\n"
        "Кнарик +374 77 585672,emil@mail.ru,тест отправлен,2026-02-26,,\n"
    )
    cands = from_csv_text(csv_text, "Бух")
    assert len(cands) == 2
    robert = cands[0]
    assert robert.full_name == "Роберт Тарланян"
    assert "docs.google.com" in robert.call_url
    assert robert.sheet_status == "оффер отправлен"
    assert robert.track == "buh"
    knarik = cands[1]
    assert knarik.phone and "374" in knarik.phone
    assert knarik.email == "emil@mail.ru"
    assert knarik.call_url is None  # no link -> will become link_missing


def test_sheet_two_status_columns_split_into_sheet_and_decision():
    # «Бух» has TWO columns named «Статус»: 1st=test status, 2nd=hiring decision.
    csv_text = (
        "Претенденты,Резюме,Статус,Дата отправки теста,Результаты теста,"
        "Первичн. собес. (ссылка на транскриб),Статус,Грейд стартовый\n"
        "Стелла,,тест заполнен,2026-01-16,,,оффер отправлен,Начинающий бухгалтер\n"
        "Арпине,,не подходит,2026-01-19,,,мы отказали,\n"
    )
    cands = from_csv_text(csv_text, "Бух")
    assert cands[0].sheet_status == "тест заполнен"
    assert cands[0].decision_status == "оффер отправлен"
    assert cands[0].role == "бухгалтер"  # track-based default
    assert cands[1].decision_status == "мы отказали"


def test_sheet_scans_row_for_misplaced_link():
    # Link sits in a non-designated column (mirrors the real messy sheet).
    csv_text = (
        "Претенденты,Статус,Колонка,Ещё\n"
        "Давит,оффер отправлен,note,https://docs.google.com/document/d/XYZ/edit\n"
    )
    cands = from_csv_text(csv_text, "Бух")
    assert cands[0].call_url.endswith("/edit")
    assert cands[0].source_column == "scanned_row"


# --- transcript cleaning ------------------------------------------------------
def test_clean_transcript_strips_timestamps_and_blanklines():
    raw = "[00:01] Speaker 1: Բարև\n\n\n\n00:05 Speaker 2:  Привет   мир\n"
    cleaned = clean_transcript(raw)
    assert "00:01" not in cleaned
    assert "Привет мир" in cleaned
    assert "\n\n\n" not in cleaned


def test_normalize_segments():
    segs = normalize_segments(
        [{"speaker": "S1", "start": 1, "end": 3, "text": "hi"}, {"text": ""}]
    )
    assert len(segs) == 1
    assert segs[0]["idx"] == 0 and segs[0]["speaker"] == "S1"
    assert segs[0]["start_ms"] == 1000  # seconds -> ms


# --- transcript resolver (local file) -----------------------------------------
def test_resolver_local_file():
    d = tempfile.mkdtemp()
    f = Path(d) / "t.txt"
    f.write_text("Полный транскрипт собеседования.", encoding="utf-8")
    res = TranscriptResolver(_config()).resolve(None, transcript_file=str(f))
    assert res.ok and res.source == "manual_file"
    assert "Полный транскрипт" in res.text


def test_resolver_missing_file_not_ok():
    res = TranscriptResolver(_config()).resolve(None, transcript_file="/no/such.txt")
    assert not res.ok and res.source == "manual_file"


def test_resolver_no_link_not_ok():
    res = TranscriptResolver(_config()).resolve(None)
    assert not res.ok


# --- analyzer -----------------------------------------------------------------
def test_analyzer_parses_and_normalizes():
    analyzer = InterviewAnalyzer(_config(), client=FakeAI(_GOOD_ANALYSIS))
    res = analyzer.analyze("какой-то транскрипт", candidate_name="Иван")
    assert res.ok
    assert res.recommendation == "hire"
    assert res.overall_score == 8
    assert "опыт в 1С" in res.candidate_strengths
    # 5 theses parsed in order, scores mapped to the flat fields.
    assert [t["id"] for t in res.theses] == [1, 2, 3, 4, 5]
    assert res.knowledge_score == 7
    assert res.skills_score == 6
    assert res.responsibility_score == 8
    assert res.resilience_score == 9
    assert res.communication_score == 8
    assert res.theses[0]["comment"] == "знает НДС"


def test_analyzer_invalid_json_fails_gracefully():
    analyzer = InterviewAnalyzer(_config(), client=FakeAI("not json at all"))
    res = analyzer.analyze("текст")
    assert not res.ok and "JSON" in res.error


def test_analyzer_empty_transcript():
    analyzer = InterviewAnalyzer(_config(), client=FakeAI(_GOOD_ANALYSIS))
    res = analyzer.analyze("   ")
    assert not res.ok


# --- store --------------------------------------------------------------------
def _store():
    return InterviewStore(_config(), client=FakeSupabaseClient())


def test_store_candidate_interview_analysis_scores():
    store = _store()
    cand = store.upsert_candidate(SheetCandidate(full_name="Иван", track="buh", role="бухгалтер"))
    interview = store.upsert_interview(
        candidate_id=cand["id"], source_id="src", source_call_id="m1", call_url="u"
    )
    analysis = store.create_analysis(
        interview_id=interview["id"], candidate_id=cand["id"], status="completed",
        summary="ok", recommendation="hire",
    )
    scores = store.create_scores(
        analysis_id=analysis["id"], interview_id=interview["id"], candidate_id=cand["id"],
        knowledge_score=7, skills_score=6, responsibility_score=8, resilience_score=9,
        communication_score=8, overall_score=8,
    )
    assert analysis["recommendation"] == "hire"
    assert scores["overall_score"] == 8
    assert scores["knowledge_score"] == 7
    # Candidate dedup: same track+name returns the same row.
    again = store.upsert_candidate(SheetCandidate(full_name="Иван", track="buh"))
    assert again["id"] == cand["id"]


# --- end-to-end pipeline (fakes) ----------------------------------------------
def test_pipeline_end_to_end_with_fakes():
    config = _config()
    store = _store()
    analyzer = InterviewAnalyzer(config, client=FakeAI(_GOOD_ANALYSIS))
    resolver = TranscriptResolver(config)

    d = tempfile.mkdtemp()
    tfile = Path(d) / "robert.txt"
    tfile.write_text("Speaker 1: Բարև ձեզ. Опыт работы 5 лет.", encoding="utf-8")
    csv_path = Path(d) / "buh.csv"
    csv_path.write_text(
        "Претенденты,Статус,Первичн. собес. (ссылка на транскриб),transcript_file\n"
        f"Роберт,оффер отправлен,,{tfile}\n"      # manual file -> full flow
        "Кнарик,тест отправлен,,\n",               # no link -> link_missing
        encoding="utf-8",
    )

    result = sync_interviews(
        config, csv_path=str(csv_path), store=store, resolver=resolver, analyzer=analyzer,
    )
    assert result["processed"] == 2
    assert result["counts"].get(STATUS_ANALYSIS_DONE) == 1
    assert result["counts"].get(STATUS_LINK_MISSING) == 1

    # Robert's interview reached analysis_done with stored analysis + scores.
    interviews = store.client.table("intv_interviews").select("*").execute().data
    robert = next(i for i in interviews if i["status"] == STATUS_ANALYSIS_DONE)
    transcripts = store.client.table("intv_transcripts").select("*").eq("interview_id", robert["id"]).execute().data
    assert transcripts and transcripts[0]["raw_text"]
    assert transcripts[0]["cleaned_text"]
    analyses = store.client.table("intv_analyses").select("*").eq("interview_id", robert["id"]).execute().data
    assert analyses[0]["recommendation"] == "hire"

    # Rerun is idempotent: no second analysis_done flips to error, robert is skipped.
    result2 = sync_interviews(
        config, csv_path=str(csv_path), store=store, resolver=resolver, analyzer=analyzer,
    )
    assert result2["counts"].get("skipped", 0) >= 1


def test_pipeline_handles_unfetchable_link_as_error():
    config = _config()
    store = _store()
    analyzer = InterviewAnalyzer(config, client=FakeAI(_GOOD_ANALYSIS))
    resolver = TranscriptResolver(config)
    d = tempfile.mkdtemp()
    csv_path = Path(d) / "buh.csv"
    # A google-doc link with no creds and no public access -> error (no crash).
    csv_path.write_text(
        "Претенденты,Статус,Первичн. собес. (ссылка на транскриб)\n"
        "Тест,на собес,https://docs.google.com/document/d/NOACCESS/edit\n",
        encoding="utf-8",
    )

    class _DeadResolver(TranscriptResolver):
        def resolve(self, call_url, *, transcript_file=None):
            from interview_pipeline.transcript_resolver import TranscriptResult
            return TranscriptResult(ok=False, source="google_docs", error="not readable")

    result = sync_interviews(
        config, csv_path=str(csv_path), store=store, resolver=_DeadResolver(config), analyzer=analyzer,
    )
    assert result["counts"].get(STATUS_ERROR) == 1
    assert result["processed"] == 1  # did not crash


# --- manual links (no sheet / no creds) ---------------------------------------
def test_parse_links_cli():
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "sync_cli", str(Path(__file__).resolve().parents[1] / "scripts" / "sync_interviews.py")
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    rows = mod._parse_links(["Роберт|https://docs.google.com/document/d/D1/edit", "https://x/y"])
    assert rows[0].full_name == "Роберт" and "D1" in rows[0].call_url
    assert rows[1].full_name == "Кандидат 2"


def test_sync_with_manual_candidates_bypasses_sheet():
    config = _config()
    store = _store()
    resolver = TranscriptResolver(config)
    analyzer = InterviewAnalyzer(config, client=FakeAI(_GOOD_ANALYSIS))
    d = tempfile.mkdtemp()
    tfile = Path(d) / "t.txt"
    tfile.write_text("Полный транскрипт.", encoding="utf-8")
    cand = SheetCandidate(full_name="Тест Линк", track="buh", call_url="x", transcript_file=str(tfile))
    res = sync_interviews(config, candidates=[cand], store=store, resolver=resolver, analyzer=analyzer)
    assert res["processed"] == 1 and res["counts"].get(STATUS_ANALYSIS_DONE) == 1


# --- scheduler (15/30-day cadence) --------------------------------------------
def test_decide_kind_cadence():
    from datetime import datetime, timedelta, timezone
    from interview_pipeline.schedule import decide_kind

    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # Never run before -> full.
    assert decide_kind(None, None, now) == "full"
    # Full 31 days ago -> full again.
    assert decide_kind(now - timedelta(days=31), now - timedelta(days=10), now) == "full"
    # Full 20 days ago, mini 16 days ago -> mini due (15) but not full (30).
    assert decide_kind(now - timedelta(days=20), now - timedelta(days=16), now) == "mini"
    # Full 10 days ago, mini 5 days ago -> nothing due.
    assert decide_kind(now - timedelta(days=10), now - timedelta(days=5), now) is None


def test_run_scheduled_not_due_when_recent():
    from datetime import datetime, timezone
    from interview_pipeline.analysis_store import SYNC_LOGS
    from interview_pipeline.schedule import run_scheduled

    store = _store()
    now = datetime(2026, 6, 1, tzinfo=timezone.utc)
    # Seed recent full + mini markers (created just now).
    for kind in ("full", "mini"):
        store.client.table(SYNC_LOGS).insert(
            {"stage": "schedule", "status": f"{kind}_done", "created_at": now.isoformat()}
        ).execute()
    result = run_scheduled(_config(), store=store, now=now)
    assert result["ran"] is False and result["reason"] == "not_due"


def test_run_scheduled_mini_no_source_processes_zero_and_no_marker():
    from interview_pipeline.analysis_store import SYNC_LOGS
    from interview_pipeline.schedule import run_scheduled

    store = _store()
    # force mini; no sheet source configured -> 0 processed, timer NOT marked.
    result = run_scheduled(_config(), store=store, force_kind="mini")
    assert result["ran"] is True and result["kind"] == "mini"
    assert result["processed"] == 0
    markers = store.client.table(SYNC_LOGS).select("*").eq("stage", "schedule").execute().data
    assert markers == []  # nothing processed -> retries next day


# --- manual runner ------------------------------------------------------------
def _run_all():
    tests = [(n, o) for n, o in sorted(globals().items()) if n.startswith("test_") and callable(o)]
    passed = failed = 0
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
