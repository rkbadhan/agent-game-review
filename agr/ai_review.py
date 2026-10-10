"""Explicit local AI review setup and durable project review jobs.

Keys stay in server memory. Durable jobs contain only destination/model settings,
capture IDs, status and bounded error messages, never trace text or credentials.
"""
import copy
import hashlib
import importlib.util
import os
import json

from . import model_reviewer as models
from .imports import ImportProblem
from .storage_io import atomic_json
from .review_identity import matching_review_key
from .userconfig import (ConfigError, FIELDS, DEFAULT_ENDPOINTS, config_path, effective_review_config,
                         require_model, save_config, validate_settings, merged_config)
from .workspace import bounded_text, identifier, job_owner, now, owner_running

ACTIVE = ("queued", "running", "cancelling")
RETRYABLE = ("failed", "completed_with_errors", "cancelled", "interrupted")


def public_failure(exc):
    """Never expose SDK exception text, which can contain keys, URLs or input."""
    if isinstance(exc, models.ProviderRequestError):
        return exc.code, str(exc)
    status = getattr(exc, "status_code", None)
    name = type(exc).__name__.lower()
    if isinstance(exc, ConfigError):
        return "invalid_configuration", str(exc)
    if isinstance(exc, models.ModelOutputError):
        return "invalid_response", "The model did not return the expected JSON response. Choose a model with JSON output support."
    if isinstance(exc, RuntimeError) and "SDK" in str(exc):
        return "missing_sdk", "Install the selected provider extra: pip install '.[model-anthropic]' or '.[model-openai]'."
    if status in (401, 403) or "authentication" in name or "permission" in name:
        return "authentication_failed", "The provider rejected authentication. Check the key and model access."
    if status == 404:
        return "model_not_found", "The endpoint or model was not found. Check the URL and model ID."
    if status == 429:
        return "rate_limited", "The provider is rate limited or out of quota. Check the account before retrying."
    if "timeout" in name:
        return "request_timeout", "The provider request timed out. Check the endpoint or increase the request timeout."
    return "provider_failed", "The provider request failed. Check connectivity, credentials, endpoint, and JSON output support."


class AIReviewService:
    def __init__(self, workspace):
        self.workspace = workspace
        self.keys = {}
        self.tests = {}
        # A crash is never presented as a successful or indefinitely running job.
        if not workspace.read_only:
            with workspace.write_lock():
                for path in (workspace.root / "reviews").glob("*.json"):
                    job = workspace._read(path)
                    if job["status"] in ACTIVE and not owner_running(job):
                        job["status"] = "interrupted"
                        job["current_run"] = None
                        atomic_json(path, job)

    def _changes(self, payload):
        if not isinstance(payload, dict) or set(payload) - FIELDS - {"api_key"}:
            raise ImportProblem("invalid_configuration", "Use provider, model, endpoint, and review limits only.")
        try:
            return validate_settings(payload)
        except ConfigError as exc:
            raise ImportProblem("invalid_configuration", str(exc)) from None

    def _key(self, settings, supplied=None):
        if supplied is not None:
            if not isinstance(supplied, str) or len(supplied) > 8192 or any(ord(c) < 32 for c in supplied):
                raise ImportProblem("invalid_credentials", "Enter a valid API key.")
            if supplied.strip():
                return supplied.strip()
        return self.keys.get(settings["configuration_id"])

    def _test_key(self, settings, key):
        secret = key or os.environ.get(settings["provider"].upper() + "_API_KEY", "")
        return (settings["configuration_id"], hashlib.sha256(secret.encode()).hexdigest())

    def settings(self):
        if self.workspace.read_only:
            return {"read_only": True}
        try:
            settings = effective_review_config()
        except ConfigError as exc:
            raise ImportProblem("invalid_configuration", str(exc)) from None
        key = self._key(settings)
        has_environment = bool(os.environ.get(settings["provider"].upper() + "_API_KEY"))
        result = dict(settings)
        result.update(saved={k: v for k, v in self._saved().items() if k in FIELDS},
                      scope="Default for local reviews", config_path=str(config_path()),
                      credentials="session" if key else "environment" if has_environment else "missing",
                      sdk_installed=importlib.util.find_spec(settings["provider"]) is not None)
        result["defaults"] = {provider: {"base_url": url} for provider, url in DEFAULT_ENDPOINTS.items()}
        result["test"] = self.tests.get(self._test_key(settings, key), {"status": "untested"})
        return result

    @staticmethod
    def _saved():
        from .userconfig import load_config
        return load_config()

    def save(self, payload):
        if self.workspace.read_only:
            raise ImportProblem("read_only", "Sample data is read-only.", 403)
        changes = self._changes(payload)
        # Validate credentials before changing the active file.
        proposed = effective_review_config(saved_config=merged_config(self._saved(), changes))
        key = self._key(proposed, payload.get("api_key"))
        try:
            save_config(**changes)
        except ConfigError as exc:
            raise ImportProblem("invalid_configuration", str(exc)) from None
        if key:
            self.keys[effective_review_config()["configuration_id"]] = key
        return self.settings()

    def forget_key(self):
        if self.workspace.read_only:
            raise ImportProblem("read_only", "Sample data is read-only.", 403)
        self.keys.clear()
        self.tests.clear()
        return self.settings()

    def _credentials(self, settings, key=None):
        key = self._key(settings, key)
        if not key and not os.environ.get(settings["provider"].upper() + "_API_KEY"):
            raise ImportProblem("missing_credentials", "Set the provider API key in the server environment or save a session-only key in AI review settings.")
        return key

    def _reviewer(self, settings, key=None):
        key = self._credentials(settings, key)
        try:
            return models.make_reviewer(**{k: settings[k] for k in FIELDS}, api_key=key)
        except Exception as exc:
            code, message = public_failure(exc)
            raise ImportProblem(code, message) from None

    def test(self, payload):
        if self.workspace.read_only:
            raise ImportProblem("read_only", "Sample data is read-only.", 403)
        settings = effective_review_config(saved_config=merged_config(self._saved(), self._changes(payload)))
        key = self._key(settings, payload.get("api_key"))
        try:
            models.test_connection(self._reviewer(settings, key))
        except Exception as exc:
            code, message = (exc.code, str(exc)) if isinstance(exc, ImportProblem) else public_failure(exc)
            result = {"status": "failed", "tested_at": now(), "code": code, "message": message}
        else:
            result = {"status": "verified", "tested_at": now(),
                      "message": "The endpoint returned the expected JSON response. No trace was sent."}
        if key and result["status"] == "verified":
            self.keys[settings["configuration_id"]] = key
        self.tests[self._test_key(settings, key)] = result
        return {**result, "configuration_id": settings["configuration_id"]}

    def models(self, payload):
        """List the endpoint's model IDs so the user picks one instead of typing it."""
        if self.workspace.read_only:
            raise ImportProblem("read_only", "Sample data is read-only.", 403)
        settings = effective_review_config(saved_config=merged_config(self._saved(), self._changes(payload)))
        key = self._credentials(settings, payload.get("api_key"))
        try:
            ids = models.list_models(settings["provider"], settings["base_url"], api_key=key,
                                     request_timeout_s=settings["request_timeout_s"])
        except Exception as exc:
            code, message = public_failure(exc)
            if code == "model_not_found":
                message = "This endpoint does not list its models. Enter the model ID instead."
            raise ImportProblem(code, message) from None
        return {"provider": settings["provider"], "base_url": settings["base_url"], "models": ids}

    def _selection(self, project_id, run_ids):
        self.workspace.reviewable_project(project_id)
        if (not isinstance(run_ids, list) or not 1 <= len(run_ids) <= 1000
                or not all(isinstance(r, str) for r in run_ids) or len(set(run_ids)) != len(run_ids)):
            raise ImportProblem("invalid_selection", "Select between 1 and 1000 distinct runs.")
        store = self.workspace.store(project_id)
        items = []
        for run_id in run_ids:
            try:
                capture_id = store.latest_capture_id(run_id)
                if not capture_id:
                    raise KeyError(run_id)
                if not store.has_derived(run_id, capture_id, "source.json"):
                    raise FileNotFoundError(run_id)
            except (KeyError, FileNotFoundError, ValueError):
                raise ImportProblem("run_not_found", "A selected run is not in this project.", 404) from None
            items.append({"run_id": run_id, "capture_id": capture_id})
        return items

    def plan(self, project_id, run_ids):
        items = self._selection(project_id, run_ids)
        settings = effective_review_config()
        try:
            require_model(settings)
        except ConfigError as exc:
            raise ImportProblem("missing_model", str(exc)) from None
        from .userconfig import reviewer_key
        key = reviewer_key(settings)
        store = self.workspace.store(project_id)
        for item in items:
            item["already_reviewed"] = bool(matching_review_key(store, item["run_id"], item["capture_id"], key))
        token = hashlib.sha256(json.dumps(
            {"items": items, "settings": {k: settings[k] for k in FIELDS}}, sort_keys=True).encode()).hexdigest()
        return {"plan_token": token, "items": items, "total": len(items),
                "already_reviewed": sum(i["already_reviewed"] for i in items),
                "settings": settings, "reviewer_key": key,
                "cost_estimate_usd": None,
                "cost_note": "Actual cost is unavailable before review. Targets are per run; one in-flight request can exceed them."}

    def submit(self, project_id, payload):
        self.workspace.reviewable_project(project_id)
        request_id = bounded_text(payload.get("idempotency_key"), "a review request ID", 96)
        if not isinstance(payload.get("force", False), bool):
            raise ImportProblem("invalid_selection", "Rerun must be true or false.")
        with self.workspace.write_lock():
            for prior in self.workspace.records("reviews", project_id):
                if prior["idempotency_key"] == request_id:
                    if prior["run_ids"] != payload.get("run_ids") or prior["configuration_id"] != payload.get("configuration_id") or prior["force"] != payload.get("force", False) or prior.get("plan_token") != payload.get("plan_token"):
                        raise ImportProblem("request_conflict", "This request ID was used for a different review.", 409)
                    return prior
            plan = self.plan(project_id, payload.get("run_ids"))
            settings = plan["settings"]
            if payload.get("configuration_id") != settings["configuration_id"]:
                raise ImportProblem("configuration_changed", "AI settings changed. Preview the review again.", 409)
            if payload.get("plan_token") != plan["plan_token"]:
                raise ImportProblem("selection_changed", "The capture or review limits changed. Preview the review again.", 409)
            # Validate setup before accepting a paid job, but do not call the model.
            self._reviewer(settings)
            job = {"id": identifier("review"), "project_id": project_id,
                   "idempotency_key": request_id, "plan_token": plan["plan_token"], "configuration_id": settings["configuration_id"],
                   "settings": {k: settings[k] for k in FIELDS},
                   "reviewer_key": plan["reviewer_key"], "run_ids": payload["run_ids"],
                   "items": plan["items"], "total": plan["total"], "results": [],
                   "force": payload.get("force", False), "status": "queued",
                   "created_at": now(), "updated_at": now(), "current_run": None, **job_owner()}
            atomic_json(self.workspace._path("reviews", job["id"]), job)
            # Snapshot session credentials for queued work; never serialize them.
            captured_key = self._key(settings)
            self.workspace.futures[job["id"]] = self.workspace.review_executor.submit(self._run, project_id, job["id"], captured_key)
            return copy.deepcopy(job)

    def _write(self, job):
        job["updated_at"] = now()
        atomic_json(self.workspace._path("reviews", job["id"]), job)

    def _run(self, project_id, job_id, api_key):
        workspace = self.workspace
        try:
            with workspace.write_lock():
                job = workspace.record("reviews", job_id, project_id)
                if job["status"] == "cancelling":
                    job["status"] = "cancelled"
                    self._write(job)
                    return
                workspace.reviewable_project(project_id)
                job["status"] = "running"
                self._write(job)
            settings = {**job["settings"], "configuration_id": job["configuration_id"]}
            reviewer = self._reviewer(settings, api_key)
            store = workspace.store(project_id)
            for item in job["items"]:
                with workspace.write_lock():
                    job = workspace.record("reviews", job_id, project_id)
                    if job["status"] == "cancelling":
                        break
                    workspace.reviewable_project(project_id)
                    if any(r["run_id"] == item["run_id"] and r["status"] in ("completed", "skipped") for r in job["results"]):
                        continue
                    job["current_run"] = item["run_id"]
                    self._write(job)
                try:
                    from .pipeline import analyze, CaptureChangedError
                    run_id, capture_id = item["run_id"], item["capture_id"]
                    with store.index_lock(run_id):
                        if store.latest_capture_id(run_id) != capture_id:
                            raise ImportProblem("capture_changed", "This run has a newer capture. Start a new review from the latest run.")
                        if not job["force"] and matching_review_key(store, run_id, capture_id, job["reviewer_key"]):
                            skip = True
                        else:
                            skip = False
                        doc = None if skip else store.read_source(run_id, capture_id)
                    if skip:
                        result = {**item, "status": "skipped"}
                    else:
                        analysis = analyze(doc, store, reviewer=reviewer, expected_capture_id=capture_id)
                        result = {**item, "status": "completed"}
                        if analysis.review_incomplete:
                            reason = analysis.review_incomplete["reason"]
                            message = {
                                "timeout": "Review reached its elapsed-time target. Increase the target in AI settings before retrying.",
                                "cost_budget": "Review reached its estimated cost target. Increase the target in AI settings before retrying.",
                                "packet_budget": "The evidence packet exceeded the request limit. Try a smaller capture.",
                                "missing_chunks": "A phase summary did not return usable evidence. Try a model with structured output support.",
                            }.get(reason, "Review stopped at a budget or evidence limit.")
                            result.update(status="incomplete", code=reason,
                                          error=message + " The deterministic baseline remains available.")
                        elif analysis.review_error:
                            result.update(status="failed", code="provider_failed",
                                          error=analysis.review_error["message"] if analysis.review_error["error_type"] == "ProviderRequestError" else
                                          "AI review failed. Check the model, endpoint, credentials, and JSON response support before retrying.")
                except CaptureChangedError as exc:
                    result = {**item, "status": "failed", "code": "capture_changed", "error": str(exc)}
                except Exception as exc:
                    code, message = (exc.code, str(exc)) if isinstance(exc, ImportProblem) else public_failure(exc)
                    result = {**item, "status": "failed", "code": code, "error": message}
                with workspace.write_lock():
                    job = workspace.record("reviews", job_id, project_id)
                    job["results"] = [r for r in job["results"] if r["run_id"] != item["run_id"]] + [result]
                    job["current_run"] = None
                    self._write(job)
            with workspace.write_lock():
                job = workspace.record("reviews", job_id, project_id)
                failures = sum(r["status"] in ("failed", "incomplete") for r in job["results"])
                job["status"] = ("cancelled" if job["status"] == "cancelling" else
                                 "failed" if failures == job["total"] else
                                 "completed_with_errors" if failures else "completed")
                job["current_run"] = None
                self._write(job)
        except Exception as exc:
            code, message = (exc.code, str(exc)) if isinstance(exc, ImportProblem) else public_failure(exc)
            with workspace.write_lock():
                job = workspace.record("reviews", job_id, project_id)
                job.update(status="interrupted", current_run=None, error=message, error_code=code)
                self._write(job)

    def cancel(self, project_id, job_id):
        self.workspace.reviewable_project(project_id)
        with self.workspace.write_lock():
            job = self.workspace.record("reviews", job_id, project_id)
            if job["status"] in ("queued", "running"):
                job["status"] = "cancelling"
                self._write(job)
            return job

    def retry(self, project_id, job_id):
        self.workspace.reviewable_project(project_id)
        with self.workspace.write_lock():
            job = self.workspace.record("reviews", job_id, project_id)
            if job["status"] not in RETRYABLE:
                raise ImportProblem("job_active", "Only unsuccessful or unfinished reviews can be retried.", 409)
            settings = {**job["settings"], "configuration_id": job["configuration_id"]}
            active = effective_review_config()
            if active["configuration_id"] == job["configuration_id"]:
                settings = active
                job["settings"] = {k: active[k] for k in FIELDS}
            self._reviewer(settings)
            job.update(status="queued", current_run=None, **job_owner())
            job.pop("error", None)
            job.pop("error_code", None)
            self._write(job)
            self.workspace.futures[job_id] = self.workspace.review_executor.submit(self._run, project_id, job_id, self._key(settings))
            return copy.deepcopy(job)


