"""Offline AI setup/review journeys, persisted history and workspace boundaries."""
import json
import threading
import time
from pathlib import Path

import pytest
pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from agr.api import create_app
from agr import model_reviewer as models
from agr.ai_review import AIReviewService
from agr.pipeline import analyze
from agr.userconfig import (ConfigError, effective_review_config, load_config,
                            resolve_review_settings, save_config, reviewer_key)
from agr.storage_io import atomic_json

ROOT = Path(__file__).resolve().parents[1]
KEY = "session-test-key-never-persist"


@pytest.fixture
def environment(tmp_path, monkeypatch):
    for key in ("AGR_REVIEW_PROVIDER", "AGR_REVIEW_MODEL", "OPENAI_BASE_URL", "ANTHROPIC_BASE_URL",
                "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "AGR_REVIEW_COST_BUDGET_USD",
                "AGR_REVIEW_TIME_BUDGET_S", "AGR_REVIEW_REQUEST_TIMEOUT_S", "AGR_READ_ONLY"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("AGR_CONFIG", str(tmp_path / "settings.json"))
    monkeypatch.setattr("agr.cli._load_env", lambda *_: None)
    return tmp_path


@pytest.fixture
def offline(monkeypatch):
    control = {"calls": [], "fail": False, "block": None, "started": threading.Event(), "response": {"moments": []}}
    class OfflineReviewer(models._LazyModelReviewer):
        provider = "openai"
        def _complete(self, system, user_json):
            control["calls"].append((self.model, json.loads(user_json)))
            control["started"].set()
            if control["block"]:
                assert control["block"].wait(8), "test provider was not released"
            if control["fail"] or len(control["calls"]) in control.get("fail_on_calls", set()):
                raise ConnectionError("SDK error echoed " + KEY)
            return control["response"]
    def factory(provider, model, base_url=None, **kwargs):
        assert provider == "openai"
        return OfflineReviewer(model, base_url, **kwargs)
    monkeypatch.setattr(models, "make_reviewer", factory)
    return control


@pytest.fixture
def client(environment, offline):
    app = create_app(str(environment / "store"))
    with TestClient(app) as client:
        yield client
    app.state.workspace.executor.shutdown(wait=True)
    app.state.workspace.review_executor.shutdown(wait=True)


def configure(client, model="test-model", endpoint="https://model.invalid/v1"):
    response = client.patch("/workspace/ai-review", json={
        "provider": "openai", "model": model, "base_url": endpoint, "api_key": KEY,
        "cost_budget_usd": 0, "time_budget_s": 0, "request_timeout_s": 10})
    assert response.status_code == 200, response.text
    return response.json()


def runs(client, count=2, project="existing"):
    store = client.app.state.workspace.store(project)
    result = []
    for index in range(count):
        doc = json.loads((ROOT / "agr/demo_fixtures/clean_pass.atif.json").read_text())
        doc["run"]["logical_run_id"] = "ai-run-" + str(index)
        analyze(doc, store)
        result.append(doc["run"]["logical_run_id"])
    return result


def plan(client, run_ids, project="existing"):
    response = client.post(f"/projects/{project}/ai-reviews/plan", json={"run_ids": run_ids})
    assert response.status_code == 200, response.text
    return response.json()


def submit(client, run_ids, project="existing", key="first", force=False):
    preview = plan(client, run_ids, project)
    payload = {"run_ids": run_ids, "configuration_id": preview["settings"]["configuration_id"],
               "plan_token": preview["plan_token"], "idempotency_key": key, "force": force}
    response = client.post(f"/projects/{project}/ai-reviews", json=payload)
    assert response.status_code == 200, response.text
    return response.json(), payload


def finish(client, job, project="existing"):
    for _ in range(400):
        response = client.get(f"/projects/{project}/ai-reviews/{job['id']}")
        assert response.status_code == 200, response.text
        job = response.json()
        if job["status"] not in ("queued", "running", "cancelling"):
            return job
        time.sleep(0.01)
    pytest.fail("review did not finish")


def test_provider_switch_and_one_off_override_discard_dependent_fields(environment):
    save_config("openai", "old-model", "https://old.invalid/v1")
    settings = resolve_review_settings(provider="anthropic")
    assert settings["model"] == ""
    assert settings["base_url"] == "https://api.anthropic.com"
    save_config(provider="anthropic")
    assert "model" not in load_config() and "base_url" not in load_config()


def test_effective_origins_environment_and_reset(environment, monkeypatch):
    save_config("openai", "saved-model", "https://saved.invalid/v1", cost_budget_usd=0)
    monkeypatch.setenv("OPENAI_BASE_URL", "https://env.invalid/v1")
    settings = effective_review_config()
    assert settings["base_url"] == "https://env.invalid/v1"
    assert settings["origins"]["base_url"] == "OPENAI_BASE_URL"
    assert settings["cost_budget_usd"] == 0
    assert effective_review_config(base_url="")["base_url"] == "https://api.openai.com/v1"


@pytest.mark.parametrize("changes", [{"cost_budget_usd": -1}, {"time_budget_s": float("inf")},
    {"request_timeout_s": 0}, {"model": []}, {"provider": "unknown"},
    {"base_url": "https://user:secret@host/v1"}, {"base_url": "https://host/v1?token=secret"}])
def test_invalid_settings_rejected_without_file_writes(environment, changes):
    with pytest.raises(ConfigError):
        save_config(**changes)
    assert not (environment / "settings.json").exists()


def test_corrupt_config_requires_repair_and_can_be_reset(client, environment):
    (environment / "settings.json").write_text("{broken")
    response = client.get("/workspace/ai-review")
    assert response.status_code == 422 and response.json()["detail"]["code"] == "invalid_configuration"
    assert client.delete("/workspace/ai-review").status_code == 200


def test_settings_save_no_calls_and_credentials_only_in_memory(client, offline, environment):
    settings = configure(client)
    assert offline["calls"] == []
    assert settings["credentials"] == "session"
    assert KEY not in json.dumps(settings)
    assert KEY not in (environment / "settings.json").read_text()
    assert client.delete("/workspace/ai-review/credentials").json()["credentials"] == "missing"


def test_connection_uses_valid_synthetic_json_and_does_not_save_draft(client, offline):
    configure(client)
    response = client.post("/workspace/ai-review/test", json={"model": "unsaved-model", "api_key": KEY})
    assert response.json()["status"] == "verified", response.text
    assert offline["calls"][-1][1] == {"self_test": True, "expected": {"moments": []}}
    assert load_config()["model"] == "test-model"
    offline["response"] = {"ok": True}
    assert client.post("/workspace/ai-review/test", json={}).json()["status"] == "failed"


def test_cli_failed_test_preserves_active_settings(environment, offline):
    from agr.cli import main
    save_config("openai", "working-model", "https://model.invalid/v1")
    offline["fail"] = True
    assert main(["config", "--model", "broken-model", "--test"]) == 4
    assert load_config()["model"] == "working-model"


def test_configuration_identity_distinguishes_endpoint(environment):
    a = models.OpenAIReviewer("same-model", "https://a.invalid/v1")
    b = models.OpenAIReviewer("same-model", "https://b.invalid/v1")
    assert a.reviewer_key != b.reviewer_key
    assert a.reviewer_key == reviewer_key({"provider": "openai", "model": "same-model", "base_url": "https://a.invalid/v1"})


def test_batch_review_history_skip_selected_model_and_idempotency(client, offline):
    configure(client)
    selected = runs(client)
    first, payload = submit(client, selected)
    assert client.post("/projects/existing/ai-reviews", json=payload).json()["id"] == first["id"]
    complete = finish(client, first)
    assert complete["status"] == "completed", complete
    assert [r["status"] for r in complete["results"]] == ["completed", "completed"]
    previous_calls = len(offline["calls"])
    second, _ = submit(client, selected, key="second")
    assert all(r["status"] == "skipped" for r in finish(client, second)["results"])
    assert len(offline["calls"]) == previous_calls
    configure(client, model="other-model")
    assert plan(client, selected)["already_reviewed"] == 0
    third, _ = submit(client, selected, key="third")
    assert finish(client, third)["status"] == "completed"
    store = client.app.state.workspace.store("existing")
    slots = store.list_reviews(selected[0], store.latest_capture_id(selected[0]))
    assert "deterministic" in slots
    assert len([key for key in slots if key.startswith("model:")]) == 2
    assert len(client.get("/projects/existing/ai-reviews").json()["jobs"]) == 3


def test_job_and_plan_project_isolation_and_snapshot_boundary(client):
    configure(client)
    selected = runs(client, 1)
    other = client.post("/projects", json={"name": "Other"}).json()["id"]
    assert client.post(f"/projects/{other}/ai-reviews/plan", json={"run_ids": selected}).status_code == 404
    job, _ = submit(client, selected)
    finish(client, job)
    assert client.get(f"/projects/{other}/ai-reviews/{job['id']}").status_code == 404
    assert client.get(f"/projects/{other}/ai-reviews").json()["jobs"] == []
    assert client.post("/projects/existing/ai-reviews/plan", json={"run_ids": selected}, headers={"x-agr-capture": "old"}).status_code == 403


def test_changed_configuration_or_budget_invalidates_plan(client):
    configure(client)
    selected = runs(client, 1)
    preview = plan(client, selected)
    payload = {"run_ids": selected, "configuration_id": preview["settings"]["configuration_id"],
               "plan_token": preview["plan_token"], "idempotency_key": "changed"}
    client.patch("/workspace/ai-review", json={"cost_budget_usd": 1})
    assert client.post("/projects/existing/ai-reviews", json=payload).status_code == 409
    configure(client, model="new-model")
    response = client.post("/projects/existing/ai-reviews", json=payload)
    assert response.status_code == 409 and response.json()["detail"]["code"] == "configuration_changed"


def test_failure_retry_keeps_completed_work_and_secrets_out_of_files(client, offline, environment):
    configure(client)
    selected = runs(client, 1)
    offline["fail"] = True
    job, _ = submit(client, selected)
    failed = finish(client, job)
    assert failed["status"] == "failed", failed
    assert KEY not in json.dumps(failed)
    assert all(KEY not in path.read_text() for path in environment.rglob("*.json"))
    offline["fail"] = False
    response = client.post(f"/projects/existing/ai-reviews/{job['id']}/retry")
    assert response.status_code == 200
    assert finish(client, response.json())["status"] == "completed"
    assert client.post(f"/projects/existing/ai-reviews/{job['id']}/retry").status_code == 409


def test_cancel_between_runs_and_retry_unfinished_only(client, offline):
    configure(client)
    selected = runs(client)
    offline["block"] = threading.Event()
    job, _ = submit(client, selected)
    assert offline["started"].wait(3)
    response = client.post(f"/projects/existing/ai-reviews/{job['id']}/cancel")
    assert response.json()["status"] == "cancelling"
    offline["block"].set()
    cancelled = finish(client, job)
    assert cancelled["status"] == "cancelled" and len(cancelled["results"]) == 1
    calls = len(offline["calls"])
    response = client.post(f"/projects/existing/ai-reviews/{job['id']}/retry")
    completed = finish(client, response.json())
    assert completed["status"] == "completed" and len(completed["results"]) == 2
    assert len(offline["calls"]) == calls + 1


def test_interrupted_job_is_recovered_without_automatic_spend(client, offline):
    configure(client)
    job, _ = submit(client, runs(client, 1))
    complete = finish(client, job)
    workspace = client.app.state.workspace
    complete.update(status="running", owner_token="old-process")
    atomic_json(workspace._path("reviews", job["id"]), complete)
    calls = len(offline["calls"])
    AIReviewService(workspace)
    recovered = client.get(f"/projects/existing/ai-reviews/{job['id']}").json()
    assert recovered["status"] == "interrupted"
    assert len(offline["calls"]) == calls


def test_read_only_origin_and_remote_access_boundaries(environment, offline):
    app = create_app(str(environment / "readonly"), read_only=True)
    with TestClient(app) as client:
        assert client.get("/workspace/ai-review").json() == {"read_only": True}
        save_config("openai", "preserved")
        assert client.delete("/workspace/ai-review").status_code == 403
        assert load_config()["model"] == "preserved"
        assert client.patch("/workspace/ai-review", json={}).status_code == 403
        assert client.post("/workspace/ai-review/test", json={}).status_code == 403
        assert client.post("/projects/existing/ai-reviews", json={}).status_code == 403
    app.state.workspace.executor.shutdown(wait=True)
    app.state.workspace.review_executor.shutdown(wait=True)
    app = create_app(str(environment / "remote"), allowed_hosts=["review.example"])
    with TestClient(app, base_url="http://review.example") as client:
        assert client.get("/workspace/ai-review").status_code == 401
        headers = {"authorization": "Bearer " + app.state.workspace_session_token}
        assert client.get("/workspace/ai-review", headers=headers).status_code == 200
        headers["origin"] = "https://evil.example"
        assert client.patch("/workspace/ai-review", json={}, headers=headers).status_code == 403
    app.state.workspace.executor.shutdown(wait=True)
    app.state.workspace.review_executor.shutdown(wait=True)



def test_mixed_batch_retry_does_not_repeat_successful_paid_work(client, offline):
    configure(client)
    offline["fail_on_calls"] = {2}
    job, _ = submit(client, runs(client))
    failed = finish(client, job)
    assert failed["status"] == "completed_with_errors"
    assert [r["status"] for r in failed["results"]] == ["completed", "failed"]
    offline["fail_on_calls"] = set()
    retried = client.post(f"/projects/existing/ai-reviews/{job['id']}/retry").json()
    complete = finish(client, retried)
    assert complete["status"] == "completed"
    assert len(offline["calls"]) == 3


def test_budget_stop_is_incomplete_and_retry_uses_updated_limits(client, monkeypatch):
    original = models._LazyModelReviewer._check_review_budget
    def elapsed(self, kind):
        if self.time_budget_s:
            self._review_started = time.monotonic() - 1
        return original(self, kind)
    monkeypatch.setattr(models._LazyModelReviewer, "_check_review_budget", elapsed)
    configure(client)
    client.patch("/workspace/ai-review", json={"time_budget_s": 1e-12})
    job, _ = submit(client, runs(client, 1))
    incomplete = finish(client, job)
    assert incomplete["status"] == "failed"
    assert incomplete["results"][0]["status"] == "incomplete"
    assert "elapsed-time target" in incomplete["results"][0]["error"]
    client.patch("/workspace/ai-review", json={"time_budget_s": 0})
    retried = client.post(f"/projects/existing/ai-reviews/{job['id']}/retry").json()
    assert finish(client, retried)["status"] == "completed"


def test_new_capture_invalidates_preview_before_spend(client, offline):
    configure(client)
    selected = runs(client, 1)
    preview = plan(client, selected)
    store = client.app.state.workspace.store("existing")
    document = store.read_source(selected[0], store.latest_capture_id(selected[0]))
    document["task"]["instruction"] += " A new captured instruction."
    analyze(document, store)
    payload = {"run_ids": selected, "configuration_id": preview["settings"]["configuration_id"],
               "plan_token": preview["plan_token"], "idempotency_key": "old-preview"}
    assert client.post("/projects/existing/ai-reviews", json=payload).status_code == 409
    assert offline["calls"] == []


def test_missing_credentials_prevents_job_acceptance(client, offline):
    configure(client)
    selected = runs(client, 1)
    client.delete("/workspace/ai-review/credentials")
    preview = plan(client, selected)
    response = client.post("/projects/existing/ai-reviews", json={
        "run_ids": selected, "configuration_id": preview["settings"]["configuration_id"],
        "plan_token": preview["plan_token"], "idempotency_key": "missing-key"})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "missing_credentials"
    assert offline["calls"] == []


def test_provider_request_options_use_session_key_and_timeout(environment, monkeypatch):
    options = {}
    class SDK:
        @staticmethod
        def OpenAI(**kwargs):
            options.update(kwargs)
            return object()
    monkeypatch.setattr(models.OpenAIReviewer, "_import_sdk", lambda *_: SDK)
    models.OpenAIReviewer("test", "http://localhost:11434/v1", api_key=KEY, request_timeout_s=12)._client()
    assert options == {"base_url": "http://localhost:11434/v1", "api_key": KEY, "timeout": 12}


def test_same_model_other_endpoint_preserves_snapshots_and_provenance(client):
    configure(client, endpoint="https://first.invalid/v1")
    selected = runs(client, 1)
    first, _ = submit(client, selected)
    assert finish(client, first)["status"] == "completed"
    configure(client, endpoint="https://second.invalid/v1")
    second, _ = submit(client, selected, key="other-endpoint")
    assert finish(client, second)["status"] == "completed"
    view = client.get("/runs/" + selected[0]).json()
    assert view["reviewer_key"] == second["reviewer_key"]
    provenance = view["available_review_configurations"]
    assert len(provenance) == 2
    assert {c["base_url"] for c in provenance.values()} == {"https://first.invalid/v1", "https://second.invalid/v1"}
    assert KEY not in json.dumps(view)


def test_failed_connection_test_does_not_replace_working_session_key(client, offline):
    configure(client)
    offline["response"] = {"unexpected": True}
    response = client.post("/workspace/ai-review/test", json={"api_key": "wrong-draft-key"})
    assert response.json()["status"] == "failed"
    settings = effective_review_config()
    service = client.app.state.workspace.ai_reviews
    assert service.keys[settings["configuration_id"]] == KEY


def test_whitespace_and_empty_fields_in_hand_edited_config(environment):
    (environment / "settings.json").write_text(json.dumps({"provider": " openai ", "model": "", "base_url": ""}))
    settings = effective_review_config()
    assert settings["provider"] == "openai"
    assert settings["model"] == "" and settings["origins"]["model"] == "unset"
    assert settings["base_url"] == "https://api.openai.com/v1"


@pytest.mark.parametrize("name,value", [("AGR_REVIEW_MODEL", "env-model"), ("OPENAI_BASE_URL", "https://env.invalid/v1")])
def test_session_key_and_test_follow_effective_environment(client, offline, monkeypatch, name, value):
    monkeypatch.setenv(name, value)
    settings = configure(client)
    assert settings["credentials"] == "session"
    response = client.post("/workspace/ai-review/test", json={
        "provider": "openai", "model": "draft-model", "base_url": "https://draft.invalid/v1", "api_key": KEY})
    assert response.json()["status"] == "verified"
    # The other unsaved draft field must be saved before its test becomes active.
    client.patch("/workspace/ai-review", json={"model": "draft-model", "base_url": "https://draft.invalid/v1"})
    active = client.get("/workspace/ai-review").json()
    assert active["test"]["status"] == "verified"
    assert active["configuration_id"] == response.json()["configuration_id"]
    job, _ = submit(client, runs(client, 1))
    assert finish(client, job)["status"] == "completed"


def test_preview_and_acceptance_do_not_read_source(client, monkeypatch):
    configure(client)
    selected = runs(client, 1)
    store = client.app.state.workspace.store("existing")
    def forbidden(*args):
        pytest.fail("Preview/acceptance must not parse trace source")
    monkeypatch.setattr(store, "read_source", forbidden)
    monkeypatch.setattr(client.app.state.workspace.ai_reviews, "_run", lambda *args: None)
    preview = plan(client, selected)
    job, _ = submit(client, selected)
    assert job["items"] == preview["items"]


def test_review_does_not_block_import_or_overwrite_a_new_capture(client, offline):
    configure(client)
    selected = runs(client, 1)
    workspace = client.app.state.workspace
    store = workspace.store("existing")
    original_capture = store.latest_capture_id(selected[0])
    doc = store.read_source(selected[0], original_capture)
    doc["task"]["instruction"] += " Updated while the reviewer is waiting."
    offline["block"] = threading.Event()
    job, _ = submit(client, selected)
    assert offline["started"].wait(3)
    try:
        imported = workspace.executor.submit(analyze, doc, store).result(timeout=3)
        assert imported.run_source.source_capture_id != original_capture
        assert store.latest_capture_id(selected[0]) == imported.run_source.source_capture_id
    finally:
        offline["block"].set()
    finished = finish(client, job)
    assert finished["status"] == "failed"
    assert finished["results"][0]["code"] == "capture_changed"
    assert job["reviewer_key"] not in store.list_reviews(selected[0], original_capture)
    assert store.list_reviews(selected[0], imported.run_source.source_capture_id) == ["deterministic"]


def test_cli_review_does_not_hold_capture_lock_during_provider_call(client, offline):
    from agr.cli import _review_one
    from agr.pipeline import CaptureChangedError
    configure(client)
    selected = runs(client, 1)
    workspace = client.app.state.workspace
    store = workspace.store("existing")
    doc = store.read_source(selected[0], store.latest_capture_id(selected[0]))
    doc["task"]["instruction"] += " Updated during CLI review."
    offline["block"] = threading.Event()
    reviewer = models.make_reviewer(**{k: effective_review_config()[k] for k in ("provider", "model", "base_url")})
    future = workspace.review_executor.submit(_review_one, store, selected[0], reviewer)
    assert offline["started"].wait(3)
    try:
        workspace.executor.submit(analyze, doc, store).result(timeout=3)
    finally:
        offline["block"].set()
    with pytest.raises(CaptureChangedError):
        future.result(timeout=3)


@pytest.mark.parametrize("provider,model", [("openai", "gpt-4o"), ("anthropic", "claude-opus-4-8")])
def test_legacy_review_matches_official_destination_only(client, provider, model):
    from agr.cli import _already_enriched
    from agr.userconfig import DEFAULT_ENDPOINTS
    selected = runs(client, 1)
    store = client.app.state.workspace.store("existing")
    capture = store.latest_capture_id(selected[0])
    legacy = "model:" + model
    store.write_review(selected[0], capture, legacy, [])
    client.patch("/workspace/ai-review", json={"provider": provider, "model": model,
                 "base_url": DEFAULT_ENDPOINTS[provider], "api_key": KEY})
    official = plan(client, selected)
    assert official["already_reviewed"] == 1
    assert _already_enriched(store, selected[0], official["reviewer_key"])
    client.patch("/workspace/ai-review", json={"base_url": "https://custom.invalid/v1"})
    assert plan(client, selected)["already_reviewed"] == 0
    store.write_derived(selected[0], capture, "review_telemetry.json", {
        "review_configuration": {"provider": provider, "model": model, "base_url": "https://custom.invalid/v1"}})
    assert plan(client, selected)["already_reviewed"] == 1


@pytest.mark.parametrize("reviewer_class,client_name", [(models.OpenAIReviewer, "OpenAI"), (models.AnthropicReviewer, "Anthropic")])
def test_default_request_timeout_allows_long_reviews_and_retains_sdk_retries(environment, monkeypatch, reviewer_class, client_name):
    options = {}
    sdk = type("SDK", (), {client_name: staticmethod(lambda **kwargs: options.update(kwargs))})
    monkeypatch.setattr(reviewer_class, "_import_sdk", lambda *_: sdk)
    reviewer_class("any-model")._client()
    assert options["timeout"] == 600
    assert "max_retries" not in options


def test_failed_forced_rerun_is_not_skipped_on_retry(client, offline):
    configure(client)
    selected = runs(client, 1)
    first, _ = submit(client, selected)
    assert finish(client, first)["status"] == "completed"
    offline["fail"] = True
    second, _ = submit(client, selected, key="forced-failure", force=True)
    assert finish(client, second)["status"] == "failed"
    assert plan(client, selected)["already_reviewed"] == 0
    calls = len(offline["calls"])
    offline["fail"] = False
    retried = client.post(f"/projects/existing/ai-reviews/{second['id']}/retry").json()
    assert finish(client, retried)["results"][0]["status"] == "completed"
    assert len(offline["calls"]) == calls + 1


def test_no_builtin_model_blocks_preview_until_one_is_chosen(client):
    selected = runs(client, 1)
    settings = client.get("/workspace/ai-review").json()
    assert settings["model"] == "" and settings["origins"]["model"] == "unset"
    assert all("model" not in d for d in settings["defaults"].values())
    response = client.post("/projects/existing/ai-reviews/plan", json={"run_ids": selected})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "missing_model"
    configure(client)
    assert plan(client, selected)["settings"]["model"] == "test-model"


def test_sample_and_archived_projects_can_run_ai_review(client, offline):
    configure(client)
    sample = client.post("/projects/sample").json()["id"]
    job, _ = submit(client, runs(client, 1, project=sample), project=sample)
    assert finish(client, job, project=sample)["status"] == "completed"
    archived = client.post("/projects", json={"name": "Old work"}).json()["id"]
    selected = runs(client, 1, project=archived)
    client.patch(f"/projects/{archived}", json={"archived": True})
    job, _ = submit(client, selected, project=archived, key="archived")
    assert finish(client, job, project=archived)["status"] == "completed"


def test_models_lists_the_draft_endpoint_without_saving(client, monkeypatch):
    seen = {}
    def fake_list(provider, base_url=None, api_key=None, request_timeout_s=None):
        seen.update(provider=provider, base_url=base_url, api_key=api_key)
        return ["model-a", "model-b"]
    monkeypatch.setattr(models, "list_models", fake_list)
    response = client.post("/workspace/ai-review/models", json={
        "provider": "openai", "base_url": "https://draft.invalid/v1", "api_key": KEY})
    assert response.status_code == 200, response.text
    assert response.json()["models"] == ["model-a", "model-b"]
    assert seen == {"provider": "openai", "base_url": "https://draft.invalid/v1", "api_key": KEY}
    assert load_config() == {}
    response = client.post("/workspace/ai-review/models", json={"provider": "openai"})
    assert response.status_code == 422 and response.json()["detail"]["code"] == "missing_credentials"
    configure(client)  # a saved session key serves listing for the same configuration
    assert client.post("/workspace/ai-review/models", json={}).json()["models"] == ["model-a", "model-b"]
    assert seen["api_key"] == KEY


def test_models_reports_endpoints_that_cannot_list(client, monkeypatch):
    class NotFound(Exception):
        status_code = 404
    def failing(*args, **kwargs):
        raise models.ProviderRequestError(NotFound("echo " + KEY))
    monkeypatch.setattr(models, "list_models", failing)
    response = client.post("/workspace/ai-review/models", json={"provider": "openai", "api_key": KEY})
    detail = response.json()["detail"]
    assert detail["code"] == "model_not_found" and "Enter the model ID" in detail["message"]
    assert KEY not in response.text


@pytest.mark.parametrize("reviewer_class,client_name", [
    (models.AnthropicReviewer, "Anthropic"), (models.OpenAIReviewer, "OpenAI")])
def test_list_models_uses_the_sdk_listing(environment, monkeypatch, reviewer_class, client_name):
    options = {}
    class Listing:
        def list(self):
            return [type("M", (), {"id": "z-model"})(), type("M", (), {"id": "a-model"})()]
    def build(**kwargs):
        options.update(kwargs)
        return type("Client", (), {"models": Listing()})()
    sdk = type("SDK", (), {client_name: staticmethod(build)})
    monkeypatch.setattr(reviewer_class, "_import_sdk", lambda *_: sdk)
    ids = models.list_models(reviewer_class.provider, "https://gw.invalid/v1", api_key=KEY, request_timeout_s=5)
    assert ids == ["a-model", "z-model"]
    assert options == {"timeout": 5, "base_url": "https://gw.invalid/v1", "api_key": KEY}


def test_cli_config_lists_models(environment, monkeypatch, capsys):
    from agr import cli
    monkeypatch.setattr(models, "list_models", lambda provider, base_url, **kwargs: ["m-1", "m-2"])
    assert cli.main(["config", "--provider", "openai", "--list-models"]) == 0
    out = capsys.readouterr().out
    assert "model: (not set) (unset)" in out and "  m-1\n  m-2" in out
