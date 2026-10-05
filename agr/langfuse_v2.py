"""Bounded single-trace retrieval using Langfuse observations v2.

Contract: https://langfuse.com/docs/api-and-data-platform/features/public-api
Legacy v3 instances can still use the existing explicit legacy fetcher.
"""
import base64
from datetime import datetime, timezone
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

from .imports import ImportProblem, MAX_FILE, MAX_SPANS


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise ImportProblem("source_redirect", "The host redirected the request. Configure its final base URL instead.", 502)


def _get(url, authorization):
    request = urllib.request.Request(url, headers={"Authorization": authorization, "Accept": "application/json"})
    try:
        with urllib.request.build_opener(_NoRedirect()).open(request, timeout=60) as response:
            raw = response.read(MAX_FILE + 1)
        if len(raw) > MAX_FILE:
            raise ImportProblem("import_limit", "The trace response is too large. Export a smaller capture.", 413)
        return json.loads(raw)
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
    except (urllib.error.URLError, TimeoutError, socket.timeout):
        raise ImportProblem("source_unreachable", "The host could not be reached or timed out. Check its address and connectivity.", 502) from None
    except (ValueError, UnicodeError):
        raise ImportProblem("unsupported_response", "Langfuse returned an unsupported response. Check the host and API version.", 502) from None


def _parse_io(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except ValueError:
            pass
    return value


def fetch_trace(trace_id, *, host, public_key, secret_key, from_time, to_time):
    try:
        start = datetime.fromisoformat(from_time.replace("Z", "+00:00"))
        end = datetime.fromisoformat(to_time.replace("Z", "+00:00"))
        if not start.tzinfo or not end.tzinfo or start >= end:
            raise ValueError()
    except (ValueError, AttributeError):
        raise ImportProblem("invalid_time_range", "Choose a valid start and end time with a timezone.") from None
    authorization = "Basic " + base64.b64encode(f"{public_key}:{secret_key}".encode()).decode()
    params = {"traceId": trace_id, "fromStartTime": start.astimezone(timezone.utc).isoformat(), "toStartTime": end.astimezone(timezone.utc).isoformat(), "fields": "core,basic,io,model,usage,trace_context", "limit": 1000}
    observations, cursors, size = [], set(), 0
    started = time.monotonic()
    while True:
        if time.monotonic() - started > 120:
            raise ImportProblem("source_timeout", "Trace retrieval exceeded two minutes. Use a smaller time range or an export.", 504)
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
    result = {"trace": {"id": trace_id, "name": root.get("traceName") or root.get("name") or "Langfuse trace", "timestamp": min(o.get("startTime") or "" for o in observations), "sessionId": root.get("sessionId")}, "observations": observations, "scores": [], "fetch_limits": {"from": from_time, "to": to_time, "root_observed": complete_root}}
    if complete_root:
        for key in ("input", "output", "level"):
            if key in root:
                result["trace"][key] = root[key]
    # Scores are retrieved separately; failure to fetch them must not fabricate
    # a clean verifier. Import remains useful with an explicit limitation.
    # Evaluation timestamps can be later than the trace execution window.
    score_params = {"traceId": trace_id, "fields": "details,subject", "limit": 100}
    warnings = ["Only observations within the selected time window were retrieved. Expand the window if this capture is incomplete."]
    try:
        seen = set()
        score_size = 0
        while True:
            if time.monotonic() - started > 120:
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
