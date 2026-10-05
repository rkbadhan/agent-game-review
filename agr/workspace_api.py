"""Optional HTTP transport for the local engineer workspace."""
from contextvars import ContextVar
import ipaddress
import secrets
from urllib.parse import urlsplit

from .imports import CATALOG, ImportProblem, MAX_ARCHIVE, MAX_FILE
from .workspace import Workspace
from .capture_view import PinnedStore
from .store import InvalidRunId


def loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


class ScopedStore:
    """Keep legacy routes intact while selecting their store per request."""
    def __init__(self, default):
        self.default = default
        self.current = ContextVar("agr_store", default=default)
        self.project = ContextVar("agr_project", default=None)

    def __getattr__(self, name):
        return getattr(self.current.get(), name)

    @property
    def _request_store(self):
        return self.current.get()


def attach_workspace(app, default_store, read_only, *, allowed_hosts=None, session_token=None, require_session=False):
    from fastapi import Body, Request
    from fastapi.responses import JSONResponse, FileResponse
    # FastAPI resolves postponed route annotations in module globals.
    globals()["Request"] = Request
    workspace = Workspace(default_store.root, read_only, default_store=default_store)
    store = ScopedStore(default_store)
    app.state.workspace = workspace
    # Exact host names only: trusting the request's own Host permits DNS rebinding.
    hosts = {"localhost", "127.0.0.1", "::1"}
    for host in allowed_hosts or ():
        host = host.lower().strip().strip("[]")
        if not host or any(c in host for c in "/:@*?#"):
            # IPv6 literal host names are also valid.
            try:
                ipaddress.IPv6Address(host)
            except ValueError:
                raise ValueError("allowed hosts must be exact host names or IP addresses") from None
        hosts.add(host)
        if not loopback(host):
            # A configured remote/proxy host can be rewritten to loopback by
            # its upstream transport; protect that transport with the token too.
            require_session = True
    session_token = session_token or secrets.token_urlsafe(32)
    app.state.workspace_session_token = session_token

    def failure(exc):
        return JSONResponse(status_code=exc.status, content={"detail": {"code": exc.code, "message": str(exc)}})

    @app.exception_handler(ImportProblem)
    async def import_problem(request, exc):
        return failure(exc)

    @app.middleware("http")
    async def scope_and_boundary(request, call_next):
        project = request.headers.get("x-agr-project")
        capture = request.headers.get("x-agr-capture")
        managed = request.url.path.startswith(("/projects", "/workspace", "/sources"))
        token = None
        project_token = store.project.set(project)
        try:
            if not read_only:
                try:
                    authority = urlsplit("//" + request.headers.get("host", ""))
                    host = authority.hostname
                    authority.port  # reject malformed ports, too
                    if authority.path or authority.query or authority.fragment or authority.username or authority.password:
                        host = None
                except ValueError:
                    host = None
                client_host = request.client.host if request.client else ""
                test_transport = client_host == "testclient" and "http.response.debug" in request.scope.get("extensions", {})
                in_process = test_transport and host == "testserver"
                if host not in hosts and not in_process:
                    raise ImportProblem("invalid_host", "This host is not allowed. Configure --allowed-host for this server.", 403)
                # The debug transport is internal to Starlette's TestClient;
                # neither Host nor forwarded client headers can enable it.
                remote = not loopback(client_host) and not test_transport
                protected = require_session or remote or (not loopback(host) and not in_process)
                public_shell = request.url.path == "/" or request.url.path.startswith("/static/")
                if protected and not public_shell:
                    supplied = request.headers.get("authorization", "")
                    if not secrets.compare_digest(supplied.encode(), ("Bearer " + session_token).encode()):
                        raise ImportProblem("session_required", "Open the session link printed by agr serve to access this workspace.", 401)
            if managed or request.method not in ("GET", "HEAD"):
                origin = request.headers.get("origin")
                if request.method not in ("GET", "HEAD") and origin:
                    def origin_key(parsed):
                        return (parsed.scheme, parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80))
                    try:
                        incoming = urlsplit(origin)
                        current = urlsplit(str(request.url))
                        same_origin = origin_key(incoming) == origin_key(current)
                    except ValueError:
                        same_origin = False
                    # Browsers attest same-origin requests even when a reverse
                    # proxy rewrites the upstream Host/scheme. Scripts cannot
                    # set Sec-Fetch-Site to forge this from another origin.
                    if not same_origin and request.headers.get("sec-fetch-site") != "same-origin":
                        raise ImportProblem("invalid_origin", "Open the AGR app on this server to make changes.", 403)
            if read_only and request.method not in ("GET", "HEAD"):
                raise ImportProblem("read_only", "Sample data is read-only.", 403)
            if project:
                chosen = workspace.project(project)
                if request.method not in ("GET", "HEAD") and (chosen.get("sample") or chosen.get("archived")):
                    raise ImportProblem("read_only", "Choose an active personal project to make changes.", 403)
                token = store.current.set(workspace.store(project))
            if capture:
                if request.method not in ("GET", "HEAD"):
                    raise ImportProblem("snapshot_read_only", "Saved evidence snapshots are read-only. Open the latest run to make review changes.", 403)
                # Resolve the full route run_id, which can contain slashes.
                from starlette.routing import Match
                for route in app.router.routes:
                    match, scope = route.matches(request.scope)
                    if match == Match.FULL:
                        run_id = scope.get("path_params", {}).get("run_id")
                        if run_id:
                            selected_store = workspace.store(project) if project else default_store
                            pinned = PinnedStore(selected_store, run_id, capture)
                            if token is not None:
                                store.current.reset(token)
                            token = store.current.set(pinned)
                        break
            return await call_next(request)
        except ImportProblem as exc:
            return failure(exc)
        except InvalidRunId:
            return JSONResponse(status_code=404, content={"detail": "run not found"})
        finally:
            store.project.reset(project_token)
            if token is not None:
                store.current.reset(token)

    @app.get("/workspace")
    def settings():
        return workspace.settings()

    @app.patch("/workspace")
    def update_settings(payload: dict = Body(...)):
        return workspace.update_settings(payload)

    @app.get("/sources/catalog")
    def source_catalog():
        return {"sources": CATALOG, "limits": {"file_bytes": MAX_FILE, "archive_bytes": MAX_ARCHIVE, "runs": 1000}}

    @app.get("/projects")
    def projects():
        return {"projects": workspace.settings()["projects"]}

    @app.post("/projects")
    def create_project(payload: dict = Body(...)):
        return workspace.create_project(payload.get("name"))

    @app.post("/projects/sample")
    def sample_project():
        return workspace.create_project("Sample data", sample=True)

    @app.get("/projects/{project_id}")
    def project_detail(project_id: str):
        from .read import list_runs
        project = dict(workspace.project(project_id))
        project["runs"] = list_runs(workspace.store(project_id))
        project["imports"] = workspace.records("jobs", project_id)[:10]
        project["investigations"] = workspace.records("investigations", project_id)[:10]
        if not read_only:
            project["store_path"] = workspace.store(project_id).root
        return project

    @app.patch("/projects/{project_id}")
    def update_project(project_id: str, payload: dict = Body(...)):
        return workspace.update_project(project_id, payload)

    @app.post("/projects/{project_id}/import-previews")
    def new_preview(project_id: str, payload: dict = Body(...)):
        return workspace.new_preview(project_id, payload.get("source"), payload.get("options"))

    @app.put("/projects/{project_id}/import-previews/{preview_id}/files")
    async def stage_file(project_id: str, preview_id: str, name: str, request: Request):
        limit = MAX_ARCHIVE if name.lower().endswith(".zip") else MAX_FILE
        content = bytearray()
        async for chunk in request.stream():
            if len(content) + len(chunk) > limit:
                raise ImportProblem("import_limit", "This file exceeds the upload limit.", 413)
            content.extend(chunk)
        return workspace.stage_file(project_id, preview_id, name, content)

    @app.post("/projects/{project_id}/import-previews/{preview_id}/inspect")
    def inspect(project_id: str, preview_id: str):
        return workspace.inspect(project_id, preview_id)

    @app.get("/projects/{project_id}/import-previews/{preview_id}")
    def preview(project_id: str, preview_id: str):
        return workspace.public_preview(workspace.preview(project_id, preview_id))

    @app.post("/projects/{project_id}/imports")
    def submit(project_id: str, payload: dict = Body(...)):
        return workspace.submit(project_id, payload)

    @app.get("/projects/{project_id}/imports")
    def imports(project_id: str):
        return {"imports": workspace.records("jobs", project_id)}

    @app.get("/projects/{project_id}/imports/{import_id}")
    def job(project_id: str, import_id: str):
        return workspace.record("jobs", import_id, project_id)

    @app.get("/projects/{project_id}/imports/{import_id}/originals")
    def original_artifact(project_id: str, import_id: str, name: str):
        job = workspace.record("jobs", import_id, project_id)
        preview = workspace.preview(project_id, job["preview_id"])
        if name not in {f["name"] for f in preview["files"]}:
            raise ImportProblem("not_found", "This file is not part of the selected import.", 404)
        from .imports import safe_relative
        path = workspace.root / "staging" / preview["id"] / "files" / safe_relative(name)
        if not path.is_file():
            raise ImportProblem("not_found", "This original input file is no longer available.", 404)
        return FileResponse(path, media_type="application/octet-stream", filename=path.name)

    @app.post("/projects/{project_id}/imports/{import_id}/cancel")
    def cancel(project_id: str, import_id: str):
        return workspace.cancel(project_id, import_id)

    @app.post("/projects/{project_id}/imports/{import_id}/retry")
    def retry(project_id: str, import_id: str):
        return workspace.retry(project_id, import_id)

    @app.get("/projects/{project_id}/connections")
    def connections(project_id: str):
        return {"connections": workspace.records("connections", project_id)}

    @app.post("/projects/{project_id}/connections")
    def save_connection(project_id: str, payload: dict = Body(...)):
        return workspace.save_connection(project_id, payload)

    @app.post("/projects/{project_id}/connections/{connection_id}/trace-previews")
    def fetch_trace(project_id: str, connection_id: str, payload: dict = Body(...)):
        return workspace.fetch_connection(project_id, connection_id, payload.get("trace_id"), payload.get("from_time"), payload.get("to_time"))

    @app.delete("/projects/{project_id}/connections/{connection_id}")
    def disconnect(project_id: str, connection_id: str):
        return workspace.disconnect(project_id, connection_id)

    @app.get("/projects/{project_id}/investigations")
    def investigations(project_id: str):
        return {"investigations": workspace.records("investigations", project_id)}

    @app.post("/projects/{project_id}/investigations")
    def save_investigation(project_id: str, payload: dict = Body(...)):
        return workspace.save_investigation(project_id, payload)

    @app.patch("/projects/{project_id}/investigations/{investigation_id}")
    def update_investigation(project_id: str, investigation_id: str, payload: dict = Body(...)):
        return workspace.save_investigation(project_id, payload, investigation_id)

    return workspace, store
