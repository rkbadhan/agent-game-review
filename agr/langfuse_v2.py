"""Bounded single-trace retrieval using Langfuse observations v2.

Contract: https://langfuse.com/docs/api-and-data-platform/features/public-api
Legacy v3 instances can still use the existing explicit legacy fetcher.
"""
import base64
import http.client
from datetime import datetime, timezone
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
    request = urllib.request.Request(url, headers={"Authorization": authorization, "Accept": "application/json"})
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
        raise ImportProblem(code, message, 502) from None
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


def list_traces(*, host, public_key, secret_key, from_time, to_time, name=None, tag=None, user_id=None, session_id=None, environment=None, limit=50):
    """Find recent traces by their root observations, newest first.

    Langfuse v4 has no trace-listing endpoint: observations v2 returns rows, so
    candidates are grouped by trace ID on the client. Every filter is also
    re-checked locally on the fields the response carries, so an ignored or
    renamed server-side filter cannot widen the selection the engineer sees.
    """
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
    window = (_parse_time(start), _parse_time(end))
    params = {"fromStartTime": start, "toStartTime": end, "isRootObservation": "true", "fields": "core,basic,trace_context", "limit": min(1000, max(100, limit * 2))}
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
    if environment:
        params["environment"] = environment
    if advanced:
        # `filter` takes precedence over the fixed parameters, so everything that must hold goes inside it.
        advanced += [{"type": "datetime", "column": "startTime", "operator": ">=", "value": start}, {"type": "datetime", "column": "startTime", "operator": "<", "value": end},
                     {"type": "boolean", "column": "isRootObservation", "operator": "=", "value": True}]
        if environment:
            advanced.append({"type": "stringOptions", "column": "environment", "operator": "any of", "value": [environment]})
        params["filter"] = json.dumps(advanced)
        params["limit"] = 1000
    authorization = _authorization(public_key, secret_key)
    found, cursors, scanned, pages, truncated, scan_capped = {}, set(), 0, 0, False, False
    started = time.monotonic()
    while True:
        if time.monotonic() - started > 60 or pages >= 20:
            truncated = scan_capped = True
            break
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
            if row.get("parentObservationId") or row.get("isRootObservation") is False:
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
                    or (environment and row.get("environment") != environment):
                continue
            if row["traceId"] in found:
                continue
            if len(found) >= limit:
                truncated = True
                continue
            found[row["traceId"]] = {"trace_id": row["traceId"], "name": str(row.get("traceName") or row.get("name") or "Unnamed trace")[:200], "started_at": row.get("startTime") if isinstance(row.get("startTime"), str) else None,
                                     "user_id": row.get("userId"), "session_id": row.get("sessionId"), "tags": [str(t)[:80] for t in (tags or [])][:12], "environment": row.get("environment"), "level": row.get("level")}
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
    traces = sorted(found.values(), key=lambda t: t["started_at"] or "", reverse=True)
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
    latest = max((t for t in (_parse_time(o.get("startTime")) for o in observations) if t), default=None)
    if latest and (datetime.now(timezone.utc) - latest).total_seconds() < RECENT_SECONDS:
        warnings.append("This trace is very recent. Langfuse can take up to 15 minutes to finish data from older SDKs and exporters, so the capture may be incomplete. Import it again later to capture the rest.")
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
