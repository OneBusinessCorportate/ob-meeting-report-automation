"""Offline tests for the interview/onboarding transcription pipeline (task II).

No network, no real secrets — uses in-memory fakes. Run with:
    python -m pytest tests/test_interview_flow.py -v
    python tests/test_interview_flow.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _fakes import FakeSupabaseClient  # noqa: E402

from interview_pipeline.interview_repo import InterviewRepo  # noqa: E402
from interview_pipeline.links_source import InterviewLink, from_csv  # noqa: E402
from interview_pipeline.transcribe import (  # noqa: E402
    _resolve_source_call_id,
    transcribe_interviews,
    transcribe_link,
)
from meeting_pipeline.config import Config  # noqa: E402
from meeting_pipeline.timeless_client import TimelessClient  # noqa: E402


def _config():
    return Config(
        supabase_url="http://fake",
        supabase_service_role_key="fake",
        timeless_api_token=None,  # force local-file fallback path
        default_language="ru",
    )


def _repo():
    return InterviewRepo(_config(), client=FakeSupabaseClient())


# --- URL id extraction --------------------------------------------------------
def test_meeting_id_from_url_variants():
    f = TimelessClient.meeting_id_from_url
    assert f("https://app.timeless.day/meetings/abc123") == "abc123"
    assert f("https://timeless.day/m/xyz?ref=1") == "xyz"
    assert f("https://api.timeless.day/v1/meetings/qq/transcript") == "qq"
    assert f("") is None


def test_resolve_source_call_id_fallback_hash():
    link = InterviewLink(call_url="https://example.com/")  # no usable id segment
    smid = _resolve_source_call_id(link)
    assert smid.startswith("url_")


def test_resolve_source_call_id_explicit():
    link = InterviewLink(call_url="x", source_call_id="explicit_99")
    assert _resolve_source_call_id(link) == "explicit_99"


# --- CSV links source ---------------------------------------------------------
def test_from_csv_parses_rows():
    d = tempfile.mkdtemp()
    csv_path = Path(d) / "links.csv"
    csv_path.write_text(
        "call_url,candidate_name,role,call_type,source_call_id,transcript_file\n"
        "https://app.timeless.day/meetings/c1,Иван,бухгалтер,interview,c1,./t.txt\n"
        ",skipme,,,,\n",  # no url -> skipped
        encoding="utf-8",
    )
    links = from_csv(str(csv_path))
    assert len(links) == 1
    assert links[0].candidate_name == "Иван"
    assert links[0].source_call_id == "c1"


# --- Local-file transcription -------------------------------------------------
def test_transcribe_link_local_file_saves_full_transcript():
    repo = _repo()
    source = repo.ensure_timeless_source()
    d = tempfile.mkdtemp()
    tfile = Path(d) / "t.txt"
    tfile.write_text("Speaker 1: Полный транскрипт собеседования.", encoding="utf-8")
    link = InterviewLink(
        call_url="https://app.timeless.day/meetings/c1",
        candidate_name="Иван",
        role="бухгалтер",
        transcript_file=str(tfile),
    )
    timeless = TimelessClient(_config())
    result = transcribe_link(
        repo, timeless, source["id"], link, default_language="ru"
    )
    assert result["ok"] is True
    assert result["status"] == "done"
    row = repo.get_by_source_call_id(source["id"], "c1")
    assert row["status"] == "done"
    assert row["raw_transcript"]["type"] == "full_transcript"
    assert "Полный транскрипт" in row["raw_transcript"]["text"]


def test_transcribe_link_idempotent_skip_and_force():
    repo = _repo()
    source = repo.ensure_timeless_source()
    d = tempfile.mkdtemp()
    tfile = Path(d) / "t.txt"
    tfile.write_text("текст", encoding="utf-8")
    link = InterviewLink(
        call_url="https://app.timeless.day/meetings/c2", transcript_file=str(tfile)
    )
    timeless = TimelessClient(_config())
    first = transcribe_link(repo, timeless, source["id"], link, default_language="ru")
    assert first["status"] == "done"
    second = transcribe_link(repo, timeless, source["id"], link, default_language="ru")
    assert second["status"] == "skipped"
    forced = transcribe_link(
        repo, timeless, source["id"], link, default_language="ru", force=True
    )
    assert forced["status"] == "done"


def test_transcribe_link_no_transcript_marks_not_found():
    repo = _repo()
    source = repo.ensure_timeless_source()
    # No Timeless token, no transcript_file -> transcript_not_found, no crash.
    link = InterviewLink(call_url="https://app.timeless.day/meetings/c3")
    timeless = TimelessClient(_config())
    result = transcribe_link(repo, timeless, source["id"], link, default_language="ru")
    assert result["ok"] is False
    assert result["status"] == "transcript_not_found"
    row = repo.get_by_source_call_id(source["id"], "c3")
    assert row["status"] == "transcript_not_found"
    assert row["error_message"]


def test_transcribe_interviews_batch():
    repo = _repo()
    timeless = TimelessClient(_config())
    d = tempfile.mkdtemp()
    good = Path(d) / "good.txt"
    good.write_text("Полный транскрипт.", encoding="utf-8")
    csv_path = Path(d) / "links.csv"
    csv_path.write_text(
        "call_url,transcript_file\n"
        f"https://app.timeless.day/meetings/b1,{good}\n"
        "https://app.timeless.day/meetings/b2,\n",  # no transcript -> not found
        encoding="utf-8",
    )
    result = transcribe_interviews(
        _config(), csv_path=str(csv_path), repo=repo, timeless=timeless
    )
    assert result["processed"] == 2
    assert result["done"] == 1
    assert result["failed"] == 1


# --- Manual runner ------------------------------------------------------------
def _run_all():
    tests = [
        (n, o)
        for n, o in sorted(globals().items())
        if n.startswith("test_") and callable(o)
    ]
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
