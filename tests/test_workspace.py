"""Engineer import journeys and project boundaries, without live credentials."""
import copy
import io
import json
from pathlib import Path
import time
import zipfile

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from agr.api import create_app
from agr import imports
from agr.imports import ImportProblem
from agr.store import Store
from agr.workspace import Workspace, atomic_json

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def client(tmp_path):
    app = create_app(str(tmp_path / "store"), allowed_hosts=["192.0.2.10", "review.example"])
    with TestClient(app, headers={"authorization": "Bearer " + app.state.workspace_session_token}) as result:
        yield result
    app.state.workspace.executor.shutdown(wait=True)


@pytest.fixture
def project(client):
    response = client.post("/projects", json={"name": "Agent engineering"})
    assert response.status_code == 200
    return response.json()["id"]


def doc(name="clean_pass"):
    return json.loads((ROOT / "agr" / "demo_fixtures" / (name + ".atif.json")).read_text())


def preview(client, project, source, files, options=None):
    prefix = f"/projects/{project}"
    created = client.post(prefix + "/import-previews", json={"source": source, "options": options or {}})
    assert created.status_code == 200, created.text
    preview_id = created.json()["id"]
    for name, value in files.items():
        content = value if isinstance(value, bytes) else json.dumps(value).encode()
        response = client.put(prefix + f"/import-previews/{preview_id}/files", params={"name": name}, content=content, headers={"content-type": "application/octet-stream"})
        assert response.status_code == 200, response.text
    return client.post(prefix + f"/import-previews/{preview_id}/inspect")


def submit(client, project, manifest, key="test-import", selected=None):
    return client.post(f"/projects/{project}/imports", json={"preview_id": manifest["id"], "manifest_hash": manifest["manifest_hash"], "selected": selected or [i["id"] for i in manifest["items"] if i["status"] != "invalid"], "idempotency_key": key, "label": "First evaluation"})


def finish(client, project, job):
    # Importing writes a complete capture; slow CI filesystems can exceed two
    # seconds. Keep a bounded wall-clock deadline rather than a poll count.
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        job = client.get(f"/projects/{project}/imports/{job['id']}").json()
        if job["status"] not in ("queued", "importing", "cancelling"):
            return job
        time.sleep(0.01)
    pytest.fail(f"Import did not finish: {job}")


def test_legacy_store_registered_without_rewriting(client, tmp_path):
    settings = client.get("/workspace").json()
    assert settings["reviewer"] == "Local reviewer"
    assert settings["projects"][0]["id"] == "existing"
    assert client.get("/runs").json() == []


def test_catalog_matches_actual_supported_sources(client):
    assert {s["id"] for s in client.get("/sources/catalog").json()["sources"]} == {"harbor", "claude", "pi", "otel", "langfuse", "langsmith", "atif"}


def test_project_names_and_unknown_ids_rejected(client):
    assert client.post("/projects", json={"name": "  "}).status_code == 422
    assert client.get("/projects/../../outside").status_code == 404
    assert client.get("/runs", headers={"x-agr-project": "unknown"}).status_code == 404


def test_preview_import_review_and_duplicate_noop(client, project):
    document = doc()
    manifest = preview(client, project, "atif", {"own-run.json": document}).json()
    item = manifest["items"][0]
    assert item["status"] == "new"
    assert "doc" not in item
    assert client.get("/runs", headers={"x-agr-project": project}).json() == []
    job = finish(client, project, submit(client, project, manifest).json())
    assert job["status"] == "completed", job
    result = job["results"][0]
    assert result["status"] == "new"
    review = client.get("/runs/" + result["run_id"], headers={"x-agr-project": project})
    assert review.status_code == 200
    assert review.json()["review_mode"] == "deterministic_only"
    second = preview(client, project, "atif", {"own-run.json": document}).json()
    assert second["items"][0]["status"] == "duplicate"
    duplicate = finish(client, project, submit(client, project, second, key="second-import").json())
    assert duplicate["results"][0]["status"] == "duplicate"
    assert len(client.get("/runs", headers={"x-agr-project": project}).json()) == 1


def test_project_cache_and_artifact_isolation(client, project):
    other = client.post("/projects", json={"name": "Another project"}).json()["id"]
    manifest = preview(client, project, "atif", {"own.json": doc()}).json()
    job = finish(client, project, submit(client, project, manifest).json())
    headers = {"x-agr-project": project}
    assert len(client.get("/runs", headers=headers).json()) == 1
    assert client.get("/runs", headers={"x-agr-project": other}).json() == []
    assert len(client.get("/runs", headers=headers).json()) == 1
    assert client.get("/runs/" + job["results"][0]["run_id"], headers={"x-agr-project": other}).status_code == 404
    assert client.get(f"/projects/{other}/imports/{job['id']}").status_code == 404
    assert client.get(f"/projects/{other}/import-previews/{manifest['id']}").status_code == 404
    assert client.get("/fleet/episodes", headers={"x-agr-project": other}).json() == []
    assert client.get(f"/projects/{other}/imports/{job['id']}/originals", params={"name": "own.json"}).status_code == 404
    original = client.get(f"/projects/{project}/imports/{job['id']}/originals", params={"name": "own.json"})
    assert original.status_code == 200
    assert json.loads(original.content) == doc()


def test_manifest_and_idempotency_constraints(client, project):
    manifest = preview(client, project, "atif", {"run.json": doc()}).json()
    changed = dict(manifest, manifest_hash="wrong")
    assert submit(client, project, changed).status_code == 409
    first = submit(client, project, manifest)
    assert first.status_code == 200
    assert submit(client, project, manifest).json()["id"] == first.json()["id"]
    assert submit(client, project, manifest, key="invalid", selected=["absent"]).status_code == 422
    assert client.put(f"/projects/{project}/import-previews/{manifest['id']}/files", params={"name": "new.json"}, content=b"{}").status_code == 409
    finish(client, project, first.json())


@pytest.mark.parametrize("name", ["../outside.json", "/outside.json", "a/../../outside.json", "C:/outside.json", "a\\outside.json", "CON.json", "file:stream", "a/./b.json"])
def test_upload_paths_cannot_escape_staging(client, project, name):
    manifest = client.post(f"/projects/{project}/import-previews", json={"source": "atif"}).json()
    assert client.put(f"/projects/{project}/import-previews/{manifest['id']}/files", params={"name": name}, content=b"{}").status_code == 422


def test_zip_traversal_rejected(client, project):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("../../outside.json", json.dumps(doc()))
    response = preview(client, project, "atif", {"capture.zip": buffer.getvalue()})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_path"


def test_invalid_files_are_accounted_without_blocking_valid_runs(client, project):
    response = preview(client, project, "atif", {"run.json": doc(), "broken.json": b"not JSON", "readme.txt": b"auxiliary"})
    assert response.status_code == 200
    manifest = response.json()
    assert [i["status"] for i in manifest["items"]].count("invalid") == 1
    assert len(manifest["ignored"]) == 1
    job = finish(client, project, submit(client, project, manifest).json())
    assert len(job["results"]) == 1


def test_claude_final_only_stays_limited_and_unverified(client, project):
    capture = {"type": "result", "subtype": "success", "session_id": "session-1", "result": "done", "is_error": False, "duration_ms": 100, "num_turns": 1}
    manifest = preview(client, project, "claude", {"session.json": capture}).json()
    assert manifest["items"][0]["status"] != "invalid", manifest
    assert not manifest["items"][0]["evaluation_available"]
    job = finish(client, project, submit(client, project, manifest).json())
    review = client.get("/runs/" + job["results"][0]["run_id"], headers={"x-agr-project": project}).json()
    assert review["outcome"]["status"] == "UNVERIFIED"


def test_span_exports_partition_by_trace_before_conversion(client, project):
    fixture = json.loads((ROOT / "tests" / "fixtures" / "otel" / "otel_toy_run.json").read_text())
    second = copy.deepcopy(fixture)
    for resource in second["resourceSpans"]:
        for scope in resource["scopeSpans"]:
            for span in scope["spans"]:
                span["traceId"] = "b" * 32
    fixture["resourceSpans"].extend(second["resourceSpans"])
    manifest = preview(client, project, "otel", {"traces.json": fixture}).json()
    assert len(manifest["items"]) == 2, manifest
    assert len({i["run_id"] for i in manifest["items"]}) == 2
    assert all(i["status"] != "invalid" for i in manifest["items"])


def test_langsmith_multitrace_unscoped_feedback_not_reused(client, project):
    runs = [{"id": key, "trace_id": key, "run_type": "chain", "name": "agent", "inputs": {"task": "test"}, "outputs": {"result": "done"}, "start_time": "2026-10-01T00:00:00Z"} for key in ("one", "two")]
    manifest = preview(client, project, "langsmith", {"runs.json": {"runs": runs, "feedback": [{"id": "f", "key": "correctness", "score": 1}]}}).json()
    assert len(manifest["items"]) == 2
    assert all(not i["evaluation_available"] for i in manifest["items"])
    assert all(any("Unscoped" in w for w in i["warnings"]) for i in manifest["items"])


def test_verifier_cannot_be_applied_to_multiple_files(client, project):
    response = preview(client, project, "atif", {"one.json": doc(), "two.json": doc(), "checks.json": {"checks": []}}, {"verifier_file": "checks.json"})
    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "invalid_verifier"


def test_read_only_demo_and_cross_origin_management(client, tmp_path, project):
    assert client.post("/projects", json={"name": "attacker"}, headers={"origin": "https://external.example"}).status_code == 403
    assert client.get("/workspace", headers={"host": "external.example"}).status_code == 403
    assert client.get("/projects", headers={"host": "external.example"}).status_code == 403
    with TestClient(create_app(str(tmp_path / "demo"), read_only=True)) as demo:
        assert demo.get("/workspace").json()["projects"][0]["sample"]
        assert demo.post("/projects", json={"name": "new"}).status_code == 403
    client.patch(f"/projects/{project}", json={"archived": True})
    assert client.post(f"/projects/{project}/import-previews", json={"source": "atif"}).status_code == 403


def test_restart_persists_projects_and_identity(tmp_path):
    root = tmp_path / "store"
    first = Workspace(root)
    project = first.create_project("Saved project")
    first.update_settings({"reviewer": "Test engineer", "last_project": project["id"]})
    first.executor.shutdown()
    second = Workspace(root)
    assert second.settings()["last_project"] == project["id"]
    assert second.settings()["reviewer"] == "Test engineer"
    assert second.project(project["id"])["name"] == "Saved project"
    second.executor.shutdown()


def test_failed_analysis_retries_only_failed_work(client, project, monkeypatch):
    from agr import workspace as module
    actual = module.publish_analysis
    failed_id = doc("ignored_failure")["run"]["logical_run_id"]
    def fail_one(document, store, scratch):
        if document["run"]["logical_run_id"] == failed_id:
            raise OSError("storage full")
        return actual(document, store, scratch)
    monkeypatch.setattr(module, "publish_analysis", fail_one)
    manifest = preview(client, project, "atif", {"one.json": doc(), "two.json": doc("ignored_failure")}).json()
    job = finish(client, project, submit(client, project, manifest).json())
    assert job["status"] == "completed_with_errors"
    assert {r["status"] for r in job["results"]} == {"new", "failed"}
    monkeypatch.setattr(module, "publish_analysis", actual)
    retried = client.post(f"/projects/{project}/imports/{job['id']}/retry").json()
    result = finish(client, project, retried)
    assert result["status"] == "completed"
    assert len(result["results"]) == 2
    assert all(r["status"] == "new" for r in result["results"])


def test_investigation_pins_capture_and_attribution(client, project):
    document = doc()
    manifest = preview(client, project, "atif", {"run.json": document}).json()
    job = finish(client, project, submit(client, project, manifest).json())
    result = job["results"][0]
    saved = client.post(f"/projects/{project}/investigations", json={"run_id": result["run_id"], "capture_id": result["capture_id"], "title": "Check retry policy", "action": "Compare one bounded retry with the baseline", "actor": "spoofed"})
    assert saved.status_code == 200, saved.text
    record = saved.json()
    assert record["capture_id"] == result["capture_id"]
    assert record["actor"] == "Local reviewer"
    document["run"]["model"] = "new-model"
    second = preview(client, project, "atif", {"run.json": document}).json()
    assert second["items"][0]["status"] == "new_capture"
    finish(client, project, submit(client, project, second, key="new-capture").json())
    route = "/runs/" + result["run_id"]
    assert client.get(route, headers={"x-agr-project": project}).json()["capture"]["capture_id"] != record["capture_id"]
    snapshot_headers = {"x-agr-project": project, "x-agr-capture": record["capture_id"]}
    assert client.get(route, headers=snapshot_headers).json()["capture"]["capture_id"] == record["capture_id"]
    updated = client.patch(f"/projects/{project}/investigations/{record['id']}", json={**record, "status": "resolved"})
    assert updated.status_code == 200, updated.text
    assert updated.json()["capture_id"] == record["capture_id"]
    assert updated.json()["revision"] == 2
    assert updated.json()["history"][0]["status"] == "open"
    assert client.post(route + "/workflow", headers=snapshot_headers, json={"disposition": "reviewed"}).status_code == 403


def test_session_credentials_not_persisted_and_disconnect_keeps_imports(client, project, monkeypatch):
    from agr import langfuse_v2
    secret = "sk-private-test-value"
    connection = client.post(f"/projects/{project}/connections", json={"host": "https://cloud.langfuse.com", "public_key": "pk-test", "secret_key": secret}).json()
    assert "secret_key" not in connection and secret not in json.dumps(connection)
    root = client.app.state.workspace.root
    assert all(secret not in p.read_text(encoding="utf-8") for p in root.rglob("*.json"))
    capture = json.loads((ROOT / "tests" / "fixtures" / "langfuse" / "langfuse_toy_run.json").read_text())
    monkeypatch.setattr(langfuse_v2, "fetch_trace", lambda *a, **kw: capture)
    fetched = client.post(f"/projects/{project}/connections/{connection['id']}/trace-previews", json={"trace_id": "trace", "from_time": "2026-10-01T00:00:00Z", "to_time": "2026-10-02T00:00:00Z"})
    assert fetched.status_code == 200, fetched.text
    job = finish(client, project, submit(client, project, fetched.json()).json())
    assert job["status"] == "completed"
    assert client.delete(f"/projects/{project}/connections/{connection['id']}").status_code == 200
    assert len(client.get("/runs", headers={"x-agr-project": project}).json()) == 1


def test_live_v2_pagination_and_scores_mapping(monkeypatch):
    from agr import langfuse_v2
    calls = []
    def get(url, authorization):
        calls.append(url)
        if "/scores" in url:
            return {"data": [{"id": "score", "name": "check", "dataType": "CATEGORICAL", "value": "pass", "subject": {"kind": "trace", "id": "trace"}}], "meta": {"cursor": None}}
        if "cursor=" in url:
            return {"data": [{"id": "child", "traceId": "trace", "type": "GENERATION", "parentObservationId": "root", "startTime": "2026-10-01T00:00:01Z", "input": "{\"prompt\": \"hello\"}", "output": "done"}], "meta": {"cursor": None}}
        return {"data": [{"id": "root", "traceId": "trace", "type": "AGENT", "isRootObservation": True, "startTime": "2026-10-01T00:00:00Z", "input": "task", "output": "done"}], "meta": {"cursor": "next"}}
    monkeypatch.setattr(langfuse_v2, "_get", get)
    capture = langfuse_v2.fetch_trace("trace", host="https://example.test", public_key="pk", secret_key="sk", from_time="2026-10-01T00:00:00Z", to_time="2026-10-02T00:00:00Z")
    assert len(capture["observations"]) == 2
    assert len(calls) == 3
    assert capture["scores"][0]["stringValue"] == "pass"
    assert capture["observations"][1]["input"] == {"prompt": "hello"}
    assert all("traceId=trace" in url for url in calls)


def test_live_v2_rejects_other_trace_and_repeated_cursor(monkeypatch):
    from agr import langfuse_v2
    monkeypatch.setattr(langfuse_v2, "_get", lambda *a: {"data": [{"id": "id", "traceId": "another"}]})
    with pytest.raises(ImportProblem, match="another trace"):
        langfuse_v2.fetch_trace("trace", host="https://example.test", public_key="pk", secret_key="sk", from_time="2026-10-01T00:00:00Z", to_time="2026-10-02T00:00:00Z")


def test_sample_creation_is_read_only_and_idempotent(client):
    sample = client.post("/projects/sample")
    assert sample.status_code == 200, sample.text
    project_id = sample.json()["id"]
    assert sample.json()["sample"]
    assert client.post("/projects/sample").json()["id"] == project_id
    assert client.get("/runs", headers={"x-agr-project": project_id}).json()
    assert client.post(f"/projects/{project_id}/import-previews", json={"source": "atif"}).status_code == 403
    first_run = client.get("/runs", headers={"x-agr-project": project_id}).json()[0]["run_id"]
    assert client.post("/runs/" + first_run + "/workflow", headers={"x-agr-project": project_id}, json={"disposition": "reviewed"}).status_code == 403


def test_harbor_uploaded_job_discovery_preserves_trial_sidecars(client, project):
    source = ROOT / "agr" / "demo_fixtures" / "real_demo" / "runs"
    # These are normalized documents, not Harbor source trajectories; use the
    # real Harbor fixture exported by the repository's existing adapter tests.
    fixture = ROOT / "archive" / "synthetic" / "fixtures" / "harbor_trial"
    files = {"job/trial/" + str(p.relative_to(fixture)).replace("\\", "/"): p.read_bytes() for p in fixture.rglob("*") if p.is_file()}
    if not files:
        pytest.fail("Harbor source fixture missing")
    manifest = preview(client, project, "harbor", files)
    assert manifest.status_code == 200, manifest.text
    assert any(i["status"] != "invalid" for i in manifest.json()["items"]), manifest.json()
    job = finish(client, project, submit(client, project, manifest.json()).json())
    assert job["status"] == "completed", job


def test_project_review_actor_comes_from_workspace(client, project):
    client.patch("/workspace", json={"reviewer": "Engineer"})
    manifest = preview(client, project, "atif", {"run.json": doc()}).json()
    job = finish(client, project, submit(client, project, manifest).json())
    run_id = job["results"][0]["run_id"]
    response = client.post("/runs/" + run_id + "/feedback", headers={"x-agr-project": project}, json={"mutation_id": "feedback1", "moment_id": "note", "kind": "agree", "actor": "spoofed"})
    assert response.status_code == 200, response.text
    feedback = client.get("/runs/" + run_id, headers={"x-agr-project": project}).json()["feedback"]
    assert feedback[0]["actor"] == "Engineer"


def test_expired_unsubmitted_previews_do_not_import(client, project):
    workspace = client.app.state.workspace
    manifest = preview(client, project, "atif", {"run.json": doc()}).json()
    record = workspace.record("previews", manifest["id"], project)
    record["created_epoch"] = time.time() - 90000
    atomic_json(workspace._path("previews", manifest["id"]), record)
    assert submit(client, project, manifest).status_code == 410


def test_submitted_preview_survives_expiry_for_retry(client, project):
    workspace = client.app.state.workspace
    manifest = preview(client, project, "atif", {"run.json": doc()}).json()
    job = finish(client, project, submit(client, project, manifest).json())
    record = workspace.record("previews", manifest["id"], project)
    record["created_epoch"] = time.time() - 90000
    atomic_json(workspace._path("previews", manifest["id"]), record)
    assert client.get(f"/projects/{project}/imports/{job['id']}/originals", params={"name": "run.json"}).status_code == 200


@pytest.mark.parametrize("source", ["pi", "otel", "langsmith", "langfuse"])
def test_file_source_reaches_project_review(client, project, source):
    if source == "pi":
        records = [{"type": "session", "version": 3, "id": "12345678-1234-1234-1234-123456789abc", "timestamp": "2026-10-01T00:00:00Z"},
                   {"type": "message", "id": "m1", "parentId": None, "timestamp": "2026-10-01T00:00:01Z", "message": {"role": "user", "content": "Inspect the repository"}},
                   {"type": "message", "id": "m2", "parentId": "m1", "timestamp": "2026-10-01T00:00:02Z", "message": {"role": "assistant", "content": [{"type": "text", "text": "Inspected"}], "model": "test-model"}}]
        files = {"session.jsonl": "\n".join(json.dumps(r) for r in records).encode()}
    else:
        files = {"export.json": (ROOT / "tests" / "fixtures" / source / (source + "_toy_run.json")).read_bytes()}
    response = preview(client, project, source, files)
    assert response.status_code == 200, response.text
    manifest = response.json()
    assert all(i["status"] != "invalid" for i in manifest["items"]), manifest
    job = finish(client, project, submit(client, project, manifest).json())
    assert job["status"] == "completed", job
    for result in job["results"]:
        review = client.get("/runs/" + result["run_id"], headers={"x-agr-project": project})
        assert review.status_code == 200, review.text
        assert review.json()["capture"]["capture_id"] == result["capture_id"]


def test_cancelled_queue_retries_and_restart_keeps_committed_results(client, project, monkeypatch):
    workspace = client.app.state.workspace
    manifest = preview(client, project, "atif", {"one.json": doc(), "two.json": doc("stuck_retry")}).json()
    real_submit = workspace.executor.submit
    monkeypatch.setattr(workspace.executor, "submit", lambda *args: None)
    job = submit(client, project, manifest).json()
    assert job["owner_pid"]
    assert client.post(f"/projects/{project}/imports/{job['id']}/cancel").json()["status"] == "cancelling"
    workspace._run_job(project, job["id"])
    cancelled = finish(client, project, job)
    assert cancelled["status"] == "cancelled" and not cancelled["results"]
    monkeypatch.setattr(workspace.executor, "submit", real_submit)
    retried = client.post(f"/projects/{project}/imports/{job['id']}/retry").json()
    finished = finish(client, project, retried)
    assert finished["status"] == "completed"
    finished.update(status="importing", owner_pid=-1)
    atomic_json(workspace._path("jobs", job["id"]), finished)
    restarted = Workspace(workspace.default_root)
    try:
        interrupted = restarted.record("jobs", job["id"], project)
        assert interrupted["status"] == "interrupted"
        assert interrupted["results"] == finished["results"]
        assert len(client.get("/runs", headers={"x-agr-project": project}).json()) == 2
    finally:
        restarted.executor.shutdown(wait=True)


def test_commit_rechecks_identity_after_preview(client, project):
    first = doc()
    later = copy.deepcopy(first)
    later["run"]["task_id"] = "another-task"
    first_preview = preview(client, project, "atif", {"first.json": first}).json()
    later_preview = preview(client, project, "atif", {"later.json": later}).json()
    finish(client, project, submit(client, project, first_preview).json())
    job = finish(client, project, submit(client, project, later_preview, key="second-request").json())
    assert job["status"] == "failed"
    run_id = first["run"]["logical_run_id"]
    assert len(client.app.state.workspace.store(project).read_index(run_id)) == 1


def test_saved_snapshot_lists_siblings_and_navigates_next(client, project):
    failed = doc()
    passed = doc()
    failed["run"]["logical_run_id"] = "team/failure"
    passed["run"]["logical_run_id"] = "team/pass"
    failed["verifier"]["checks"][0]["status"] = "failed"
    manifest = preview(client, project, "atif", {"failed.json": failed, "passed.json": passed}).json()
    job = finish(client, project, submit(client, project, manifest).json())
    capture = next(r["capture_id"] for r in job["results"] if r["run_id"] == "team/failure")
    failed["run"]["model"] = "different-model"
    newer = preview(client, project, "atif", {"new.json": failed}).json()
    finish(client, project, submit(client, project, newer, key="newer").json())
    headers = {"x-agr-project": project, "x-agr-capture": capture}
    root = "/runs/team%2Ffailure"
    assert client.get(root, headers=headers).json()["capture"]["capture_id"] == capture
    divergence = client.get(root + "/divergence", headers=headers)
    assert divergence.status_code == 200, divergence.text
    following = client.get(root + "/next", headers=headers)
    assert following.status_code == 200, following.text


@pytest.mark.parametrize("base_url", ["http://192.0.2.10:8000", "https://review.example"])
def test_remote_server_boot_and_legacy_mutations(client, project, base_url):
    manifest = preview(client, project, "atif", {"run.json": doc()}).json()
    result = finish(client, project, submit(client, project, manifest).json())["results"][0]
    headers = {"x-agr-project": project, "origin": base_url, "authorization": "Bearer " + client.app.state.workspace_session_token}
    with TestClient(client.app, base_url=base_url) as remote:
        for route in ("/workspace", "/sources/catalog", f"/projects/{project}", "/queue"):
            assert remote.get(route, headers=headers).status_code == 200, route
        run = "/runs/" + result["run_id"]
        base_version = remote.get(run, headers=headers).json()["workflow"]["workflow_version"]
        workflow = remote.post(run + "/workflow", headers=headers, json={"progress": "in_progress", "base_version": base_version})
        assert workflow.status_code == 200, workflow.text
        feedback = remote.post(run + "/feedback", headers=headers, json={"mutation_id": "remote-note", "moment_id": "note", "kind": "agree"})
        assert feedback.status_code == 200, feedback.text
        # Route validation, rather than the new access gate, handles missing fields.
        assert remote.post(run + "/lessons", headers=headers, json={}).status_code != 403


@pytest.mark.parametrize("wrapper", ["job", "export/wrapper/job"])
def test_zipped_harbor_jobs_are_discovered(client, project, wrapper):
    fixture = ROOT / "archive" / "synthetic" / "fixtures" / "harbor_trial"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(wrapper + "/result.json", '{"stats": {"trials": 1}}')
        for path in fixture.rglob("*"):
            if path.is_file():
                archive.writestr(wrapper + "/trial/" + path.relative_to(fixture).as_posix(), path.read_bytes())
    response = preview(client, project, "harbor", {"job.zip": buffer.getvalue()})
    assert response.status_code == 200, response.text
    manifest = response.json()
    assert len([i for i in manifest["items"] if i["status"] != "invalid"]) == 1, manifest
    assert any("result.json" in f["file"] for f in manifest["ignored"])
    assert finish(client, project, submit(client, project, manifest).json())["status"] == "completed"


def test_pid_reused_by_restarted_server_is_interrupted(client, project):
    import os
    workspace = client.app.state.workspace
    manifest = preview(client, project, "atif", {"run.json": doc()}).json()
    finished = finish(client, project, submit(client, project, manifest).json())
    finished.update(status="importing", owner_pid=os.getpid(), owner_token="a-previous-server-incarnation")
    atomic_json(workspace._path("jobs", finished["id"]), finished)
    restarted = Workspace(workspace.default_root)
    try:
        interrupted = restarted.record("jobs", finished["id"], project)
        assert interrupted["status"] == "interrupted"
        assert interrupted["results"] == finished["results"]
        retried = restarted.retry(project, finished["id"])
        restarted.futures[finished["id"]].result(timeout=10)
        assert restarted.record("jobs", retried["id"], project)["status"] == "completed"
    finally:
        restarted.executor.shutdown(wait=True)


@pytest.mark.parametrize("problem", ["redirect", "limit"])
def test_langfuse_get_preserves_specific_errors(monkeypatch, problem):
    from agr import langfuse_v2
    class Response:
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
        def read(self, limit):
            return b"too large"
    class Opener:
        def open(self, request, timeout):
            if problem == "redirect":
                return langfuse_v2._NoRedirect().redirect_request(request, None, 302, "Found", {}, "https://other.test")
            return Response()
    monkeypatch.setattr(langfuse_v2.urllib.request, "build_opener", lambda *args: Opener())
    monkeypatch.setattr(langfuse_v2, "MAX_FILE", 4)
    with pytest.raises(ImportProblem) as failure:
        langfuse_v2._get("https://example.test/api/public/v2/observations", "Basic test")
    assert failure.value.code == ("source_redirect" if problem == "redirect" else "import_limit")
    assert failure.value.status == (502 if problem == "redirect" else 413)


def test_multitrace_sidecar_retains_specific_error(client, project):
    capture = json.loads((ROOT / "tests" / "fixtures" / "otel" / "otel_toy_run.json").read_text())
    second = copy.deepcopy(capture)
    for resource in second["resourceSpans"]:
        for scope in resource["scopeSpans"]:
            for span in scope["spans"]:
                span["traceId"] = "b" * 32
    capture["resourceSpans"].extend(second["resourceSpans"])
    response = preview(client, project, "otel", {"traces.json": capture, "evaluation.json": doc()["verifier"]}, options={"verifier_file": "evaluation.json"})
    assert response.status_code == 422, response.text
    assert response.json()["detail"]["code"] == "invalid_verifier"
    assert "multiple traces" in response.json()["detail"]["message"]


@pytest.mark.parametrize("fails", [False, True])
def test_disconnect_during_fetch_cannot_recreate_connection(client, project, monkeypatch, fails):
    from concurrent.futures import ThreadPoolExecutor
    import threading
    from agr import langfuse_v2
    workspace = client.app.state.workspace
    connection = workspace.save_connection(project, {"host": "https://example.test", "public_key": "pk", "secret_key": "sk"})
    started, released = threading.Event(), threading.Event()
    def fetch(*args, **kwargs):
        started.set()
        assert released.wait(10)
        if fails:
            raise ImportProblem("source_unreachable", "The source is unavailable", 502)
        return json.loads((ROOT / "tests" / "fixtures" / "langfuse" / "langfuse_toy_run.json").read_text())
    monkeypatch.setattr(langfuse_v2, "fetch_trace", fetch)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(workspace.fetch_connection, project, connection["id"], "trace")
        try:
            assert started.wait(10)
            workspace.disconnect(project, connection["id"])
        finally:
            released.set()
        with pytest.raises(ImportProblem):
            future.result(timeout=10)
    assert workspace.records("connections", project) == []
    assert connection["id"] not in workspace.secrets


def test_cli_capture_published_during_analysis_is_not_lost(client, project, monkeypatch, tmp_path):
    from agr import pipeline
    store = client.app.state.workspace.store(project)
    initial = doc()
    manifest = preview(client, project, "atif", {"first.json": initial}).json()
    finish(client, project, submit(client, project, manifest).json())
    browser, cli = copy.deepcopy(initial), copy.deepcopy(initial)
    browser["run"]["model"], cli["run"]["model"] = "browser-model", "cli-model"
    real_analyze = pipeline.analyze
    cli_result = None
    def analyze(document, isolated):
        nonlocal cli_result
        # This runs after publish_analysis copied the old index into scratch.
        cli_result = real_analyze(cli, Store(store.root))
        return real_analyze(document, isolated)
    monkeypatch.setattr(pipeline, "analyze", analyze)
    result = imports.publish_analysis(browser, store, tmp_path / "scratch")
    index = store.read_index(initial["run"]["logical_run_id"])
    assert len(index) == 3
    assert [e["capture_revision"] for e in index] == [1, 2, 3]
    assert index[-1]["supersedes_source_capture_id"] == cli_result.run_source.source_capture_id
    assert store.read_derived(result["run_id"], result["capture_id"], "run_source.json")["capture_revision"] == 3


def test_investigation_saves_displayed_capture_after_new_import(client, project):
    document = doc("stuck_retry")
    manifest = preview(client, project, "atif", {"first.json": document}).json()
    first = finish(client, project, submit(client, project, manifest).json())["results"][0]
    headers = {"x-agr-project": project}
    review = client.get("/runs/" + first["run_id"], headers=headers).json()
    moment = None
    document["run"]["model"] = "newer-model"
    newer = preview(client, project, "atif", {"new.json": document}).json()
    finish(client, project, submit(client, project, newer, key="newer").json())
    payload = {"run_id": first["run_id"], "capture_id": first["capture_id"], "moment_id": moment, "title": "Original finding", "action": "Investigate this behavior"}
    saved = client.post(f"/projects/{project}/investigations", json=payload)
    assert saved.status_code == 200, saved.text
    assert saved.json()["capture_id"] == first["capture_id"]
    invalid = client.post(f"/projects/{project}/investigations", json={**payload, "capture_id": "nonexistent"})
    assert invalid.status_code == 404
    missing = client.post(f"/projects/{project}/investigations", json={k: v for k, v in payload.items() if k != "capture_id"})
    assert missing.status_code == 422


def test_langfuse_scores_created_after_trace_window_are_retained(monkeypatch):
    from agr import langfuse_v2
    from urllib.parse import urlsplit, parse_qs
    calls = []
    def get(url, authorization):
        params = parse_qs(urlsplit(url).query)
        calls.append(params)
        if "/scores" in url:
            # Model the API's filtering by the score's own timestamp.
            scores = [] if "toTimestamp" in params else [{"id": "later-score", "name": "correctness", "dataType": "CATEGORICAL", "value": "pass", "timestamp": "2026-10-05T00:00:00Z", "subject": {"kind": "trace", "id": "trace"}}]
            return {"data": scores, "meta": {"cursor": None}}
        return {"data": [{"id": "root", "traceId": "trace", "type": "AGENT", "isRootObservation": True, "startTime": "2026-10-01T00:00:00Z", "input": "task", "output": "done"}], "meta": {"cursor": None}}
    monkeypatch.setattr(langfuse_v2, "_get", get)
    result = langfuse_v2.fetch_trace("trace", host="https://example.test", public_key="pk", secret_key="sk", from_time="2026-10-01T00:00:00Z", to_time="2026-10-02T00:00:00Z")
    assert result["scores"][0]["id"] == "later-score"
    assert "fromStartTime" in calls[0] and "toStartTime" in calls[0]
    assert "fromTimestamp" not in calls[1] and "toTimestamp" not in calls[1]


def test_browser_origin_survives_proxy_host_rewrite(client):
    headers = {"origin": "https://review.example", "sec-fetch-site": "same-origin"}
    response = client.post("/projects", headers=headers, json={"name": "Behind a proxy"})
    assert response.status_code == 200, response.text
    assert client.patch("/workspace", headers=headers, json={"reviewer": "Proxy reviewer"}).status_code == 200
    cross_site = {"origin": "https://other.example", "sec-fetch-site": "cross-site"}
    assert client.post("/projects", headers=cross_site, json={"name": "Rejected"}).status_code == 403


@pytest.mark.parametrize("host", ["localhost:8000", "192.0.2.10:8000", "review.example"])
def test_remote_requests_without_origin_require_session(client, host):
    with TestClient(client.app, base_url="http://" + host, client=("192.0.2.99", 1234)) as remote:
        for path in ("/workspace", "/runs", "/projects/existing/connections"):
            response = remote.post(path, json={"host": "https://attacker.test"}) if path.endswith("connections") else remote.get(path)
            assert response.status_code == 401, response.text
        authorized = {"authorization": "Bearer " + client.app.state.workspace_session_token}
        assert remote.get("/workspace", headers=authorized).status_code == 200
        assert remote.patch("/workspace", headers=authorized, json={"reviewer": "Remote reviewer"}).status_code == 200


def test_rebinding_host_rejected_even_with_matching_origin_and_session(client):
    headers = {"host": "evil.example:8000", "origin": "http://evil.example:8000", "sec-fetch-site": "same-origin", "authorization": "Bearer " + client.app.state.workspace_session_token}
    assert client.post("/projects/existing/connections", headers=headers, json={"host": "https://attacker.test"}).status_code == 403
    assert client.get("/workspace", headers=headers).status_code == 403
    for host in ("localhost/path", "localhost:bad", "user@localhost", "localhost#fragment"):
        assert client.get("/workspace", headers={"host": host}).status_code == 403


def test_proxy_session_required_even_when_upstream_is_loopback(tmp_path):
    app = create_app(str(tmp_path / "store"), allowed_hosts=["review.example"], session_token="test-session")
    try:
        with TestClient(app, base_url="http://localhost", client=("127.0.0.1", 1234)) as proxy:
            assert proxy.get("/").status_code == 200
            assert proxy.get("/workspace").status_code == 401
            headers = {"authorization": "Bearer test-session", "origin": "https://review.example", "sec-fetch-site": "same-origin"}
            assert proxy.post("/projects", headers=headers, json={"name": "Proxy session"}).status_code == 200
            assert proxy.get("/workspace", headers={"authorization": "Bearer wrong"}).status_code == 401
    finally:
        app.state.workspace.executor.shutdown(wait=True)


def test_environment_credentials_cannot_be_sent_to_other_hosts(client, monkeypatch):
    from agr import langfuse_v2
    monkeypatch.setenv("LANGFUSE_HOST", "https://langfuse.example/base")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "environment-public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "environment-secret")
    calls = []
    capture = json.loads((ROOT / "tests" / "fixtures" / "langfuse" / "langfuse_toy_run.json").read_text())
    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return capture
    monkeypatch.setattr(langfuse_v2, "fetch_trace", fetch)
    workspace = client.app.state.workspace
    for host in ("https://attacker.test", "http://langfuse.example/base", "https://langfuse.example:444/base", "https://langfuse.example/other"):
        response = client.post("/projects/existing/connections", json={"host": host})
        assert response.status_code == 422
        assert response.json()["detail"]["code"] == "missing_credentials"
    configured = workspace.save_connection("existing", {"host": "https://LANGFUSE.example:443/base/"})
    assert workspace.secrets[configured["id"]] == ("environment-public", "environment-secret")
    arbitrary = workspace.save_connection("existing", {"host": "https://attacker.test", "public_key": "explicit-pk", "secret_key": "explicit-sk"})
    workspace.secrets.clear()  # a new server/session must also check host binding
    with pytest.raises(ImportProblem, match="Session credentials expired"):
        workspace.fetch_connection("existing", arbitrary["id"], "trace")
    assert calls == []
    assert workspace.environment_keys(configured["host"]) == ("environment-public", "environment-secret")
    assert workspace.environment_keys("http://localhost:3000") == (None, None)
    restarted = Workspace(workspace.default_root)
    try:
        result = restarted.fetch_connection("existing", configured["id"], capture["id"])
        assert result["items"]
        assert calls[0]["host"] == configured["host"]
        assert calls[0]["public_key"] == "environment-public" and calls[0]["secret_key"] == "environment-secret"
    finally:
        restarted.executor.shutdown(wait=True)
    monkeypatch.delenv("LANGFUSE_HOST")
    assert workspace.environment_keys("https://cloud.langfuse.com") == (None, None)


@pytest.mark.parametrize("host_env", [None, ""])
def test_environment_keys_use_legacy_default_host_after_restart(client, monkeypatch, host_env):
    from agr import langfuse_api, langfuse_v2
    if host_env is None:
        monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    else:
        monkeypatch.setenv("LANGFUSE_HOST", host_env)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "default-public")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "default-secret")
    connection = client.post("/projects/existing/connections", json={"host": langfuse_api._DEFAULT_HOST + "/"})
    assert connection.status_code == 200, connection.text
    workspace = client.app.state.workspace
    assert workspace.environment_keys("http://LOCALHOST:3000/") == ("default-public", "default-secret")
    for other in ("https://attacker.example", "http://127.0.0.1:3000", "https://localhost:3000", "http://localhost:3001", "http://localhost:3000/other"):
        assert workspace.environment_keys(other) == (None, None)
        assert client.post("/projects/existing/connections", json={"host": other}).status_code == 422
    calls = []
    capture = json.loads((ROOT / "tests" / "fixtures" / "langfuse" / "langfuse_toy_run.json").read_text())
    def fetch(*args, **kwargs):
        calls.append(kwargs)
        return capture
    monkeypatch.setattr(langfuse_v2, "fetch_trace", fetch)
    restarted = Workspace(workspace.default_root)
    try:
        assert not restarted.secrets
        result = restarted.fetch_connection("existing", connection.json()["id"], capture["id"], "2026-09-01T00:00:00Z", "2026-09-02T00:00:00Z")
        assert result["items"]
        assert calls[0]["host"].rstrip("/") == langfuse_api._DEFAULT_HOST
        assert calls[0]["public_key"] == "default-public" and calls[0]["secret_key"] == "default-secret"
    finally:
        restarted.executor.shutdown(wait=True)


def test_snapshot_invalid_run_id_is_404(client):
    for path in ("/runs/..%2Fx", "/runs/..%2Fx/next", "/runs/..%2Fx/divergence"):
        assert client.get(path, headers={"x-agr-capture": "saved"}).status_code == 404


def test_snapshot_list_reuses_base_cache_and_reads_only_pinned_run(client, project, monkeypatch):
    from agr import read
    from agr.capture_view import PinnedStore
    manifest = preview(client, project, "atif", {"one.json": doc(), "two.json": doc("stuck_retry")}).json()
    first = finish(client, project, submit(client, project, manifest).json())["results"][0]
    newer = doc()
    newer["run"]["model"] = "newer"
    updated = preview(client, project, "atif", {"newer.json": newer}).json()
    finish(client, project, submit(client, project, updated, key="newer").json())
    store = client.app.state.workspace.store(project)
    calls = []
    real = read._list_runs_uncached
    def counted(store, run_ids=None):
        calls.append(run_ids)
        return real(store, run_ids=run_ids)
    monkeypatch.setattr(read, "_list_runs_uncached", counted)
    latest = read.list_runs(store)
    for _ in range(3):
        rows = read.list_runs(PinnedStore(store, first["run_id"], first["capture_id"]))
        assert next(r for r in rows if r["run_id"] == first["run_id"])["capture_id"] == first["capture_id"]
    assert calls == [None] + [[first["run_id"]]] * 3
    assert next(r for r in latest if r["run_id"] == first["run_id"])["capture_id"] != first["capture_id"]


def test_snapshot_includes_new_cli_run_inside_list_cache_ttl(tmp_path):
    from agr import read
    from agr.capture_view import PinnedStore
    from agr.pipeline import analyze
    store = Store(str(tmp_path / "store"))
    assert read.list_runs(store) == []
    # A different Store simulates CLI writes unseen by this instance's counter.
    analyzed = analyze(doc(), Store(store.root))
    rs = analyzed.run_source
    rows = read.list_runs(PinnedStore(store, rs.run_id, rs.source_capture_id))
    assert [row["run_id"] for row in rows] == [rs.run_id]


def test_cancel_and_project_switch_do_not_wait_for_analysis(client, project, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    from agr import workspace as module
    entered, release = threading.Event(), threading.Event()
    real = module.publish_analysis
    count = []
    def blocked(*args):
        count.append(1)
        entered.set()
        assert release.wait(10)
        return real(*args)
    monkeypatch.setattr(module, "publish_analysis", blocked)
    manifest = preview(client, project, "atif", {"one.json": doc(), "two.json": doc("stuck_retry")}).json()
    workspace = client.app.state.workspace
    job = submit(client, project, manifest).json()
    try:
        assert entered.wait(5)
        with ThreadPoolExecutor() as pool:
            cancelled = pool.submit(workspace.cancel, project, job["id"])
            switched = pool.submit(workspace.update_settings, {"last_project": "existing"})
            try:
                assert cancelled.result(timeout=2)["status"] == "cancelling"
                assert switched.result(timeout=2)["last_project"] == "existing"
            finally:
                release.set()
        finished = finish(client, project, job)
        assert finished["status"] == "cancelled"
        assert len(finished["results"]) == 1 and len(count) == 1
    finally:
        release.set()


@pytest.mark.parametrize("saved,current,expected", [(None, "birth", True), ("birth", None, True), ("birth", "birth", True), ("old", "new", False)])
def test_foreign_owner_identity_is_conservative_when_unavailable(monkeypatch, saved, current, expected):
    from agr import workspace
    monkeypatch.setattr(workspace, "process_running", lambda pid: True)
    monkeypatch.setattr(workspace, "process_identity", lambda pid: current)
    assert workspace.owner_running({"owner_pid": 987654321, "owner_identity": saved}) is expected


def test_second_server_keeps_live_job_without_saved_identity(client, project, monkeypatch):
    from agr import workspace as module
    workspace = client.app.state.workspace
    monkeypatch.setattr(workspace.executor, "submit", lambda *args: None)
    manifest = preview(client, project, "atif", {"run.json": doc()}).json()
    job = submit(client, project, manifest).json()
    job.update(status="importing", owner_pid=987654321, owner_identity=None)
    atomic_json(workspace._path("jobs", job["id"]), job)
    monkeypatch.setattr(module, "process_running", lambda pid: True)
    monkeypatch.setattr(module, "process_identity", lambda pid: "available-now")
    second = Workspace(workspace.default_root)
    try:
        assert second.record("jobs", job["id"], project)["status"] == "importing"
    finally:
        second.executor.shutdown(wait=True)


def test_existing_store_cache_is_shared_by_scoped_and_legacy_routes(client):
    assert client.get("/runs").json() == []
    manifest = preview(client, "existing", "atif", {"run.json": doc()}).json()
    imported = finish(client, "existing", submit(client, "existing", manifest).json())
    assert len(client.get("/runs").json()) == 1
    client.patch("/workspace", json={"reviewer": "Existing project reviewer"})
    route = "/runs/" + imported["results"][0]["run_id"] + "/feedback"
    scoped = client.post(route, headers={"x-agr-project": "existing"}, json={"mutation_id": "scoped", "moment_id": "note", "kind": "agree", "actor": "spoofed"})
    assert scoped.status_code == 200, scoped.text
    assert scoped.json()["feedback"]["actor"] == "Existing project reviewer"
    legacy = client.post(route, json={"mutation_id": "legacy", "moment_id": "note", "kind": "agree", "actor": "Legacy reviewer"})
    assert legacy.status_code == 200, legacy.text
    assert legacy.json()["feedback"]["actor"] == "Legacy reviewer"


def test_index_lock_serializes_a_second_process(tmp_path):
    import subprocess
    import sys
    store = Store(str(tmp_path / "store"))
    script = "\n".join([
        "import sys",
        "from agr.store import Store",
        "store = Store(sys.argv[1])",
        "print('ready', flush=True)",
        "store.register_capture('run', 'capture-second', 'sha256:second', 'test', 'complete')",
        "print('done', flush=True)",
    ])
    child = None
    try:
        with store.index_lock("run"):
            store.register_capture("run", "capture-first", "sha256:first", "test", "complete")
            child = subprocess.Popen([sys.executable, "-c", script, store.root], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            assert child.stdout.readline().strip() == "ready"
            with pytest.raises(subprocess.TimeoutExpired):
                child.communicate(timeout=0.2)
        stdout, stderr = child.communicate(timeout=10)
        assert child.returncode == 0, stderr
        assert "done" in stdout
        assert [e["capture_revision"] for e in store.read_index("run")] == [1, 2]
    finally:
        if child is not None and child.poll() is None:
            child.kill()
            child.wait(timeout=10)


def test_json_io_retries_transient_windows_sharing_errors(tmp_path, monkeypatch):
    from agr import storage_io
    path = tmp_path / "record.json"
    atomic_json(path, {"status": "queued"})
    real_read, real_replace = Path.read_text, storage_io.os.replace
    read_attempts, write_attempts = 0, 0
    def conflict():
        error = PermissionError(13, "Sharing violation")
        error.winerror = 32
        return error
    def read(target, *args, **kwargs):
        nonlocal read_attempts
        read_attempts += 1
        if read_attempts == 1:
            raise conflict()
        return real_read(target, *args, **kwargs)
    def replace(source, target):
        nonlocal write_attempts
        write_attempts += 1
        if write_attempts == 1:
            raise conflict()
        return real_replace(source, target)
    monkeypatch.setattr(Path, "read_text", read)
    monkeypatch.setattr(storage_io.os, "replace", replace)
    assert storage_io.read_json(path) == {"status": "queued"}
    storage_io.atomic_json(path, {"status": "completed"})
    assert storage_io.read_json(path) == {"status": "completed"}
    assert read_attempts == 3 and write_attempts == 2
