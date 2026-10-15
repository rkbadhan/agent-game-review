"""Trace listing and retrieval through the Langfuse v1 trace API, for Langfuse 3.x servers.

Langfuse 4 moved trace reads to observations v2 (``agr/langfuse_v2.py``) and removes the
v1 trace endpoints; a 3.x server only has these:

* ``GET /api/public/traces`` lists traces, filtered by the server and paged with ``page``.
* ``GET /api/public/traces/{id}`` returns one whole trace with its observations and scores.

The by-ID response is the flat trace-details shape the Langfuse adapter already accepts,
so a fetched trace needs no conversion. Requests go through ``langfuse_v2._get`` so both
APIs share its redirect refusal, size and time limits, and error mapping.
"""
import json
import time
import urllib.parse

from .imports import ImportProblem, MAX_SPANS
from . import langfuse_v2 as v2

PAGE_SIZE = 100


def _strings(value, limit=12):
    return [str(item)[:80] for item in value][:limit] if isinstance(value, list) else []


def list_traces(*, host, public_key, secret_key, from_time, to_time, name=None, tag=None, user_id=None, session_id=None, environment=None, limit=50, trace_id=None, metadata_key=None, metadata_value=None):
    """Find recent traces newest first, with every filter re-checked on the rows returned."""
    start, end, window, limit, wanted = v2.check_listing(from_time, to_time, limit, name, tag, user_id, session_id, environment, trace_id, metadata_key, metadata_value)
    params = {"fromTimestamp": start, "toTimestamp": end, "orderBy": "timestamp.desc", "fields": "core,io,metrics", "limit": PAGE_SIZE, "page": 1}
    for key, value in (("name", wanted["name"]), ("tags", wanted["tag"]), ("userId", wanted["user_id"]), ("sessionId", wanted["session_id"]), ("environment", wanted["environment"])):
        if value:
            params[key] = value
    if wanted["trace_id"] or wanted["metadata"]:
        # `filter` takes precedence over the plain parameters, so when it is needed it carries every condition.
        conditions = [{"type": "datetime", "column": "timestamp", "operator": ">=", "value": start}, {"type": "datetime", "column": "timestamp", "operator": "<", "value": end}]
        if wanted["name"]:
            conditions.append({"type": "string", "column": "name", "operator": "=", "value": wanted["name"]})
        if wanted["tag"]:
            conditions.append({"type": "arrayOptions", "column": "tags", "operator": "any of", "value": [wanted["tag"]]})
        if wanted["user_id"]:
            conditions.append({"type": "string", "column": "userId", "operator": "=", "value": wanted["user_id"]})
        if wanted["session_id"]:
            conditions.append({"type": "string", "column": "sessionId", "operator": "=", "value": wanted["session_id"]})
        if wanted["environment"]:
            conditions.append({"type": "stringOptions", "column": "environment", "operator": "any of", "value": [wanted["environment"]]})
        if wanted["trace_id"]:
            conditions.append({"type": "string", "column": "id", "operator": "contains", "value": wanted["trace_id"]})
        if wanted["metadata"]:
            conditions.append({"type": "stringObject", "column": "metadata", "key": wanted["metadata"][0], "operator": "=", "value": wanted["metadata"][1]})
        params["filter"] = json.dumps(conditions)
    authorization = v2._authorization(public_key, secret_key)
    found, scanned, pages, truncated, scan_capped = {}, 0, 0, False, False
    started = time.monotonic()
    while True:
        if time.monotonic() - started > 60 or pages >= 20:
            truncated = scan_capped = True
            break
        page = v2._get(host.rstrip("/") + "/api/public/traces?" + urllib.parse.urlencode(params), authorization)
        pages += 1
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise ImportProblem("unsupported_response", "Expected a paginated list of Langfuse traces.", 502)
        for row in page["data"]:
            if not isinstance(row, dict) or not isinstance(row.get("id"), str) or not row["id"]:
                continue
            scanned += 1
            if len(row["id"]) > v2.MAX_TRACE_ID or row["id"] != row["id"].strip():
                continue
            # The window and every filter are re-checked here; a field the response omits fails closed.
            stamp = v2._parse_time(row.get("timestamp"))
            if stamp is None or not window[0] <= stamp < window[1]:
                continue
            tags = row.get("tags") if isinstance(row.get("tags"), list) else None
            if (wanted["name"] and row.get("name") != wanted["name"]) or (wanted["tag"] and (tags is None or wanted["tag"] not in tags)) \
                    or (wanted["user_id"] and row.get("userId") != wanted["user_id"]) or (wanted["session_id"] and row.get("sessionId") != wanted["session_id"]) \
                    or (wanted["environment"] and row.get("environment") != wanted["environment"]) or (wanted["trace_id"] and wanted["trace_id"] not in row["id"]) or not v2.metadata_matches(row, wanted["metadata"]):
                continue
            if row["id"] in found:
                continue
            if len(found) >= limit:
                truncated = True
                continue
            found[row["id"]] = {"trace_id": row["id"], "name": str(row.get("name") or "Unnamed trace")[:200], "started_at": row["timestamp"], "user_id": row.get("userId"),
                                "session_id": row.get("sessionId"), "tags": _strings(tags), "environment": row.get("environment"), "level": None,
                                **v2.trace_extras(row, ("latency",), ("totalCost",))}
        meta = page.get("meta") if isinstance(page.get("meta"), dict) else {}
        total_pages = meta.get("totalPages")
        if len(found) >= limit:
            truncated = truncated or (isinstance(total_pages, int) and params["page"] < total_pages) or (not isinstance(total_pages, int) and len(page["data"]) >= PAGE_SIZE)
            break
        # The last page is the one the server says is last, or the first one that comes back short or empty.
        if not page["data"] or (isinstance(total_pages, int) and params["page"] >= total_pages) or (not isinstance(total_pages, int) and len(page["data"]) < PAGE_SIZE):
            break
        params["page"] += 1
    traces = sorted(found.values(), key=lambda t: v2._parse_time(t["started_at"]), reverse=True)
    return {"traces": traces, "truncated": truncated, "scan_capped": scan_capped, "scanned": scanned}


def fetch_trace(trace_id, *, host, public_key, secret_key, from_time=None, to_time=None, deadline=None):
    """Fetch one whole trace by ID. The v1 endpoint takes no time range, so the capture is complete."""
    authorization = v2._authorization(public_key, secret_key)
    url = host.rstrip("/") + "/api/public/traces/" + urllib.parse.quote(str(trace_id), safe="") + "?" + urllib.parse.urlencode({"fields": "core,io,scores,observations,metrics"})
    started = time.monotonic()
    v2._budget.end = started + 120 if deadline is None else min(started + 120, deadline)
    v2._budget.batch = deadline is not None and deadline < started + 120
    try:
        data = v2._get(url, authorization)
    except ImportProblem as exc:
        if getattr(exc, "http_status", None) != 404:
            raise
        # A 3.x server answers 404 for an ID it does not have. The endpoint flag stays so langfuse_source can still
        # try the other API, but a trace that is simply missing must not be described as a wrong host.
        missing = ImportProblem("source_not_found", "Langfuse has no trace with this ID in this project. Check the ID and that the keys belong to the project that holds it.", 502)
        missing.endpoint_missing, missing.http_status = True, 404
        raise missing from None
    finally:
        v2._budget.end = None
        v2._budget.batch = False
    if not isinstance(data, dict) or data.get("id") != trace_id:
        raise ImportProblem("unsupported_response", "Langfuse returned a different trace than the one requested.", 502)
    observations = data.get("observations") if isinstance(data.get("observations"), list) else []
    if not observations:
        raise ImportProblem("source_not_found", "This trace has no observations to import.", 404)
    if len(observations) > MAX_SPANS:
        raise ImportProblem("import_limit", "The trace exceeds the capture limits. Import a smaller export.", 413)
    if not isinstance(data.get("scores"), list):
        data["scores"] = []
    note = v2.recent_data_note(observations)
    data["import_warnings"] = [note] if note else []
    return data
