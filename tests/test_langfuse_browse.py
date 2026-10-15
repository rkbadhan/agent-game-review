"""Browse, select and batch-import Langfuse traces from the UI's API, against a fake Langfuse."""
import copy
import json
import re
from pathlib import Path
import time
import urllib.parse
from urllib.parse import parse_qs, urlsplit

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from agr import langfuse_v1, langfuse_v2
from agr.api import create_app
from agr.imports import ImportProblem
from agr.workspace import atomic_json

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = json.loads((ROOT / "tests" / "fixtures" / "langfuse" / "langfuse_toy_run.json").read_text())
WINDOW = {"from_time": "2026-09-01T00:00:00Z", "to_time": "2026-09-02T00:00:00Z"}
HOST = "https://langfuse.example"


class FakeLangfuse:
    """Just enough of Langfuse to list and fetch whole traces.

    Version 4 serves observations v2 and scores v3; version 3 serves the v1 trace endpoints. Each answers 404 on the
    other's endpoints, as a real server does, and /api/public/health reports the version.
    """

    def __init__(self, count=5, page=2, recent=False, version="4.0.0"):
        from datetime import datetime, timedelta, timezone
        self.page, self.calls, self.missing, self.version, self.health_calls = page, [], set(), version, 0
        # What /health reports: None for no version field, False for an error, a string to claim a version the endpoints do not match.
        self.reported, self.health_auth, self.wrong_endpoint_hits = "", None, 0
        self.project = {"id": "proj-prod-1", "name": "Production agent"}
        # What the server actually serves, when that differs from what its version suggests (None follows the version).
        self.serves_v1 = self.serves_v2 = None
        # Whether /api/public/v2/metrics answers (off by default, as on a self-hosted server without the preview opt-in).
        self.metrics, self.metric_queries = False, []
        # `recent` places traces within the last hour, for tests that search with a relative window.
        now = datetime.now(timezone.utc)
        start = (lambda n: (now - timedelta(minutes=n)).isoformat().replace("+00:00", "Z")) if recent else (lambda n: f"2026-09-01T10:{n % 60:02d}:00.000Z")
        self.traces = [
            {"id": f"trace-{n:02d}", "name": "config-fixer" if n % 2 else "triage", "user": "ana" if n < 3 else "ben", "tags": ["prod"] if n % 2 else ["staging"],
             "start": start(n)} for n in range(1, count + 1)]

    def details(self, t):
        number = int(t["id"].rsplit("-", 1)[-1]) if t["id"].rsplit("-", 1)[-1].isdigit() else 0
        return {"input": json.dumps({"task": "fix the config " + t["id"]}), "output": "done " + t["id"], "metadata": {"customer": "acme" if number % 2 else "globex", "attempt": number},
                "latency": 1.5 + number, "release": "rel-1", "version": "v7"}

    def root_row(self, t):
        return {"id": f"root-{t['id']}", "traceId": t["id"], "traceName": t["name"], "name": t["name"], "type": "AGENT", "startTime": t["start"], "userId": t["user"],
                "sessionId": "s-" + t["id"], "tags": t["tags"], "environment": "default", "isRootObservation": True, "parentObservationId": None,
                "totalCost": 0.0125 * (1 + len(t["id"]) % 3), **self.details(t)}

    @staticmethod
    def check_top_n(query):
        """The server's rule for a high-cardinality dimension (validateQuery.ts): a top-N with an explicit limit and a descending measure."""
        problem = ImportProblem("source_fetch_failed", "High cardinality dimension(s) 'traceId' require both 'config.row_limit' and 'orderBy' with direction 'desc' on a measure field.", 502)
        problem.http_status = 400
        if any(d["field"] == "traceId" for d in query["dimensions"]):
            if query.get("timeDimension") or (query.get("config") or {}).get("row_limit") is None:
                raise problem
            measures = [m["measure"] for m in query["metrics"]]
            desc = [o["field"] for o in (query.get("orderBy") or []) if o["direction"] == "desc"]
            if not desc or not all(any(f == m or f.endswith("_" + m) for m in measures) for f in desc):
                raise problem

    def endpoint_missing(self):
        problem = ImportProblem("source_not_found", "This endpoint was not found. Check the host and selected Langfuse API version.", 502)
        problem.endpoint_missing, problem.http_status = True, 404
        return problem

    def v1_trace(self, trace_id):
        rows = [dict(o, id=f"{trace_id}-{o['id']}") for o in FIXTURE["observations"]]
        base = next((t for t in self.traces if t["id"] == trace_id), None)
        return {"id": trace_id, "name": base["name"] if base else "trace", "timestamp": base["start"] if base else "2026-09-01T10:00:00.000Z", "sessionId": "s-" + trace_id, "userId": "ana",
                "tags": base["tags"] if base else [], "input": FIXTURE["input"], "output": FIXTURE["output"], "observations": rows, "scores": []}

    def v1_get(self, path, params):
        if path.endswith("/api/public/traces"):
            rows = [{"id": t["id"], "name": t["name"], "timestamp": t["start"], "userId": t["user"], "sessionId": "s-" + t["id"], "tags": t["tags"], "environment": "default",
                     "totalCost": 0.02, **self.details(t)} for t in self.traces]
            if "filter" in params:
                for rule in json.loads(params["filter"]):
                    if rule["column"] == "id":
                        rows = [r for r in rows if rule["value"] in r["id"]]
                    if rule["column"] == "metadata":
                        rows = [r for r in rows if str(r["metadata"].get(rule["key"])) == rule["value"]]
            if "name" in params:
                rows = [r for r in rows if r["name"] == params["name"]]
            if "tags" in params:
                rows = [r for r in rows if params["tags"] in r["tags"]]
            size, number = int(params.get("limit", 50)), int(params.get("page", 1))
            size = min(size, self.page)
            return {"data": rows[(number - 1) * size:number * size], "meta": {"page": number, "limit": size, "totalItems": len(rows), "totalPages": max(1, -(-len(rows) // size))}}
        trace_id = urllib.parse.unquote(path.rsplit("/", 1)[1])
        if trace_id in self.missing:
            raise self.endpoint_missing()
        return self.v1_trace(trace_id)

    def get(self, url, authorization):
        path = urlsplit(url).path
        if path.endswith("/api/public/health"):
            self.health_calls += 1
            self.health_auth = authorization
            if self.reported is False:
                raise ImportProblem("source_fetch_failed", "Langfuse could not return the requested trace.", 502)
            return {"status": "OK", "version": self.reported or self.version} if self.reported is not None else {"status": "OK"}
        assert authorization.startswith("Basic ")
        if path.endswith("/api/public/projects"):
            return {"data": [self.project]} if self.project else {"data": []}
        params = {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}
        if path.endswith("/api/public/v2/metrics"):
            if not self.metrics or self.version.startswith("3"):
                raise self.endpoint_missing()
            query = json.loads(params["query"])
            self.metric_queries.append(query)
            self.check_top_n(query)
            ids = query["filters"][0]["value"]
            return {"data": [{"traceId": t["id"], "sum_totalCost": str(round(0.25 + 0.01 * int(t["id"].rsplit("-", 1)[-1]), 4))} for t in self.traces if t["id"] in ids]}
        v1 = "/api/public/traces" in path
        older = self.version.startswith("3")
        serves_v1 = older if self.serves_v1 is None else self.serves_v1
        serves_v2 = (not older) if self.serves_v2 is None else self.serves_v2
        if (v1 and not serves_v1) or (not v1 and not serves_v2):
            self.wrong_endpoint_hits += 1
            raise self.endpoint_missing()
        self.calls.append((path, params))
        if v1:
            return self.v1_get(path, params)
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
                if rule["column"] == "traceId":
                    rows = [r for r in rows if rule["value"] in r["traceId"]]
                if rule["column"] == "metadata":
                    rows = [r for r in rows if str(r["metadata"].get(rule["key"])) == rule["value"]]
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
    assert langfuse.calls[0][1]["limit"] == "100" and langfuse.calls[0][1]["fields"] == "core,basic,io,metadata,usage,metrics,trace_context"


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


# --- Langfuse 3 servers: the v1 trace endpoints, chosen from /api/public/health ---


def paths(fake):
    return {path for path, _ in fake.calls}


@pytest.fixture
def v3(monkeypatch):
    fake = FakeLangfuse(version="3.12.0")
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    return fake


@pytest.mark.parametrize("reported,api", [("4.0.0", "v2"), ("4", "v2"), ("v4.1.2", "v2"), ("12.0.0", "v2"), ("3.12.0", "v1"), ("3", "v1"), ("v3.1", "v1"), ("2.9.4", "v1"), ("1.25.0", "v1")])
def test_the_server_version_picks_the_api(monkeypatch, reported, api):
    from agr import langfuse_source
    fake = FakeLangfuse(version="4.0.0")
    fake.reported = reported
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    assert langfuse_source.detect(HOST) == {"api": api, "server_version": reported}


@pytest.mark.parametrize("reported", [None, False, "unknown", "latest"])
def test_an_unreadable_version_is_unknown_not_an_error(monkeypatch, reported):
    from agr import langfuse_source
    fake = FakeLangfuse()
    fake.reported = reported
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    assert langfuse_source.detect(HOST)["api"] is None


def test_detection_is_cached_unauthenticated_and_refreshable(monkeypatch):
    from agr import langfuse_source
    fake = FakeLangfuse(version="3.12.0")
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    for _ in range(3):
        assert langfuse_source.detect(HOST)["api"] == "v1"
    assert fake.health_calls == 1 and fake.health_auth is None
    fake.version = "4.0.0"
    assert langfuse_source.detect(HOST)["api"] == "v1"
    assert langfuse_source.detect(HOST, refresh=True)["api"] == "v2" and fake.health_calls == 2


def test_a_version_3_server_is_listed_through_the_v1_trace_api(v3):
    from agr import langfuse_source
    import re
    result = langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=50, **WINDOW)
    assert [t["trace_id"] for t in result["traces"]] == [f"trace-{n:02d}" for n in range(5, 0, -1)]
    assert paths(v3) == {"/api/public/traces"} and v3.wrong_endpoint_hits == 0
    first = v3.calls[0][1]
    assert first["orderBy"] == "timestamp.desc" and first["fields"] == "core,io,metrics" and first["limit"] == "100" and first["page"] == "1"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z", first["fromTimestamp"]) and "fromStartTime" not in first
    assert result["truncated"] is False and result["scan_capped"] is False
    assert result["traces"][0]["user_id"] == "ben" and result["traces"][-1]["tags"] == ["prod"]


def test_v1_filters_are_sent_and_rechecked(v3):
    from agr import langfuse_source
    def run(**filters):
        return [t["trace_id"] for t in langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=50, **WINDOW, **filters)["traces"]]
    assert run(name="config-fixer") == ["trace-05", "trace-03", "trace-01"]
    assert v3.calls[-1][1]["name"] == "config-fixer"
    assert run(tag="staging") == ["trace-04", "trace-02"] and v3.calls[-1][1]["tags"] == "staging"
    assert run(user_id="ana") == ["trace-02", "trace-01"] and v3.calls[-1][1]["userId"] == "ana"
    assert run(session_id="s-trace-03") == ["trace-03"] and v3.calls[-1][1]["sessionId"] == "s-trace-03"
    assert run(environment="default")[0] == "trace-05" and run(environment="other") == []


def test_v1_listing_pages_and_reports_truncation(v3):
    from agr import langfuse_source
    full = langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=50, **WINDOW)
    assert len(full["traces"]) == 5 and [c[1]["page"] for c in v3.calls] == ["1", "2", "3"]
    v3.calls.clear()
    capped = langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=2, **WINDOW)
    assert len(capped["traces"]) == 2 and capped["truncated"] is True and len(v3.calls) == 1
    exact = langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=5, **WINDOW)
    assert exact["truncated"] is False


def test_v1_rows_outside_the_window_or_missing_fields_are_never_offered(monkeypatch):
    from agr import langfuse_v1
    rows = [{"id": "in", "name": "n", "timestamp": "2026-09-01T10:00:00.000Z", "tags": ["prod"], "userId": "ana"},
            {"id": "late", "name": "n", "timestamp": "2026-10-14T10:00:00.000Z", "tags": ["prod"], "userId": "ana"},
            {"id": "early", "name": "n", "timestamp": "2026-08-01T10:00:00.000Z", "tags": ["prod"], "userId": "ana"},
            {"id": "stampless", "name": "n", "tags": ["prod"], "userId": "ana"},
            {"id": "bare", "name": "n", "timestamp": "2026-09-01T11:00:00.000Z"},
            {"id": " padded ", "name": "n", "timestamp": "2026-09-01T11:00:00.000Z", "tags": ["prod"], "userId": "ana"},
            {"id": "x" * 600, "name": "n", "timestamp": "2026-09-01T11:00:00.000Z", "tags": ["prod"], "userId": "ana"}]
    monkeypatch.setattr(langfuse_v2, "_get", lambda url, authorization: {"data": rows, "meta": {"page": 1, "totalPages": 1}})
    listed = lambda **f: [t["trace_id"] for t in langfuse_v1.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=50, **WINDOW, **f)["traces"]]
    assert set(listed()) == {"in", "bare"}
    assert listed(tag="prod", user_id="ana") == ["in"]


def test_v1_scan_cap_is_reported(monkeypatch):
    from agr import langfuse_v1
    def endless(url, authorization):
        return {"data": [{"id": "x", "name": "n", "timestamp": "2026-10-14T10:00:00.000Z"}] * 100, "meta": {"page": 1, "totalPages": 1000}}
    monkeypatch.setattr(langfuse_v2, "_get", endless)
    result = langfuse_v1.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=5, **WINDOW)
    assert result["traces"] == [] and result["truncated"] is True and result["scan_capped"] is True


def test_v1_listing_rejects_bad_input_like_v2(v3):
    from agr import langfuse_source
    for kwargs, code in (({"limit": 0}, "invalid_limit"), ({"limit": 9999}, "invalid_limit"), ({"name": "x" * 300}, "invalid_filter"),
                         ({"from_time": "2026-09-02T00:00:00Z", "to_time": "2026-09-01T00:00:00Z"}, "invalid_time_range")):
        base = {"host": HOST, "public_key": "pk", "secret_key": "sk", "limit": 5, **WINDOW}
        with pytest.raises(ImportProblem) as raised:
            langfuse_source.list_traces(**{**base, **kwargs})
        assert raised.value.code == code
    assert not v3.calls


def test_a_version_3_trace_is_fetched_whole_through_v1(v3):
    from agr import langfuse_source
    result = langfuse_source.fetch_trace("trace-02", host=HOST, public_key="pk", secret_key="sk", **WINDOW)
    assert result["id"] == "trace-02" and result["observations"] and "fetch_limits" not in result
    assert [c[0] for c in v3.calls] == ["/api/public/traces/trace-02"] and v3.calls[0][1]["fields"] == "core,io,scores,observations,metrics"
    assert v3.wrong_endpoint_hits == 0 and result["import_warnings"] == []


def test_v1_fetch_checks_what_came_back(monkeypatch):
    from agr import langfuse_v1
    state = {}
    cases = {"other": {"id": "someone-else", "observations": [{"id": "o"}]}, "empty": {"id": "t", "observations": []}, "list": [], "huge": {"id": "t", "observations": [{"id": "o"}] * 3}}
    monkeypatch.setattr(langfuse_v2, "_get", lambda url, authorization: cases[state["case"]])
    expected = {"other": ("unsupported_response", 502), "empty": ("source_not_found", 404), "list": ("unsupported_response", 502)}
    for case, (code, status) in expected.items():
        state["case"] = case
        with pytest.raises(ImportProblem) as raised:
            langfuse_v1.fetch_trace("t", host=HOST, public_key="pk", secret_key="sk")
        assert (raised.value.code, raised.value.status) == (code, status), case
        assert not getattr(raised.value, "endpoint_missing", False)
    state["case"] = "huge"
    monkeypatch.setattr(langfuse_v1, "MAX_SPANS", 2)
    with pytest.raises(ImportProblem) as raised:
        langfuse_v1.fetch_trace("t", host=HOST, public_key="pk", secret_key="sk")
    assert raised.value.code == "import_limit"


def test_a_very_recent_v1_trace_carries_the_capture_note(monkeypatch):
    from datetime import datetime, timedelta, timezone
    from agr import langfuse_v1
    recent = (datetime.now(timezone.utc) - timedelta(minutes=2)).isoformat()
    monkeypatch.setattr(langfuse_v2, "_get", lambda url, authorization: {"id": "t", "observations": [{"id": "o", "startTime": recent}], "scores": None})
    result = langfuse_v1.fetch_trace("t", host=HOST, public_key="pk", secret_key="sk")
    assert any("15 minutes" in w for w in result["import_warnings"]) and result["scores"] == []


def test_the_v1_fetch_respects_the_batch_budget():
    from agr import langfuse_v1
    server = serve_slowly(b'{"id": "t", "observations": [{"id": "o"}]}' + b" " * 60, 0.1)
    try:
        with pytest.raises(ImportProblem) as raised:
            langfuse_v1.fetch_trace("t", host=f"http://127.0.0.1:{server.server_port}", public_key="pk", secret_key="sk", deadline=time.monotonic() + 0.3)
        assert raised.value.code == "source_timeout" and "batch" in str(raised.value)
        assert langfuse_v2._budget.end is None and langfuse_v2._budget.batch is False
    finally:
        server.shutdown()
        server.server_close()


def test_a_version_4_server_never_touches_the_v1_endpoints(monkeypatch):
    from agr import langfuse_source
    fake = FakeLangfuse(version="4.0.0")
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=5, **WINDOW)
    langfuse_source.fetch_trace("trace-01", host=HOST, public_key="pk", secret_key="sk", **WINDOW)
    assert all("/v2/observations" in p or "/v3/scores" in p for p in paths(fake)) and fake.wrong_endpoint_hits == 0


def test_an_unknown_version_tries_v2_then_remembers_v1(monkeypatch):
    from agr import langfuse_source
    fake = FakeLangfuse(version="3.12.0")
    fake.reported = None
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    first = langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=5, **WINDOW)
    assert len(first["traces"]) == 5 and fake.wrong_endpoint_hits == 1
    langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=5, **WINDOW)
    langfuse_source.fetch_trace("trace-01", host=HOST, public_key="pk", secret_key="sk", **WINDOW)
    assert fake.wrong_endpoint_hits == 1
    assert langfuse_source.detect(HOST)["api"] == "v1"


def test_a_version_that_does_not_match_its_endpoints_falls_back(monkeypatch):
    from agr import langfuse_source
    fake = FakeLangfuse(version="3.12.0")
    fake.reported = "4.0.0"
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    assert len(langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=5, **WINDOW)["traces"]) == 5
    assert langfuse_source.detect(HOST)["api"] == "v1"
    reverse = FakeLangfuse(version="4.0.0")
    reverse.reported = "3.12.0"
    langfuse_source.clear_cache()
    monkeypatch.setattr(langfuse_v2, "_get", reverse.get)
    assert len(langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=5, **WINDOW)["traces"]) == 5
    assert langfuse_source.detect(HOST)["api"] == "v2"


@pytest.mark.parametrize("code", ["source_auth_failed", "source_rate_limited", "source_timeout", "source_unreachable", "unsupported_response"])
def test_only_a_missing_endpoint_triggers_the_fallback(monkeypatch, code):
    from agr import langfuse_source
    fake = FakeLangfuse(version="4.0.0")
    def get(url, authorization):
        if "health" in url:
            return fake.get(url, authorization)
        raise ImportProblem(code, "no", 502)
    monkeypatch.setattr(langfuse_v2, "_get", get)
    with pytest.raises(ImportProblem) as raised:
        langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=5, **WINDOW)
    assert raised.value.code == code and not fake.calls


def test_a_missing_trace_on_a_version_3_server_reports_the_first_error(v3):
    from agr import langfuse_source
    v3.missing.add("gone")
    with pytest.raises(ImportProblem) as raised:
        langfuse_source.fetch_trace("gone", host=HOST, public_key="pk", secret_key="sk", **WINDOW)
    assert raised.value.code == "source_not_found" and raised.value.status == 502
    # A trace that is simply absent is not blamed on the host or the API version.
    assert "endpoint" not in str(raised.value).lower() and "no trace with this id" in str(raised.value).lower()
    assert langfuse_source.detect(HOST)["api"] == "v1"


def _health_blocked_v3(monkeypatch, retry_error):
    """A 3.x server whose health path is blocked: v2 is tried first and 404s, then the retry on v1 fails with retry_error."""
    fake = FakeLangfuse(version="3.12.0")
    fake.reported = False
    def get(url, authorization):
        if "health" in url:
            return fake.get(url, authorization)
        if "/v2/observations" in url:
            raise fake.endpoint_missing()
        raise retry_error
    monkeypatch.setattr(langfuse_v2, "_get", get)


@pytest.mark.parametrize("code", ["source_auth_failed", "source_rate_limited", "source_timeout", "source_unreachable"])
def test_a_real_failure_on_the_retry_is_reported_as_it_is(monkeypatch, code):
    from agr import langfuse_source
    _health_blocked_v3(monkeypatch, ImportProblem(code, "The retry failed.", 502))
    with pytest.raises(ImportProblem) as raised:
        langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=5, **WINDOW)
    # Bad keys must not read as a wrong host, and a rate limit must not mark the connection unusable.
    assert raised.value.code == code and not getattr(raised.value, "endpoint_missing", False)
    assert code not in ("source_not_found",)


def test_a_retry_that_is_also_missing_reports_the_first_error(monkeypatch):
    from agr import langfuse_source
    fake = FakeLangfuse(version="3.12.0")
    _health_blocked_v3(monkeypatch, fake.endpoint_missing())
    with pytest.raises(ImportProblem) as raised:
        langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=5, **WINDOW)
    assert raised.value.code == "source_not_found" and getattr(raised.value, "endpoint_missing", False)
    assert langfuse_source.detect(HOST)["api"] is None  # a failed retry is not remembered


def test_a_rate_limited_retry_does_not_mark_the_connection_access_failed(client, project, connection, monkeypatch):
    from agr import langfuse_source
    _health_blocked_v3(monkeypatch, ImportProblem("source_rate_limited", "Slow down.", 502))
    listed = client.post(route(project, connection, "traces"), json={"limit": 5, **WINDOW})
    assert listed.status_code == 502 and listed.json()["detail"]["code"] == "source_rate_limited"
    assert connection_status(client, project) != "access_failed"


def test_v2_orders_traces_by_instant_not_by_text(monkeypatch):
    from agr import langfuse_source
    fake = FakeLangfuse(count=3, page=10)
    # Text order would put the whole-second stamp after the fractional one and misplace the +02:00 offset.
    for trace, stamp in zip(fake.traces, ["2026-09-01T10:00:00Z", "2026-09-01T10:00:00.900Z", "2026-09-01T11:30:00+02:00"]):
        trace["start"] = stamp
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    listed = langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=10, **WINDOW)
    assert [t["trace_id"] for t in listed["traces"]] == ["trace-02", "trace-01", "trace-03"]
    v1 = FakeLangfuse(count=3, page=10, version="3.12.0")
    for trace, stamp in zip(v1.traces, ["2026-09-01T10:00:00Z", "2026-09-01T10:00:00.900Z", "2026-09-01T11:30:00+02:00"]):
        trace["start"] = stamp
    monkeypatch.setattr(langfuse_v2, "_get", v1.get)
    again = langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=10, **WINDOW)
    assert [t["trace_id"] for t in again["traces"]] == [t["trace_id"] for t in listed["traces"]]


def test_a_batch_chooses_the_api_once_before_its_workers_start(client, project, connection, langfuse):
    from agr import langfuse_source
    langfuse_source.clear_cache()
    langfuse.health_calls = 0
    ids = [t["id"] for t in langfuse.traces[:4]]
    previewed = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ids, **WINDOW})
    assert previewed.status_code == 200 and langfuse.health_calls == 1


def test_the_connection_records_the_api_it_found(client, project, langfuse):
    langfuse.version = "3.12.0"
    created = client.post(f"/projects/{project}/connections", json={"name": "Old", "host": HOST, "public_key": "pk", "secret_key": "sk-lf-hidden"}).json()
    tested = client.post(route(project, created["id"], "test")).json()
    assert tested["status"] == "accessible" and tested["api"] == "v1" and tested["server_version"] == "3.12.0" and "v1 trace API" in tested["api_label"]
    langfuse.version = "4.0.0"
    again = client.post(route(project, created["id"], "test")).json()
    assert again["api"] == "v2" and again["server_version"] == "4.0.0" and "v2 observations API" in again["api_label"]
    assert "sk-lf-hidden" not in json.dumps(again)


def test_a_version_3_server_end_to_end_through_the_workspace(client, project, monkeypatch):
    fake = FakeLangfuse(version="3.12.0")
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    connection = client.post(f"/projects/{project}/connections", json={"name": "Old", "host": HOST, "public_key": "pk", "secret_key": "sk"}).json()["id"]
    assert client.post(route(project, connection, "test")).json()["api"] == "v1"
    listed = client.post(route(project, connection, "traces"), json={**WINDOW, "tag": "prod", "limit": 10}).json()["traces"]
    assert [t["trace_id"] for t in listed] == ["trace-05", "trace-03", "trace-01"]
    fake.missing.add("trace-99")
    preview = client.post(route(project, connection, "trace-previews"), json={"trace_ids": ["trace-05", "trace-01", "trace-99"], **WINDOW})
    assert preview.status_code == 200, preview.text
    manifest = preview.json()
    assert len(manifest["items"]) == 2 and all(i["status"] == "new" for i in manifest["items"])
    # The v1 endpoint returns the whole trace, so there is no "only the selected window was retrieved" note.
    assert not any("time window" in w for i in manifest["items"] for w in i["warnings"])
    assert [e["trace_id"] for e in manifest["fetch_errors"]] == ["trace-99"]
    job = client.post(f"/projects/{project}/imports", json={"preview_id": manifest["id"], "manifest_hash": manifest["manifest_hash"], "selected": [i["id"] for i in manifest["items"]], "idempotency_key": "v3-1", "label": "Old Langfuse"}).json()
    deadline = time.monotonic() + 15
    while job["status"] in ("queued", "importing") and time.monotonic() < deadline:
        time.sleep(0.02)
        job = client.get(f"/projects/{project}/imports/{job['id']}").json()
    assert job["status"] == "completed", job
    assert len([r for r in job["results"] if r["run_id"]]) == 2
    again = client.post(route(project, connection, "traces"), json={**WINDOW, "tag": "prod", "limit": 10}).json()["traces"]
    assert {t["trace_id"]: t["already_imported"] for t in again} == {"trace-05": True, "trace-03": False, "trace-01": True}
    # Only the missing trace tried the other API (a 404 may mean the endpoint, so it is retried once).
    assert not any("/v2/" in p or "/v3/" in p for p in paths(fake)) and fake.wrong_endpoint_hits == 1


# --- What Langfuse itself shows for a trace: ID, metadata, input and output, latency, cost; searching by ID ---


@pytest.mark.parametrize("version", ["4.0.0", "3.12.0"])
def test_rows_carry_what_the_langfuse_trace_table_shows(monkeypatch, version):
    from agr import langfuse_source
    fake = FakeLangfuse(version=version)
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    rows = langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=50, **WINDOW)["traces"]
    top = next(r for r in rows if r["trace_id"] == "trace-03")
    assert top["name"] == "config-fixer" and top["session_id"] == "s-trace-03" and top["user_id"] == "ben" and top["environment"] == "default" and top["tags"] == ["prod"]
    assert top["release"] == "rel-1" and top["version"] == "v7" and top["latency_s"] == 4.5 and isinstance(top["cost"], float)
    assert top["input_preview"] == '{"task": "fix the config trace-03"}' and top["output_preview"] == "done trace-03"
    assert top["metadata"] == {"customer": "acme", "attempt": "3"}


def test_previews_are_short_single_lines_and_metadata_is_flat():
    long_value = {"text": "word " * 200, "nested": {"a": [1, 2, {"b": 3}]}}
    preview = langfuse_v2.preview_text(long_value)
    assert len(preview) == 160 and preview.endswith("…") and "\n" not in preview
    assert langfuse_v2.preview_text("a\n  b\tc") == "a b c" and langfuse_v2.preview_text(None) is None
    meta = langfuse_v2.metadata_summary({**{f"k{n}": n for n in range(40)}, "long": "x" * 500, "nested": {"a": 1}})
    assert len(meta) == 20 and all(len(v) <= 120 for v in meta.values())
    assert langfuse_v2.metadata_summary('{"a": 1}') == {"a": "1"} and langfuse_v2.metadata_summary("not json") == {} and langfuse_v2.metadata_summary([1]) == {}


def test_numbers_must_be_real_numbers():
    row = {"latency": "fast", "totalCost": float("nan"), "cost": True, "calculatedTotalCost": float("inf")}
    extras = langfuse_v2.trace_extras(row, ("latency",), ("totalCost", "calculatedTotalCost", "cost"))
    assert extras["latency_s"] is None and extras["cost"] is None and extras["release"] is None and extras["metadata"] == {}


@pytest.mark.parametrize("version", ["4.0.0", "3.12.0"])
def test_searching_by_trace_id_and_metadata(monkeypatch, version):
    from agr import langfuse_source
    fake = FakeLangfuse(version=version)
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    run = lambda **f: [t["trace_id"] for t in langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=50, **WINDOW, **f)["traces"]]
    assert run(trace_id="trace-03") == ["trace-03"]
    assert run(trace_id="trace-0") == [f"trace-0{n}" for n in range(5, 0, -1)]
    assert run(trace_id="nothing-like-this") == []
    assert run(metadata_key="customer", metadata_value="acme") == ["trace-05", "trace-03", "trace-01"]
    assert run(metadata_key="attempt", metadata_value="4") == ["trace-04"]
    assert run(metadata_key="customer", metadata_value="acme", tag="prod", trace_id="trace-0") == ["trace-05", "trace-03", "trace-01"]
    sent = json.loads(fake.calls[-1][1]["filter"])
    columns = {c["column"] for c in sent}
    assert {"metadata"} <= columns and ({"traceId", "startTime"} <= columns if version.startswith("4") else {"id", "timestamp"} <= columns)
    assert next(c for c in sent if c["column"] == "metadata") == {"type": "stringObject", "column": "metadata", "key": "customer", "operator": "=", "value": "acme"}


def test_a_v1_filter_carries_every_condition_because_it_overrides_the_plain_parameters(monkeypatch):
    from agr import langfuse_source
    fake = FakeLangfuse(version="3.12.0")
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=5, name="triage", tag="staging", user_id="ben", session_id="s-trace-04", environment="default", trace_id="trace-0", **WINDOW)
    sent = json.loads(fake.calls[-1][1]["filter"])
    assert {c["column"] for c in sent} == {"timestamp", "name", "tags", "userId", "sessionId", "environment", "id"}
    assert [c["operator"] for c in sent if c["column"] == "timestamp"] == [">=", "<"]


def test_id_and_metadata_filters_fail_closed_when_the_server_ignores_them(monkeypatch):
    from agr import langfuse_v1
    rows = [{"id": "trace-a", "name": "n", "timestamp": "2026-09-01T10:00:00.000Z", "metadata": {"customer": "acme"}},
            {"id": "trace-b", "name": "n", "timestamp": "2026-09-01T10:00:00.000Z", "metadata": {"customer": "globex"}},
            {"id": "other-c", "name": "n", "timestamp": "2026-09-01T10:00:00.000Z"}]
    monkeypatch.setattr(langfuse_v2, "_get", lambda url, authorization: {"data": rows, "meta": {"page": 1, "totalPages": 1}})
    run = lambda **f: [t["trace_id"] for t in langfuse_v1.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=50, **WINDOW, **f)["traces"]]
    assert run(trace_id="trace-") == ["trace-a", "trace-b"] or set(run(trace_id="trace-")) == {"trace-a", "trace-b"}
    assert run(metadata_key="customer", metadata_value="acme") == ["trace-a"]
    assert run(metadata_key="missing", metadata_value="x") == []


@pytest.mark.parametrize("kwargs", [{"metadata_key": "k"}, {"metadata_value": "v"}, {"trace_id": "x" * 300}, {"metadata_key": 5, "metadata_value": "v"}])
def test_bad_id_and_metadata_filters_are_input_errors(kwargs):
    with pytest.raises(ImportProblem) as raised:
        list_all(**kwargs)
    assert raised.value.code == "invalid_filter"


def test_the_workspace_forwards_id_and_metadata_and_widens_the_range_for_an_id(client, project, connection, langfuse):
    found = client.post(route(project, connection, "traces"), json={**WINDOW, "trace_id": "trace-03"})
    assert found.status_code == 200, found.text
    body = found.json()
    assert [t["trace_id"] for t in body["traces"]] == ["trace-03"]
    # A trace ID is looked up over a long range, not the window the form happened to carry, and the range used is reported.
    sent = langfuse.calls[-1][1]
    assert sent["fromStartTime"] < "2026-09-01" and body["searched"]["from_time"] < "2026-09-01" and body["searched"]["to_time"] > "2026-10-01"
    meta = client.post(route(project, connection, "traces"), json={**WINDOW, "metadata_key": "customer", "metadata_value": "acme"}).json()
    assert [t["trace_id"] for t in meta["traces"]] == ["trace-05", "trace-03", "trace-01"] and meta["searched"]["from_time"].startswith("2026-09-01")
    assert client.post(route(project, connection, "traces"), json={**WINDOW, "metadata_key": "only"}).status_code == 422


def test_the_connection_records_the_langfuse_project(client, project, langfuse):
    created = client.post(f"/projects/{project}/connections", json={"name": "Prod", "host": HOST, "public_key": "pk", "secret_key": "sk-lf-hidden"}).json()
    tested = client.post(route(project, created["id"], "test")).json()
    assert tested["langfuse_project"] == {"id": "proj-prod-1", "name": "Production agent"}
    assert "sk-lf-hidden" not in json.dumps(tested)
    langfuse.project = None
    assert client.post(route(project, created["id"], "test")).json()["langfuse_project"] is None


@pytest.mark.parametrize("payload", [{"data": []}, {"data": [{"id": "x"}]}, {"data": "nope"}, [], {"data": [None]}])
def test_an_unreadable_project_answer_is_no_project(monkeypatch, payload):
    from agr import langfuse_source
    monkeypatch.setattr(langfuse_v2, "_get", lambda url, authorization: payload)
    assert langfuse_source.project(HOST, "pk", "sk") is None


def test_a_project_lookup_failure_never_fails_the_connection(client, project, monkeypatch):
    fake = FakeLangfuse()
    real = fake.get
    def get(url, authorization):
        if url.endswith("/api/public/projects"):
            raise ImportProblem("source_not_found", "This endpoint was not found.", 502)
        return real(url, authorization)
    monkeypatch.setattr(langfuse_v2, "_get", get)
    created = client.post(f"/projects/{project}/connections", json={"name": "Prod", "host": HOST, "public_key": "pk", "secret_key": "sk"}).json()
    tested = client.post(route(project, created["id"], "test"))
    assert tested.status_code == 200 and tested.json()["status"] == "accessible" and tested.json()["langfuse_project"] is None


# --- Checked against Langfuse's documentation: field groups, exact metadata, and the isRootObservation disagreement ---


def test_v2_requests_the_field_groups_that_carry_each_displayed_value(langfuse):
    """Per the observations API docs: cost is in `usage`, latency in `metrics`, trace name, tags and release in
    `trace_context`, input and output in `io`, metadata in its own group."""
    list_all(limit=5)
    groups = set(langfuse.calls[0][1]["fields"].split(","))
    assert {"core", "basic", "io", "metadata", "usage", "metrics", "trace_context"} <= groups


def test_a_metadata_filter_compares_the_whole_value_not_its_display_preview():
    from agr.langfuse_v2 import metadata_matches
    long_a, long_b = "x" * 130 + "A", "x" * 130 + "B"
    assert metadata_matches({"metadata": {"k": long_a}}, ("k", long_a)) is True
    assert metadata_matches({"metadata": {"k": long_a}}, ("k", long_b)) is False
    assert metadata_matches({"metadata": '{"k": "acme", "n": 3}'}, ("k", "acme")) is True
    assert metadata_matches({"metadata": {"n": 3}}, ("n", "3")) is True
    assert metadata_matches({"metadata": {"k": {"a": 1}}}, ("k", '{"a": 1}')) is True
    # Absent, unreadable or a different key never matches.
    for bad in ({"metadata": {"other": "acme"}}, {"metadata": "not json"}, {"metadata": None}, {"metadata": [1]}, {}):
        assert metadata_matches(bad, ("k", "acme")) is False
    assert metadata_matches({}, None) is True


def rejecting_root_flag(rows, after_first_page=False):
    calls = []
    def get(url, authorization):
        if "/v2/metrics" in url:
            raise ImportProblem("source_not_found", "This endpoint was not found.", 502)
        calls.append(url)
        if "isRootObservation" in url and (not after_first_page or len(calls) > 1):
            problem = ImportProblem("source_fetch_failed", "Langfuse could not return the requested trace.", 502)
            problem.http_status = 400
            raise problem
        return {"data": rows, "meta": {}}
    return get, calls


def test_a_server_that_rejects_the_root_flag_is_asked_again_without_it(monkeypatch):
    rows = [{"id": "r1", "traceId": "t1", "traceName": "n", "startTime": "2026-09-01T10:00:00Z", "tags": ["prod"]},
            {"id": "c1", "traceId": "t1", "parentObservationId": "r1", "traceName": "n", "startTime": "2026-09-01T10:00:01Z", "tags": ["prod"]},
            {"id": "r2", "traceId": "t2", "traceName": "n", "startTime": "2026-09-01T11:00:00Z", "tags": ["prod"]}]
    for kwargs in ({}, {"tag": "prod"}):
        get, calls = rejecting_root_flag(rows)
        monkeypatch.setattr(langfuse_v2, "_get", get)
        found = list_all(**kwargs)
        assert sorted(t["trace_id"] for t in found["traces"]) == ["t1", "t2"]
        assert len(calls) == 2 and "isRootObservation" in calls[0] and "isRootObservation" not in calls[1]


def test_the_root_flag_retry_is_only_for_a_rejected_first_request(monkeypatch):
    rows = [{"id": "r1", "traceId": "t1", "traceName": "n", "startTime": "2026-09-01T10:00:00Z"}]
    # Rejected after the first page: a real failure, not retried.
    state = {"n": 0}
    def later(url, authorization):
        state["n"] += 1
        if state["n"] == 1:
            return {"data": rows, "meta": {"cursor": "next"}}
        problem = ImportProblem("source_fetch_failed", "no", 502)
        problem.http_status = 400
        raise problem
    monkeypatch.setattr(langfuse_v2, "_get", later)
    with pytest.raises(ImportProblem):
        list_all()
    assert state["n"] == 2
    # Other failures are never retried as a 400 would be.
    for code, status in (("source_auth_failed", 401), ("source_fetch_failed", 500), ("source_rate_limited", 429)):
        calls = []
        def failing(url, authorization, code=code, status=status):
            calls.append(url)
            problem = ImportProblem(code, "no", 502)
            problem.http_status = status
            raise problem
        monkeypatch.setattr(langfuse_v2, "_get", failing)
        with pytest.raises(ImportProblem) as raised:
            list_all()
        assert raised.value.code == code and len(calls) == 1


def test_a_failed_retry_reports_its_own_error(monkeypatch):
    def always_400(url, authorization):
        problem = ImportProblem("source_fetch_failed", "Langfuse could not return the requested trace.", 502)
        problem.http_status = 400
        raise problem
    monkeypatch.setattr(langfuse_v2, "_get", always_400)
    with pytest.raises(ImportProblem) as raised:
        list_all()
    assert raised.value.code == "source_fetch_failed"


def test_http_errors_carry_their_status_for_callers(monkeypatch):
    import io
    import urllib.error
    class Opener:
        def open(self, request, timeout):
            raise urllib.error.HTTPError(request.full_url, 400, "Bad Request", {}, io.BytesIO(b"{}"))
    monkeypatch.setattr(langfuse_v2.urllib.request, "build_opener", lambda *args: Opener())
    with pytest.raises(ImportProblem) as raised:
        langfuse_v2._get("https://example.test/api/public/v2/observations", "Basic test")
    assert raised.value.http_status == 400 and raised.value.endpoint_missing is False


# --- Checked against the open-source Langfuse server's own source (recorded in fixtures/langfuse/server_contract.json) ---

CONTRACT = json.loads((ROOT / "tests" / "fixtures" / "langfuse" / "server_contract.json").read_text())
EVERY_FILTER = [dict(), dict(name="triage"), dict(tag="prod"), dict(user_id="u"), dict(session_id="s"), dict(environment="default"), dict(trace_id="abc"),
                dict(metadata_key="customer", metadata_value="acme"),
                dict(name="n", tag="t", user_id="u", session_id="s", environment="e", trace_id="abc", metadata_key="k", metadata_value="v")]


def recorded_requests(monkeypatch, version):
    """Every request AGR makes for every combination of filters, as (path, query) pairs."""
    from agr import langfuse_source
    fake = FakeLangfuse(version=version)
    seen = []
    def get(url, authorization):
        if "/api/public/traces" in url or "/v2/observations" in url:
            seen.append((urlsplit(url).path, {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}))
        return fake.get(url, authorization)
    monkeypatch.setattr(langfuse_v2, "_get", get)
    for combo in EVERY_FILTER:
        langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=5, **WINDOW, **combo)
    langfuse_source.fetch_trace("trace-01", host=HOST, public_key="pk", secret_key="sk", **WINDOW)
    return seen


@pytest.mark.parametrize("version,api,contract_key", [("3.12.0", "v1", "v1_traces"), ("4.59.0", "v2", "v2_observations")])
def test_every_request_stays_within_what_the_langfuse_server_accepts(monkeypatch, version, api, contract_key):
    import re
    contract, accepted = CONTRACT[contract_key], CONTRACT["filter_operators"]
    stamp = re.compile(CONTRACT["datetime_regex"])
    requests = recorded_requests(monkeypatch, version)
    assert len(requests) >= len(EVERY_FILTER)
    for path, query in requests:
        if path.endswith("/api/public/traces") or path.endswith("/v2/observations"):
            assert set(query) <= set(contract["query_parameters"]), (path, set(query) - set(contract["query_parameters"]))
            assert int(query["limit"]) <= contract["limit_max"]
        assert set(query.get("fields", "").split(",")) - {""} <= set(contract["field_groups"]), query.get("fields")
        for key in ("fromTimestamp", "toTimestamp", "fromStartTime", "toStartTime"):
            if key in query:
                assert stamp.match(query[key]), (key, query[key])
        if "isRootObservation" in query:
            assert query["isRootObservation"] in CONTRACT["v2_observations"]["is_root_observation_values"]
        if "orderBy" in query:
            assert query["orderBy"].split(".")[0] in CONTRACT["v1_traces"]["order_by_columns"]
        if "filter" in query:
            for condition in json.loads(query["filter"]):
                assert condition["column"] in contract["filter_columns"], condition
                assert condition["operator"] in accepted[condition["type"]], condition
                if condition["type"] == "datetime":
                    assert stamp.match(condition["value"]), condition
                if condition["type"] == "stringObject":
                    assert condition.get("key"), condition
                if condition["type"] in ("stringOptions", "arrayOptions"):
                    assert isinstance(condition["value"], list) and condition["value"], condition
    # The API that was chosen is the only one that was called.
    assert all(("/v2/" in path) == (api == "v2") for path, _ in requests if "health" not in path and "/projects" not in path)


def test_the_contract_records_where_it_came_from():
    assert CONTRACT["source"]["commit"] and CONTRACT["source"]["web_package_version"].startswith("4.")
    assert CONTRACT["v1_traces"]["limit_max"] == 100 and CONTRACT["v2_observations"]["limit_max"] == 1000
    assert langfuse_v1.PAGE_SIZE <= CONTRACT["v1_traces"]["limit_max"]


def test_both_timestamp_forms_the_server_accepts_are_what_we_send():
    import re
    stamp = re.compile(CONTRACT["datetime_regex"])
    assert stamp.match("2026-09-01T00:00:00.000Z") and stamp.match("2026-09-01T00:00:00+00:00")
    assert not stamp.match("2026-09-01T00:00:00") and not stamp.match("2026-09-01 00:00:00Z") and not stamp.match("2026-09-01T00:00:00+0200")


@pytest.mark.parametrize("flags,expected_api,hits", [({"serves_v2": False, "serves_v1": True}, "v1", 1), ({"serves_v1": False, "serves_v2": True}, "v2", 0)])
def test_a_self_hosted_version_4_server_works_whichever_api_its_mode_serves(monkeypatch, flags, expected_api, hits):
    """Observations v2 answers 404 on a self-hosted 4.x server unless its v4 preview opt-in is set, and the v1 trace
    endpoints answer 404 in events-only mode. The version alone cannot tell which, so a 404 moves to the other API."""
    from agr import langfuse_source
    fake = FakeLangfuse(version="4.59.0")
    for key, value in flags.items():
        setattr(fake, key, value)
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    listed = langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=5, **WINDOW)
    assert len(listed["traces"]) == 5 and fake.wrong_endpoint_hits == hits
    fetched = langfuse_source.fetch_trace("trace-01", host=HOST, public_key="pk", secret_key="sk", **WINDOW)
    assert fetched["observations"] or fetched.get("trace")
    assert fake.wrong_endpoint_hits == hits  # remembered: the other API is not asked again
    assert langfuse_source.detect(HOST)["api"] == expected_api


def test_the_connection_shows_the_api_a_self_hosted_version_4_server_really_uses(client, project, monkeypatch):
    fake = FakeLangfuse(version="4.59.0")
    fake.serves_v2, fake.serves_v1 = False, True
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    created = client.post(f"/projects/{project}/connections", json={"name": "Self-hosted", "host": HOST, "public_key": "pk", "secret_key": "sk"}).json()
    tested = client.post(route(project, created["id"], "test")).json()
    assert tested["status"] == "accessible" and tested["server_version"] == "4.59.0" and tested["api"] == "v1" and "v1 trace API" in tested["api_label"]


def test_an_app_root_observation_with_a_parent_id_is_still_a_trace_root(monkeypatch):
    """The SDK can mark an observation as the application root while it keeps a physical parent; the server's
    isRootObservation flag, not the parent ID, says which row represents the trace."""
    rows = [{"id": "app", "traceId": "t1", "parentObservationId": "outer", "isRootObservation": True, "traceName": "n", "startTime": "2026-09-01T10:00:00Z"},
            {"id": "child", "traceId": "t1", "parentObservationId": "app", "isRootObservation": False, "traceName": "n", "startTime": "2026-09-01T10:00:01Z"},
            {"id": "plain", "traceId": "t2", "parentObservationId": None, "isRootObservation": True, "traceName": "n", "startTime": "2026-09-01T11:00:00Z"},
            {"id": "unflagged-child", "traceId": "t3", "parentObservationId": "x", "traceName": "n", "startTime": "2026-09-01T12:00:00Z"},
            {"id": "unflagged-root", "traceId": "t4", "traceName": "n", "startTime": "2026-09-01T13:00:00Z"}]
    monkeypatch.setattr(langfuse_v2, "_get", lambda url, authorization: {"data": rows, "meta": {}})
    assert sorted(t["trace_id"] for t in list_all()["traces"]) == ["t1", "t2", "t4"]


def test_langfuses_minus_one_for_uncomputed_latency_and_cost_is_unknown():
    row = {"latency": -1, "totalCost": -1}
    extras = langfuse_v2.trace_extras(row, ("latency",), ("totalCost",))
    assert extras["latency_s"] is None and extras["cost"] is None
    assert langfuse_v2.trace_extras({"latency": 0, "totalCost": 0.0}, ("latency",), ("totalCost",)) | {} == {**extras, "latency_s": 0, "cost": 0.0}


def test_a_v1_trace_with_the_cloud_deprecation_notice_still_imports(monkeypatch):
    """On Langfuse Cloud the deprecated v1 responses carry a `_deprecation` object beside the trace."""
    from agr import langfuse_source
    from agr.ingest_langfuse import LANGFUSE_ADAPTER
    fake = FakeLangfuse(version="3.12.0")
    trace = fake.v1_trace("trace-01")
    trace["_deprecation"] = {"sunsetDate": "2026-11-16", "replacement": "GET /api/public/v2/observations"}
    trace.update(externalId=None, projectId="proj-prod-1", public=False, bookmarked=False, createdAt="2026-09-01T10:00:00Z", updatedAt="2026-09-01T10:00:00Z", htmlPath="/project/p/traces/trace-01", totalCost=0.1, latency=2.0)
    monkeypatch.setattr(langfuse_v2, "_get", lambda url, authorization: fake.get(url, authorization) if "health" in url else trace)
    fetched = langfuse_source.fetch_trace("trace-01", host=HOST, public_key="pk", secret_key="sk", **WINDOW)
    result = LANGFUSE_ADAPTER.convert(fetched)
    assert result.doc["steps"] and result.doc["run"]["logical_run_id"].endswith("trace-01")


# --- Whole-trace cost on Langfuse 4 (observation rows only carry the root observation's cost) ---

def _trace_cost(trace_id):
    return round(0.25 + 0.01 * int(trace_id.rsplit("-", 1)[-1]), 4)


def test_v2_lists_the_whole_trace_cost_from_the_metrics_api(monkeypatch):
    from agr import langfuse_source
    fake = FakeLangfuse(count=3, page=10)
    fake.metrics = True
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    listed = langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=10, **WINDOW)["traces"]
    assert {t["trace_id"]: t["cost"] for t in listed} == {t["id"]: _trace_cost(t["id"]) for t in fake.traces}
    assert all(t["cost_scope"] == "trace" for t in listed)


def test_the_metrics_request_matches_the_servers_query_model(monkeypatch):
    from agr import langfuse_source
    fake = FakeLangfuse(count=3, page=10)
    fake.metrics = True
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=10, **WINDOW)
    contract = CONTRACT["v2_metrics"]
    (query,) = fake.metric_queries
    assert query["view"] in contract["views"] and query["dimensions"] == [{"field": contract["observations_dimension"]}]
    assert query["metrics"] == [{"measure": contract["measure"], "aggregation": contract["aggregation"]}]
    # The trace-ID dimension is high cardinality: the server only groups by it for a top-N (explicit limit, descending measure).
    (bound,) = query["filters"]
    assert bound["column"] == contract["observations_dimension"] and bound["operator"] == "any of" and bound["type"] == "stringOptions" and all(bound["value"])
    assert query["orderBy"] == [{"field": contract["order_by_field"], "direction": contract["order_by_direction"]}]
    assert 0 < query["config"]["row_limit"] <= contract["row_limit_max"] and "timeDimension" not in query
    assert re.fullmatch(CONTRACT["datetime_regex"], query["fromTimestamp"]) and re.fullmatch(CONTRACT["datetime_regex"], query["toTimestamp"])


def test_many_traces_are_costed_in_batches_the_server_accepts(monkeypatch):
    from agr import langfuse_source
    fake = FakeLangfuse(count=120, page=250)
    fake.metrics = True
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    listed = langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=120, **WINDOW)["traces"]
    assert len(listed) == 120 and all(t["cost_scope"] == "trace" for t in listed)
    sizes = [len(q["filters"][0]["value"]) for q in fake.metric_queries]
    assert sizes == [50, 50, 20]


@pytest.mark.parametrize("failure", ["missing", "rate_limited", "garbage"])
def test_without_the_metrics_api_the_root_cost_stays(monkeypatch, failure):
    from agr import langfuse_source
    fake = FakeLangfuse(count=3, page=10)
    fake.metrics = True
    def get(url, authorization):
        if "/v2/metrics" in url:
            if failure == "missing":
                raise fake.endpoint_missing()
            if failure == "rate_limited":
                raise ImportProblem("source_rate_limited", "Slow down.", 502)
            return {"data": "nope"}
        return fake.get(url, authorization)
    monkeypatch.setattr(langfuse_v2, "_get", get)
    listed = langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=10, **WINDOW)["traces"]
    assert len(listed) == 3 and all(t["cost_scope"] == "root" and t["cost"] is not None for t in listed)
    assert fake.metric_queries == [] or failure == "garbage"


def test_a_servers_odd_cost_values_never_break_the_listing(monkeypatch):
    from agr import langfuse_source
    fake = FakeLangfuse(count=3, page=10)
    def get(url, authorization):
        if "/v2/metrics" in url:
            return {"data": [{"traceId": "trace-01", "sum_totalCost": "not a number"}, {"traceId": "trace-02", "sum_totalCost": -1}, {"traceId": "unknown", "sum_totalCost": 5}, {"traceId": "trace-03", "sum_totalCost": 0.5}, "junk"]}
        return fake.get(url, authorization)
    monkeypatch.setattr(langfuse_v2, "_get", get)
    listed = {t["trace_id"]: t for t in langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=10, **WINDOW)["traces"]}
    assert listed["trace-03"]["cost"] == 0.5 and listed["trace-03"]["cost_scope"] == "trace"
    assert listed["trace-01"]["cost_scope"] == listed["trace-02"]["cost_scope"] == "root"
    assert "unknown" not in listed


def test_version_3_costs_are_already_trace_level(monkeypatch):
    from agr import langfuse_source
    fake = FakeLangfuse(count=3, page=10, version="3.12.0")
    monkeypatch.setattr(langfuse_v2, "_get", fake.get)
    listed = langfuse_source.list_traces(host=HOST, public_key="pk", secret_key="sk", limit=10, **WINDOW)["traces"]
    assert all(t["cost"] == 0.02 and "cost_scope" not in t for t in listed)


def test_the_fake_enforces_the_servers_top_n_rule():
    base = {"view": "observations", "dimensions": [{"field": "traceId"}], "metrics": [{"measure": "totalCost", "aggregation": "sum"}], "config": {"row_limit": 1000},
            "orderBy": [{"field": "sum_totalCost", "direction": "desc"}]}
    FakeLangfuse.check_top_n(base)
    for broken in ({**base, "orderBy": None}, {**base, "orderBy": [{"field": "sum_totalCost", "direction": "asc"}]}, {**base, "orderBy": [{"field": "sum_other", "direction": "desc"}]},
                   {**base, "config": {}}, {**base, "timeDimension": {"granularity": "day"}}):
        with pytest.raises(ImportProblem) as raised:
            FakeLangfuse.check_top_n(broken)
        assert raised.value.http_status == 400


def test_a_slow_metrics_server_cannot_hold_a_listing_past_its_budget(monkeypatch):
    import time
    from http.server import BaseHTTPRequestHandler, HTTPServer
    import threading

    class Slow(BaseHTTPRequestHandler):
        def do_GET(self):
            time.sleep(3)
            try:
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"data": []}')
            except OSError:
                pass

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Slow)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        monkeypatch.setattr(langfuse_v2, "METRICS_SECONDS", 0.4)
        started = time.monotonic()
        costs = langfuse_v2.trace_costs("http://127.0.0.1:%d" % server.server_port, "Basic x", ["a", "b"], "2026-09-01T00:00:00.000Z", "2026-09-02T00:00:00.000Z")
        # The request in flight is cut off by the budget instead of running to the per-request limit.
        assert costs == {} and time.monotonic() - started < 2.0
        # The budget is cleared afterwards so an ordinary request is not capped by it.
        assert getattr(langfuse_v2._budget, "end", None) is None and not getattr(langfuse_v2._budget, "batch", False)
    finally:
        server.shutdown()
        server.server_close()


def test_the_cost_requests_share_one_budget(monkeypatch):
    seen = []
    def get(url, authorization):
        seen.append(getattr(langfuse_v2._budget, "end", None))
        return {"data": []}
    monkeypatch.setattr(langfuse_v2, "_get", get)
    langfuse_v2.trace_costs(HOST, "Basic x", [f"t{n}" for n in range(120)], "2026-09-01T00:00:00.000Z", "2026-09-02T00:00:00.000Z")
    assert len(seen) == 3 and all(end is not None for end in seen) and len(set(seen)) == 1
