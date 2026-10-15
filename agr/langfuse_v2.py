"""Bounded single-trace retrieval using Langfuse observations v2.

Contract: https://langfuse.com/docs/api-and-data-platform/features/public-api
Legacy v3 instances can still use the existing explicit legacy fetcher.
"""
import base64
import http.client
from datetime import datetime, timedelta, timezone
import json
import re
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from .imports import ImportProblem, MAX_FILE, MAX_SPANS


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ImportProblem("source_redirect", "The host redirected the request. Configure its final base URL instead.", 502)


# The calling thread's absolute time budget (a time.monotonic() value), set by fetch_trace so a
# batch deadline also bounds each request, not only the gaps between them.
_budget = threading.local()


def _timed_out(message="Langfuse took too long to send this response. Try a smaller range or import an export."):
    return ImportProblem("source_timeout", message, 504)


BATCH_LIMIT_MESSAGE = "The batch reached its time limit while this trace was being fetched. Preview it again separately."


def _get(url, authorization):
    headers = {"Accept": "application/json"}
    if authorization:
        headers["Authorization"] = authorization
    request = urllib.request.Request(url, headers=headers)
    started = time.monotonic()
    end = started + _REQUEST_SECONDS
    cap = getattr(_budget, "end", None)
    if cap is not None:
        end = min(end, cap)
    # When the batch deadline, not the per-request limit, ends the wait, say so.
    by_batch = cap is not None and getattr(_budget, "batch", False) and cap <= started + _REQUEST_SECONDS
    if end <= started:
        raise _timed_out(BATCH_LIMIT_MESSAGE if by_batch else "The time limit was reached before this request could start.")
    try:
        # Each socket wait is bounded by what is left of the budget; the checks below bound the total.
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=min(60, max(0.05, end - started))) as response:
            if time.monotonic() > end:
                raise _timed_out(BATCH_LIMIT_MESSAGE) if by_batch else _timed_out()
            # read1 returns what has arrived; read(n) would block until n bytes or the end,
            # so the deadline could not be checked while a server dribbles a body out.
            read = getattr(response, "read1", response.read)
            chunks, size = [], 0
            while True:
                chunk = read(65536)
                if not chunk:
                    break
                chunks.append(chunk)
                size += len(chunk)
                if size > MAX_FILE:
                    raise ImportProblem("import_limit", "The trace response is too large. Export a smaller capture.", 413)
                if time.monotonic() > end:
                    raise _timed_out(BATCH_LIMIT_MESSAGE) if by_batch else _timed_out()
            # read1 reports a connection cut short as an empty read rather than an error.
            if (getattr(response, "length", None) or 0) > 0 or (getattr(response, "chunked", False) and (getattr(response, "chunk_left", None) or 0) > 0):
                raise ImportProblem("source_unreachable", "The connection closed before the whole response arrived. Try again.", 502)
        return json.loads(b"".join(chunks))
    except ImportProblem:
        raise
    except urllib.error.HTTPError as exc:
        code, message = {
            401: ("source_auth_failed", "Authentication failed. Check the public and secret keys for this project."),
            403: ("source_auth_failed", "These keys do not have access to this project."),
            404: ("source_not_found", "This endpoint was not found. Check the host and selected Langfuse API version."),
            429: ("source_rate_limited", "Langfuse rate-limited this request. Wait and try again."),
        }.get(exc.code, ("source_fetch_failed", "Langfuse could not return the requested trace. Try again or import an export."))
        problem = ImportProblem(code, message, 502)
        # A 404 can mean the server lacks this API version; langfuse_source retries the other one.
        problem.endpoint_missing = exc.code == 404
        problem.http_status = exc.code
        raise problem from None
    except (TimeoutError, socket.timeout):
        raise _timed_out("Langfuse did not answer in time. Try a smaller time range or retry.") from None
    except urllib.error.URLError:
        # urllib wraps connect, TLS and send failures, timeouts included, in URLError. A host that never
        # completes the connection is unreachable; only a bare timeout while waiting for the answer is a slow query.
        raise ImportProblem("source_unreachable", "The host could not be reached. Check its address and connectivity.", 502) from None
    except (OSError, http.client.HTTPException):
        raise ImportProblem("source_unreachable", "The host could not be reached or timed out. Check its address and connectivity.", 502) from None
    except (ValueError, UnicodeError, RecursionError):
        raise ImportProblem("unsupported_response", "Langfuse returned an unsupported response. Check the host and API version.", 502) from None


def _parse_io(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except (ValueError, RecursionError):
            pass
    return value


def _iso_z(value):
    """The form Langfuse documents: UTC with milliseconds and a Z suffix."""
    value = value.astimezone(timezone.utc)
    return value.strftime("%Y-%m-%dT%H:%M:%S.") + f"{value.microsecond // 1000:03d}Z"


def _window(from_time, to_time):
    try:
        start = datetime.fromisoformat(from_time.replace("Z", "+00:00"))
        end = datetime.fromisoformat(to_time.replace("Z", "+00:00"))
        if not start.tzinfo or not end.tzinfo or start >= end:
            raise ValueError()
        return _iso_z(start), _iso_z(end)
    except (ValueError, AttributeError, OverflowError):
        raise ImportProblem("invalid_time_range", "Choose a valid start and end time with a timezone.") from None


def _authorization(public_key, secret_key):
    return "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()


MAX_LISTED = 500
MAX_TRACE_ID = 512
_REQUEST_SECONDS = 90
RECENT_SECONDS = 15 * 60


_TIMESTAMP = re.compile(r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}:\d{2})(?:[.,](\d+))?(Z|[+-]\d{2}(?::?\d{2})?)?")


def _parse_time(value):
    """Parse a Langfuse timestamp the same way on every supported Python (3.10 only takes 3 or 6 fraction digits)."""
    if not isinstance(value, str):
        return None
    match = _TIMESTAMP.fullmatch(value.strip())
    if not match:
        return None
    day, clock, fraction, zone = match.groups()
    if not zone or zone == "Z":
        zone = "+00:00"
    elif len(zone) == 3:
        zone += ":00"
    elif ":" not in zone:
        zone = zone[:3] + ":" + zone[3:]
    try:
        return datetime.fromisoformat(f"{day}T{clock}.{(fraction or '')[:6].ljust(6, '0')}{zone}")
    except ValueError:
        return None


def recent_data_note(observations):
    latest = max((t for t in (_parse_time(o.get("startTime")) for o in observations if isinstance(o, dict)) if t), default=None)
    if latest and (datetime.now(timezone.utc) - latest).total_seconds() < RECENT_SECONDS:
        return "This trace is very recent. Langfuse can take up to 15 minutes to finish data from older SDKs and exporters, so the capture may be incomplete. Import it again later to capture the rest."
    return None


def preview_text(value, limit=160):
    """A short single-line look at an input, output or metadata value of any shape."""
    if value is None:
        return None
    text = value if isinstance(value, str) else json.dumps(value, default=str, ensure_ascii=False, separators=(", ", ": "))
    text = " ".join(text.split())
    return text if len(text) <= limit else text[:limit - 1] + "\u2026"


def metadata_summary(value, keys=20):
    """Metadata as a small flat dict of short strings; anything else is no metadata."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return {}
    if not isinstance(value, dict):
        return {}
    return {str(k)[:60]: preview_text(v, 120) or "" for k, v in list(value.items())[:keys]}


def _number(*values):
    for value in values:
        # Langfuse answers -1 for latency and cost it was not asked to compute, so a negative is "unknown".
        if isinstance(value, (int, float)) and not isinstance(value, bool) and value == value and abs(value) != float("inf") and value >= 0:
            return value
    return None


def trace_extras(row, latency_keys, cost_keys):
    """What Langfuse's own trace table shows beside the ID: release, version, latency, cost, input, output, metadata."""
    return {"release": row.get("release") if isinstance(row.get("release"), str) else None, "version": row.get("version") if isinstance(row.get("version"), str) else None,
            "latency_s": _number(*[row.get(k) for k in latency_keys]), "cost": _number(*[row.get(k) for k in cost_keys]),
            "input_preview": preview_text(row.get("input")), "output_preview": preview_text(row.get("output")), "metadata": metadata_summary(row.get("metadata"))}


def metadata_matches(row, wanted):
    """A metadata filter fails closed: the row must carry that key with exactly that value.

    Observations v2 truncates metadata values over 200 characters unless asked not to, so a longer value cannot be
    confirmed and never matches; a short one is compared in full, not by its display preview.
    """
    if not wanted:
        return True
    value = row.get("metadata")
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (ValueError, RecursionError):
            return False
    if not isinstance(value, dict) or wanted[0] not in value:
        return False
    found = value[wanted[0]]
    return (found if isinstance(found, str) else json.dumps(found, default=str, ensure_ascii=False)) == wanted[1] or str(found) == wanted[1]


def check_listing(from_time, to_time, limit, name, tag, user_id, session_id, environment, trace_id=None, metadata_key=None, metadata_value=None):
    """Validate a trace search the same way for every Langfuse API version."""
    start, end = _window(from_time, to_time)
    try:
        limit = int(limit)
    except (TypeError, ValueError, OverflowError):
        raise ImportProblem("invalid_limit", "Choose how many traces to list.") from None
    if not 1 <= limit <= MAX_LISTED:
        raise ImportProblem("invalid_limit", f"List between 1 and {MAX_LISTED} traces at a time.")
    wanted = {"name": name or None, "tag": tag or None, "user_id": user_id or None, "session_id": session_id or None}
    for label, value in wanted.items():
        if value is not None and (not isinstance(value, str) or len(value) > 256):
            raise ImportProblem("invalid_filter", "Filters must be short text values.")
    if environment is not None and (not isinstance(environment, str) or len(environment) > 256):
        raise ImportProblem("invalid_filter", "Filters must be short text values.")
    wanted["environment"] = environment or None
    for value in (trace_id, metadata_key, metadata_value):
        if value is not None and (not isinstance(value, str) or len(value) > 256):
            raise ImportProblem("invalid_filter", "Filters must be short text values.")
    if bool(metadata_key) != bool(metadata_value):
        raise ImportProblem("invalid_filter", "Filter by metadata with both a key and a value.")
    wanted["trace_id"] = trace_id or None
    wanted["metadata"] = (metadata_key, metadata_value) if metadata_key else None
    return start, end, (_parse_time(start), _parse_time(end)), limit, wanted


METRICS_ID_BATCH = 50  # Keeps the query string short; the ID filter also bounds what the server groups.
METRICS_SECONDS = 30  # For all the cost requests of one listing together, each request included.


def trace_costs(host, authorization, trace_ids, start, end):
    """Whole-trace cost for listed traces, ``{trace_id: usd}``, from the metrics v2 API (cost summed over every observation).

    The observation rows this listing is built from carry only the root observation's cost. Any failure here (the
    endpoint is off on this server, a rate limit, an unexpected answer) just leaves the root cost in place.
    """
    until = _iso_z(min(datetime.now(timezone.utc), (_parse_time(end) or datetime.now(timezone.utc)) + timedelta(days=1)))
    costs, started = {}, time.monotonic()
    # The one budget covers every request, a slow one included: _get stops waiting when it runs out.
    _budget.end, _budget.batch = started + METRICS_SECONDS, False
    try:
        for offset in range(0, len(trace_ids), METRICS_ID_BATCH):
            ids = trace_ids[offset:offset + METRICS_ID_BATCH]
            # A trace ID is a high-cardinality dimension, which the server only groups by when the query is a top-N:
            # an explicit row limit and a descending order on a measure (here the summed cost, "{aggregation}_{measure}").
            query = {"view": "observations", "dimensions": [{"field": "traceId"}], "metrics": [{"measure": "totalCost", "aggregation": "sum"}],
                     "filters": [{"column": "traceId", "operator": "any of", "type": "stringOptions", "value": ids}],
                     "orderBy": [{"field": "sum_totalCost", "direction": "desc"}],
                     "fromTimestamp": start, "toTimestamp": until, "config": {"row_limit": 1000}}
            try:
                data = _get(host.rstrip("/") + "/api/public/v2/metrics?" + urllib.parse.urlencode({"query": json.dumps(query)}), authorization)
            except ImportProblem:
                break
            rows = data.get("data") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                break
            for row in rows:
                if not isinstance(row, dict) or row.get("traceId") not in ids:
                    continue
                value = row.get("sum_totalCost")
                if isinstance(value, str):
                    try:
                        value = float(value)
                    except ValueError:
                        continue
                cost = _number(value)
                if cost is not None:
                    costs[row["traceId"]] = cost
    finally:
        _budget.end, _budget.batch = None, False
    return costs


def list_traces(*, host, public_key, secret_key, from_time, to_time, name=None, tag=None, user_id=None, session_id=None, environment=None, limit=50, trace_id=None, metadata_key=None, metadata_value=None):
    """Find recent traces by their root observations, newest first.

    Langfuse v4 has no trace-listing endpoint: observations v2 returns rows, so
    candidates are grouped by trace ID on the client. Every filter is also
    re-checked locally on the fields the response carries, so an ignored or
    renamed server-side filter cannot widen the selection the engineer sees.
    """
    start, end, window, limit, wanted = check_listing(from_time, to_time, limit, name, tag, user_id, session_id, environment, trace_id, metadata_key, metadata_value)
    # io and metadata make rows large, so pages are smaller than a bare listing would need.
    params = {"fromStartTime": start, "toStartTime": end, "isRootObservation": "true", "fields": "core,basic,io,metadata,usage,metrics,trace_context", "limit": min(250, max(100, limit * 2))}
    advanced = []
    if wanted["name"]:
        advanced.append({"type": "string", "column": "traceName", "operator": "=", "value": wanted["name"]})
    if wanted["tag"]:
        advanced.append({"type": "arrayOptions", "column": "tags", "operator": "any of", "value": [wanted["tag"]]})
    if wanted["user_id"]:
        params["userId"] = wanted["user_id"]
        advanced.append({"type": "string", "column": "userId", "operator": "=", "value": wanted["user_id"]})
    if wanted["session_id"]:
        params["sessionId"] = wanted["session_id"]
        advanced.append({"type": "string", "column": "sessionId", "operator": "=", "value": wanted["session_id"]})
    if wanted["trace_id"]:
        advanced.append({"type": "string", "column": "traceId", "operator": "contains", "value": wanted["trace_id"]})
    if wanted["metadata"]:
        advanced.append({"type": "stringObject", "column": "metadata", "key": wanted["metadata"][0], "operator": "=", "value": wanted["metadata"][1]})
    if environment:
        params["environment"] = environment
    if advanced:
        # `filter` takes precedence over the fixed parameters, so everything that must hold goes inside it.
        advanced += [{"type": "datetime", "column": "startTime", "operator": ">=", "value": start}, {"type": "datetime", "column": "startTime", "operator": "<", "value": end},
                     {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True}]
        if environment:
            advanced.append({"type": "stringOptions", "column": "environment", "operator": "any of", "value": [environment]})
        params["filter"] = json.dumps(advanced)
    authorization = _authorization(public_key, secret_key)
    found, cursors, scanned, pages, truncated, scan_capped = {}, set(), 0, 0, False, False
    started = time.monotonic()
    while True:
        if time.monotonic() - started > 60 or pages >= 20:
            truncated = scan_capped = True
            break
        try:
            page = _get(host.rstrip("/") + "/api/public/v2/observations?" + urllib.parse.urlencode(params), authorization)
        except ImportProblem as exc:
            # Langfuse's docs disagree on whether isRootObservation is a parameter and filter column everywhere. A server
            # that rejects the very first request (400) is asked again without it, and non-root rows are skipped locally.
            if pages or getattr(exc, "http_status", None) != 400 or not (params.pop("isRootObservation", None) or "filter" in params):
                raise
            if "filter" in params:
                params["filter"] = json.dumps([c for c in json.loads(params["filter"]) if c.get("column") != "isRootObservation"])
            page = _get(host.rstrip("/") + "/api/public/v2/observations?" + urllib.parse.urlencode(params), authorization)
        pages += 1
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise ImportProblem("unsupported_response", "Expected paginated Langfuse observation records.", 502)
        for row in page["data"]:
            if not isinstance(row, dict) or not isinstance(row.get("traceId"), str) or not row["traceId"]:
                continue
            scanned += 1
            # Previews address a trace by exact, bounded ID; one that cannot be fetched later is not offered.
            if len(row["traceId"]) > MAX_TRACE_ID or row["traceId"] != row["traceId"].strip():
                continue
            # The server's logical root flag decides: an observation the SDK marked as an app root keeps its parent ID
            # and is still a root. Only a row that does not say falls back to "has no parent".
            root = row.get("isRootObservation")
            if root is False or (root is None and row.get("parentObservationId")):
                continue
            # The window is re-checked locally; a row outside it, or without a readable start, is never offered.
            started_at = _parse_time(row.get("startTime"))
            if started_at is None or not window[0] <= started_at < window[1]:
                continue
            tags = row.get("tags") if isinstance(row.get("tags"), list) else None
            # A filtered field the response does not carry fails closed: an ignored
            # server-side filter must never widen what the engineer sees.
            if (wanted["name"] and row.get("traceName") != wanted["name"]) or (wanted["tag"] and (tags is None or wanted["tag"] not in tags)) \
                    or (wanted["user_id"] and row.get("userId") != wanted["user_id"]) or (wanted["session_id"] and row.get("sessionId") != wanted["session_id"]) \
                    or (environment and row.get("environment") != environment) or (wanted["trace_id"] and wanted["trace_id"] not in row["traceId"]) or not metadata_matches(row, wanted["metadata"]):
                continue
            if row["traceId"] in found:
                continue
            if len(found) >= limit:
                truncated = True
                continue
            found[row["traceId"]] = {"trace_id": row["traceId"], "name": str(row.get("traceName") or row.get("name") or "Unnamed trace")[:200], "started_at": row.get("startTime") if isinstance(row.get("startTime"), str) else None,
                                     "user_id": row.get("userId"), "session_id": row.get("sessionId"), "tags": [str(t)[:80] for t in (tags or [])][:12], "environment": row.get("environment"), "level": row.get("level"),
                                     **trace_extras(row, ("latency",), ("totalCost", "calculatedTotalCost", "cost"))}
        meta = page.get("meta") or {}
        if not isinstance(meta, dict) or (meta.get("cursor") is not None and not isinstance(meta["cursor"], str)):
            raise ImportProblem("unsupported_response", "Unsupported observation pagination metadata.", 502)
        cursor = meta.get("cursor")
        if len(found) >= limit:
            # Stop paging; more may exist beyond this page.
            truncated = truncated or bool(cursor)
            break
        if not cursor:
            break
        if cursor in cursors:
            raise ImportProblem("unsupported_response", "The source repeated a pagination cursor; the list may be incomplete.", 502)
        cursors.add(cursor)
        params["cursor"] = cursor
    if found:
        costs = trace_costs(host, authorization, list(found), start, end)
        for trace_id, trace in found.items():
            if trace_id in costs:
                trace["cost"], trace["cost_scope"] = costs[trace_id], "trace"
            else:
                trace["cost_scope"] = "root"
    # Timestamps are compared as instants, not text: "10:00:00Z" and "10:00:00.900Z" or mixed offsets do not sort as strings.
    traces = sorted(found.values(), key=lambda t: _parse_time(t["started_at"]), reverse=True)
    return {"traces": traces, "truncated": truncated, "scan_capped": scan_capped, "scanned": scanned}


def fetch_trace(trace_id, *, host, public_key, secret_key, from_time, to_time, deadline=None):
    try:
        return _fetch_trace(trace_id, host=host, public_key=public_key, secret_key=secret_key, from_time=from_time, to_time=to_time, deadline=deadline)
    finally:
        _budget.end = None
        _budget.batch = False


def _fetch_trace(trace_id, *, host, public_key, secret_key, from_time, to_time, deadline=None):
    start_iso, end_iso = _window(from_time, to_time)
    authorization = _authorization(public_key, secret_key)
    params = {"traceId": trace_id, "fromStartTime": start_iso, "toStartTime": end_iso, "fields": "core,basic,io,model,usage,trace_context", "limit": 1000}
    observations, cursors, size = [], set(), 0
    started = time.monotonic()
    hard_end = started + 120 if deadline is None else min(started + 120, deadline)
    batch_limited = deadline is not None and deadline < started + 120
    _budget.end = hard_end
    _budget.batch = batch_limited
    while True:
        if time.monotonic() > hard_end:
            raise ImportProblem("source_timeout", "The batch reached its time limit before this trace was fetched. Preview it again separately." if batch_limited else "Trace retrieval exceeded two minutes. Use a smaller time range or an export.", 504)
        page = _get(host.rstrip("/") + "/api/public/v2/observations?" + urllib.parse.urlencode(params), authorization)
        if not isinstance(page, dict) or not isinstance(page.get("data"), list):
            raise ImportProblem("unsupported_response", "Expected paginated Langfuse observation records.", 502)
        for observation in page["data"]:
            if not isinstance(observation, dict) or observation.get("traceId") != trace_id or not observation.get("id"):
                raise ImportProblem("unsupported_response", "The response contains missing IDs or another trace.", 502)
            observations.append(observation)
        size += len(json.dumps(page).encode())
        if len(observations) > MAX_SPANS or size > MAX_FILE:
            raise ImportProblem("import_limit", "The trace exceeds the capture limits. Import a smaller export.", 413)
        meta = page.get("meta") or {}
        if not isinstance(meta, dict) or (meta.get("cursor") is not None and not isinstance(meta["cursor"], str)):
            raise ImportProblem("unsupported_response", "Unsupported observation pagination metadata.", 502)
        cursor = meta.get("cursor")
        if not cursor:
            break
        if cursor in cursors:
            raise ImportProblem("unsupported_response", "The source repeated a pagination cursor; no partial trace was imported.", 502)
        cursors.add(cursor)
        params["cursor"] = cursor
    if not observations:
        raise ImportProblem("source_not_found", "No observations for this trace were found in that time range. Check the ID and expand the range.", 404)
    ids = {o["id"] for o in observations}
    if len(ids) != len(observations):
        raise ImportProblem("unsupported_response", "The response repeated observation IDs.", 502)
    roots = [o for o in observations if not o.get("parentObservationId")]
    logical = [o for o in observations if o.get("isRootObservation")]
    root = (logical or roots or observations)[0]
    complete_root = len(logical or roots) == 1
    for observation in observations:
        for key in ("input", "output"):
            if key in observation:
                observation[key] = _parse_io(observation[key])
        usage = observation.get("usageDetails") or {}
        if not usage and (observation.get("inputUsage") is not None or observation.get("outputUsage") is not None):
            observation["usage"] = {"input": observation.get("inputUsage"), "output": observation.get("outputUsage")}
    # The wrapper is a structural source boundary, not a fabricated verifier.
    result = {"trace": {"id": trace_id, "name": root.get("traceName") or root.get("name") or "Langfuse trace", "timestamp": min(str(o.get("startTime") or "") for o in observations), "sessionId": root.get("sessionId")}, "observations": observations, "scores": [], "fetch_limits": {"from": from_time, "to": to_time, "root_observed": complete_root}}
    if complete_root:
        for key in ("input", "output", "level"):
            if key in root:
                result["trace"][key] = root[key]
    # Scores are retrieved separately; failure to fetch them must not fabricate
    # a clean verifier. Import remains useful with an explicit limitation.
    # Evaluation timestamps can be later than the trace execution window.
    score_params = {"traceId": trace_id, "fields": "details,subject", "limit": 100}
    # Scores get their own minute so a deadline cannot drop a trace's evaluation, but never far past the batch limit.
    scores_end = time.monotonic() + 60 if deadline is None else min(time.monotonic() + 60, deadline + 30)
    _budget.end = scores_end
    _budget.batch = False
    warnings = ["Only observations within the selected time window were retrieved. Expand the window if this capture is incomplete."]
    note = recent_data_note(observations)
    if note:
        warnings.append(note)
    try:
        seen = set()
        score_size = 0
        while True:
            # Scores have their own budget: a batch deadline must not silently drop a trace's evaluation.
            if time.monotonic() > scores_end:
                raise ImportProblem("source_timeout", "Evaluation score retrieval timed out.", 504)
            page = _get(host.rstrip("/") + "/api/public/v3/scores?" + urllib.parse.urlencode(score_params), authorization)
            score_size += len(json.dumps(page).encode())
            if score_size + size > MAX_FILE:
                raise ImportProblem("import_limit", "The combined trace and score response exceeds the capture limit.", 413)
            if not isinstance(page, dict) or not isinstance(page.get("data"), list):
                raise ImportProblem("unsupported_response", "Unsupported scores response.", 502)
            for score in page["data"]:
                if not isinstance(score, dict):
                    raise ImportProblem("unsupported_response", "Unsupported score record.", 502)
                subject = score.get("subject") or {}
                if not isinstance(subject, dict):
                    raise ImportProblem("unsupported_response", "Unsupported score subject.", 502)
                if not ((subject.get("kind") == "trace" and subject.get("id") == trace_id) or subject.get("traceId") == trace_id):
                    raise ImportProblem("unsupported_response", "Unexpected score subject.", 502)
                if score.get("dataType") == "CATEGORICAL":
                    score["stringValue"] = score.get("value")
                result["scores"].append(score)
            if len(result["scores"]) > MAX_SPANS:
                raise ImportProblem("import_limit", "Too many evaluation scores.")
            meta = page.get("meta") or {}
            if not isinstance(meta, dict) or (meta.get("cursor") is not None and not isinstance(meta["cursor"], str)):
                raise ImportProblem("unsupported_response", "Unsupported score pagination metadata.", 502)
            cursor = meta.get("cursor")
            if not cursor:
                break
            if cursor in seen:
                raise ImportProblem("unsupported_response", "Repeated score cursor.")
            seen.add(cursor)
            score_params["cursor"] = cursor
    except ImportProblem as exc:
        if exc.code in ("import_limit", "source_redirect"):
            raise
        result["scores"] = []
        warnings.append("Evaluation scores could not be retrieved. No task outcome is inferred from observation completion.")
    result["import_warnings"] = warnings
    return result
