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
    """Prefer structured exception information when the WHOLE text is a JSON
    object (AGR-07 priority tier 1).

    Some harnesses (mini-swe-agent's own result serialisation) transport a
    command's output as ``{"returncode": N, "output": "..."}`` rather than
    plain text. Without this, EVERY such failure's first line is the JSON's
    opening ``{`` — a content-free signature that collapses every distinct
    failure into one uninformative bucket.

    Keys are checked in order of how DIRECTLY they name the problem: an
    explicit exception field first, then a dedicated error/stderr channel,
    and only last the general combined-output blob (``output``/``stdout``,
    which may be mostly unrelated successful output with the diagnostic
    buried inside it — tier 3 below still has to find it). Recognising a
    fixed, common set of keys is still mechanical (no interpretation of what
    the message MEANS); anything that isn't a JSON object, or has none of
    these keys non-empty, is returned unchanged.
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
    for key in ("exception_info", "error", "error_message", "stderr", "message", "output", "stdout"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            return val
    return text


# A closed, mechanical set of substrings a genuine diagnostic line commonly
# carries — matched literally, never a semantic read of what the line MEANS
# (the same philosophy as ingest_harbor.py's _SHELL_ERROR_MARKERS). Ordering
# does not matter here: any single match qualifies a line.
_DIAGNOSTIC_MARKERS = (
    "Error:", "error:", "ERROR:", "Error ", "ERROR ",
    "Exception:", "exception:", "Exception ",
    "AssertionError", "assert ",
    "FAILED", "Failed:", "failed:",
    "panic:", "fatal:", "Fatal:",
    "Errno ",
    # Shell error text the shell itself prints verbatim (same closed set as
    # agr.ingest_harbor._SHELL_ERROR_MARKERS — a diagnostic marker in one
    # place should be recognised as one here too).
    "command not found",
    "No such file or directory",
    "Permission denied",
    "syntax error near unexpected token",
    "is not recognized as an internal or external command",
)

# A Python exception class name by convention ends in Error/Exception/Warning
# and starts the line it's raised on (or a pytest "E   " short-summary
# prefix) — mechanical naming-convention matching, not interpretation.
_PY_EXCEPTION_LINE = re.compile(r"^(E\s+)?[\w.]*(Error|Exception|Warning)\b")

_TRACEBACK_HEADER = "Traceback (most recent call last)"


def _looks_like_diagnostic(line: str) -> bool:
    if _PY_EXCEPTION_LINE.match(line):
        return True
    return any(marker in line for marker in _DIAGNOSTIC_MARKERS)


def _select_diagnostic(text: str) -> tuple[str, str]:
    """Pick the line most likely to identify WHAT failed, and how it was
    picked (AGR-07 priority tiers 2-4; tier 1 is :func:`_unwrap_json_transport`,
    applied by the caller before this runs).

    Returns ``(line, basis)``. ``basis`` is one of:

    * ``"traceback_exception"`` — the substantive line after a Python
      traceback's LAST header (never the header itself: a truncated capture
      with nothing after the header falls through instead of letting every
      truncated traceback collapse into one generic bucket).
    * ``"diagnostic_line"`` — the first line anywhere carrying a recognised
      diagnostic marker. Preferred over blindly using the first line, which
      noisy preamble (an ``/etc/os-release`` dump, a progress banner, a bare
      JSON opening brace) would otherwise win.
    * ``"fallback_last_nonempty"`` — no traceback and no recognised marker
      anywhere: the last non-blank line, used and clearly labelled only
      because nothing more specific is available.
    """
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    if not lines:
        return "", "fallback_last_nonempty"

    tb_indices = [i for i, ln in enumerate(lines) if ln.startswith(_TRACEBACK_HEADER)]
    if tb_indices:
        after = lines[tb_indices[-1] + 1:]
        if after:
            # The actual exception line by Python's own naming convention,
            # searched from the END of the traceback's own block backward —
            # trailing output AFTER the traceback (e.g. a wrapper script's
            # own message) must not be mistaken for the exception itself.
            # Falls back to the last post-header line only when nothing
            # matches that convention (an exception type this doesn't
            # recognise is still more informative than the header).
            exception_line = next((ln for ln in reversed(after) if _PY_EXCEPTION_LINE.match(ln)), None)
            return (exception_line if exception_line is not None else after[-1]), "traceback_exception"
        # Header is the last substantive line captured (truncated trace):
        # never select the generic header itself — search what remains.

    # Non-header lines only: a bare traceback header (truncated, or with no
    # usable post-header content) must never itself become the fallback.
    candidates = [ln for ln in lines if not ln.startswith(_TRACEBACK_HEADER)]
    if not candidates:
        return "", "fallback_last_nonempty"
    for ln in candidates:
        if _looks_like_diagnostic(ln):
            return ln, "diagnostic_line"

    return candidates[-1], "fallback_last_nonempty"


def error_signature_with_basis(text: str) -> tuple[str, str]:
    """Like :func:`error_signature`, also returning which tier selected the
    line (AGR-07: "mark the signature basis as fallback" — and every other
    tier, so a consumer can tell a high-confidence signature from a guess).
    """
    line, basis = _select_diagnostic(_unwrap_json_transport(text or ""))
    for pattern, placeholder in _SUBSTITUTIONS:
        line = pattern.sub(placeholder, line)
    line = " ".join(line.split())
    if len(line) > _MAX_SIGNATURE_CHARS:
        line = line[:_MAX_SIGNATURE_CHARS] + "…"
    return line or "<empty>", basis


def error_signature(text: str) -> str:
    """A normalised, bounded grouping key for an error message.

    Pure and deterministic. Never raises: an empty/non-string-like input
    returns the literal string ``"<empty>"`` rather than an exception, since
    this runs over arbitrary, possibly-malformed captured tool output.
    """
    return error_signature_with_basis(text)[0]
