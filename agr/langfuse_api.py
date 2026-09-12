"""Live fetch layer for the Langfuse adapter (agr/ingest_langfuse.py).

Deliberately a SEPARATE module: :func:`agr.ingest_langfuse.convert` stays a
pure, network-free function (fixture-testable, no side effects, safe to call
in a loop over a store) — the only thing in this whole adapter that ever
talks to the network is :func:`fetch_trace`, here. ``agr.cli``'s
``ingest-langfuse-api`` subcommand is the thin seam that wires the two
together: fetch, then ``LANGFUSE_ADAPTER.convert(fetched_dict, ...)``.

``fetch_trace`` calls ``GET {host}/api/public/traces/{trace_id}``, which
returns a FLAT ``TraceWithFullDetails`` object — the trace's own fields at
the top level, ``observations``/``scores`` nested inside as siblings of
``id`` (see ``agr/ingest_langfuse.py``'s module docstring for the full shape
discussion). That is exactly the shape :func:`agr.ingest_langfuse.convert`
now accepts directly (langfuse-adapter-0.2), so this module hands back a
plain dict with no adaptation needed.

IMPORTANT — this endpoint is DEPRECATED ON LANGFUSE CLOUD (scheduled removal
2026-11-16); Langfuse v4 steers cloud callers toward
``GET /api/public/v2/observations?fromStartTime=&toStartTime=`` (a
time-range query, not a by-id fetch). Self-hosted Langfuse is unaffected
until it upgrades to v4. This module still implements the by-id fetcher —
chosen deliberately here — because it works TODAY on both self-hosted and
cloud, and a single trace id is the natural unit for
``agr ingest-langfuse-api --trace-id ...``. Migrating to (or adding) the v2
endpoint is future work if/when the by-id endpoint is actually removed.

Standard library only (``urllib.request``): httpx is a test/dev extra of
this package, not a runtime dependency, and this fetcher must not become one
just to make one authenticated GET request.
"""

from __future__ import annotations

import base64
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

# Re-bound at module level (rather than called as ``urllib.request.urlopen``
# inline) purely so tests can monkeypatch ``agr.langfuse_api.urlopen``
# without needing to reach into the ``urllib.request`` module itself.
urlopen = urllib.request.urlopen

# Langfuse's own SDK/docs default for a freshly self-hosted instance. Used
# only when neither an explicit ``host`` argument nor ``$LANGFUSE_HOST`` is
# set — never a guess at a *project's* address, just the well-known default.
_DEFAULT_HOST = "http://localhost:3000"

# What GET /api/public/traces/{id} is asked to include. All five sections:
# this fetcher exists to feed the adapter, and the adapter wants everything
# the endpoint can give it (core trace fields, input/output, scores,
# observations, and derived metrics) in one round trip.
_DEFAULT_FIELDS = "core,io,scores,observations,metrics"


class LangfuseAPIError(RuntimeError):
    """A :func:`fetch_trace` call failed against the Langfuse public API.

    Kept distinct from ``ValueError`` (used for a misconfigured caller — a
    missing trace id or credentials) so callers can tell "you forgot to set
    something" apart from "the request went out and the server said no."
    """


def _resolve(explicit: Optional[str], env_var: str) -> Optional[str]:
    """Explicit argument wins; else the named environment variable; else None."""
    return explicit if explicit is not None else os.environ.get(env_var)


def fetch_trace(
    trace_id: str,
    *,
    host: Optional[str] = None,
    public_key: Optional[str] = None,
    secret_key: Optional[str] = None,
    fields: str = _DEFAULT_FIELDS,
) -> dict:
    """Fetch one Langfuse trace as a flat ``TraceWithFullDetails`` dict.

    Credentials and host resolve in this order: the explicit keyword
    argument, else the matching environment variable (``LANGFUSE_HOST``,
    ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY``) — never invented, and a
    missing piece raises ``ValueError`` naming exactly what's absent rather
    than sending a request that can only fail. ``host`` additionally falls
    back to the well-known self-hosted default when nothing at all is given.

    The secret key is used ONLY to build the Basic auth header for this one
    request; it is never logged, printed, or included in any exception
    message this function raises.

    Raises ``ValueError`` for a caller-side configuration problem (no trace
    id, no credentials) and ``LangfuseAPIError`` for anything the HTTP round
    trip itself reports (401/403 auth, 404 no such trace, any other status,
    unreachable host, or an unparseable/non-object response body).
    """
    if not trace_id:
        raise ValueError("fetch_trace: trace_id is required")

    resolved_host = _resolve(host, "LANGFUSE_HOST") or _DEFAULT_HOST
    pk = _resolve(public_key, "LANGFUSE_PUBLIC_KEY")
    sk = _resolve(secret_key, "LANGFUSE_SECRET_KEY")
    missing = []
    if not pk:
        missing.append("public key (--public-key or $LANGFUSE_PUBLIC_KEY)")
    if not sk:
        missing.append("secret key (--secret-key or $LANGFUSE_SECRET_KEY)")
    if missing:
        raise ValueError(f"fetch_trace: missing Langfuse credential(s): {', '.join(missing)}")

    # Trailing slash tolerated on the host (a common copy-paste artifact from
    # a browser address bar); the path is always appended fresh.
    base = resolved_host.rstrip("/")
    query = urllib.parse.urlencode({"fields": fields})
    url = f"{base}/api/public/traces/{urllib.parse.quote(str(trace_id), safe='')}?{query}"

    # HTTP Basic auth (base64 of "public_key:secret_key"), built by hand
    # rather than via urllib's HTTPPasswordMgr/HTTPBasicAuthHandler: one
    # visible line, easy to audit for exactly what goes into the header and
    # that nothing else does.
    token = base64.b64encode(f"{pk}:{sk}".encode("utf-8")).decode("ascii")
    request = urllib.request.Request(url, headers={"Authorization": f"Basic {token}"})

    try:
        with urlopen(request) as resp:
            body = resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise LangfuseAPIError(
                f"Langfuse API auth failed ({exc.code}) fetching trace {trace_id!r} from "
                f"{resolved_host}: check that the public/secret key pair is valid and "
                f"belongs to the project the trace lives in"
            ) from exc
        if exc.code == 404:
            raise LangfuseAPIError(
                f"no such Langfuse trace {trace_id!r} at {resolved_host} (check the trace "
                f"id and that --host points at the right project/instance)"
            ) from exc
        raise LangfuseAPIError(
            f"Langfuse API request failed ({exc.code} {exc.reason}) fetching trace "
            f"{trace_id!r} from {resolved_host}"
        ) from exc
    except urllib.error.URLError as exc:
        raise LangfuseAPIError(f"could not reach Langfuse at {resolved_host}: {exc.reason}") from exc

    try:
        data = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise LangfuseAPIError(
            f"Langfuse API returned unparseable JSON for trace {trace_id!r}: {exc}"
        ) from exc
    if not isinstance(data, dict):
        raise LangfuseAPIError(
            f"Langfuse API returned a non-object response for trace {trace_id!r}"
        )
    return data
