"""Persistent local projects, staged imports, and resumable import jobs."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import threading
import time
import uuid

from .imports import CATALOG, ImportProblem, MAX_ARCHIVE, MAX_ENTRIES, MAX_EXPANDED, MAX_FILE, discover, publish_analysis, safe_relative
from .store import InvalidRunId, Store
from .storage_io import atomic_json, file_lock, read_json


LOG = logging.getLogger(__name__)
MAX_FETCH_TRACES = 100
MAX_TRACE_ID = 512
MAX_BATCH_SECONDS = 240
ID_LOOKUP_DAYS = 400
MAX_CONCURRENT_FETCHES = 2
# Errors that say the connection itself is unusable; a rate limit or a size problem does not.
ACCESS_FAILURES = ("source_auth_failed", "source_unreachable", "source_not_found", "unsupported_response", "source_redirect")


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
        # Each batch holds up to MAX_ARCHIVE in memory, so how many run at once is bounded per server.
        self.fetch_slots = threading.BoundedSemaphore(MAX_CONCURRENT_FETCHES)
        self._previews_expired_at = 0.0
        self._stores = {"existing": default_store} if default_store is not None else {}
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agr-import")
        self.review_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="agr-review")
        self.futures = {}
        with _locks_guard:
            self.lock = _locks.setdefault(str(self.root), threading.RLock())
        if not read_only:
            with self.write_lock():
                if not (self.root / "workspace.json").exists():
                    atomic_json(self.root / "workspace.json", {"reviewer": "Local reviewer", "last_project": "existing", "projects": [{"id": "existing", "name": "Local runs", "created_at": now(), "archived": False, "sample": False}]})
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
        settings = self._read(self.root / "workspace.json")
        # Upgrade the old default label without changing user-named projects.
        for project in settings["projects"]:
            if project["id"] == "existing" and project["name"] == "Existing runs" and not project.get("updated_at"):
                project["name"] = "Local runs"
        return settings

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
        rows = sorted([r for path in (self.root / kind).glob("*.json") if (r := self._read(path))["project_id"] == project_id], key=lambda r: r.get("created_at", ""), reverse=True)
        return [self.connection_state(r) for r in rows] if kind == "connections" else rows

    def connection_state(self, connection):
        """Availability is current, not the result of a test before a server restart."""
        keys = self.secrets.get(connection["id"]) or self.environment_keys(connection["host"])
        return dict(connection, credentials_available=all(keys),
                    status=connection["status"] if all(keys) else "needs_keys")

    def reconnect(self, project_id, connection_id, payload):
        self.writable_project(project_id)
        connection = self.record("connections", connection_id, project_id)
        # Keep host/project identity fixed; replacement keys must belong to it.
        public, secret = payload.get("public_key"), payload.get("secret_key")
        if not isinstance(public, str) or not isinstance(secret, str) or not public.strip() or not secret.strip():
            raise ImportProblem("missing_credentials", "Enter both project keys to reconnect.")
        from . import langfuse_source
        # Authenticate first so revoked/wrong keys retain an actionable auth error.
        from datetime import datetime, timedelta, timezone
        end = datetime.now(timezone.utc)
        langfuse_source.list_traces(host=connection["host"], public_key=public.strip(), secret_key=secret.strip(),
                                   from_time=(end - timedelta(hours=1)).isoformat(), to_time=end.isoformat(), limit=1)
        remote = langfuse_source.project(connection["host"], public.strip(), secret.strip())
        expected = (connection.get("langfuse_project") or {}).get("id")
        if not expected or not remote or remote.get("id") != expected:
            raise ImportProblem("project_mismatch", "These keys could not be verified for this Langfuse project. Check the keys or add a separate connection. A saved connection without a known project ID cannot be rebound.")
        with self.write_lock():
            current = self.record("connections", connection_id, project_id)
            current.update(status="accessible", last_test_at=now())
            if remote:
                current["langfuse_project"] = remote
            self.secrets[connection_id] = (public.strip(), secret.strip())
            atomic_json(self._path("connections", connection_id), current)
        return self.connection_state(current)

    def _expire_previews(self):
        # Cheap checks first: only previews past their day are compared with job history, and each project's
        # submitted previews are read once. One unreadable file must not stop the sweep.
        submitted = {}
        for path in (self.root / "previews").glob("*.json"):
            try:
                preview = self._read(path)
                if preview["status"] == "expired" or time.time() - preview["created_epoch"] <= 86400:
                    continue
                if preview["project_id"] not in submitted:
                    submitted[preview["project_id"]] = {j.get("preview_id") for j in self.records("jobs", preview["project_id"])}
                if preview["id"] in submitted[preview["project_id"]]:
                    # Submitted manifests and original artifacts are durable import
                    # provenance, including the source needed to retry failed work.
                    continue
                folder = self.root / "staging" / preview["id"]
                if folder.exists():
                    shutil.rmtree(folder)
                preview["status"] = "expired"
                preview["items"] = []
                atomic_json(path, preview)
            except (ValueError, KeyError, TypeError, OSError):
                LOG.warning("Skipping an unreadable preview record while expiring old previews", exc_info=True)

    def _expire_previews_periodically(self):
        # Previews abandoned while the server keeps running (a closed tab, a proxy timeout) are cleaned up too.
        if time.time() - self._previews_expired_at < 600:
            return
        self._previews_expired_at = time.time()
        try:
            with self.write_lock():
                self._expire_previews()
        except Exception:
            # Housekeeping must never fail the request that happened to trigger it.
            LOG.warning("Expiring old previews failed", exc_info=True)

    def new_preview(self, project_id, source, options=None):
        self.writable_project(project_id)
        self._expire_previews_periodically()
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

    def public_preview(self, preview):
        result = copy.deepcopy(preview)
        result.pop("options", None)
        submitted = next((j for j in self.records("jobs", preview["project_id"]) if j.get("preview_id") == preview["id"]), None) if preview["status"] == "ready" else None
        result["submitted_import"] = {"id": submitted["id"], "status": submitted["status"]} if submitted else None
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
            submitted = next((j for j in self.records("jobs", project_id) if j.get("preview_id") == payload.get("preview_id")), None)
            if submitted:
                if submitted["selected"] == selected and submitted["manifest_hash"] == payload.get("manifest_hash"):
                    return submitted
                raise ImportProblem("preview_submitted", "This preview was already imported. Open its import to retry unfinished work, or create a new preview.", 409)
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
                    result["title"] = item.get("title")
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
        host = self._validated_host(payload.get("host"))
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
    def _validated_host(value):
        """A Langfuse base URL a browser typed: anything unparseable is an input error, never a server error."""
        from urllib.parse import urlsplit
        message = "Enter an HTTP(S) host without credentials, query parameters, or a fragment."
        if not isinstance(value, str) or len(value) > 2048:
            raise ImportProblem("invalid_host", message)
        host = value.strip().rstrip("/")
        if any(c.isspace() or ord(c) < 32 or ord(c) == 127 for c in host):
            raise ImportProblem("invalid_host", message)
        try:
            parsed = urlsplit(host)
            parsed.port  # raises for a non-numeric or out-of-range port
        except ValueError:
            raise ImportProblem("invalid_host", "Enter a valid host address. IPv6 addresses need brackets, and a port must be a number from 0 to 65535.") from None
        if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ImportProblem("invalid_host", message)
        return host

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

    def _connection_keys(self, project_id, connection_id):
        self.writable_project(project_id)
        connection = self.record("connections", connection_id, project_id)
        keys = self.secrets.get(connection_id)
        if not keys:
            keys = self.environment_keys(connection["host"])
        if not all(keys):
            raise ImportProblem("missing_credentials", "Connection needs keys after the AGR server restarted. Reconnect this connection to continue.")
        return connection, keys

    def _mark_connection(self, project_id, connection_id, status, must_exist=False, **details):
        with self.write_lock():
            path = self._path("connections", connection_id)
            # A disconnect wins over a request that was already in flight.
            if must_exist or path.exists():
                current = self.record("connections", connection_id, project_id)
                current.update(status=status, last_test_at=now(), **details)
                atomic_json(path, current)

    def test_connection(self, project_id, connection_id):
        from datetime import datetime, timedelta, timezone
        from . import langfuse_source
        connection, keys = self._connection_keys(project_id, connection_id)
        end = datetime.now(timezone.utc)
        # A fresh look at the server's version, so a Langfuse upgrade is picked up when access is retested.
        langfuse_source.detect(connection["host"], refresh=True)
        try:
            langfuse_source.list_traces(host=connection["host"], public_key=keys[0], secret_key=keys[1], from_time=(end - timedelta(hours=1)).isoformat(), to_time=end.isoformat(), limit=1)
        except ImportProblem as exc:
            if exc.code in ACCESS_FAILURES:
                self._mark_connection(project_id, connection_id, "access_failed")
            raise
        info = langfuse_source.detect(connection["host"])
        project = langfuse_source.project(connection["host"], keys[0], keys[1])
        self._mark_connection(project_id, connection_id, "accessible", must_exist=True, api=info["api"], api_label=langfuse_source.LABELS.get(info["api"]), server_version=info["server_version"], langfuse_project=project or connection.get("langfuse_project"))
        return self.record("connections", connection_id, project_id)

    def browse_connection(self, project_id, connection_id, payload):
        from .ingest_langfuse import run_id_for_trace
        from .langfuse_source import list_traces
        connection, keys = self._connection_keys(project_id, connection_id)
        filters = {k: payload.get(k) for k in ("name", "tag", "user_id", "session_id", "environment", "trace_id", "metadata_key", "metadata_value")}
        from_time, to_time = payload.get("from_time"), payload.get("to_time")
        if filters["trace_id"]:
            # Looking a trace up by its ID should not depend on when it ran, so the form's time range is replaced
            # by a long one. The range used is reported, and a preview fetches over the same range.
            from datetime import datetime, timedelta, timezone
            now_utc = datetime.now(timezone.utc)
            from_time, to_time = (now_utc - timedelta(days=ID_LOOKUP_DAYS)).isoformat(), (now_utc + timedelta(hours=1)).isoformat()
        try:
            result = list_traces(host=connection["host"], public_key=keys[0], secret_key=keys[1], from_time=from_time, to_time=to_time, limit=payload.get("limit", 50), **filters)
        except ImportProblem as exc:
            if exc.code in ACCESS_FAILURES:
                self._mark_connection(project_id, connection_id, "access_failed")
            raise
        self._mark_connection(project_id, connection_id, "accessible", must_exist=True)
        store = self.store(project_id)
        for trace in result["traces"]:
            try:
                trace["already_imported"] = bool(store.read_index(run_id_for_trace(trace["trace_id"])))
            except InvalidRunId:
                # SDK users choose trace IDs; one the store cannot hold is simply not imported yet.
                trace["already_imported"] = False
        result["searched"] = {"from_time": from_time, "to_time": to_time}
        return result

    @staticmethod
    def _extended_window_end(to_time):
        # A trace found by its root can still be running past the end of the search
        # window, so its observations are fetched a day later, never into the future.
        from datetime import datetime, timedelta, timezone
        try:
            end = datetime.fromisoformat(to_time.replace("Z", "+00:00"))
        except (AttributeError, ValueError):
            return to_time
        current = datetime.now(timezone.utc)
        if not end.tzinfo or end >= current:
            return to_time
        return min(end + timedelta(days=1), current).isoformat()

    def fetch_connection(self, *args, **kwargs):
        if not self.fetch_slots.acquire(blocking=False):
            raise ImportProblem("fetch_busy", "Other trace fetches are still running. Wait for them to finish, then try again.", 429)
        try:
            return self._fetch_connection(*args, **kwargs)
        finally:
            self.fetch_slots.release()

    def _fetch_connection(self, project_id, connection_id, trace_id=None, from_time=None, to_time=None, trace_ids=None):
        """Stage one preview from one trace ID (``trace_id``) or a browsed selection (``trace_ids``)."""
        from concurrent.futures import as_completed
        from .langfuse_source import detect, fetch_trace
        connection, keys = self._connection_keys(project_id, connection_id)
        browsed = trace_ids is not None
        if not browsed:
            trace_ids = [trace_id]
        elif not isinstance(trace_ids, list):
            raise ImportProblem("invalid_selection", "Select at least one trace.")
        if browsed:
            # A selection is validated per trace, so one unusable ID cannot fail the rest.
            candidates = list(dict.fromkeys(t for t in trace_ids if isinstance(t, str)))
            ids = [t for t in candidates if t == t.strip() and 0 < len(t) <= MAX_TRACE_ID]
            if not ids or len(trace_ids) > MAX_FETCH_TRACES:
                raise ImportProblem("invalid_selection", f"Select between 1 and {MAX_FETCH_TRACES} traces to preview at once.")
        else:
            ids = [bounded_text(trace_ids[0], "a trace ID", MAX_TRACE_ID)]
        window_to = self._extended_window_end(to_time) if browsed else to_time
        always_fatal = ("source_auth_failed", "source_redirect", "invalid_time_range")
        fetched, errors, staged_bytes, unreachable = {}, [], 0, 0
        for t in trace_ids if browsed else []:
            if t not in ids:
                errors.append({"trace_id": t if isinstance(t, str) else str(t)[:80], "code": "invalid_trace_id", "message": "This trace ID cannot be fetched.", "status": 422})
        halted = None
        deadline = time.monotonic() + MAX_BATCH_SECONDS
        # Queued fetches observe this flag instead of being cancelled: cancelling a future
        # after as_completed has started never wakes it, and the request would hang.
        stop = threading.Event()
        def one(identifier_):
            if stop.is_set() or time.monotonic() > deadline:
                return None
            try:
                result = fetch_trace(identifier_, host=connection["host"], public_key=keys[0], secret_key=keys[1], from_time=from_time, to_time=window_to, deadline=deadline)
                observations = result.get("observations") or []
                remote = connection.get("langfuse_project") or {}
                result["source_context"] = {"host": connection["host"], "project_id": (result.get("trace") or result).get("projectId") or next((o.get("projectId") for o in observations if o.get("projectId")), None) or remote.get("id")}
                return result
            except ImportProblem:
                raise
            except Exception:
                # Malformed or hostile JSON must fail one trace, never the batch; the cause is still logged.
                LOG.warning("Unexpected error while fetching a Langfuse trace", exc_info=True)
                raise ImportProblem("unsupported_response", "Langfuse returned a trace AGR cannot read. Import an export of it instead.", 502) from None
        def halt(code, message):
            nonlocal halted
            if not halted:
                halted = (code, message)
                stop.set()
        # Choose the API once, here: on a cold cache each worker would otherwise wait on the health endpoint
        # by itself, outside the batch deadline and the stop flag.
        detect(connection["host"])
        pool = ThreadPoolExecutor(max_workers=min(4, len(ids)))
        abandon = False
        try:
            futures = {pool.submit(one, t): t for t in ids}
            for future in as_completed(futures):
                tid = futures[future]
                try:
                    result = future.result()
                    if result is None:
                        if time.monotonic() > deadline:
                            halt("source_timeout", "Not fetched: the batch reached its time limit. Preview the remaining traces separately.")
                        continue
                    try:
                        content = json.dumps(result).encode()
                    except (ValueError, TypeError, RecursionError):
                        raise ImportProblem("unsupported_response", "Langfuse returned a trace AGR cannot read. Import an export of it instead.", 502) from None
                except ImportProblem as exc:
                    # An unreachable host is fatal only until something has been fetched; after that it is one trace's failure.
                    if exc.code in always_fatal or len(ids) == 1 or (exc.code == "source_unreachable" and not fetched):
                        abandon = True
                        stop.set()
                        raise
                    errors.append({"trace_id": tid, "code": exc.code, "message": str(exc), "status": exc.status})
                    if exc.code == "source_rate_limited":
                        halt("source_rate_limited", "Not fetched: Langfuse rate-limited the batch. Wait, then preview the remaining traces.")
                    if exc.code == "source_unreachable":
                        unreachable += 1
                        if unreachable >= 3:
                            halt("source_unreachable", "Not fetched: Langfuse stopped responding. Check connectivity, then preview the remaining traces.")
                    continue
                if len(content) > MAX_FILE:
                    errors.append({"trace_id": tid, "code": "import_limit", "message": "This trace is too large to import. Import a smaller export of it.", "status": 413})
                    continue
                staged_bytes += len(content)
                if staged_bytes > MAX_ARCHIVE:
                    errors.append({"trace_id": tid, "code": "import_limit", "message": "Skipped: this batch reached the size limit. Preview the remaining traces separately.", "status": 413})
                    halt("import_limit", "Not fetched: this batch reached the size limit. Preview the remaining traces separately.")
                    continue
                fetched[tid] = content
        except ImportProblem as exc:
            # A bad time range is the caller's input, not a sign the connection is unusable.
            if exc.code != "invalid_time_range":
                self._mark_connection(project_id, connection_id, "fetch_failed")
            raise exc
        finally:
            # A connection-level failure must not wait for every queued fetch; in-flight ones finish alone.
            stop.set()
            pool.shutdown(wait=not abandon)
        for tid in ids:
            if tid not in fetched and not any(e["trace_id"] == tid for e in errors):
                errors.append({"trace_id": tid, "code": halted[0] if halted else "not_fetched", "message": halted[1] if halted else "Not fetched.", "status": 413 if halted and halted[0] == "import_limit" else 502})
        if not fetched:
            # The outcome must not depend on which trace happened to finish first.
            rank = {code: n for n, code in enumerate(("source_auth_failed", "source_redirect", "source_unreachable", "source_rate_limited", "source_timeout", "source_fetch_failed", "unsupported_response", "import_limit", "invalid_trace_id", "source_not_found"))}
            first = min(errors, key=lambda e: rank.get(e["code"], len(rank)))
            self._mark_connection(project_id, connection_id, "fetch_failed")
            raise ImportProblem(first["code"], first["message"], first["status"])
        with self.write_lock():
            # A disconnect wins over a fetch that was already in flight.
            self.record("connections", connection_id, project_id)
        preview = self.new_preview(project_id, "langfuse")
        try:
            for position, tid in enumerate(ids, 1):
                if tid in fetched:
                    safe = re.sub(r"[^A-Za-z0-9._-]", "_", tid)[:80].strip(".") or "trace"
                    self.stage_file(project_id, preview["id"], f"{position:03d}-{safe}.json" if len(ids) > 1 else "trace.json", fetched[tid])
            if errors:
                with self.write_lock():
                    record = self.preview(project_id, preview["id"])
                    record["fetch_errors"] = [{k: v for k, v in e.items() if k != "status"} for e in errors]
                    atomic_json(self._path("previews", preview["id"]), record)
            result = self.inspect(project_id, preview["id"])
            # A disconnect wins, and then nothing staged for it may linger.
            self._mark_connection(project_id, connection_id, "trace_accessible", must_exist=True)
        except BaseException:
            self._discard_preview(preview["id"])
            raise
        return result

    def _discard_preview(self, preview_id):
        """Drop a preview that could not be completed; it must not linger as staging."""
        try:
            with self.write_lock():
                shutil.rmtree(self.root / "staging" / preview_id, ignore_errors=True)
                path = self._path("previews", preview_id)
                if path.exists():
                    record = read_json(path)
                    record.update(status="expired", items=[], files=[])
                    atomic_json(path, record)
        except Exception:
            pass

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
