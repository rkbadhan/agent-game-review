"""Item 33 (2026-09-11 audit): ``RecoveryEpisode.expected_probe`` and its
consumer, ``IgnoredToolFailure``.

The audit sampled five UNRECOVERED episodes ``ignored_tool_failure`` had
flagged and found 1/5 precision: four were a read-only existence/state
CHECK that came back non-zero because the target was absent — the answer
the check existed to get — immediately followed by the agent creating or
handling that same target. The fifth was a genuine unresolved failure and
was correctly flagged.

These tests hold the fix to the audit's own corrections:
  * the raw non-zero tool_result stays a fact (``classification`` is still
    ``UNRECOVERED``; only ``expected_probe`` changes) — see
    ``test_expected_probe_does_not_change_the_raw_classification``;
  * a command is never exempted because it contains ``2>/dev/null`` — a
    genuine failure can redirect stderr too;
  * a subsequent action must address the SAME target as the probe — an
    unrelated state-changing action earns nothing.
"""

from agr.recovery import UNRECOVERED, classify_recoveries
from agr.schema import DerivedEvent


def ev(eid, etype, tool=None, content="", seq=None, exit_code=None, path=None, extra=None):
    payload = {}
    if tool:
        payload["tool"] = tool
    if content:
        payload["content"] = content
    if path:
        payload["path"] = path
    if exit_code is not None:
        payload["exit_code"] = exit_code
    if extra:
        payload.update(extra)
    return DerivedEvent(event_id=eid, run_id="r", source_capture_id="c",
                        sequence=seq or int(eid.split("_")[1]),
                        source_step_ids=[eid], event_type=etype,
                        actor="main_agent", payload=payload)


# --- the four false positives (§1: expected probe, branch taken) -----------

def test_test_flag_probe_then_touch_is_an_expected_probe():
    """``test -f`` returning false (exit 1, POSIX) because the file is
    absent, followed by the agent creating exactly that file."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="test -f config.json"),
        ev("evt_2", "tool_result", tool="shell", content="", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="touch config.json"),
        ev("evt_4", "tool_result", tool="shell", content="", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    assert eps[0].classification == UNRECOVERED
    assert eps[0].expected_probe is True


def test_ls_probe_then_mkdir_is_an_expected_probe():
    """``ls`` on a missing directory, followed by mkdir of that directory."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="ls /app/plugins"),
        ev("evt_2", "tool_result", tool="shell",
           content="ls: cannot access '/app/plugins': No such file or directory", exit_code=2),
        ev("evt_3", "tool_call", tool="shell", content="mkdir /app/plugins"),
        ev("evt_4", "tool_result", tool="shell", content="", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    assert eps[0].expected_probe is True


def test_stat_probe_then_write_is_an_expected_probe():
    """``stat`` on a missing build artifact, followed by the agent writing
    that same path with a structured Write call (not a shell command)."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="stat build/output.bin"),
        ev("evt_2", "tool_result", tool="shell",
           content="stat: cannot stat 'build/output.bin': No such file or directory", exit_code=1),
        ev("evt_3", "tool_call", tool="Write", path="build/output.bin"),
        ev("evt_4", "tool_result", tool="Write", content="wrote 128 bytes", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    assert eps[0].expected_probe is True


def test_which_probe_then_install_is_an_expected_probe():
    """``which`` on a missing executable (empty output, exit 1 — its
    ordinary "not found" answer), followed by installing it."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="which black"),
        ev("evt_2", "tool_result", tool="shell", content="", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="pip install black"),
        ev("evt_4", "tool_result", tool="shell", content="Successfully installed black", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    # "pip" is not in the closed branch-executable set (deliberately narrow,
    # like ``_STATE_CHANGING_EXECUTABLES`` — no multi-purpose package
    # manager whose common subcommands are often read-only) — this one is
    # intentionally NOT credited, a regression guard documenting the
    # boundary. See the sibling test below for the recognized shape.
    assert eps[0].expected_probe is False


def test_which_probe_then_matching_target_install_is_an_expected_probe():
    """Same shape, but the follow-up command's target literally names the
    probed executable (the common ``mkdir``-style shape the mechanical
    check recognizes)."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="which black"),
        ev("evt_2", "tool_result", tool="shell", content="", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="install black /usr/local/bin/black"),
        ev("evt_4", "tool_result", tool="shell", content="", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    assert eps[0].expected_probe is True


# --- the fifth (genuine, correctly flagged) example -------------------------

def test_probe_with_no_followup_is_not_an_expected_probe():
    """The same probe shape, but nothing ever addresses the missing target —
    a genuinely ignored failure, which must stay expected_probe=False so
    IgnoredToolFailure still flags it."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="test -f secrets.env"),
        ev("evt_2", "tool_result", tool="shell", content="", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="python app.py"),
        ev("evt_4", "tool_result", tool="shell", content="KeyError: 'API_KEY'", exit_code=1),
    ]
    eps = classify_recoveries(events, "r", "c")
    by_failure = {e.failure_event_id: e for e in eps}
    assert by_failure["evt_2"].expected_probe is False


# --- counterexamples (§2: do not overcorrect) --------------------------------

def test_suppressed_stderr_genuine_failure_is_not_exempted():
    """A build failure that happens to redirect stderr to /dev/null is not a
    probe at all (``python`` is not in the closed executable set) — the
    ``2>/dev/null`` text must never be read as a signal either way."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="python build.py 2>/dev/null"),
        ev("evt_2", "tool_result", tool="shell", content="ImportError: no module named yaml", exit_code=1),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert len(eps) == 1
    assert eps[0].expected_probe is False


def test_probe_shaped_command_failing_for_a_different_reason_is_not_exempted():
    """``ls`` failing on a permission error (not an absence-shaped
    diagnostic) is a genuine problem sharing the probe's own executable —
    must not be exempted, and the stderr redirect on it changes nothing."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="ls /root 2>/dev/null"),
        ev("evt_2", "tool_result", tool="shell", content="ls: Permission denied", exit_code=2),
        ev("evt_3", "tool_call", tool="shell", content="mkdir /root/data"),
        ev("evt_4", "tool_result", tool="shell", content="Operation not permitted", exit_code=1),
    ]
    eps = classify_recoveries(events, "r", "c")
    by_failure = {e.failure_event_id: e for e in eps}
    assert by_failure["evt_2"].expected_probe is False


def test_test_usage_error_is_not_exempted():
    """``test``'s own exit 2 is a malformed invocation (a genuine tool
    misuse), not the condition being false — must not be read as absence,
    even though a target token ("config.json") is present and a same-target
    follow-up exists, so this isolates the exit-code check specifically."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="test -f config.json extra"),
        ev("evt_2", "tool_result", tool="shell",
           content="test: too many arguments", exit_code=2),
        ev("evt_3", "tool_call", tool="shell", content="touch config.json"),
        ev("evt_4", "tool_result", tool="shell", content="", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert eps[0].expected_probe is False


def test_unrelated_subsequent_action_does_not_count_as_the_branch():
    """A probe fails, then an UNRELATED state-changing command runs (a
    different target) — must not be credited as "the intended branch"."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="test -f missing.txt"),
        ev("evt_2", "tool_result", tool="shell", content="", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="mkdir logs"),
        ev("evt_4", "tool_result", tool="shell", content="", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert eps[0].expected_probe is False


def test_expected_probe_does_not_change_the_raw_classification():
    """The behavioural judgement is additive, never a replacement: an
    expected-probe episode is STILL classification=unrecovered_failure with
    its true evidence — only ``expected_probe`` is new."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="test -f config.json"),
        ev("evt_2", "tool_result", tool="shell", content="", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="touch config.json"),
        ev("evt_4", "tool_result", tool="shell", content="", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert eps[0].classification == UNRECOVERED
    assert eps[0].failure_event_id == "evt_2"
    assert eps[0].resolution_event_id is None
    assert "evt_2" in eps[0].evidence_event_ids


# --- PR #69 review findings (2026-09-12) -------------------------------------
#
# A code-review pass on the item-33 changes found five real bugs in the first
# cut, all now fixed in ``agr/recovery.py``. Each test below reproduces the
# exact failure mode reported.


def test_failing_call_with_empty_content_does_not_crash():
    """Finding #1 (highest severity): a failing tool_call with empty/
    whitespace-only content previously crashed classify_recoveries for the
    ENTIRE capture with an IndexError (an unguarded ``.split()[0]`` on the
    probe's executable) — before ever reaching the ``probe_targets`` guard.
    Any failing call with empty content aborted classification, not just a
    probe-shaped one."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content=""),
        ev("evt_2", "tool_result", tool="shell", content="some error", exit_code=1),
    ]
    eps = classify_recoveries(events, "r", "c")  # must not raise
    assert len(eps) == 1
    assert eps[0].expected_probe is False


def test_short_target_token_is_not_substring_matched():
    """Finding #2: probe targets were compared by bidirectional substring
    containment with no minimum-length floor, unlike the sibling
    ``is_state_changing_action_related_to`` (``MIN_RELATED_TOKEN_LEN`` = 3).
    A one-character probe target ("x") matched an UNRELATED "mkdir bax" via
    plain substring containment ("x" in "bax"), wrongly suppressing a
    genuinely unresolved failure."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="test -f x"),
        ev("evt_2", "tool_result", tool="shell", content="", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="mkdir bax"),
        ev("evt_4", "tool_result", tool="shell", content="", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert eps[0].expected_probe is False


def test_failed_followup_action_is_not_credited_as_the_branch():
    """Finding #3: the follow-up action's OWN result must be a success — a
    ``touch``/``mkdir`` on the right target that itself FAILS (e.g.
    permission denied) leaves the target just as absent as before. The
    original code credited any target-matching follow-up regardless of
    whether it succeeded, unlike the ``strategy_change`` path elsewhere in
    this same module, which already requires ``is_tool_success``."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="test -f secrets.env"),
        ev("evt_2", "tool_result", tool="shell", content="", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="touch secrets.env"),
        ev("evt_4", "tool_result", tool="shell", content="Permission denied", exit_code=1),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert eps[0].expected_probe is False


def test_absence_diagnostic_carried_only_in_captured_stderr_is_recognized():
    """Finding #4: ``ingest_claude.py`` captures a Bash result's raw stdout/
    stderr separately (``tool_use_result``) from the rendered ``content``
    text. A probe whose "No such file or directory" diagnostic lands only in
    that captured stderr — not in ``content`` — must still be recognized as
    absence-shaped, or the exemption never fires on exactly the captures the
    audit's false positives came from."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="ls /app/plugins"),
        ev("evt_2", "tool_result", tool="shell", content="", exit_code=2,
           extra={"tool_use_result": {
               "stdout": "",
               "stderr": "ls: cannot access '/app/plugins': No such file or directory",
           }}),
        ev("evt_3", "tool_call", tool="shell", content="mkdir /app/plugins"),
        ev("evt_4", "tool_result", tool="shell", content="", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert eps[0].expected_probe is True


def test_compound_command_does_not_inherit_the_chained_commands_targets():
    """Finding #5: ``test -f somefile || mkdir otherdir`` is one tool_call in
    most captures. Only ``somefile`` is what the PROBE itself checked;
    ``otherdir`` (and the bare operator ``||``/executable ``mkdir``) belong
    to the chained command and must never be read as part of the probe's own
    target set. Before the fix, all of ``{"otherdir", "||", "mkdir"}`` were
    (wrongly) captured as probe targets, so an unrelated later ``mkdir
    otherdir`` was credited as resolving THIS probe."""
    events = [
        ev("evt_1", "tool_call", tool="shell", content="test -f somefile || mkdir otherdir"),
        ev("evt_2", "tool_result", tool="shell", content="", exit_code=1),
        ev("evt_3", "tool_call", tool="shell", content="mkdir otherdir"),
        ev("evt_4", "tool_result", tool="shell", content="", exit_code=0),
    ]
    eps = classify_recoveries(events, "r", "c")
    assert eps[0].expected_probe is False
