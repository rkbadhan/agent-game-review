"""Tests for the Langfuse live-fetch layer (agr/langfuse_api.py).

No network access happens in this file: ``urlopen`` is monkeypatched to a
stub that hands back a canned response and records exactly what request it
was given, so the assertions are about the REQUEST agr builds (URL, auth
header) and about how agr reacts to a canned response/error — never about
reaching a real Langfuse instance.
"""

from __future__ import annotations

import base64
import json
import urllib.error

import pytest

import agr.langfuse_api as langfuse_api
from agr.ingest_langfuse import convert
from agr.langfuse_api import LangfuseAPIError, fetch_trace

_CANNED_TRACE = {
    "id": "trace-live-1",
    "name": "live-fetch-check",
    "timestamp": "2026-09-01T10:00:00.000Z",
    "input": "do the live thing",
    "output": "did the live thing",
    "observations": [
        {
            "id": "obs-1",
            "type": "TOOL",
            "parentObservationId": None,
            "name": "do_thing",
            "startTime": "2026-09-01T10:00:01.000Z",
            "level": "DEFAULT",
            "output": "done",
        },
        {
            "id": "obs-2",
            "type": "GENERATION",
            "parentObservationId": None,
            "name": "chat-1",
            "model": "gpt-x",
            "startTime": "2026-09-01T10:00:02.000Z",
            "usage": {"input": 10, "output": 5},
            "output": "ok",
        },
    ],
    "scores": [],
}


class _FakeResponse:
    """Just enough of ``http.client.HTTPResponse`` for fetch_trace: a
    context manager whose ``read()`` returns the canned body."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self) -> bytes:
        return self._body


def _install_fake_urlopen(monkeypatch, *, body: bytes | None = None, http_error: int | None = None):
    """Monkeypatch agr.langfuse_api.urlopen and capture the Request it receives."""
    captured: dict = {}

    def fake_urlopen(request, *args, **kwargs):
        captured["request"] = request
        if http_error is not None:
            raise urllib.error.HTTPError(
                request.full_url, http_error, "canned error", {}, None
            )
        return _FakeResponse(body if body is not None else b"{}")

    monkeypatch.setattr(langfuse_api, "urlopen", fake_urlopen)
    return captured


def test_fetch_trace_builds_expected_url_and_auth_header(monkeypatch):
    captured = _install_fake_urlopen(
        monkeypatch, body=json.dumps(_CANNED_TRACE).encode("utf-8")
    )
    result = fetch_trace(
        "trace-live-1",
        host="https://cloud.langfuse.com",
        public_key="pk-test-123",
        secret_key="sk-test-456",
    )
    request = captured["request"]
    assert request.full_url == (
        "https://cloud.langfuse.com/api/public/traces/trace-live-1"
        "?fields=core%2Cio%2Cscores%2Cobservations%2Cmetrics"
    )
    auth_header = request.get_header("Authorization")
    assert auth_header is not None and auth_header.startswith("Basic ")
    token = auth_header.removeprefix("Basic ")
    decoded = base64.b64decode(token).decode("utf-8")
    assert decoded == "pk-test-123:sk-test-456"
    assert result == _CANNED_TRACE


def test_fetch_trace_strips_trailing_slash_from_host(monkeypatch):
    captured = _install_fake_urlopen(
        monkeypatch, body=json.dumps(_CANNED_TRACE).encode("utf-8")
    )
    fetch_trace(
        "trace-live-1",
        host="http://localhost:3000/",
        public_key="pk",
        secret_key="sk",
    )
    assert captured["request"].full_url.startswith("http://localhost:3000/api/public/traces/")
    assert "//api" not in captured["request"].full_url


def test_fetch_trace_result_converts_with_nonempty_observations(monkeypatch):
    """End-to-end seam: fetch_trace's return value feeds straight into
    convert() with no intermediate file, and produces real observations."""
    _install_fake_urlopen(monkeypatch, body=json.dumps(_CANNED_TRACE).encode("utf-8"))
    fetched = fetch_trace("trace-live-1", host="http://localhost:3000",
                          public_key="pk", secret_key="sk")
    res = convert(fetched, task_id="t")
    assert res.meta["observation_count"] == 2
    tool_steps = [s for s in res.doc["steps"] if s.get("tool") == "do_thing"]
    assert tool_steps


def test_fetch_trace_missing_credentials_raises_value_error(monkeypatch):
    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    with pytest.raises(ValueError, match="missing Langfuse credential"):
        fetch_trace("trace-live-1")


def test_fetch_trace_missing_trace_id_raises_value_error():
    with pytest.raises(ValueError, match="trace_id is required"):
        fetch_trace("")


def test_fetch_trace_credentials_from_env(monkeypatch):
    captured = _install_fake_urlopen(
        monkeypatch, body=json.dumps(_CANNED_TRACE).encode("utf-8")
    )
    monkeypatch.setenv("LANGFUSE_HOST", "http://localhost:3000")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-env")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-env")
    fetch_trace("trace-live-1")
    auth_header = captured["request"].get_header("Authorization")
    decoded = base64.b64decode(auth_header.removeprefix("Basic ")).decode("utf-8")
    assert decoded == "pk-env:sk-env"


def test_fetch_trace_404_raises_clear_langfuse_api_error(monkeypatch):
    _install_fake_urlopen(monkeypatch, http_error=404)
    with pytest.raises(LangfuseAPIError, match="no such Langfuse trace"):
        fetch_trace("trace-missing", host="http://localhost:3000",
                    public_key="pk", secret_key="sk")


def test_fetch_trace_401_raises_auth_error_without_leaking_secret(monkeypatch):
    _install_fake_urlopen(monkeypatch, http_error=401)
    with pytest.raises(LangfuseAPIError) as excinfo:
        fetch_trace("trace-live-1", host="http://localhost:3000",
                    public_key="pk", secret_key="sk-super-secret-value")
    message = str(excinfo.value)
    assert "auth failed" in message
    assert "sk-super-secret-value" not in message


def test_fetch_trace_other_http_error_raises_langfuse_api_error(monkeypatch):
    _install_fake_urlopen(monkeypatch, http_error=500)
    with pytest.raises(LangfuseAPIError, match="500"):
        fetch_trace("trace-live-1", host="http://localhost:3000",
                    public_key="pk", secret_key="sk")


def test_fetch_trace_non_object_response_raises_langfuse_api_error(monkeypatch):
    _install_fake_urlopen(monkeypatch, body=b"[1, 2, 3]")
    with pytest.raises(LangfuseAPIError, match="non-object"):
        fetch_trace("trace-live-1", host="http://localhost:3000",
                    public_key="pk", secret_key="sk")


# --- CLI wiring: `agr ingest-langfuse-api` (Fix 5) -----------------------------

def test_cli_ingest_langfuse_api_fetches_and_ingests(monkeypatch, tmp_path):
    """The subcommand composes fetch_trace() + the langfuse adapter's
    convert() + the same _ingest_doc tail ingest-from uses -- exercised here
    without any network access (fetch_trace itself is monkeypatched)."""
    from agr.cli import main

    calls = {}

    def fake_fetch_trace(trace_id, *, host=None):
        calls["trace_id"] = trace_id
        calls["host"] = host
        return _CANNED_TRACE

    monkeypatch.setattr("agr.langfuse_api.fetch_trace", fake_fetch_trace)
    rc = main([
        "--store", str(tmp_path / "store"),
        "ingest-langfuse-api",
        "--trace-id", "trace-live-1",
        "--host", "http://localhost:3000",
        "--task-id", "demo-task",
    ])
    assert rc == 0
    assert calls == {"trace_id": "trace-live-1", "host": "http://localhost:3000"}
