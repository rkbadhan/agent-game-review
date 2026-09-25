"""P0-6: internal spec references (§N.N, "item NN", AGR-NN, "Stage X") must
never reach a user — neither the CLI's own ``--help`` output nor a string the
SPA renders (labels, tooltips, toasts). They're fine in code comments and
docstrings, which a contributor reads in the source, not in the terminal or
the browser.
"""

import io
import contextlib
import os
import re

import pytest

_MARKER_RE = re.compile(r"§|\bitem \d+\b|\bAGR-\d+\b|\bStage [A-Z]\b")

_JS_DIR = os.path.join(os.path.dirname(__file__), "..", "agr", "static", "js")


def test_cli_help_output_has_no_internal_spec_refs():
    from agr.cli import build_parser

    parser = build_parser()
    outputs = []
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        parser.print_help()
    outputs.append(("agr --help", buf.getvalue()))

    subparsers_action = next(
        a for a in parser._subparsers._group_actions if a.choices
    )
    for name, subparser in subparsers_action.choices.items():
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            subparser.print_help()
        outputs.append((f"agr {name} --help", buf.getvalue()))

    offenders = []
    for label, text in outputs:
        for line in text.splitlines():
            if _MARKER_RE.search(line):
                offenders.append(f"{label}: {line.strip()}")
    assert not offenders, "internal spec references leaked into --help output:\n" + "\n".join(offenders)


def _strip_js_comments(text):
    """Best-effort: drop full-line ``//`` comments and ``/* ... */`` blocks.

    Not a real JS parser — good enough to tell a rendered string literal from
    a comment in this codebase's style (block comments never share a line
    with code, and ``//`` never appears inside one of the checked strings).
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.DOTALL)
    kept = []
    for line in text.splitlines():
        if line.strip().startswith("//"):
            continue
        kept.append(line)
    return "\n".join(kept)


def test_ui_rendered_strings_have_no_internal_spec_refs():
    offenders = []
    for name in sorted(os.listdir(_JS_DIR)):
        if not name.endswith(".js"):
            continue
        path = os.path.join(_JS_DIR, name)
        with open(path, encoding="utf-8") as fh:
            code = _strip_js_comments(fh.read())
        for lineno, line in enumerate(code.splitlines(), start=1):
            if _MARKER_RE.search(line):
                offenders.append(f"{name}:{lineno}: {line.strip()}")
    assert not offenders, "internal spec references leaked into a rendered UI string:\n" + "\n".join(offenders)
