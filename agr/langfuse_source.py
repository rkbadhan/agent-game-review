"""Choose the Langfuse trace API a server supports, then list and fetch traces through it.

Langfuse 4 reads traces through observations v2 (``langfuse_v2``); Langfuse 3.x only has the
v1 trace endpoints (``langfuse_v1``). The server says which it is: the unauthenticated
``GET /api/public/health`` returns ``{"status": "OK", "version": "3.x.y"}``. Version 4 and
later use v2; anything earlier uses v1.

When the version cannot be read (an old proxy, a blocked health path) v2 is tried first. A
chosen API whose endpoint answers 404 is retried once on the other one, so a server whose
version does not match the endpoints it serves still works. If the retry also answers 404,
the first API's error is reported, so a trace that is simply missing is described as it was;
any other retry failure (bad keys, a rate limit, a timeout) is reported as it is.
"""
import re
import threading
import time

from .imports import ImportProblem
from . import langfuse_v1, langfuse_v2

V1, V2 = "v1", "v2"
LABELS = {V1: "v1 trace API", V2: "v2 observations API"}
_TTL = 600
_cache = {}
_lock = threading.Lock()


def clear_cache():
    with _lock:
        _cache.clear()


def _major(version):
    match = re.match(r"\s*v?(\d+)(?:\.|$)", version) if isinstance(version, str) else None
    return int(match.group(1)) if match else None


def detect(host, refresh=False):
    """Return ``{"api": "v1" | "v2" | None, "server_version": str | None}`` for a host, cached for ten minutes."""
    key = host.rstrip("/")
    now = time.monotonic()
    with _lock:
        cached = _cache.get(key)
        if cached and not refresh and cached[0] > now:
            return dict(cached[1])
    result = {"api": None, "server_version": None}
    try:
        # Health needs no credentials, so none are sent.
        health = langfuse_v2._get(key + "/api/public/health", None)
        version = health.get("version") if isinstance(health, dict) else None
        major = _major(version)
        if major is not None:
            result = {"api": V2 if major >= 4 else V1, "server_version": str(version)[:40]}
    except ImportProblem:
        pass
    with _lock:
        _cache[key] = (now + _TTL, result)
    return dict(result)


def _remember(host, api):
    with _lock:
        key = host.rstrip("/")
        cached = _cache.get(key)
        version = cached[1]["server_version"] if cached else None
        _cache[key] = (time.monotonic() + _TTL, {"api": api, "server_version": version})


def _apis(host):
    """The APIs to try, best first. An unknown version tries v2 and then v1."""
    preferred = detect(host)["api"] or V2
    return [preferred, V1 if preferred == V2 else V2]


def _call(host, operation, **kwargs):
    modules = {V1: langfuse_v1, V2: langfuse_v2}
    apis = _apis(host)
    try:
        return getattr(modules[apis[0]], operation)(host=host, **kwargs)
    except ImportProblem as first:
        # Only an endpoint the server does not have moves on; auth, rate limits and timeouts describe the
        # server, not the API choice, and a trace that is simply missing is reported as it was.
        if not getattr(first, "endpoint_missing", False):
            raise
        try:
            result = getattr(modules[apis[1]], operation)(host=host, **kwargs)
        except ImportProblem as second:
            # Both APIs missing the endpoint, or a trace that is simply absent, is described as the first answer.
            # Any other failure (bad keys, a rate limit, a timeout) is the real problem and is reported as it is.
            if getattr(second, "endpoint_missing", False):
                raise first from None
            raise
        _remember(host, apis[1])
        return result


def project(host, public_key, secret_key):
    """The Langfuse project a key pair belongs to, as ``{"id", "name"}``, or None when the server does not say.

    A key pair is bound to one project, so this is the project every listing and fetch on the connection reads.
    """
    try:
        data = langfuse_v2._get(host.rstrip("/") + "/api/public/projects", langfuse_v2._authorization(public_key, secret_key))
    except ImportProblem:
        return None
    rows = data.get("data") if isinstance(data, dict) else None
    row = rows[0] if isinstance(rows, list) and rows and isinstance(rows[0], dict) else None
    if not row or not isinstance(row.get("name"), str):
        return None
    return {"id": str(row.get("id") or "")[:80] or None, "name": row["name"][:120]}


def list_traces(*, host, **kwargs):
    return _call(host, "list_traces", **kwargs)


def fetch_trace(trace_id, *, host, **kwargs):
    return _call(host, "fetch_trace", trace_id=trace_id, **kwargs)
