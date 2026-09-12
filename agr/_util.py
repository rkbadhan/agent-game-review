"""Shared event helpers for the deterministic analysis stages."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from .schema import DerivedEvent

# Tool-result status vocabulary, separate from an OS process exit code: a tool
# error flag (or a missing result) is tool status, not an exit code. Sources
# that record a real exit code keep it; sources that record only an error flag
# get ``status`` only.
STATUS_OK = "ok"
STATUS_ERROR = "error"
STATUS_UNKNOWN = "unknown"


def exit_code(event: DerivedEvent) -> Optional[int]:
    ec = event.payload.get("exit_code")
    return ec if isinstance(ec, int) else None


def tool_status(event: DerivedEvent) -> str:
    """The tool result's status: ``ok`` / ``error`` / ``unknown``.

    Exit code wins where the source recorded one; otherwise an explicit
    ``status`` field; otherwise unknown — which is deliberately neither a
    success nor a failure (an unobserved outcome is never classified).
    """
    ec = exit_code(event)
    if ec is not None:
        return STATUS_ERROR if ec != 0 else STATUS_OK
    status = event.payload.get("status")
    if status in (STATUS_OK, STATUS_ERROR):
        return status
    return STATUS_UNKNOWN


def is_tool_failure(event: DerivedEvent) -> bool:
    if event.event_type == "error_observed":
        return True
    if event.event_type == "tool_result" and event.payload.get("permission_denied"):
        # Item 23 (2026-09-08): a permission-denied call never opens a
        # failure/recovery episode — it is a harness/user-imposed governance
        # block, not a competence failure the agent should be evaluated on
        # recovering from.
        return False
    if event.event_type == "tool_result" and event.payload.get("submission_control_response"):
        # AGR-03: the harness's own "I did not run your submission echo"
        # acknowledgement (mini-swe-agent intercepts COMPLETE_TASK_AND_
        # SUBMIT_FINAL_OUTPUT as a control signal and never executes it) is
        # the submission protocol working as designed — not a tool failure,
        # and never grounds a recovery episode or an ignored-failure finding.
        return False
    return event.event_type == "tool_result" and tool_status(event) == STATUS_ERROR


def is_tool_success(event: Optional[DerivedEvent]) -> bool:
    if event is None:
        return False
    return event.event_type == "tool_result" and tool_status(event) == STATUS_OK


# Tools whose *structured* input is what distinguishes one action from another
# (review 2026-09-07 R2): two Edits of the same path with different old/new
# strings are NOT the same action. The hoisted ``content`` (the path) alone
# could not tell them apart, so the structured arguments join the identity.
# This set ALSO gates ``is_mutation`` below — it must stay exactly the tools
# whose acknowledgement text says nothing about the resulting state.
_STRUCTURED_INPUT_TOOLS = {"Edit", "MultiEdit", "Write", "NotebookEdit"}

# Item 3 (2026-09-07): the same problem exists for non-mutation, non-command
# tools whose hoisted ``content`` is just one field of several that matter.
# ``Read`` hoists ``file_path`` alone, so a re-read of the same file at a
# different ``offset``/``limit`` looked like the identical action. ``Grep``
# hoists whichever of ``path``/``pattern`` the adapter's key-priority list
# reaches first — a different ``pattern`` at the same ``path`` also collapsed
# to one signature. ``Task`` has no path/command at all. None of these are
# state mutations (so they must NOT join ``_STRUCTURED_INPUT_TOOLS`` and
# thereby skip ``repeated_action_no_new_info``'s ack-text check) — they are a
# separate, broader set that only widens the IDENTITY component.
_NON_COMMAND_TOOLS = _STRUCTURED_INPUT_TOOLS | {"Read", "Grep", "Task"}

# Per-event bound on the structured-input identity carried into evidence views
# and action signatures — large file writes stay distinguishable by their
# prefix without bloating every comparison or the reviewer packet.
_INPUT_IDENTITY_CHARS = 200


def _full_input_hash(tool_input: dict) -> str:
    """A collision-resistant digest of the COMPLETE structured tool input.

    Used when no single field is known to carry the distinguishing text (item
    3): a bounded JSON prefix could make two large, differently-shaped inputs
    that happen to share their first 200 characters collide into one
    signature. Hashing the full canonical serialization does not.
    """
    canonical = json.dumps(tool_input, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]


def _input_identity(tool: Optional[str], tool_input: Any) -> str:
    """A short canonical key for the structured arguments that distinguish a
    file-mutation OR non-command call (Edit/Write/NotebookEdit/Read/Grep/Task).
    Empty for tools whose command/path text already identifies them (e.g.
    Bash), so command-tail recovery is unchanged."""
    if tool not in _NON_COMMAND_TOOLS or not isinstance(tool_input, dict):
        return ""
    parts: list[str] = []
    for key in ("old_string", "new_string", "content", "file_text", "new_content"):
        val = tool_input.get(key)
        if isinstance(val, str) and val:
            parts.append(f"{key}={val[:_INPUT_IDENTITY_CHARS]}")
    if parts:
        return "|".join(parts)
    # No distinguished text key present (Read's offset/limit, Grep's
    # pattern/path/flags, Task's subagent_type/description/prompt, or a
    # mutation tool using a different key like NotebookEdit's "new_source"):
    # hash the full structured input rather than guess at a field.
    return f"sha256:{_full_input_hash(tool_input)}"


def action_signature(event: DerivedEvent) -> tuple:
    """Identity of a tool call for 'same vs changed action' comparison.

    ``(tool, content_key, input_identity)``. ``content_key`` is the hoisted
    command/path text (what command-tail recovery keys on); ``input_identity``
    is the structured-argument key for file-mutation tools, so two Edits of the
    same path with different replacements are distinct actions (R2). For
    command tools ``input_identity`` is empty and the signature reduces to the
    prior ``(tool, content)`` semantics.
    """
    p = event.payload
    return (
        p.get("tool"),
        p.get("content") or p.get("data") or p.get("path") or "",
        _input_identity(p.get("tool"), p.get("tool_input")),
    )


def _call_id(event: DerivedEvent) -> Optional[str]:
    cid = event.payload.get("tool_use_id")
    return str(cid) if cid else None


def paired_call_index(events: list[DerivedEvent], idx: int) -> Optional[int]:
    """Like :func:`paired_call`, returning the INDEX into ``events`` instead
    of the event itself — needed by a caller that must walk further from that
    position (e.g. AGR-05/06's initiating-attempt-cost lookup, which needs to
    walk backward from the call to the start of its own turn)."""
    ev = events[idx]
    if ev.event_type != "tool_result":
        return None
    cid = _call_id(ev)
    if cid:
        for j in range(idx - 1, -1, -1):
            if events[j].event_type == "tool_call" and _call_id(events[j]) == cid:
                return j
        return None  # id present but no matching call: never guess
    for j in range(idx - 1, -1, -1):
        if events[j].event_type == "tool_call":
            return j
    return None


def paired_call(events: list[DerivedEvent], idx: int) -> Optional[DerivedEvent]:
    """The tool_call event that produced the result at ``events[idx]``.

    F1 follow-up (review 2026-09-07, R2): when the capture records tool-use
    ids, the result is matched to THE call it answers — correct for parallel
    calls, where the nearest preceding call is not necessarily the right one.
    Adjacency is the fallback, clearly used only when the source carries no
    ids (and a result with an id that matches no captured call stays
    unpaired rather than being silently re-attached to a neighbour).
    """
    j = paired_call_index(events, idx)
    return events[j] if j is not None else None


def preceding_call_signature(events: list[DerivedEvent], idx: int) -> Optional[tuple]:
    """Signature of the call that produced the result at ``events[idx]``.

    ID-linked when the capture carries tool-use ids; nearest-preceding only
    as the adjacency fallback for id-less captures. Only the ``tool`` and
    ``content`` components are meaningful for recovery objective identity
    (``_operation_key`` indexes those); the structured-input component is
    irrelevant to whether a later command resolved the failed objective.
    """
    call = paired_call(events, idx)
    if call is not None:
        return action_signature(call)
    return None


def paired_result(events: list[DerivedEvent], call_idx: int) -> Optional[DerivedEvent]:
    """The tool_result event that answers the tool_call at ``events[call_idx]``.

    The reverse of :func:`paired_call`, and the ONE call↔result index used by
    recovery, repetition detection, fact validation, and evidence views (review
    2026-09-07 R2/R3 — no separate adjacency pairing algorithms). When the
    capture records tool-use ids the result is matched to THE call it answers
    by id (correct for parallel calls). Id-less captures use a CONSERVATIVE
    adjacency fallback: a result is attached only when no other call intervenes
    — parallel id-less results are ambiguous and stay unpaired rather than
    guessed by position. A result whose id matches no captured call stays
    unpaired too.
    """
    ev = events[call_idx]
    if ev.event_type != "tool_call":
        return None
    cid = _call_id(ev)
    if cid:
        for j in range(call_idx + 1, len(events)):
            e2 = events[j]
            if e2.event_type == "tool_result" and _call_id(e2) == cid:
                return e2
        return None  # id present but no matching result: never guess
    for j in range(call_idx + 1, len(events)):
        e2 = events[j]
        if e2.event_type == "tool_call":
            return None  # another call before any result: ambiguous — do not pair
        if e2.event_type == "tool_result":
            return e2
    return None


# Per-event bound on the structured tool input carried into the reviewer
# packet and expansion evidence (R2): full inputs are retrievable via the
# expansion round, but the always-sent packet keeps a bounded excerpt.
_PACKET_INPUT_CHARS = 2000


def structured_input(event: DerivedEvent, max_chars: int = _PACKET_INPUT_CHARS) -> Optional[str]:
    """A bounded JSON serialization of the call's structured ``tool_input``.

    Returns ``None`` when the source recorded no structured input. Used by the
    reviewer packet and evidence expansion so the model can actually retrieve
    an Edit's old/new strings or a Write's content — the display excerpt stays
    separate and shorter; this is the retained evidence (R2).
    """
    ti = event.payload.get("tool_input")
    if ti is None:
        return None
    s = json.dumps(ti, ensure_ascii=False, sort_keys=True)
    if len(s) > max_chars:
        s = s[:max_chars] + " …[truncated]"
    return s


def is_mutation(event: DerivedEvent) -> bool:
    """Whether this call mutates state in a way identical acknowledgement text
    cannot capture (review 2026-09-07 R2). An Edit/Write ack of "ok" says nothing
    about what the file became, so equivalent ack text cannot establish that a
    repeated mutation did no useful work — the repetition detector skips these
    unless resulting-state evidence is available (it is not, in this core)."""
    return event.payload.get("tool") in _STRUCTURED_INPUT_TOOLS


# Item 5 (2026-09-07): shell executables whose ordinary effect is a state
# mutation — the command-tool equivalent of Edit/Write. Deliberately a small,
# closed, UNAMBIGUOUS set: no multi-purpose CLI (git, pip, npm, apt...) whose
# common subcommands are often read-only (status, list, show) is in it, so
# this never depends on interpreting arguments or subcommands — only the
# executable itself, mechanically.
_STATE_CHANGING_EXECUTABLES = {
    "rm", "mv", "cp", "mkdir", "rmdir", "chmod", "chown", "chgrp",
    "ln", "dd", "truncate", "install", "patch", "tee",
}


def is_state_changing_action(event: DerivedEvent) -> bool:
    """Whether this tool_call plausibly mutated state (item 5, 2026-09-07).

    True for the structured mutation tools (Edit/Write/MultiEdit/NotebookEdit
    — ``is_mutation``) and for a shell call whose executable (the first
    token of its hoisted command text) is in the closed
    ``_STATE_CHANGING_EXECUTABLES`` set. This is what "the agent changed
    something" mechanically looks like in a trajectory — used to derive a
    ``strategy_change`` signal for sources whose adapter never emits the
    literal event, without interpreting any command's arguments or output.
    """
    if is_mutation(event):
        return True
    if event.event_type != "tool_call":
        return False
    content = str(event.payload.get("content") or "")
    tokens = content.split()
    return bool(tokens) and tokens[0] in _STATE_CHANGING_EXECUTABLES


# Minimum token length considered for the substring relatedness check below —
# excludes trivial single/double-character tokens that would match almost
# anything ("rm -f a" should not "relate" to any command via the token "a").
MIN_RELATED_TOKEN_LEN = 3

# AGR-04 (PR #56 review), narrowed further by AGR-02 (review 82cc113): a
# closed, mechanical set of PATH markers — never a file extension — that can
# never be the FUNCTIONAL fix for a failing command. A path marker names a
# conventionally repo-meta location (project documentation, licensing,
# changelog) that no test runner or build ever reads as an input; a bare
# extension does NOT — a review 82cc113 finding confirmed that excluding
# every ``.txt``/``.md``/``.rst`` edit by extension alone wrongly excluded a
# test fixture (``tests/fixtures/input.txt``) and a dependency manifest
# (``requirements.txt``), both of which are genuinely functional inputs a
# test run reads. Deliberately narrow and path-based (never a semantic read
# of the file's actual content): a source-code edit, or a plain-text file
# NOT under one of these markers, still credits unconditionally below, which
# is what preserves the "the fix commonly lives in a source file the failing
# command's own target text never names" reasoning this function was built
# on — only the unambiguous non-functional, repo-meta locations are excluded.
_NON_FUNCTIONAL_EDIT_MARKERS = (
    "docs/", "documentation/", "/docs/", "readme",
    "changelog", "contributing", "license", "code_of_conduct", "notice",
)


def _is_functional_edit_target(path: str) -> bool:
    lower = path.lower()
    return not any(marker in lower for marker in _NON_FUNCTIONAL_EDIT_MARKERS)


# AGR-04 (review 82cc113): a closed set of executables whose ordinary effect
# is a NETWORK probe/request with no plausible relationship to an arbitrary
# LOCAL file edit — unlike a test/build command (where "the fix lives in a
# different source file" is the common, expected shape this module was built
# to credit), a raw connectivity failure has no such story: editing
# ``calculator.py`` cannot be why a ``curl`` retry to an unrelated host
# started succeeding. Deliberately narrow and closed, mirroring
# ``_STATE_CHANGING_EXECUTABLES``: these are the only executables for which a
# structured mutation is held to the SAME target-overlap test a shell
# mutation already needs, rather than crediting unconditionally.
# "http"/"https" are HTTPie's own executable names (invoked as `http GET
# example.com` / `https example.com`), not URL scheme prefixes.
_NETWORK_PROBE_EXECUTABLES = {"curl", "wget", "nc", "ncat", "ping", "telnet", "http", "https"}


def target_tokens(target_sig: Optional[tuple]) -> set[str]:
    tokens = str((target_sig[1] if target_sig else "") or "").split()
    return {t for t in tokens[1:] if not t.startswith("-") and len(t) >= MIN_RELATED_TOKEN_LEN}


def is_state_changing_action_related_to(event: DerivedEvent, target_sig: Optional[tuple]) -> bool:
    """Refined ``is_state_changing_action`` (review 2026-09-07, finding #3).

    The unconditional version credited ANY state-changing shell command as a
    strategy change, even one wholly unrelated to the failed operation — a
    failed ``curl X`` followed by an unrelated ``mkdir logs`` followed by an
    IDENTICAL, unchanged ``curl X`` retry was misclassified ``good_recovery``.

    The structured mutation tools (Edit/Write/MultiEdit/NotebookEdit) count
    for any FUNCTIONAL target (``_is_functional_edit_target`` — AGR-04/PR #56
    review, narrowed by AGR-02/review 82cc113 to path markers only, never a
    bare extension) WHEN the failed operation is not a network probe —
    editing ANY source file between a failure and its retry is item 5's own
    primary example of "the agent changed something", and a literal
    path/token match is unreliable there anyway (the fix is usually in a
    SOURCE file, not the TEST path the failed command names). AGR-04 (review
    82cc113): that leniency does not extend to a failed NETWORK PROBE
    (``_NETWORK_PROBE_EXECUTABLES``) — a source-code edit's extension does
    not establish any dependency on an unrelated network request, so a
    mutation only counts there when it shares a target with the failed
    command, the SAME substring-containment check a shell mutation needs
    below. A shell state-changing command counts only when it shares a
    target with ``target_sig``'s command tail — checked as substring
    containment, not exact equality, so ``rm -rf tests/__pycache__`` still
    relates to a failed ``pytest tests/`` (the target/tail token ``tests/``
    names a directory the rm's own target path lives under). A target under
    ``MIN_RELATED_TOKEN_LEN`` characters is excluded from the check.
    """
    failed_tokens = str((target_sig[1] if target_sig else "") or "").split()
    failed_executable = failed_tokens[0] if failed_tokens else None
    if is_mutation(event):
        path = str(event.payload.get("path") or event.payload.get("content") or "")
        if not _is_functional_edit_target(path):
            return False
        if failed_executable not in _NETWORK_PROBE_EXECUTABLES:
            return True
        failed_targets = target_tokens(target_sig)
        if not failed_targets:
            return True  # nothing to compare against — cannot rule out relatedness
        return any(t in path or path in t for t in failed_targets)
    if event.event_type != "tool_call":
        return False
    content = str(event.payload.get("content") or "")
    tokens = content.split()
    if not tokens or tokens[0] not in _STATE_CHANGING_EXECUTABLES:
        return False
    if target_sig is None:
        return False
    action_targets = {t for t in tokens[1:] if not t.startswith("-") and len(t) >= MIN_RELATED_TOKEN_LEN}
    failed_targets = target_tokens(target_sig)
    return any(a in b or b in a for a in action_targets for b in failed_targets)


def evidence_projection(event: DerivedEvent) -> dict:
    """Structured per-event evidence for the reviewer packet and expansion.

    Supplements the textual projection (``DerivedEvent.text()``) with the
    structured fields the deterministic matchers use but text hid (R2): the
    tool-use id, explicit tool-result status, and the bounded structured
    ``tool_input``. Redaction and size limits are applied by the caller; this
    helper only selects and bounds the fields.
    """
    p = event.payload
    out: dict[str, Any] = {"event_type": event.event_type}
    tid = p.get("tool_use_id")
    if tid:
        out["tool_use_id"] = str(tid)
    if event.event_type == "tool_result":
        status = p.get("status")
        if status in (STATUS_OK, STATUS_ERROR):
            out["status"] = status
    si = structured_input(event)
    if si is not None:
        out["tool_input"] = si
    return out
