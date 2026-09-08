"""Error signature normaliser (item 28, 2026-09-08).

Deterministic, versioned, stdlib-only grouping key for otherwise-noisy error
text (a Python traceback line, a shell error message, an assertion failure).

This is NOT intelligent clustering — no ML, no semantic reading of what the
error "means". It is a small, fixed, ordered set of regex substitutions that
strip the parts of an error message that vary run-to-run (line numbers,
hex addresses, timestamps, UUIDs, quoted literals, filesystem paths) while
keeping the parts that identify WHAT failed, so the fleet view (item 30) can
group "the same error" across many runs and count its repeat rate.

Same input always yields the same output (pure function); the substitution
order matters (a quoted path is replaced as one literal before the bare-path
pattern would otherwise try to match pieces of it).
"""

from __future__ import annotations

import json
import re

# Bound on the returned signature — long tracebacks/log dumps stay
# comparable and cheap to group/store without truncating mid-substitution.
_MAX_SIGNATURE_CHARS = 200

# Applied in order, each match replaced by its placeholder. Order matters:
# a quoted literal (which may itself contain digits or slashes) is consumed
# whole before the bare-path or bare-integer patterns would otherwise chew
# into it piecemeal.
_SUBSTITUTIONS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"0x[0-9a-fA-F]+"), "<HEX>"),
    (re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
     "<UUID>"),
    (re.compile(r"\b\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?\b"),
     "<TS>"),
    (re.compile(r"'[^']*'|\"[^\"]*\""), "<STR>"),
    (re.compile(r"(?<![\w.])/(?:[\w.\-]+/)+[\w.\-]*"), "<PATH>"),
    (re.compile(r"\b\d+\b"), "<N>"),
]


def _unwrap_json_transport(text: str) -> str:
    """Prefer a message-like field when the WHOLE text is a JSON object.

    Some harnesses (mini-swe-agent's own result serialisation) transport a
    command's output as ``{"returncode": N, "output": "..."}`` rather than
    plain text. Without this, EVERY such failure's first line is the JSON's
    opening ``{`` — a content-free signature that collapses every distinct
    failure into one uninformative bucket. Recognising a fixed, common set of
    message-like keys is still mechanical (no interpretation of what the
    message MEANS), and anything that isn't a JSON object, or has none of
    these keys, is returned unchanged.
    """
    stripped = text.strip()
    if not stripped.startswith("{"):
        return text
    try:
        obj = json.loads(stripped)
    except (json.JSONDecodeError, ValueError):
        return text
    if not isinstance(obj, dict):
        return text
    for key in ("output", "stderr", "error", "message", "exception_info", "error_message"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return text


def _first_meaningful_line(text: str) -> str:
    """The line most likely to identify WHAT failed.

    A Python traceback's own convention puts the actual exception on the
    LAST line, after the "Traceback (most recent call last):" header and
    the call-stack frames (which are pure noise — file paths and line
    numbers that differ on every run even for the identical bug). Anything
    else (a shell error, an assertion, a one-line log message) uses the
    FIRST non-blank line.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return ""
    if any(ln.startswith("Traceback (most recent call last)") for ln in lines):
        return lines[-1]
    return lines[0]


def error_signature(text: str) -> str:
    """A normalised, bounded grouping key for an error message.

    Pure and deterministic. Never raises: an empty/non-string-like input
    returns the literal string ``"<empty>"`` rather than an exception, since
    this runs over arbitrary, possibly-malformed captured tool output.
    """
    line = _first_meaningful_line(_unwrap_json_transport(text or ""))
    for pattern, placeholder in _SUBSTITUTIONS:
        line = pattern.sub(placeholder, line)
    line = " ".join(line.split())
    if len(line) > _MAX_SIGNATURE_CHARS:
        line = line[:_MAX_SIGNATURE_CHARS] + "…"
    return line or "<empty>"
