"""Browse, select and batch-import Langfuse traces from the UI's API, against a fake Langfuse."""
import copy
import json
from pathlib import Path
import time
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from agr import langfuse_v2
from agr.api import create_app
from agr.imports import ImportProblem
from agr.workspace import atomic_json

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "langfuse" / "langfuse_toy_run.json").read_text())
WINDOW = {"from_time": "2026-09-01T00:00:00Z", "to_time": "2026-09-02T00:00:00Z"}
HOST = "https://langfuse.example"


class FakeLangfuse:
    """Just enough of observations v2 and scores v3 to list and fetch whole traces."""

    def __init__(self, count=5, page=2, recent=False):
        from datetime import datetime, timedelta, timezone
        self.page, self.calls, self.missing = page, [], set()
        # `recent` places traces within the last hour, for tests that search with a relative window.
        now = datetime.now(timezone.utc)
        start = (lambda n: (now - timedelta(minutes=n)).isoformat().replace("+00:00", "Z")) if recent else (lambda n: f"2026-09-01T10:{n % 60:02d}:00.000Z")
        self.traces = [
            {"id": f"trace-{n:02d}", "name": "config-fixer" if n % 2 else "triage", "user": "ana" if n < 3 else "ben", "tags": ["prod"] if n % 2 else ["staging"],
             "start": start(n)} for n in range(1, count + 1)]

    def root_row(self, t):
        return {"id": f"root-{t['id']}", "traceId": t["id"], "traceName": t["name"], "name": t["name"], "type": "AGENT", "startTime": t["start"], "userId": t["user"],
                "sessionId": "s-" + t["id"], "tags": t["tags"], "environment": "default", "isRootObservation": True, "parentObservationId": None}

    def get(self, url, authorization):
        assert authorization.startswith("Basic ")
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        self.calls.append((urlsplit(url).path, params))
        if urlsplit(url).path.endswith("/v3/scores"):
            return {"data": [], "meta": {}}
        if "traceId" in params:
            if params["traceId"] in self.missing:
                return {"data": [], "meta": {}}
            rows = [dict(o, traceId=params["traceId"], id=f"{params['traceId']}-{o['id']}") for o in FIXTURE["observations"]]
            rows[0]["isRootObservation"] = True
            for row in rows:
                row.pop("parentObservationId", None)
            return {"data": rows, "meta": {}}
        rows = [self.root_row(t) for t in self.traces]
        if "filter" in params:
            for rule in json.loads(params["filter"]):
                if rule["column"] == "traceName":
                    rows = [r for r in rows if r["traceName"] == rule["value"]]
                if rule["column"] == "tags":
                    rows = [r for r in rows if set(rule["value"]) & set(r["tags"])]
        # Child observations that must never be listed as traces.
        rows.insert(1, {"id": "child", "traceId": rows[0]["traceId"], "name": "tool", "parentObservationId": rows[0]["id"], "startTime": rows[0]["startTime"]} if rows else None)
        rows = [r for r in rows if r]
        start = int(params.get("cursor", 0))
        chunk = rows[start:start + self.page]
        more = start + self.page < len(rows)
        return {"data": chunk, "meta": {"cursor": str(start + self.page) if more else None}}


@pytest.fixture
def langfuse(monkeypatch):
    fake = FakeLangfuse()
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    return fake


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "store"))
    with TestClient(app, headers={"authorization": "Bearer " + app.state.workspace_session_token}) as result:
        yield result
    app.state.workspace.executor.shutdown(wait=True)


@pytest.fixture
def project(client):
    return client.post("/projects", json={"name": "Production traces"}).json()["id"]


@pytest.fixture
def connection(client, project, langfuse):
    response = client.post(f"/projects/{project}/connections", json={"name": "Prod", "host": HOST, "public_key": "pk-lf-visible", "secret_key": "sk-lf-hidden"})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def route(project, connection, tail):
    return f"/projects/{project}/connections/{connection}/{tail}"


def list_all(**kwargs):
    base = dict(host=HOST, public_key="pk", secret_key="sk", **WINDOW)
    return langfuse_v2.list_traces(**{**base, **kwargs})


def test_listing_groups_root_observations_across_pages(langfuse):
    result = list_all(limit=50)
    assert [t["trace_id"] for t in result["traces"]] == [f"trace-{n:02d}" for n in range(5, 0, -1)]
    assert result["truncated"] is False
    assert result["traces"][0]["user_id"] == "ben" and result["traces"][-1]["tags"] == ["prod"]
    first = langfuse.calls[0][1]
    assert first["isRootObservation"] == "true" and first["fromStartTime"].startswith("2026-09-01") and "traceId" not in first


def test_listing_filters_and_reports_truncation(langfuse):
    assert [t["trace_id"] for t in list_all(name="config-fixer")["traces"]] == ["trace-05", "trace-03", "trace-01"]
    assert {t["user_id"] for t in list_all(user_id="ana")["traces"]} == {"ana"}
    assert [t["trace_id"] for t in list_all(tag="staging")["traces"]] == ["trace-04", "trace-02"]
    capped = list_all(limit=2)
    assert len(capped["traces"]) == 2 and capped["truncated"] is True


def test_listing_rechecks_filters_the_server_ignored(langfuse):
    # A server that ignores the JSON filter must not widen the engineer's selection.
    original = langfuse.get
    langfuse.get = lambda url, auth: original(url.replace("filter=", "ignored="), auth)
    assert [t["trace_id"] for t in list_all(name="triage")["traces"]] == ["trace-04", "trace-02"]


@pytest.mark.parametrize("kwargs,code", [({"limit": 0}, "invalid_limit"), ({"limit": 9999}, "invalid_limit"), ({"limit": "many"}, "invalid_limit"),
                                         ({"from_time": "2026-09-02T00:00:00Z", "to_time": "2026-09-01T00:00:00Z"}, "invalid_time_range"), ({"name": "x" * 300}, "invalid_filter")])
def test_listing_rejects_bad_input_before_calling_langfuse(langfuse, kwargs, code):
    with pytest.raises(ImportProblem) as failure:
        list_all(**kwargs)
    assert failure.value.code == code and not langfuse.calls


def test_listing_stops_on_a_repeated_cursor(monkeypatch):
    monkeypatch.setattr(langfuse_v2, "_get", lambda url, auth: {"data": [], "meta": {"cursor": "same"}})
    with pytest.raises(ImportProblem) as failure:
        list_all()
    assert failure.value.code == "unsupported_response"


def test_connection_test_reports_access_and_failure(client, project, connection, langfuse, monkeypatch):
    ok = client.post(route(project, connection, "test"))
    assert ok.status_code == 200 and ok.json()["status"] == "accessible"
    def denied(url, authorization):
        raise ImportProblem("source_auth_failed", "Authentication failed. Check the public and secret keys for this project.", 502)
    monkeypatch.setattr(langfuse_v2, "_get", denied)
    bad = client.post(route(project, connection, "test"))
    assert bad.status_code == 502 and bad.json()["detail"]["code"] == "source_auth_failed"
    assert client.get(f"/projects/{project}/connections").json()["connections"][0]["status"] == "access_failed"


def test_browse_batch_preview_and_import(client, project, connection, langfuse):
    found = client.post(route(project, connection, "traces"), json={**WINDOW, "tag": "prod", "limit": 10})
    assert found.status_code == 200, found.text
    traces = found.json()["traces"]
    assert [t["trace_id"] for t in traces] == ["trace-05", "trace-03", "trace-01"]
    assert not any(t["already_imported"] for t in traces)
    wanted = ["trace-05", "trace-01", "trace-99"]
    langfuse.missing.add("trace-99")
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": wanted, **WINDOW})
    assert response.status_code == 200, response.text
    manifest = response.json()
    assert len(manifest["items"]) == 2 and all(i["status"] == "new" for i in manifest["items"])
    assert [e["trace_id"] for e in manifest["fetch_errors"]] == ["trace-99"]
    # Observations are fetched past the search window, but never into the future.
    fetched = [params for path, params in langfuse.calls if params.get("traceId") == "trace-05" and "observations" in path][0]
    assert fetched["fromStartTime"].startswith("2026-09-01T00:00") and fetched["toStartTime"].startswith("2026-09-03T00:00")
    job = client.post(f"/projects/{project}/imports", json={"preview_id": manifest["id"], "manifest_hash": manifest["manifest_hash"], "selected": [i["id"] for i in manifest["items"]], "idempotency_key": "browse-1", "label": "Prod traces"}).json()
    deadline = time.monotonic() + 15
    while job["status"] in ("queued", "importing") and time.monotonic() < deadline:
        time.sleep(0.02)
        job = client.get(f"/projects/{project}/imports/{job['id']}").json()
    assert job["status"] == "completed", job
    assert len([r for r in job["results"] if r["run_id"]]) == 2
    again = client.post(route(project, connection, "traces"), json={**WINDOW, "tag": "prod", "limit": 10}).json()["traces"]
    assert {t["trace_id"]: t["already_imported"] for t in again} == {"trace-05": True, "trace-03": False, "trace-01": True}
    runs = {r["run_id"] for r in client.get("/queue", headers={"x-agr-project": project}).json()["runs"]}
    assert len(runs) == 2


def test_credentials_never_appear_in_responses(client, project, connection, langfuse):
    bodies = [client.get(f"/projects/{project}/connections").text, client.post(route(project, connection, "test")).text,
              client.post(route(project, connection, "traces"), json=WINDOW).text,
              client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["trace-01"], **WINDOW}).text]
    assert not any("sk-lf-hidden" in b or "pk-lf-visible" in b for b in bodies)


@pytest.mark.parametrize("selection", [[], "trace-01", [1], [""], [" "]])
def test_batch_preview_rejects_invalid_selection(client, project, connection, selection):
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": selection, **WINDOW})
    assert response.status_code == 422, response.text


def test_batch_preview_is_capped(client, project, connection):
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": [f"t{n}" for n in range(101)], **WINDOW})
    assert response.status_code == 422 and "between 1 and 100" in response.text


@pytest.mark.parametrize("code,message", [("source_auth_failed", "auth"), ("source_unreachable", "down")])
def test_batch_preview_stops_on_connection_level_failures(client, project, connection, monkeypatch, code, message):
    def fail(*args, **kwargs):
        raise ImportProblem(code, message, 502)
    monkeypatch.setattr(langfuse_v2, "fetch_trace", fail)
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["a", "b", "c"], **WINDOW})
    assert response.status_code == 502 and response.json()["detail"]["code"] == code


def test_batch_preview_keeps_good_traces_when_rate_limited(client, project, connection, monkeypatch):
    real = copy.copy(langfuse_v2.fetch_trace)
    calls = []
    def limited(trace_id, **kwargs):
        calls.append(trace_id)
        if trace_id == "trace-02":
            raise ImportProblem("source_rate_limited", "Langfuse rate-limited this request. Wait and try again.", 502)
        return real(trace_id, **kwargs)
    monkeypatch.setattr(langfuse_v2, "fetch_trace", limited)
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["trace-01", "trace-02"], **WINDOW})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 1
    assert [e["trace_id"] for e in body["fetch_errors"]] == ["trace-02"] and body["fetch_errors"][0]["code"] == "source_rate_limited"


def test_truncation_is_reported_only_when_more_exist(langfuse):
    assert list_all(limit=5)["truncated"] is False
    assert list_all(limit=4)["truncated"] is True
    assert list_all(limit=6)["truncated"] is False


def test_filters_fail_closed_when_the_response_omits_the_field(monkeypatch):
    def bare(url, authorization):
        # No tags, user, session, environment or traceName on the rows.
        return {"data": [{"id": "r1", "traceId": "t1", "name": "root-name", "startTime": "2026-09-01T10:00:00Z", "isRootObservation": True}], "meta": {}}
    monkeypatch.setattr(langfuse_v2, "_get", bare)
    for filters in ({"tag": "prod"}, {"user_id": "ana"}, {"session_id": "s"}, {"environment": "default"}, {"name": "root-name"}):
        assert list_all(**filters)["traces"] == [], filters
    assert [t["trace_id"] for t in list_all()["traces"]] == ["t1"]


def test_unusual_trace_ids_do_not_break_the_listing(client, project, connection, langfuse):
    langfuse.traces.append({"id": "order:42 retry/../x", "name": "triage", "user": "ana", "tags": ["prod"], "start": "2026-09-01T11:00:00.000Z"})
    response = client.post(route(project, connection, "traces"), json=WINDOW)
    assert response.status_code == 200, response.text
    odd = next(t for t in response.json()["traces"] if t["trace_id"].startswith("order:42"))
    assert odd["already_imported"] is False


def many(count):
    return [f"trace-{n:03d}" for n in range(count)]


def test_rate_limit_in_a_large_batch_keeps_fetched_traces(client, project, connection, monkeypatch):
    real = langfuse_v2.fetch_trace
    calls = []
    def limited(trace_id, **kwargs):
        calls.append(trace_id)
        if len(calls) == 1:
            raise ImportProblem("source_rate_limited", "Langfuse rate-limited this request. Wait and try again.", 502)
        time.sleep(0.05)
        return real(trace_id, **kwargs)
    monkeypatch.setattr(langfuse_v2, "fetch_trace", limited)
    monkeypatch.setattr(langfuse_v2, "_get", FakeLangfuse().get)
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": many(12), **WINDOW})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(calls) < 12
    assert len(body["items"]) + len(body["fetch_errors"]) == 12
    assert body["items"] and {e["code"] for e in body["fetch_errors"]} <= {"source_rate_limited"}


def test_fatal_error_stops_a_large_batch_early(client, project, connection, monkeypatch):
    calls = []
    def failing(trace_id, **kwargs):
        calls.append(trace_id)
        if len(calls) == 1:
            raise ImportProblem("source_auth_failed", "Authentication failed.", 502)
        time.sleep(0.2)
        raise ImportProblem("source_auth_failed", "Authentication failed.", 502)
    monkeypatch.setattr(langfuse_v2, "fetch_trace", failing)
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": many(40), **WINDOW})
    assert response.status_code == 502 and response.json()["detail"]["code"] == "source_auth_failed"
    time.sleep(0.5)
    assert len(calls) <= 8, len(calls)


def test_batch_stops_at_the_size_cap_and_reports_the_rest(client, project, connection, langfuse, monkeypatch):
    from agr import workspace
    real = langfuse_v2.fetch_trace
    calls = []
    one = len(json.dumps(real("trace-001", host=HOST, public_key="pk", secret_key="sk", **WINDOW)))
    def counted(trace_id, **kwargs):
        calls.append(trace_id)
        time.sleep(0.05)
        return real(trace_id, **kwargs)
    monkeypatch.setattr(langfuse_v2, "fetch_trace", counted)
    monkeypatch.setattr(workspace, "MAX_ARCHIVE", int(one * 2.5))
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": many(40), **WINDOW})
    assert response.status_code == 200, response.text
    body = response.json()
    assert 1 <= len(body["items"]) <= 2
    assert len(body["items"]) + len(body["fetch_errors"]) == 40
    assert {e["code"] for e in body["fetch_errors"]} <= {"import_limit"}
    assert len(calls) <= 12, len(calls)


def test_every_trace_missing_reports_not_found(client, project, connection, langfuse):
    langfuse.missing.update({"gone-1", "gone-2"})
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["gone-1", "gone-2"], **WINDOW})
    assert response.status_code == 404 and response.json()["detail"]["code"] == "source_not_found"


def staging_previews(client, project):
    return [p for p in client.app.state.workspace.records("previews", project) if p["status"] == "staging"]


def test_one_oversized_trace_is_skipped_not_fatal(client, project, connection, langfuse, monkeypatch):
    from agr import workspace
    real = langfuse_v2.fetch_trace
    normal = len(json.dumps(real("trace-001", host=HOST, public_key="pk", secret_key="sk", **WINDOW)))
    def sized(trace_id, **kwargs):
        data = real(trace_id, **kwargs)
        if trace_id == "trace-003":
            data["padding"] = "a" * 5000
        return data
    monkeypatch.setattr(langfuse_v2, "fetch_trace", sized)
    monkeypatch.setattr(workspace, "MAX_FILE", normal + 1000)
    ids = [f"trace-{n:03d}" for n in range(1, 9)]
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ids, **WINDOW})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 7
    assert [(e["trace_id"], e["code"]) for e in body["fetch_errors"]] == [("trace-003", "import_limit")]
    assert not staging_previews(client, project)


def test_failed_staging_discards_the_preview(client, project, connection, langfuse, monkeypatch):
    workspace = client.app.state.workspace
    real, calls = workspace.stage_file, []
    def failing(*args, **kwargs):
        calls.append(1)
        if len(calls) == 2:
            raise ImportProblem("storage_unavailable", "Free local disk space and try again.", 507)
        return real(*args, **kwargs)
    monkeypatch.setattr(workspace, "stage_file", failing)
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["trace-001", "trace-002", "trace-003"], **WINDOW})
    assert response.status_code == 507
    assert not staging_previews(client, project)
    assert not any((workspace.root / "staging" / p["id"] / "files").exists() and any((workspace.root / "staging" / p["id"] / "files").iterdir()) for p in workspace.records("previews", project))


def test_unfetchable_trace_ids_are_not_listed_and_do_not_fail_a_preview(client, project, connection, langfuse):
    langfuse.traces.append({"id": "x" * 600, "name": "triage", "user": "ana", "tags": ["prod"], "start": "2026-09-01T11:00:00.000Z"})
    langfuse.traces.append({"id": " padded ", "name": "triage", "user": "ana", "tags": ["prod"], "start": "2026-09-01T11:05:00.000Z"})
    listed = client.post(route(project, connection, "traces"), json=WINDOW).json()["traces"]
    assert len(listed) == 5 and all(t["trace_id"] == t["trace_id"].strip() and len(t["trace_id"]) <= 512 for t in listed)
    mixed = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["trace-001", "x" * 600, " padded ", 7, "trace-002"], **WINDOW})
    assert mixed.status_code == 200, mixed.text
    body = mixed.json()
    assert len(body["items"]) == 2
    assert [e["code"] for e in body["fetch_errors"]] == ["invalid_trace_id"] * 3


def test_a_late_timeout_keeps_the_traces_already_fetched(client, project, connection, langfuse, monkeypatch):
    real = langfuse_v2.fetch_trace
    def flaky(trace_id, **kwargs):
        if trace_id == "trace-009":
            time.sleep(0.4)
            raise ImportProblem("source_unreachable", "The host could not be reached or timed out.", 502)
        return real(trace_id, **kwargs)
    monkeypatch.setattr(langfuse_v2, "fetch_trace", flaky)
    ids = [f"trace-{n:03d}" for n in range(10)]
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ids, **WINDOW})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 9 and [e["trace_id"] for e in body["fetch_errors"]] == ["trace-009"]


def test_repeated_timeouts_stop_the_batch(client, project, connection, langfuse, monkeypatch):
    real, calls = langfuse_v2.fetch_trace, []
    def dying(trace_id, **kwargs):
        calls.append(trace_id)
        if trace_id != "trace-000":
            time.sleep(0.05)
            raise ImportProblem("source_unreachable", "The host could not be reached or timed out.", 502)
        return real(trace_id, **kwargs)
    monkeypatch.setattr(langfuse_v2, "fetch_trace", dying)
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": many(40), **WINDOW})
    assert response.status_code == 200, response.text
    assert len(calls) <= 14, len(calls)
    assert {e["code"] for e in response.json()["fetch_errors"]} == {"source_unreachable"}


def test_batch_time_limit_reports_the_rest(client, project, connection, langfuse, monkeypatch):
    from agr import workspace
    real, calls = langfuse_v2.fetch_trace, []
    def slow(trace_id, **kwargs):
        calls.append(trace_id)
        time.sleep(0.1)
        return real(trace_id, **kwargs)
    monkeypatch.setattr(langfuse_v2, "fetch_trace", slow)
    monkeypatch.setattr(workspace, "MAX_BATCH_SECONDS", 0.6)
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": many(40), **WINDOW})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["items"] and len(calls) < 40
    assert {e["code"] for e in body["fetch_errors"]} == {"source_timeout"}


@pytest.mark.parametrize("flip", [False, True])
def test_total_failure_status_does_not_depend_on_finish_order(client, project, connection, monkeypatch, flip):
    def failing(trace_id, **kwargs):
        slow_one = trace_id == ("b" if flip else "a")
        if slow_one:
            time.sleep(0.2)
        if trace_id == "a":
            raise ImportProblem("source_not_found", "No observations for this trace were found.", 404)
        raise ImportProblem("source_rate_limited", "Langfuse rate-limited this request. Wait and try again.", 502)
    monkeypatch.setattr(langfuse_v2, "fetch_trace", failing)
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["a", "b"], **WINDOW})
    assert response.status_code == 502 and response.json()["detail"]["code"] == "source_rate_limited"


def test_wrong_endpoint_keeps_its_own_status_in_a_batch(client, project, connection, monkeypatch):
    def failing(trace_id, **kwargs):
        raise ImportProblem("source_not_found", "This endpoint was not found. Check the host and selected Langfuse API version.", 502)
    monkeypatch.setattr(langfuse_v2, "fetch_trace", failing)
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["a", "b"], **WINDOW})
    assert response.status_code == 502 and response.json()["detail"]["code"] == "source_not_found"


@pytest.mark.parametrize("failure", [ConnectionResetError("reset"), __import__("http.client").client.RemoteDisconnected("closed"), __import__("http.client").client.IncompleteRead(b"x")])
def test_dropped_connections_are_reported_as_unreachable(monkeypatch, failure):
    class Opener:
        def open(self, request, timeout):
            raise failure
    monkeypatch.setattr(langfuse_v2.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ImportProblem) as raised:
        langfuse_v2._get("https://example.test/api/public/v2/observations", "Basic test")
    assert raised.value.code == "source_unreachable" and raised.value.status == 502


def connection_status(client, project):
    return client.get(f"/projects/{project}/connections").json()["connections"][0]["status"]


def test_a_malformed_trace_fails_only_itself(client, project, connection, langfuse, monkeypatch):
    real = langfuse_v2.fetch_trace
    def malformed(trace_id, **kwargs):
        if trace_id == "trace-003":
            raise TypeError("'<' not supported between instances of 'int' and 'str'")
        return real(trace_id, **kwargs)
    monkeypatch.setattr(langfuse_v2, "fetch_trace", malformed)
    ids = [f"trace-{n:03d}" for n in range(1, 9)]
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ids, **WINDOW})
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["items"]) == 7
    assert [(e["trace_id"], e["code"]) for e in body["fetch_errors"]] == [("trace-003", "unsupported_response")]
    single = client.post(route(project, connection, "trace-previews"), json={"trace_id": "trace-003", **WINDOW})
    assert single.status_code == 502 and single.json()["detail"]["code"] == "unsupported_response"


def test_non_string_start_times_do_not_break_a_fetch(monkeypatch):
    def get(url, authorization):
        if "/scores" in url:
            return {"data": [], "meta": {}}
        return {"data": [{"id": "a", "traceId": "t", "type": "AGENT", "isRootObservation": True, "startTime": 1790000000, "input": "task", "output": "done"},
                         {"id": "b", "traceId": "t", "type": "TOOL", "parentObservationId": "a", "startTime": "2026-09-01T10:00:00Z"}], "meta": {}}
    monkeypatch.setattr(langfuse_v2, "_get", get)
    result = langfuse_v2.fetch_trace("t", host=HOST, public_key="pk", secret_key="sk", **WINDOW)
    assert result["trace"]["id"] == "t"


def test_deeply_nested_json_is_an_unsupported_response(monkeypatch):
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def __init__(self):
            self.sent = False
        def read(self, size):
            if self.sent:
                return b""
            self.sent = True
            return b"[" * 200000
    class Opener:
        def open(self, request, timeout):
            return Response()
    monkeypatch.setattr(langfuse_v2.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ImportProblem) as raised:
        langfuse_v2._get("https://example.test/api/public/v2/observations", "Basic test")
    assert raised.value.code == "unsupported_response"
    assert langfuse_v2._parse_io("[" * 200000) == "[" * 200000


def serve_slowly(body, delay, chunked=False):
    """A real loopback server that dribbles a body out one byte at a time."""
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            else:
                self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                for byte in body:
                    time.sleep(delay)
                    self.wfile.write(b"%x\r\n%c\r\n" % (1, byte) if chunked else bytes([byte]))
                    self.wfile.flush()
                if chunked:
                    self.wfile.write(b"0\r\n\r\n")
            except OSError:
                pass
        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


@pytest.mark.parametrize("chunked", [False, True])
def test_a_trickling_response_hits_the_request_deadline(monkeypatch, chunked):
    server = serve_slowly(b'{"data": [], "meta": {}}' + b" " * 30, 0.1, chunked)
    monkeypatch.setattr(langfuse_v2, "_REQUEST_SECONDS", 0.5)
    try:
        started = time.monotonic()
        with pytest.raises(ImportProblem) as raised:
            langfuse_v2._get(f"http://127.0.0.1:{server.server_port}/api/public/v2/observations", "Basic test")
        elapsed = time.monotonic() - started
    finally:
        server.shutdown()
        server.server_close()
    assert raised.value.code == "source_timeout" and raised.value.status == 504
    # A read that waits for a whole 64 KiB block would take the full 5 seconds here.
    assert elapsed < 2.5, elapsed


def test_a_prompt_real_response_is_read_in_full():
    server = serve_slowly(b'{"data": [], "meta": {}}', 0.0)
    try:
        assert langfuse_v2._get(f"http://127.0.0.1:{server.server_port}/x", "Basic test") == {"data": [], "meta": {}}
    finally:
        server.shutdown()
        server.server_close()


def test_fetch_stops_between_pages_at_the_batch_deadline(langfuse):
    with pytest.raises(ImportProblem) as raised:
        langfuse_v2.fetch_trace("trace-001", host=HOST, public_key="pk", secret_key="sk", deadline=time.monotonic() - 1, **WINDOW)
    assert raised.value.code == "source_timeout" and not langfuse.calls


def test_overflowing_numbers_and_dates_are_rejected_not_crashed(client, project, connection, langfuse):
    huge = client.post(route(project, connection, "traces"), content=b'{"limit": 1e400, "from_time": "2026-09-01T00:00:00Z", "to_time": "2026-09-02T00:00:00Z"}', headers={"content-type": "application/json"})
    assert huge.status_code == 422 and huge.json()["detail"]["code"] == "invalid_limit"
    far = {"from_time": "2026-09-01T00:00:00Z", "to_time": "9999-12-31T23:00:00-05:00"}
    for tail, payload in (("traces", far), ("trace-previews", {"trace_ids": ["trace-001", "trace-002"], **far})):
        response = client.post(route(project, connection, tail), json=payload)
        assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_time_range", (tail, response.text)


def test_disconnect_after_staging_leaves_nothing_behind(client, project, connection, langfuse, monkeypatch):
    workspace = client.app.state.workspace
    real = workspace.inspect
    def inspect_then_disconnect(*args, **kwargs):
        result = real(*args, **kwargs)
        workspace.disconnect(project, connection)
        return result
    monkeypatch.setattr(workspace, "inspect", inspect_then_disconnect)
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["trace-001", "trace-002"], **WINDOW})
    assert response.status_code == 404, response.text
    for preview in workspace.records("previews", project):
        assert preview["status"] == "expired" and preview["items"] == []
        assert not any((workspace.root / "staging" / preview["id"]).rglob("*.json"))


def test_connection_status_after_a_failed_batch_and_a_rate_limit(client, project, connection, langfuse, monkeypatch):
    assert client.post(route(project, connection, "test")).json()["status"] == "accessible"
    def limited(url, authorization):
        raise ImportProblem("source_rate_limited", "Langfuse rate-limited this request. Wait and try again.", 502)
    monkeypatch.setattr(langfuse_v2, "_get", limited)
    assert client.post(route(project, connection, "test")).status_code == 502
    assert connection_status(client, project) == "accessible"
    langfuse.missing.update({"gone-1", "gone-2"})
    monkeypatch.setattr(langfuse_v2, "_get", langfuse.get)
    assert client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["gone-1", "gone-2"], **WINDOW}).status_code == 404
    assert connection_status(client, project) == "fetch_failed"


def test_scan_cap_is_reported_separately_from_the_limit(monkeypatch):
    fake = FakeLangfuse(count=60, page=2)
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    capped = list_all(user_id="nobody", limit=5)
    assert capped["traces"] == [] and capped["truncated"] is True and capped["scan_capped"] is True
    assert list_all(limit=2)["scan_capped"] is False


def test_batch_deadline_does_not_drop_a_traces_scores(monkeypatch):
    def get(url, authorization):
        if "/scores" in url:
            return {"data": [{"id": "s1", "name": "correctness", "dataType": "CATEGORICAL", "value": "pass", "timestamp": "2026-09-05T00:00:00Z", "subject": {"kind": "trace", "id": "t"}}], "meta": {}}
        time.sleep(0.3)
        return {"data": [{"id": "a", "traceId": "t", "type": "AGENT", "isRootObservation": True, "startTime": "2026-09-01T10:00:00Z", "input": "task", "output": "done"}], "meta": {}}
    monkeypatch.setattr(langfuse_v2, "_get", get)
    result = langfuse_v2.fetch_trace("t", host=HOST, public_key="pk", secret_key="sk", deadline=time.monotonic() + 0.1, **WINDOW)
    assert [s["id"] for s in result["scores"]] == ["s1"] and not any("could not be retrieved" in w for w in result["import_warnings"])


def test_timeout_message_names_the_limit_that_fired(langfuse):
    with pytest.raises(ImportProblem) as batch:
        langfuse_v2.fetch_trace("trace-001", host=HOST, public_key="pk", secret_key="sk", deadline=time.monotonic() - 1, **WINDOW)
    assert "batch" in str(batch.value) and "two minutes" not in str(batch.value)


def test_unexpected_errors_are_logged_not_swallowed(client, project, connection, monkeypatch, caplog):
    def broken(trace_id, **kwargs):
        raise KeyError("programming error")
    monkeypatch.setattr(langfuse_v2, "fetch_trace", broken)
    with caplog.at_level("WARNING", logger="agr.workspace"):
        response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["a", "b"], **WINDOW})
    assert response.status_code == 502
    assert any("KeyError" in (r.exc_text or "") or r.exc_info for r in caplog.records)


def test_an_invalid_time_range_does_not_mark_the_connection_failed(client, project, connection):
    assert client.post(route(project, connection, "test")).json()["status"] == "accessible"
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["a", "b"], "from_time": "2026-09-02T00:00:00Z", "to_time": "2026-09-01T00:00:00Z"})
    assert response.status_code == 422
    assert client.get(f"/projects/{project}/connections").json()["connections"][0]["status"] == "accessible"


def serve_raw(payload, gap=0.0, pieces=None):
    """A loopback server that answers one request with raw bytes, optionally in timed pieces, then closes."""
    import socket
    import threading
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    def run():
        conn, _ = listener.accept()
        try:
            conn.recv(65536)
            for piece in (pieces or [payload]):
                conn.sendall(piece)
                time.sleep(gap)
        except OSError:
            pass
        finally:
            conn.close()
            listener.close()
    threading.Thread(target=run, daemon=True).start()
    return f"http://127.0.0.1:{listener.getsockname()[1]}/x"


def get_quickly(url, seconds=0.2):
    from agr import langfuse_v2 as module
    original = module._REQUEST_SECONDS
    module._REQUEST_SECONDS = seconds
    try:
        started = time.monotonic()
        with pytest.raises(ImportProblem) as raised:
            module._get(url, "Basic test")
        return raised.value, time.monotonic() - started
    finally:
        module._REQUEST_SECONDS = original


def test_windows_and_root_flag_are_enforced_when_a_filter_overrides_fixed_parameters(langfuse):
    langfuse.traces.append({"id": "t-new", "name": "triage", "user": "ana", "tags": ["prod"], "start": "2026-10-14T09:00:00.000Z"})
    langfuse.traces.append({"id": "t-old", "name": "triage", "user": "ana", "tags": ["prod"], "start": "2026-08-01T09:00:00.000Z"})
    langfuse.traces.append({"id": "t-none", "name": "triage", "user": "ana", "tags": ["prod"], "start": None})
    for filters in ({"name": "triage"}, {}):
        assert {t["trace_id"] for t in list_all(**filters)["traces"]} .isdisjoint({"t-new", "t-old", "t-none"})
    langfuse.calls.clear()
    list_all(name="triage")
    sent = json.loads(langfuse.calls[0][1]["filter"])
    assert {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True} in sent
    assert {c["column"] for c in sent} >= {"traceName", "startTime", "isRootObservation"}
    assert [c["operator"] for c in sent if c["column"] == "startTime"] == [">=", "<"]
    assert langfuse.calls[0][1]["limit"] == "1000"


def test_the_batch_deadline_also_bounds_each_request():
    server = serve_slowly(b'{"data": [], "meta": {}}' + b" " * 40, 0.1)
    langfuse_v2._budget.end = time.monotonic() + 0.3
    try:
        started = time.monotonic()
        with pytest.raises(ImportProblem) as raised:
            langfuse_v2._get(f"http://127.0.0.1:{server.server_port}/x", "Basic test")
        assert raised.value.code == "source_timeout" and time.monotonic() - started < 2.5
    finally:
        langfuse_v2._budget.end = None
        server.shutdown()
        server.server_close()


def test_scores_budget_stays_near_the_batch_deadline(monkeypatch):
    seen = {}
    def get(url, authorization):
        if "/scores" in url:
            seen["scores"] = langfuse_v2._budget.end - time.monotonic()
            return {"data": [], "meta": {}}
        seen["observations"] = langfuse_v2._budget.end - time.monotonic()
        return {"data": [{"id": "a", "traceId": "t", "type": "AGENT", "isRootObservation": True, "startTime": "2026-09-01T10:00:00Z", "input": "x", "output": "y"}], "meta": {}}
    monkeypatch.setattr(langfuse_v2, "_get", get)
    langfuse_v2.fetch_trace("t", host=HOST, public_key="pk", secret_key="sk", deadline=time.monotonic() + 5, **WINDOW)
    assert seen["observations"] <= 5.1 and 5 < seen["scores"] <= 35.1
    assert langfuse_v2._budget.end is None
    langfuse_v2.fetch_trace("t", host=HOST, public_key="pk", secret_key="sk", **WINDOW)
    assert 100 < seen["observations"] <= 120.1 and 55 < seen["scores"] <= 60.1


def test_slow_headers_are_bounded_and_reported_as_a_timeout():
    url = serve_raw(b"", gap=0.15, pieces=[b"HTTP/1.1 200 OK\r\n", b"Content-Type: application/json\r\n", b"Content-Length: 2\r\n", b"\r\n{}"])
    problem, elapsed = get_quickly(url, 0.2)
    assert problem.code == "source_timeout" and elapsed < 2.5


def test_a_silent_server_is_a_timeout_not_an_unreachable_host():
    url = serve_raw(b"", gap=1.0, pieces=[b""])
    problem, _ = get_quickly(url, 0.2)
    assert problem.code == "source_timeout" and problem.status == 504


@pytest.mark.parametrize("payload", [b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\n" + b'{"data": [', b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n20\r\n" + b'{"data": [', b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n"])
def test_a_cut_off_body_is_reported_as_unreachable(payload):
    url = serve_raw(payload)
    problem, _ = get_quickly(url, 5)
    assert problem.code == "source_unreachable", problem


def test_a_complete_body_is_not_mistaken_for_a_cut_off_one():
    for payload in (b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}", b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\n{}\r\n0\r\n\r\n"):
        assert langfuse_v2._get(serve_raw(payload), "Basic test") == {}


def test_a_slow_query_does_not_mark_the_connection_failed(client, project, connection, monkeypatch):
    assert client.post(route(project, connection, "test")).json()["status"] == "accessible"
    def slow(url, authorization):
        raise ImportProblem("source_timeout", "Langfuse did not answer in time. Try a smaller time range or retry.", 504)
    monkeypatch.setattr(langfuse_v2, "_get", slow)
    assert client.post(route(project, connection, "traces"), json=WINDOW).status_code == 504
    assert connection_status(client, project) == "accessible"


def test_concurrent_batches_are_limited(client, project, connection, monkeypatch):
    import threading
    workspace = client.app.state.workspace
    workspace.fetch_slots = threading.BoundedSemaphore(1)
    started, release = threading.Event(), threading.Event()
    real = langfuse_v2.fetch_trace
    def blocking(trace_id, **kwargs):
        started.set()
        assert release.wait(10)
        return real(trace_id, **kwargs)
    monkeypatch.setattr(langfuse_v2, "fetch_trace", blocking)
    monkeypatch.setattr(langfuse_v2, "_get", FakeLangfuse().get)
    results = {}
    first = threading.Thread(target=lambda: results.setdefault("first", client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["trace-001", "trace-002"], **WINDOW})))
    first.start()
    try:
        assert started.wait(10)
        busy = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["trace-003", "trace-004"], **WINDOW})
        assert busy.status_code == 429 and busy.json()["detail"]["code"] == "fetch_busy"
    finally:
        release.set()
        first.join(timeout=20)
    assert results["first"].status_code == 200
    again = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["trace-003", "trace-004"], **WINDOW})
    assert again.status_code == 200, again.text


def test_abandoned_previews_expire_while_the_server_runs(client, project):
    workspace = client.app.state.workspace
    old = workspace.new_preview(project, "langfuse")
    workspace.stage_file(project, old["id"], "trace.json", b"{}")
    record = workspace.preview(project, old["id"])
    record["created_epoch"] -= 90000
    atomic_json(workspace._path("previews", old["id"]), record)
    workspace._previews_expired_at = 0.0
    workspace.new_preview(project, "langfuse")
    assert workspace.record("previews", old["id"], project)["status"] == "expired"
    assert not (workspace.root / "staging" / old["id"]).exists()


def test_a_host_that_never_completes_tls_is_unreachable_not_slow():
    url = serve_raw(b"", gap=1.0, pieces=[b""]).replace("http://", "https://")
    problem, _ = get_quickly(url, 0.3)
    assert problem.code == "source_unreachable", problem


def test_a_stalled_host_stops_a_batch_at_the_first_failure(client, project, connection, monkeypatch):
    calls = []
    def stalled(trace_id, **kwargs):
        calls.append(trace_id)
        time.sleep(0.05)
        raise ImportProblem("source_unreachable", "The host could not be reached.", 502)
    monkeypatch.setattr(langfuse_v2, "fetch_trace", stalled)
    response = client.post(route(project, connection, "trace-previews"), json={"trace_ids": many(40), **WINDOW})
    assert response.status_code == 502 and response.json()["detail"]["code"] == "source_unreachable"
    time.sleep(0.3)
    assert len(calls) <= 8, len(calls)


def test_timestamps_are_sent_in_the_documented_z_form(langfuse):
    list_all(name="triage")
    first = langfuse.calls[0][1]
    pattern = r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z"
    import re
    assert re.fullmatch(pattern, first["fromStartTime"]) and re.fullmatch(pattern, first["toStartTime"])
    assert all(re.fullmatch(pattern, c["value"]) for c in json.loads(first["filter"]) if c["column"] == "startTime")
    assert "+00:00" not in first["fromStartTime"] + first["toStartTime"] + first["filter"]
    langfuse.calls.clear()
    langfuse_v2.fetch_trace("trace-001", host=HOST, public_key="pk", secret_key="sk", from_time="2026-09-01T00:00:00+02:00", to_time="2026-09-02T00:00:00.5+02:00")
    observed = langfuse.calls[0][1]
    assert observed["fromStartTime"] == "2026-08-31T22:00:00.000Z" and observed["toStartTime"] == "2026-09-01T22:00:00.500Z"


@pytest.mark.parametrize("value,expected", [
    ("2026-10-14T11:00:00Z", (11, 0, 0)), ("2026-10-14T11:00:00.1Z", (11, 0, 100000)), ("2026-10-14T11:00:00.123456789Z", (11, 0, 123456)),
    ("2026-10-14 11:00:00+0000", (11, 0, 0)), ("2026-10-14T11:00:00", (11, 0, 0)), ("2026-10-14T13:00:00.5+02:00", (11, 0, 500000)),
])
def test_timestamps_parse_the_same_on_every_python(value, expected):
    parsed = langfuse_v2._parse_time(value).astimezone(__import__("datetime").timezone.utc)
    assert (parsed.hour, parsed.minute, parsed.microsecond) == expected
    assert langfuse_v2._parse_time("not a time") is None and langfuse_v2._parse_time(7) is None


def test_rows_with_unusual_but_valid_timestamps_are_kept(monkeypatch):
    def get(url, authorization):
        return {"data": [{"id": "a", "traceId": "t1", "traceName": "n", "startTime": "2026-09-01T10:00:00.123456789Z", "isRootObservation": True},
                         {"id": "b", "traceId": "t2", "traceName": "n", "startTime": "2026-09-01 11:00:00+0000", "isRootObservation": True}], "meta": {}}
    monkeypatch.setattr(langfuse_v2, "_get", get)
    assert {t["trace_id"] for t in list_all(name="n")["traces"]} == {"t1", "t2"}


def test_a_trace_from_the_last_quarter_hour_carries_a_note(monkeypatch):
    from datetime import datetime, timedelta, timezone
    def make(minutes):
        start = (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()
        def get(url, authorization):
            if "/scores" in url:
                return {"data": [], "meta": {}}
            return {"data": [{"id": "a", "traceId": "t", "type": "AGENT", "isRootObservation": True, "startTime": start, "input": "x", "output": "y"}], "meta": {}}
        return get
    window = {"from_time": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat(), "to_time": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()}
    for minutes, noted in ((3, True), (60, False)):
        monkeypatch.setattr(langfuse_v2, "_get", make(minutes))
        result = langfuse_v2.fetch_trace("t", host=HOST, public_key="pk", secret_key="sk", **window)
        assert any("15 minutes" in w for w in result["import_warnings"]) is noted


def test_a_request_cut_short_by_the_batch_says_so():
    server = serve_slowly(b'{"data": [], "meta": {}}' + b" " * 40, 0.1)
    langfuse_v2._budget.end, langfuse_v2._budget.batch = time.monotonic() + 0.3, True
    try:
        with pytest.raises(ImportProblem) as raised:
            langfuse_v2._get(f"http://127.0.0.1:{server.server_port}/x", "Basic test")
        assert raised.value.code == "source_timeout" and "batch" in str(raised.value)
        langfuse_v2._budget.batch = False
        with pytest.raises(ImportProblem) as plain:
            langfuse_v2._get(f"http://127.0.0.1:{server.server_port}/x", "Basic test")
        assert plain.value.code == "source_timeout" and "batch" not in str(plain.value)
    finally:
        langfuse_v2._budget.end = None
        langfuse_v2._budget.batch = False
        server.shutdown()
        server.server_close()


def test_the_expiry_sweep_skips_young_previews_without_reading_jobs(client, project, monkeypatch):
    workspace = client.app.state.workspace
    for _ in range(5):
        workspace.new_preview(project, "langfuse")
    reads = []
    real = workspace.records
    monkeypatch.setattr(workspace, "records", lambda kind, *args, **kwargs: (reads.append(kind), real(kind, *args, **kwargs))[1])
    workspace._previews_expired_at = 0.0
    workspace.new_preview(project, "langfuse")
    assert "jobs" not in reads


def test_a_corrupt_preview_file_does_not_break_new_previews(client, project):
    workspace = client.app.state.workspace
    (workspace.root / "previews").mkdir(parents=True, exist_ok=True)
    (workspace.root / "previews" / "broken.json").write_text("{not json", encoding="utf-8")
    workspace._previews_expired_at = 0.0
    assert workspace.new_preview(project, "langfuse")["status"] == "staging"
    old = workspace.new_preview(project, "langfuse")
    record = workspace.preview(project, old["id"])
    record["created_epoch"] -= 90000
    atomic_json(workspace._path("previews", old["id"]), record)
    workspace._previews_expired_at = 0.0
    workspace.new_preview(project, "langfuse")
    assert workspace.record("previews", old["id"], project)["status"] == "expired"


def test_a_submitted_old_preview_is_kept_for_its_import(client, project):
    workspace = client.app.state.workspace
    old = workspace.new_preview(project, "langfuse")
    record = workspace.preview(project, old["id"])
    record["created_epoch"] -= 90000
    atomic_json(workspace._path("previews", old["id"]), record)
    job_id = "import_" + "a" * 32
    (workspace.root / "jobs").mkdir(parents=True, exist_ok=True)
    atomic_json(workspace._path("jobs", job_id), {"id": job_id, "project_id": project, "preview_id": old["id"], "status": "completed", "results": [], "selected": [], "idempotency_key": "k"})
    workspace._previews_expired_at = 0.0
    workspace.new_preview(project, "langfuse")
    assert workspace.record("previews", old["id"], project)["status"] == "staging"


@pytest.mark.parametrize("host", ["http://[::1", "http://[::1]:99999", "http://host:abc", "http://exa mple.com", "http://host\n.example", "http://", "ftp://host", "http://user:pw@host", "http://host?x=1", "http://host#frag", "", " ", None, 5, ["http://host"], pytest.param("http://" + "a" * 3000, id="long")])
def test_invalid_hosts_are_input_errors_not_server_errors(client, project, host):
    response = client.post(f"/projects/{project}/connections", json={"host": host, "public_key": "pk", "secret_key": "sk"})
    assert response.status_code == 422, (host, response.status_code, response.text)
    assert response.json()["detail"]["code"] == "invalid_host"
    assert client.get(f"/projects/{project}/connections").json()["connections"] == []


@pytest.mark.parametrize("host,stored", [("http://[::1]:8000", "http://[::1]:8000"), ("https://cloud.langfuse.com/", "https://cloud.langfuse.com"), ("  https://self.example/base/  ", "https://self.example/base"), ("http://localhost:3000", "http://localhost:3000")])
def test_valid_hosts_are_accepted_and_normalized(client, project, host, stored):
    response = client.post(f"/projects/{project}/connections", json={"host": host, "public_key": "pk", "secret_key": "sk"})
    assert response.status_code == 200, response.text
    assert response.json()["host"] == stored


def test_a_missing_host_is_an_input_error(client, project):
    response = client.post(f"/projects/{project}/connections", json={"public_key": "pk", "secret_key": "sk"})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_host"
