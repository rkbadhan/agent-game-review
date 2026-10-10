"""Persistent local projects, staged imports, and resumable import jobs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from .imports import CATALOG, ImportProblem, MAX_ARCHIVE, MAX_ENTRIES, MAX_EXPANDED, MAX_FILE, discover, publish_analysis, safe_relative
from .store import Store
from .storage_io import atomic_json, file_lock, read_json


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def identifier(prefix):
    return prefix + "_" + uuid.uuid4().hex


def bounded_text(value, name, limit=200):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ImportProblem("invalid_field", f"Enter {name} with at most {limit} characters.")
    return value.strip()


_locks = {}
_locks_guard = threading.Lock()
_PROCESS_TOKEN = uuid.uuid4().hex


def process_identity(pid):
    """Birth identity, rather than a PID that an OS/container can reuse."""
    if not isinstance(pid, int) or pid <= 0:
        return None
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetProcessTimes.argtypes = [wintypes.HANDLE] + [ctypes.POINTER(wintypes.FILETIME)] * 4
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return None
        try:
            times = [wintypes.FILETIME() for _ in range(4)]
            if kernel.GetProcessTimes(handle, *(ctypes.byref(t) for t in times)):
                return f"{times[0].dwHighDateTime}:{times[0].dwLowDateTime}"
        finally:
            kernel.CloseHandle(handle)
        return None
    try:
        start = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19]
        boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
        return boot + ":" + start
    except (OSError, IndexError):
        return None


def job_owner():
    return {"owner_pid": os.getpid(), "owner_token": _PROCESS_TOKEN, "owner_identity": process_identity(os.getpid())}


def owner_running(job):
    pid = job.get("owner_pid")
    if pid == os.getpid():
        return job.get("owner_token") == _PROCESS_TOKEN
    if not process_running(pid):
        return False
    identity = process_identity(pid)
    saved_identity = job.get("owner_identity")
    return saved_identity == identity if saved_identity is not None and identity is not None else True


def process_running(pid):
    if not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x1000, False, pid)
        if not handle:
            return ctypes.get_last_error() == 5  # access denied is not proof of death
        try:
            code = wintypes.DWORD()
            return not kernel.GetExitCodeProcess(handle, ctypes.byref(code)) or code.value == 259
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


class Workspace:
    def __init__(self, store_root, read_only=False, default_store=None):
        self.default_root = Path(store_root).resolve()
        self.root = self.default_root / ".workspace"
        self.read_only = read_only
        self.secrets = {}
        self._stores = {"existing": default_store} if default_store is not None else {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agr-import")
        self.review_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agr-review")
        self.futures = {}
        with _locks_guard:
            self.lock = _locks.setdefault(str(self.root), threading.RLock())
        if not read_only:
            with self.write_lock():
                if not (self.root / "workspace.json").exists():
                    atomic_json(self.root / "workspace.json", {"reviewer": "Local reviewer", "last_project": "existing", "projects": [{"id": "existing", "name": "Existing runs", "created_at": now(), "archived": False, "sample": False}]})
                # An interrupted process cannot claim that its work finished.
                for path in (self.root / "jobs").glob("*.json"):
                    job = self._read(path)
                    if job.get("status") in ("queued", "importing", "cancelling") and not owner_running(job):
                        job["status"] = "interrupted"
                        job["updated_at"] = now()
                        atomic_json(path, job)
                self._expire_previews()

    @contextmanager
    def write_lock(self):
        if self.read_only:
            raise ImportProblem("read_only", "Sample data is read-only. Start a local workspace to analyze your runs.", 403)
        with self.lock, file_lock(self.root / ".lock"):
            yield

    def _read(self, path):
        try:
            return read_json(path)
        except FileNotFoundError:
            raise ImportProblem("not_found", "This record is no longer available.", 404) from None

    def settings(self):
        if self.read_only:
            return {"reviewer": "Local reviewer", "last_project": "existing", "projects": [{"id": "existing", "name": "Sample data", "sample": True, "archived": False}]}
        return self._read(self.root / "workspace.json")

    def project(self, project_id):
        for project in self.settings()["projects"]:
            if project["id"] == project_id:
                return project
        raise ImportProblem("project_not_found", "This project is not in this workspace.", 404)

    def store(self, project_id):
        project = self.project(project_id)
        root = self.default_root if project_id == "existing" else self.root / "projects" / project_id / "store"
        with self.lock:
            if project_id not in self._stores:
                self._stores[project_id] = Store(str(root))
            return self._stores[project_id]

    def create_project(self, name, sample=False):
        name = bounded_text(name, "a project name")
        with self.write_lock():
            settings = self.settings()
            project = {"id": identifier("project"), "name": name, "sample": bool(sample), "archived": False, "created_at": now()}
            if sample:
                existing = next((p for p in settings["projects"] if p.get("sample")), None)
                if existing:
                    return existing
            root = self.root / "projects" / project["id"] / "store"
            Store(str(root))
            if sample:
                from .demo import build_real_demo_store
                build_real_demo_store(Store(str(root)))
            settings["projects"].append(project)
            settings["last_project"] = project["id"]
            atomic_json(self.root / "workspace.json", settings)
            return project

    def update_settings(self, payload):
        with self.write_lock():
            settings = self.settings()
            if "reviewer" in payload:
                settings["reviewer"] = bounded_text(payload["reviewer"], "a reviewer name", 96)
            if "last_project" in payload:
                self.project(payload["last_project"])
                settings["last_project"] = payload["last_project"]
            atomic_json(self.root / "workspace.json", settings)
            return settings

    def update_project(self, project_id, payload):
        with self.write_lock():
            settings = self.settings()
            self.project(project_id)
            for p in settings["projects"]:
                if p["id"] == project_id:
                    if "name" in payload:
                        p["name"] = bounded_text(payload["name"], "a project name")
                    if "archived" in payload:
                        if not isinstance(payload["archived"], bool):
                            raise ImportProblem("invalid_field", "Archive state must be true or false.")
                        p["archived"] = payload["archived"]
                    p["updated_at"] = now()
                    result = p
            atomic_json(self.root / "workspace.json", settings)
            return result

    def writable_project(self, project_id):
        p = self.project(project_id)
        if self.read_only or p.get("sample") or p.get("archived"):
            raise ImportProblem("read_only", "Choose an active personal project to make changes.", 403)
        return p

    def reviewable_project(self, project_id):
        """AI review only adds review snapshots, so sample and archived projects allow it."""
        p = self.project(project_id)
        if self.read_only:
            raise ImportProblem("read_only", "AI review is unavailable on a read-only server.", 403)
        return p

    def _path(self, kind, record_id):
        if not isinstance(record_id, str) or not re.fullmatch(r"[a-z]+_[a-f0-9]{32}", record_id):
            raise ImportProblem("not_found", "This record is no longer available.", 404)
        return self.root / kind / (record_id + ".json")

    def record(self, kind, record_id, project_id):
        self.project(project_id)
        record = self._read(self._path(kind, record_id))
        if record["project_id"] != project_id:
            raise ImportProblem("not_found", "This record is not in the selected project.", 404)
        return record

    def records(self, kind, project_id):
        self.project(project_id)
        return sorted([r for path in (self.root / kind).glob("*.json") if (r := self._read(path))["project_id"] == project_id], key=lambda r: r.get("created_at", ""), reverse=True)

    def _expire_previews(self):
        for path in (self.root / "previews").glob("*.json"):
            preview = self._read(path)
            if any(j.get("preview_id") == preview["id"] for j in self.records("jobs", preview["project_id"])):
                # Submitted manifests and original artifacts are durable import
                # provenance, including the source needed to retry failed work.
                continue
            if time.time() - preview["created_epoch"] > 86400:
                folder = self.root / "staging" / preview["id"]
                if folder.exists():
                    shutil.rmtree(folder)
                preview["status"] = "expired"
                preview["items"] = []
                atomic_json(path, preview)

    def new_preview(self, project_id, source, options=None):
        self.writable_project(project_id)
        if source not in {s["id"] for s in CATALOG}:
            raise ImportProblem("unsupported_source", "Choose a supported source.")
        with self.write_lock():
            record = {"id": identifier("preview"), "project_id": project_id, "source": source, "options": options or {}, "created_at": now(), "created_epoch": time.time(), "status": "staging", "files": [], "items": [], "ignored": []}
            (self.root / "staging" / record["id"] / "files").mkdir(parents=True)
            atomic_json(self._path("previews", record["id"]), record)
            return self.public_preview(record)

    def preview(self, project_id, preview_id):
        preview = self.record("previews", preview_id, project_id)
        submitted = any(j.get("preview_id") == preview_id for j in self.records("jobs", project_id))
        if preview["status"] == "expired" or (not submitted and time.time() - preview["created_epoch"] > 86400):
            raise ImportProblem("preview_expired", "This preview expired. Select your files or fetch the trace again.", 410)
        return preview

    def stage_file(self, project_id, preview_id, name, content):
        self.writable_project(project_id)
        relative = safe_relative(name)
        limit = MAX_ARCHIVE if relative.suffix.lower() == ".zip" else MAX_FILE
        if len(content) > limit:
            raise ImportProblem("import_limit", "This file is too large. Select a smaller export.", 413)
        with self.write_lock():
            preview = self.preview(project_id, preview_id)
            if preview["status"] != "staging":
                raise ImportProblem("preview_locked", "Create a new preview to change the selected files.", 409)
            if len(preview["files"]) >= MAX_ENTRIES or sum(f["bytes"] for f in preview["files"]) + len(content) > MAX_EXPANDED:
                raise ImportProblem("import_limit", "Select a smaller batch.", 413)
            target = self.root / "staging" / preview_id / "files" / relative
            if target.exists():
                raise ImportProblem("duplicate_filename", "Two selected files have the same relative filename.", 409)
            if shutil.disk_usage(self.root).free < len(content) + 16 * 1024 * 1024:
                raise ImportProblem("storage_unavailable", "Free local disk space and try again.", 507)
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(content)
            except OSError:
                raise ImportProblem("storage_unavailable", "This file could not be staged. Check filename conflicts and free disk space.", 507) from None
            preview["files"].append({"name": name, "bytes": len(content), "byte_hash": "sha256:" + hashlib.sha256(content).hexdigest()})
            atomic_json(self._path("previews", preview_id), preview)
            return {"files": len(preview["files"]), "bytes": sum(f["bytes"] for f in preview["files"])}

    def inspect(self, project_id, preview_id):
        self.writable_project(project_id)
        with self.write_lock():
            preview = self.preview(project_id, preview_id)
            if preview["status"] == "ready":
                return self.public_preview(preview)
            if preview["status"] != "staging":
                raise ImportProblem("preview_locked", "Create a new preview to change this selection.", 409)
            items, ignored = discover(self.root / "staging" / preview_id / "files", preview["source"], self.store(project_id), preview["options"])
            preview.update(status="ready", items=items, ignored=ignored)
            preview["manifest_hash"] = hashlib.sha256(json.dumps(preview, sort_keys=True).encode()).hexdigest()
            atomic_json(self._path("previews", preview_id), preview)
            return self.public_preview(preview)

    @staticmethod
    def public_preview(preview):
        result = copy.deepcopy(preview)
        result.pop("options", None)
        for item in result["items"]:
            item.pop("doc", None)
        return result

    def submit(self, project_id, payload):
        self.writable_project(project_id)
        key = bounded_text(payload.get("idempotency_key"), "an import request ID", 96)
        selected = payload.get("selected")
        if not isinstance(selected, list) or not selected or not all(isinstance(s, str) for s in selected) or len(selected) != len(set(selected)):
            raise ImportProblem("invalid_selection", "Select at least one valid run, with no repeated selections.")
        with self.write_lock():
            existing = next((j for j in self.records("jobs", project_id) if j["idempotency_key"] == key), None)
            if existing:
                if existing["preview_id"] != payload.get("preview_id") or existing["selected"] != selected:
                    raise ImportProblem("request_conflict", "This request ID was used for a different selection.", 409)
                return existing
            preview = self.preview(project_id, payload.get("preview_id"))
            if preview["status"] != "ready" or preview.get("manifest_hash") != payload.get("manifest_hash"):
                raise ImportProblem("preview_changed", "Preview the current selection before importing.", 409)
            allowed = {i["id"] for i in preview["items"] if i["status"] != "invalid"}
            if not set(selected) <= allowed:
                raise ImportProblem("invalid_selection", "The selection includes invalid or unavailable runs.")
            job = {"id": identifier("import"), "project_id": project_id, "preview_id": preview["id"], "manifest_hash": preview["manifest_hash"], "idempotency_key": key, "selected": selected, "label": bounded_text(payload.get("label") or "Imported runs", "a batch label"), "status": "queued", **job_owner(), "created_at": now(), "updated_at": now(), "results": [], "total": len(selected)}
            atomic_json(self._path("jobs", job["id"]), job)
        self.futures[job["id"]] = self.executor.submit(self._run_job, project_id, job["id"])
        return job

    def _run_job(self, project_id, job_id):
        try:
            with self.write_lock():
                job = self.record("jobs", job_id, project_id)
                if job["status"] == "cancelling":
                    job["status"] = "cancelled"
                    atomic_json(self._path("jobs", job_id), job)
                    return
                preview = self.preview(project_id, job["preview_id"])
                job["status"] = "importing"
                job.update(job_owner())
                atomic_json(self._path("jobs", job_id), job)
            for item in preview["items"]:
                if item["id"] not in job["selected"] or item["id"] in {r["item_id"] for r in job["results"] if r["status"] != "failed"}:
                    continue
                with self.write_lock():
                    job = self.record("jobs", job_id, project_id)
                    if job["status"] == "cancelling":
                        break
                    scratch = self.root / "staging" / job["preview_id"] / ("analysis-" + uuid.uuid4().hex)
                    self.writable_project(project_id)
                    target_store = self.store(project_id)
                # Analysis uses scratch files and publication's per-run lock.
                # Cancellation/project settings must not wait for this work.
                try:
                    result = publish_analysis(item["doc"], target_store, scratch)
                    result["item_id"] = item["id"]
                except Exception:
                    result = {"item_id": item["id"], "status": "failed", "error": "Analysis or storage did not complete. Original selected files are retained; retry after checking disk space and the capture format."}
                finally:
                    shutil.rmtree(scratch, ignore_errors=True)
                with self.write_lock():
                    job = self.record("jobs", job_id, project_id)
                    job["results"] = [r for r in job["results"] if r["item_id"] != item["id"]] + [result]
                    job["updated_at"] = now()
                    atomic_json(self._path("jobs", job_id), job)
                time.sleep(0)  # allow cancellation between committed items
            with self.write_lock():
                job = self.record("jobs", job_id, project_id)
                failed = any(r["status"] == "failed" for r in job["results"])
                job["status"] = "cancelled" if job["status"] == "cancelling" else "completed_with_errors" if failed else "completed"
                if failed and all(r["status"] == "failed" for r in job["results"]):
                    job["status"] = "failed"
                job["updated_at"] = now()
                atomic_json(self._path("jobs", job_id), job)
        except Exception:
            with self.write_lock():
                job = self.record("jobs", job_id, project_id)
                job["status"] = "interrupted"
                atomic_json(self._path("jobs", job_id), job)

    def cancel(self, project_id, job_id):
        self.writable_project(project_id)
        with self.write_lock():
            job = self.record("jobs", job_id, project_id)
            if job["status"] in ("queued", "importing"):
                job["status"] = "cancelling"
                atomic_json(self._path("jobs", job_id), job)
            return job

    def retry(self, project_id, job_id):
        self.writable_project(project_id)
        with self.write_lock():
            job = self.record("jobs", job_id, project_id)
            if job["status"] not in ("failed", "completed_with_errors", "cancelled", "interrupted"):
                raise ImportProblem("job_active", "This import cannot be retried in its current state.", 409)
            self.preview(project_id, job["preview_id"])
            job["status"] = "queued"
            job.update(job_owner())
            atomic_json(self._path("jobs", job_id), job)
        self.futures[job_id] = self.executor.submit(self._run_job, project_id, job_id)
        return job

    def save_connection(self, project_id, payload):
        self.writable_project(project_id)
        from urllib.parse import urlsplit
        host = payload.get("host", "").rstrip("/")
        parsed = urlsplit(host)
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ImportProblem("invalid_host", "Enter an HTTP(S) host without credentials, query parameters, or a fragment.")
        environment = self.environment_keys(host)
        public = payload.get("public_key") or environment[0]
        secret = payload.get("secret_key") or environment[1]
        if not isinstance(public, str) or not isinstance(secret, str) or not public or not secret:
            raise ImportProblem("missing_credentials", "Provide public and secret keys, or configure them in the server environment.")
        with self.write_lock():
            connection = {"id": identifier("connection"), "project_id": project_id, "source": "langfuse", "name": bounded_text(payload.get("name") or "Langfuse", "a connection name"), "host": host, "credentials": "session_only", "status": "not_tested", "created_at": now()}
            self.secrets[connection["id"]] = (public, secret)
            atomic_json(self._path("connections", connection["id"]), connection)
        return connection

    @staticmethod
    def environment_keys(host):
        # Environment credentials belong to the configured or CLI-default endpoint.
        # Never send them to an arbitrary host supplied by a browser or record.
        from urllib.parse import urlsplit
        from .langfuse_api import _DEFAULT_HOST
        def endpoint(value):
            parsed = urlsplit(value.rstrip("/"))
            if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                return None
            return (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), parsed.path)
        try:
            configured = endpoint(os.environ.get("LANGFUSE_HOST") or _DEFAULT_HOST)
            matches = configured is not None and endpoint(host) == configured
        except ValueError:
            matches = False
        return (os.environ.get("LANGFUSE_PUBLIC_KEY"), os.environ.get("LANGFUSE_SECRET_KEY")) if matches else (None, None)

    def fetch_connection(self, project_id, connection_id, trace_id, from_time=None, to_time=None):
        self.writable_project(project_id)
        connection = self.record("connections", connection_id, project_id)
        trace_id = bounded_text(trace_id, "a trace ID", 512)
        from .langfuse_v2 import fetch_trace
        keys = self.secrets.get(connection_id)
        if not keys:
            keys = self.environment_keys(connection["host"])
        if not all(keys):
            raise ImportProblem("missing_credentials", "Session credentials expired. Configure a new connection or server environment keys.")
        try:
            data = fetch_trace(trace_id, host=connection["host"], public_key=keys[0], secret_key=keys[1], from_time=from_time, to_time=to_time)
        except ImportProblem as exc:
            with self.write_lock():
                path = self._path("connections", connection_id)
                if path.exists():
                    current = self.record("connections", connection_id, project_id)
                    current.update(status="fetch_failed", last_test_at=now())
                    atomic_json(path, current)
            raise exc
        with self.write_lock():
            # A disconnect wins over a fetch that was already in flight.
            self.record("connections", connection_id, project_id)
        preview = self.new_preview(project_id, "langfuse")
        self.stage_file(project_id, preview["id"], "trace.json", json.dumps(data).encode())
        result = self.inspect(project_id, preview["id"])
        with self.write_lock():
            current = self.record("connections", connection_id, project_id)
            current.update(status="trace_accessible", last_test_at=now())
            atomic_json(self._path("connections", connection_id), current)
        return result

    def disconnect(self, project_id, connection_id):
        self.writable_project(project_id)
        with self.write_lock():
            self.record("connections", connection_id, project_id)
            self._path("connections", connection_id).unlink()
            self.secrets.pop(connection_id, None)
        return {"disconnected": True}

    def save_investigation(self, project_id, payload, investigation_id=None):
        self.writable_project(project_id)
        from .read import get_review, RunNotFound
        from .capture_view import PinnedStore
        previous = self.record("investigations", investigation_id, project_id) if investigation_id else None
        run_id = payload.get("run_id")
        if previous and (run_id != previous["run_id"] or payload.get("moment_id") != previous["moment_id"]):
            raise ImportProblem("invalid_evidence", "Create a new investigation to change its saved evidence.")
        capture_id = previous["capture_id"] if previous else bounded_text(payload.get("capture_id"), "the reviewed capture ID", 128)
        if previous and payload.get("capture_id", capture_id) != capture_id:
            raise ImportProblem("invalid_evidence", "Create a new investigation to change its saved capture.")
        try:
            pinned = PinnedStore(self.store(project_id), run_id, capture_id)
            review = get_review(pinned, run_id)
        except ImportProblem:
            raise
        except (RunNotFound, ValueError, TypeError):
            raise ImportProblem("run_not_found", "Choose an available run from this project.", 404) from None
        title = bounded_text(payload.get("title"), "a title")
        action = bounded_text(payload.get("action"), "a next action", 4000)
        status = payload.get("status", "open")
        if status not in ("open", "in_progress", "resolved"):
            raise ImportProblem("invalid_field", "Choose Open, In progress, or Resolved.")
        moment_id = payload.get("moment_id")
        moments = review.get("moments", [])
        if not previous and moment_id and moment_id not in {m["moment_id"] for m in moments}:
            raise ImportProblem("invalid_evidence", "This finding is no longer available in the selected review.")
        with self.write_lock():
            previous = self.record("investigations", investigation_id, project_id) if investigation_id else None
            record = {"id": investigation_id or identifier("investigation"), "project_id": project_id, "run_id": run_id, "capture_id": capture_id, "moment_id": moment_id, "title": title, "action": action, "status": status, "actor": self.settings()["reviewer"], "created_at": previous["created_at"] if previous else now(), "updated_at": now(), "revision": previous["revision"] + 1 if previous else 1, "history": (previous["history"] + [{k: v for k, v in previous.items() if k != "history"}]) if previous else []}
            atomic_json(self._path("investigations", record["id"]), record)
            return record
