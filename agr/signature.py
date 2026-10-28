"""Task Ability Signature (spec §4.3).

Assembles the per-run signature table from mechanically supported evidence:
verifier checks, opportunities, and recovery episodes. Every row is grounded in
deterministic facts.

The cardinal rule (spec §4.3): an ability with no qualifying opportunity is
"Not measured" and must never be rendered as a positive or negative judgment.
``Not reviewed`` (analysis not run) is a separate state used only by
model-enriched signatures and does not appear in this deterministic core.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

# Result / interpretation vocabularies kept aligned with the spec example.
RESULT_SUCCESS = "Successful"
RESULT_FAILED = "Failed"
RESULT_NOT_OBSERVED = "Not observed"
# AGR-04: a plausible (narrowed/adjacent-command) resolution is real evidence
# but weaker than a confirmed exact-rerun success — it must read as its own
# category, never as "Successful", so a consumer reading only the structured
# `result` field (not the prose) cannot mistake it for confirmed recovery.
RESULT_PLAUSIBLE = "Plausible"

INTERP_POSITIVE = "Positive evidence"
INTERP_NEGATIVE = "Negative evidence in this run"
INTERP_NOT_MEASURED = "Not measured"
INTERP_PLAUSIBLE = "Plausible evidence — not confirmed"


@dataclass
class SignatureRow:
    ability: str
    observed_behaviour: str
    result: str
    interpretation: str
    measured: bool
    evidence: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "ability": self.ability,
            "observed_behaviour": self.observed_behaviour,
            "result": self.result,
            "interpretation": self.interpretation,
            "measured": self.measured,
            "evidence": self.evidence,
        }


def _requirement_rows(checks) -> list[SignatureRow]:
    rows: list[SignatureRow] = []
    for c in checks:
        # AGR-02: the CURRENT (reconciled) view, as outcome() reads it. A check
        # a later same-scope observation superseded has no effective status, and
        # a pass made stale by a later mutation demotes to "unknown"; neither is
        # current signature evidence, and neither may be scored as a pass/fail.
        status = c.effective_status
        if status == "passed":
            result, interp = RESULT_SUCCESS, INTERP_POSITIVE
        elif status == "failed":
            result, interp = RESULT_FAILED, INTERP_NEGATIVE
        else:
            continue  # superseded/stale/skipped/unknown checks are not signature evidence
        observed = c.name
        if status == "failed" and c.expected is not None:
            observed = f"expected {c.expected}, observed {c.observed}"
        rows.append(SignatureRow(
            ability=c.name,
            observed_behaviour=observed,
            result=result,
            interpretation=interp,
            measured=True,
            evidence=[c.check_id] + list(c.contract_item_ids),
        ))
    return rows


def _recovery_row(analysis) -> SignatureRow:
    from .recovery import GOOD_RECOVERY, PLAUSIBLE_RECOVERY, UNCHANGED_RETRY

    has_failure = any(o.trigger == "tool_failure" for o in analysis.opportunities)
    if not has_failure:
        return SignatureRow(
            ability="Recover from tool failure",
            observed_behaviour="No qualifying failure occurred",
            result=RESULT_NOT_OBSERVED,
            interpretation=INTERP_NOT_MEASURED,
            measured=False,
        )
    good = any(ep.classification == GOOD_RECOVERY for ep in analysis.recoveries)
    unchanged = any(ep.classification == UNCHANGED_RETRY for ep in analysis.recoveries)
    plausible = any(ep.classification == PLAUSIBLE_RECOVERY for ep in analysis.recoveries)
    if good:
        observed = "Strategy change resolved the failure"
        result, interp = RESULT_SUCCESS, INTERP_POSITIVE
    elif unchanged:
        # AGR-05: a successful unchanged retry IS a recovery — a strategy
        # change is not a universal requirement for sensible recovery. The
        # wording stays honest about what resolved it.
        observed = "Same action retried unchanged and succeeded — resolved without a strategy change"
        result, interp = RESULT_SUCCESS, INTERP_POSITIVE
    elif plausible:
        # Item 4 / AGR-04: a narrowed/adjacent command on an overlapping
        # target succeeded — real, but weaker, evidence than an exact rerun.
        # RESULT_PLAUSIBLE (not RESULT_SUCCESS) so this never reads as
        # confirmed resolution even to a consumer that only looks at the
        # structured `result` field, not this prose.
        observed = "A narrowed or adjacent command on an overlapping target succeeded — plausible, not confirmed, resolution"
        result, interp = RESULT_PLAUSIBLE, INTERP_PLAUSIBLE
    else:
        observed = "Failure not resolved"
        result, interp = RESULT_FAILED, INTERP_NEGATIVE
    return SignatureRow(
        ability="Recover from tool failure",
        observed_behaviour=observed,
        result=result,
        interpretation=interp,
        measured=True,
        evidence=[ep.failure_event_id for ep in analysis.recoveries],
    )


# What counts as a verification action: a tool call that RUNS a test runner,
# linter, type checker, verifier script or compare/hash command. The command is
# split into its simple commands and classified by what it executes, so naming
# a tool (`which pytest`, `pip install mypy`) or a file (`cat test.py`,
# `git checkout`) is not verification.
_COMMAND_SPLIT_RE = re.compile(r"&&|\|\||[;|\n]")
_ENV_ASSIGNMENT_RE = re.compile(r"^[A-Za-z_]\w*=")
_RUNNER_WRAPPERS = (("uv", "run"), ("poetry", "run"), ("pipenv", "run"), ("pnpm", "exec"), ("bundle", "exec"))
_SKIPPED_WORDS = {"sudo", "time", "nohup", "xargs", "npx", "bunx"}
_VERIFY_EXECUTABLES = {
    "pytest", "py.test", "nosetests", "tox", "nox", "jest", "vitest", "mocha", "rspec", "phpunit", "ctest",
    "mypy", "pyright", "tsc", "eslint", "ruff", "flake8", "pylint", "shellcheck",
    "diff", "cmp", "sha1sum", "sha256sum", "md5sum",
}
_VERIFY_SUBCOMMANDS = {
    "npm": {"test", "check", "lint", "verify"}, "yarn": {"test", "check", "lint", "verify"},
    "pnpm": {"test", "check", "lint", "verify"},
    "cargo": {"test", "check", "clippy"}, "go": {"test", "vet"},
    "bun": {"test"}, "deno": {"test"},
    "mvn": {"test", "verify"}, "gradle": {"test", "check"}, "gradlew": {"test", "check"},
    "dotnet": {"test"},
}
_PYTHON_RE = re.compile(r"^python[\d.]*$")
_SCRIPT_INTERPRETERS = {"bash", "sh", "zsh", "node", "ruby", "perl"}
# A script whose name says what it does: check.sh, verify_output.py, run_tests.sh.
_VERIFY_SCRIPT_RE = re.compile(
    r"^(?:run[_-]?)?(?:check|verify|validate|grade|tests?)[\w.-]*\.(?:py|sh|js|ts|rb)$|^run[_-]?tests?\.\w+$")
# A tool whose own name says it verifies, e.g. run_tests or verify_answer.
_VERIFY_TOOL_RE = re.compile(r"^(?:run_)?(?:tests?|pytest|unittests?)$|^(?:verify|validate|verifier|validator)(?:_\w+)?$")


def _is_verification_command(command: str) -> bool:
    for simple in _COMMAND_SPLIT_RE.split(command):
        words = simple.split()
        while words:
            if words[0] in _SKIPPED_WORDS or _ENV_ASSIGNMENT_RE.match(words[0]):
                words = words[1:]
            elif tuple(words[:2]) in _RUNNER_WRAPPERS and len(words) > 2:
                words = words[2:]
            else:
                break
        if not words:
            continue
        head = words[0].rsplit("/", 1)[-1].lower()
        rest = [w for w in words[1:] if not w.startswith("-")]
        if head in _VERIFY_EXECUTABLES:
            return True
        if head in _VERIFY_SUBCOMMANDS:
            sub = rest[1] if rest[:1] == ["run"] and len(rest) > 1 else (rest[0] if rest else "")
            if sub in _VERIFY_SUBCOMMANDS[head]:
                return True
        elif head == "make":
            if {"test", "tests", "check", "verify"} & set(rest):
                return True
        elif head in ("test", "[") and words[1:2] and words[1] in ("-e", "-f", "-s", "-d"):
            return True
        elif _PYTHON_RE.match(head):
            if words[1:3] and words[1] == "-m" and words[2] in ("pytest", "unittest", "doctest"):
                return True
            if rest and _VERIFY_SCRIPT_RE.match(rest[0].rsplit("/", 1)[-1].lower()):
                return True
        elif head in _SCRIPT_INTERPRETERS:
            if rest and _VERIFY_SCRIPT_RE.match(rest[0].rsplit("/", 1)[-1].lower()):
                return True
        elif (words[0].startswith(("./", "/")) or words[0].endswith(".sh")) and _VERIFY_SCRIPT_RE.match(head):
            return True  # a script run directly, e.g. ./check.sh
    return False


def _is_verification_action(event) -> bool:
    if event.event_type == "verifier_check":
        return True
    # Only the call states what was run; a result's output can mention anything
    # ("check" in a log line, a path) without a verification having happened.
    if event.event_type != "tool_call":
        return False
    tool = event.payload.get("tool")
    if isinstance(tool, str):
        # camelCase and kebab-case tool names normalise to snake_case words.
        name = re.sub(r"[^a-z0-9]+", "_", re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", tool).lower()).strip("_")
        if _VERIFY_TOOL_RE.match(name):
            return True
    command = event.payload.get("content")
    return isinstance(command, str) and _is_verification_command(command)


def _verification_row(analysis) -> SignatureRow:
    verify_opp = next((o for o in analysis.opportunities if o.trigger == "required_artifact_exists"), None)
    if verify_opp is None:
        return SignatureRow(
            ability="Verify before submission",
            observed_behaviour="No artifact existed to verify before submission",
            result=RESULT_NOT_OBSERVED,
            interpretation=INTERP_NOT_MEASURED,
            measured=False,
        )
    # A verification action = a verifier_check event, or a tool call that runs
    # a test/check/compare command, occurring within the opportunity window.
    ids = [e.event_id for e in analysis.events]
    start, end = ids.index(verify_opp.start_event_id), ids.index(verify_opp.end_event_id)
    window = analysis.events[start:end + 1]
    verifying = next((e for e in window if _is_verification_action(e)), None)
    verified = verifying is not None
    # AGR-02: the CURRENT view — an obsolete, reconciled failure must not
    # still read as "this run failed" once a later same-scope check passed.
    any_failed = any(c.effective_status == "failed" for c in analysis.checks)
    if verified:
        return SignatureRow(
            ability="Verify before submission",
            observed_behaviour="Verification action taken before submission",
            result=RESULT_SUCCESS,
            interpretation=INTERP_POSITIVE,
            measured=True,
            evidence=[verifying.event_id, verify_opp.start_event_id, verify_opp.end_event_id],
        )
    # AGR-05: verification is scored independently of the final outcome. A
    # passing run with no observed verification action provides NO positive
    # evidence of verification — success is not verification.
    return SignatureRow(
        ability="Verify before submission",
        observed_behaviour="No verification action before submission",
        result=RESULT_NOT_OBSERVED,
        interpretation="No positive evidence — a passing outcome is not verification evidence"
        if not any_failed else INTERP_NEGATIVE,
        measured=True,
        evidence=[verify_opp.start_event_id, verify_opp.end_event_id],
    )


def build_signature(analysis) -> list[SignatureRow]:
    rows = _requirement_rows(analysis.checks)
    rows.append(_recovery_row(analysis))
    rows.append(_verification_row(analysis))
    return rows
