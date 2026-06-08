"""Offline tests for the hardened TimelessClient.

No network and no credentials: a programmable fake session returns queued
responses and records each call. Sleep is injected as a no-op so retry/backoff
never actually waits. Run with:

    python -m pytest tests/test_timeless_client.py -v
    # or
    python tests/test_timeless_client.py
"""
from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from meeting_pipeline.config import Config
from meeting_pipeline.timeless_client import TimelessClient


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeResp:
    def __init__(self, status_code=200, payload=None, headers=None, raise_json=False):
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("invalid json")
        return self._payload


class SeqSession:
    """Returns queued responses in order; records every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def get(self, url, headers=None, params=None, timeout=None):
        self.calls.append({"url": url, "headers": headers, "params": params})
        if not self._responses:
            raise AssertionError("SeqSession ran out of queued responses")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _config(**overrides):
    base = dict(
        timeless_api_token="secret-token",
        timeless_api_base_url="https://api.timeless.test/v1",
        timeless_max_retries=3,
    )
    base.update(overrides)
    return Config(**base)


def _client(responses, **cfg_overrides):
    slept = []
    client = TimelessClient(
        _config(**cfg_overrides),
        session=SeqSession(responses),
        sleep=slept.append,
    )
    return client, slept


# --------------------------------------------------------------------------- #
# Auth scheme
# --------------------------------------------------------------------------- #
def test_auth_scheme_bearer_default():
    client, _ = _client([])
    headers = client._headers()
    assert headers["Authorization"] == "Bearer secret-token"
    assert "X-API-Key" not in headers


def test_auth_scheme_x_api_key():
    client, _ = _client([], timeless_auth_scheme="x-api-key")
    headers = client._headers()
    assert headers["X-API-Key"] == "secret-token"
    assert "Authorization" not in headers


def test_auth_scheme_token():
    client, _ = _client([], timeless_auth_scheme="token")
    assert client._headers()["Authorization"] == "Token secret-token"


# --------------------------------------------------------------------------- #
# Retry / backoff
# --------------------------------------------------------------------------- #
def test_get_retries_transient_then_succeeds():
    client, slept = _client([FakeResp(503), FakeResp(200, {"ok": True})])
    resp = client._get("meetings")
    assert resp.status_code == 200
    assert len(slept) == 1  # one backoff between the two attempts


def test_get_retry_exhausted_returns_last_response():
    client, slept = _client(
        [FakeResp(503), FakeResp(503), FakeResp(503)], timeless_max_retries=2
    )
    resp = client._get("meetings")
    assert resp.status_code == 503
    assert len(slept) == 2  # retried twice, then gave up


def test_get_network_error_then_success():
    client, slept = _client([ConnectionError("boom"), FakeResp(200, {"ok": 1})])
    resp = client._get("meetings")
    assert resp.status_code == 200
    assert len(slept) == 1


def test_get_all_network_errors_returns_none():
    client, slept = _client(
        [ConnectionError("a"), ConnectionError("b")], timeless_max_retries=1
    )
    assert client._get("meetings") is None
    assert len(slept) == 1


def test_get_honours_retry_after_header():
    client, slept = _client(
        [FakeResp(429, headers={"Retry-After": "5"}), FakeResp(200, {})]
    )
    client._get("meetings")
    assert slept == [5.0]  # used Retry-After, not the exponential default


def test_non_retryable_4xx_not_retried():
    client, slept = _client([FakeResp(404)])
    resp = client._get("meetings")
    assert resp.status_code == 404
    assert slept == []


# --------------------------------------------------------------------------- #
# Listing + pagination
# --------------------------------------------------------------------------- #
def test_list_meetings_page_number_pagination():
    client, _ = _client(
        [
            FakeResp(200, {"meetings": [{"id": "1"}], "page": 1, "total_pages": 2}),
            FakeResp(200, {"meetings": [{"id": "2"}], "page": 2, "total_pages": 2}),
        ]
    )
    result = client.list_today_meetings(date(2026, 6, 8))
    assert result.ok is True
    assert [m["id"] for m in result.meetings] == ["1", "2"]
    # Second request advanced to page 2.
    assert client._session.calls[1]["params"].get("page") == 2


def test_list_meetings_cursor_pagination():
    client, _ = _client(
        [
            FakeResp(200, {"data": [{"id": "a"}], "next_cursor": "CUR"}),
            FakeResp(200, {"data": [{"id": "b"}]}),
        ]
    )
    result = client.list_today_meetings(date(2026, 6, 8))
    assert result.ok is True
    assert [m["id"] for m in result.meetings] == ["a", "b"]
    assert client._session.calls[1]["params"].get("cursor") == "CUR"


def test_list_meetings_sends_real_query_params():
    """Matches the documented Timeless API: start_date/end_date/status."""
    client, _ = _client([FakeResp(200, {"data": [], "has_more": False})])
    client.list_today_meetings(date(2026, 6, 8))
    params = client._session.calls[0]["params"]
    assert params["start_date"] == "2026-06-08"
    assert params["end_date"] == "2026-06-08"
    assert params["status"] == "completed"


def test_list_meetings_range_params():
    client, _ = _client([FakeResp(200, {"data": [{"id": "1"}], "has_more": False})])
    result = client.list_meetings(date(2026, 5, 25), date(2026, 6, 8))
    assert result.ok is True
    params = client._session.calls[0]["params"]
    assert params["start_date"] == "2026-05-25"
    assert params["end_date"] == "2026-06-08"


def test_list_meetings_unavailable_returns_blocker():
    client, _ = _client([FakeResp(500), FakeResp(500), FakeResp(500), FakeResp(500),
                         FakeResp(500), FakeResp(500), FakeResp(500), FakeResp(500)])
    result = client.list_today_meetings(date(2026, 6, 8))
    assert result.ok is False
    assert result.error


def test_list_not_configured_returns_blocker():
    client = TimelessClient(_config(timeless_api_token=None))
    result = client.list_today_meetings(date(2026, 6, 8))
    assert result.ok is False


# --------------------------------------------------------------------------- #
# Transcript endpoint templates
# --------------------------------------------------------------------------- #
def test_get_full_transcript_uses_env_templates():
    client, _ = _client(
        [
            FakeResp(404),  # first template misses
            FakeResp(200, {"transcript": "Full text here"}),  # second hits
        ],
        timeless_transcript_path_templates="calls/{id}/x,calls/{id}/full",
    )
    result = client.get_full_transcript("ID9")
    assert result.ok is True
    assert result.transcript_text == "Full text here"
    # Confirm our custom templates (not the defaults) were used.
    assert client._session.calls[0]["url"].endswith("/calls/ID9/x")
    assert client._session.calls[1]["url"].endswith("/calls/ID9/full")


def test_get_full_transcript_builds_from_segments():
    client, _ = _client(
        [FakeResp(200, {"segments": [{"speaker": "A", "text": "Привет"}]})]
    )
    result = client.get_full_transcript("ID1")
    assert result.ok is True
    assert "A: Привет" in result.transcript_text


def test_get_full_transcript_resolves_speaker_ids():
    """Real Timeless shape: segments reference speaker_id; speakers[] maps to names."""
    client, _ = _client(
        [
            FakeResp(
                200,
                {
                    "meeting_id": "mtg_1",
                    "language": "hy",
                    "speakers": [
                        {"id": "spk_1", "name": "Гор"},
                        {"id": "spk_2", "name": "Лилит"},
                    ],
                    "segments": [
                        {"speaker_id": "spk_1", "text": "Начнём планёрку."},
                        {"speaker_id": "spk_2", "text": "Готово."},
                    ],
                },
            )
        ]
    )
    result = client.get_full_transcript("mtg_1")
    assert result.ok is True
    assert "Гор: Начнём планёрку." in result.transcript_text
    assert "Лилит: Готово." in result.transcript_text
    assert (result.raw or {}).get("language") == "hy"


# --------------------------------------------------------------------------- #
# Probe / discovery
# --------------------------------------------------------------------------- #
def test_probe_reports_working_endpoint():
    client, _ = _client(
        [
            FakeResp(200, {"meetings": [{"id": "M1"}]}),  # listing
            FakeResp(200, {"transcript": "hello world"}),  # transcript
        ]
    )
    report = client.probe()
    assert report["configured"] is True
    assert report["discovered_meeting_id"] == "M1"
    assert report["ok"] is True
    assert any(a.get("working") for a in report["attempts"])


def test_probe_not_configured():
    client = TimelessClient(_config(timeless_api_token=None))
    report = client.probe()
    assert report["configured"] is False
    assert report["ok"] is False


# --------------------------------------------------------------------------- #
# Manual runner
# --------------------------------------------------------------------------- #
def _run_all():
    tests = [
        (name, obj)
        for name, obj in sorted(globals().items())
        if name.startswith("test_") and callable(obj)
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
