"""Error signature normaliser (item 28, 2026-09-08).

Pinned input -> output fixtures: a deterministic, stdlib-only grouping key,
not intelligent clustering. Each case documents WHY that shape groups the
way it does.
"""

from __future__ import annotations

import json

import pytest

from agr.error_signature import error_signature, error_signature_with_basis

CASES = [
    (
        "AssertionError: 5 != 4",
        "AssertionError: <N> != <N>",
        "bare integers are the varying part of an assertion failure",
    ),
    (
        "Traceback (most recent call last):\n"
        '  File "/app/calc.py", line 12, in add\n'
        "    return a + b + 1\n"
        "AssertionError: assert 4 == 5",
        "AssertionError: assert <N> == <N>",
        "a Python traceback: the exception is the LAST line, never the noisy "
        "file/line-number frames above it",
    ),
    (
        "bash: nonexistentcmd: command not found",
        "bash: nonexistentcmd: command not found",
        "a shell error with no variable parts is left untouched",
    ),
    (
        "FileNotFoundError: [Errno 2] No such file or directory: '/tmp/abc123.txt'",
        "FileNotFoundError: [Errno <N>] No such file or directory: <STR>",
        "the quoted path is consumed whole as one literal, not fragmented "
        "into separate <PATH>/<N> pieces",
    ),
    (
        "E   AssertionError: 5 != 4\n1 failed in 0.12s",
        "E AssertionError: <N> != <N>",
        "the first non-blank line is used when there is no traceback header",
    ),
    (
        "Error: ENOENT: no such file or directory, open '/app/config/db8f3c2e-1a4b-4d9e-9c7f-2b6a5e1d0f33.json'",
        "Error: ENOENT: no such file or directory, open <STR>",
        "a UUID inside a quoted path is still consumed as one <STR>, since "
        "the quote substitution runs before the UUID/path ones",
    ),
    (
        "connect ECONNREFUSED 127.0.0.1:8080",
        "connect ECONNREFUSED <N>.<N>.<N>.<N>:<N>",
        "an IP:port with no dedicated pattern falls through to per-token "
        "integer normalisation — mechanical, not a special case",
    ),
    (
        "panic: runtime error at 0x7ffeeb2a1c40: invalid memory address",
        "panic: runtime error at <HEX>: invalid memory address",
        "a hex address is normalised before the bare-integer pass could "
        "otherwise chew into its digits",
    ),
    (
        "Request failed at 2026-09-08T14:32:01.512Z: timeout",
        "Request failed at <TS>: timeout",
        "an ISO-8601 timestamp is recognised whole, not fragmented",
    ),
    (
        "",
        "<empty>",
        "empty input never raises and never collapses to an empty string",
    ),
    (
        "   \n  \n",
        "<empty>",
        "whitespace-only input is treated the same as empty",
    ),
]


@pytest.mark.parametrize("raw,expected,reason", CASES, ids=[c[2] for c in CASES])
def test_error_signature_fixtures(raw, expected, reason):
    assert error_signature(raw) == expected


def test_deterministic_same_input_same_output():
    text = "AssertionError: 5 != 4"
    assert error_signature(text) == error_signature(text)


def test_two_runs_of_the_same_bug_with_different_line_numbers_group_together():
    """The whole point: two failures of the identical assertion, differing
    only in the traceback's file/line noise, must normalise to ONE signature."""
    a = (
        "Traceback (most recent call last):\n"
        '  File "/home/ci-runner-7/app/calc.py", line 12, in add\n'
        "    return a + b + 1\n"
        "AssertionError: assert 4 == 5"
    )
    b = (
        "Traceback (most recent call last):\n"
        '  File "/home/ci-runner-42/workspace/app/calc.py", line 87, in add\n'
        "    return a + b + 1\n"
        "AssertionError: assert 4 == 5"
    )
    assert error_signature(a) == error_signature(b)


def test_genuinely_different_assertions_do_not_collapse_together():
    a = "AssertionError: 5 != 4"
    b = "AssertionError: connection refused"
    assert error_signature(a) != error_signature(b)


def test_long_signature_is_bounded():
    text = "Error: " + ("x" * 500)
    sig = error_signature(text)
    assert len(sig) <= 201  # _MAX_SIGNATURE_CHARS + the truncation ellipsis
    assert sig.endswith("…")


def test_non_string_input_does_not_raise():
    assert error_signature(None) == "<empty>"


# --- JSON-transport unwrap (real-world mini-swe-agent shape) -----------------

def test_json_wrapped_output_is_unwrapped_not_flattened_to_a_brace():
    """mini-swe-agent transports a command's result as
    {"returncode": N, "output": "..."} — without unwrapping, every such
    failure's first line is the JSON's opening brace, collapsing every
    distinct failure into one uninformative '{' bucket."""
    raw = '{\n  "returncode": 1,\n  "output": "bash: nonexistentcmd: command not found"\n}'
    assert error_signature(raw) == "bash: nonexistentcmd: command not found"


def test_json_wrapped_empty_output_falls_back_to_exception_info():
    raw = '{\n  "returncode": -1,\n  "output": "", "exception_info": "action was not executed"\n}'
    assert error_signature(raw) == "action was not executed"


def test_json_with_no_recognised_message_key_falls_back_to_last_line():
    """Pretty-printed (multi-line) JSON with no non-empty message-like field:
    the unwrap leaves the text unchanged, and with no traceback and no
    recognised diagnostic marker anywhere, AGR-07's documented fallback tier
    is the LAST non-blank line — the real shape a terminated-command result
    takes (mini-swe-agent's own returncode:-15 SIGTERM case, which carries no
    message at all). Neither the opening brace nor the closing one carries
    any more information than the other; this pins which one the documented
    fallback rule actually picks."""
    raw = '{\n  "returncode": -15,\n  "output": ""\n}'
    assert error_signature(raw) == "}"


def test_single_line_json_with_no_message_key_normalises_the_whole_object():
    """A single-line (non-pretty-printed) JSON blob has no newline to isolate
    the brace — the whole object is the first "line", normalised like any
    other text."""
    raw = '{"returncode": -15, "output": ""}'
    assert error_signature(raw) == "{<STR>: -<N>, <STR>: <STR>}"


def test_non_json_curly_text_is_not_mistaken_for_json():
    raw = "{not actually json"
    assert error_signature(raw) == "{not actually json"


# --- AGR-07: diagnostic selection over noisy multi-line output --------------


def test_os_release_preamble_does_not_win_over_a_real_diagnostic():
    """A script that dumps /etc/os-release for context before actually
    failing must not have that dump picked as the signature."""
    raw = (
        'PRETTY_NAME="Debian GNU/Linux 12 (bookworm)"\n'
        "NAME=Debian\nVERSION_ID=\"12\"\n"
        "bash: nonexistentcmd: command not found"
    )
    sig, basis = error_signature_with_basis(raw)
    assert sig == "bash: nonexistentcmd: command not found"
    assert basis == "diagnostic_line"


def test_progress_banner_does_not_win_over_a_later_failure_line():
    raw = (
        "=== Running test suite ===\n"
        "Step 1/5: installing dependencies\n"
        "Step 2/5: building\n"
        "FAILED tests/test_x.py::test_thing - AssertionError: boom"
    )
    sig, basis = error_signature_with_basis(raw)
    assert sig == "FAILED tests/test_x.py::test_thing - AssertionError: boom"
    assert basis == "diagnostic_line"


def test_json_opening_brace_preamble_does_not_win_over_a_later_diagnostic():
    """A tool prints a JSON status blob and THEN a plain-text failure — the
    whole text is not itself valid JSON, so _unwrap_json_transport declines,
    and the line scan must not stop at the bare opening brace."""
    raw = '{"status": "running"}\nmore progress output\nFatal: disk quota exceeded'
    sig, basis = error_signature_with_basis(raw)
    assert sig == "Fatal: disk quota exceeded"
    assert basis == "diagnostic_line"


def test_stderr_field_preferred_over_a_large_unrelated_output_field():
    """A dedicated stderr/error channel is preferred over the general
    combined-output blob, even when output is non-empty."""
    raw = json.dumps({
        "returncode": 1,
        "output": "Compiling...\nLinking...\nRunning 200 checks, 199 ok\n",
        "stderr": "FATAL: disk full",
    })
    sig, basis = error_signature_with_basis(raw)
    assert sig == "FATAL: disk full"


def test_exception_info_preferred_over_error_and_output_fields():
    raw = json.dumps({
        "output": "some generic output",
        "error": "generic error field",
        "exception_info": "action was not executed",
    })
    assert error_signature(raw) == "action was not executed"


def test_truncated_traceback_never_selects_the_generic_header_itself():
    """The traceback header with nothing substantive captured after it must
    not become the signature — every truncated traceback would otherwise
    collapse into one generic, content-free bucket."""
    raw = "some setup output\nTraceback (most recent call last):"
    sig, basis = error_signature_with_basis(raw)
    assert sig != "Traceback (most recent call last):"
    assert basis != "traceback_exception"


def test_traceback_not_at_the_very_end_still_selects_its_own_exception_line():
    """Trailing output AFTER a traceback (e.g. a wrapper script's own
    message) must not steal the traceback's own exception line."""
    raw = (
        "Traceback (most recent call last):\n"
        '  File "/app/calc.py", line 12, in add\n'
        "    return a + b + 1\n"
        "AssertionError: assert 4 == 5\n"
        "wrapper.sh: cleanup complete"
    )
    sig, basis = error_signature_with_basis(raw)
    assert sig == "AssertionError: assert <N> == <N>"
    assert basis == "traceback_exception"


def test_no_diagnostic_anywhere_uses_labelled_fallback_basis():
    raw = "line one of ordinary output\nline two of ordinary output"
    sig, basis = error_signature_with_basis(raw)
    assert sig == "line two of ordinary output"
    assert basis == "fallback_last_nonempty"


def test_single_line_diagnostic_basis_is_not_fallback():
    sig, basis = error_signature_with_basis("AssertionError: 5 != 4")
    assert basis in ("diagnostic_line", "traceback_exception")
    assert basis != "fallback_last_nonempty"


def test_empty_input_basis_is_fallback():
    _, basis = error_signature_with_basis("")
    assert basis == "fallback_last_nonempty"
