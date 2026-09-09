"""Sibling divergence view (item 21, 2026-09-08).

For a failed run, finds a PASSING sibling on the same task and aligns their
tool_call timelines by (phase kind, tool, content) using difflib's stdlib
sequence matching — the same "same objective" identity ``recovery.py``
already uses (tool + hoisted content), widened with the structural phase
kind so a matching command in a different phase (e.g. a retry attempt vs.
the original planning) is not mistaken for the same step.

The point of this is not "the outcome differs" (the caller already knows
that) — it is exactly WHERE in the timeline the two runs' behaviour first
diverged, so a reviewer can look at the run right up to that point and never
build a theory of failure ("the agent never even tried X") that a shared
diff would refute one action later.
"""

from __future__ import annotations

import difflib
from typing import Optional

from . import read
from ._util import action_signature
from .schema import DerivedEvent
from .store import Store


def _derived_configuration_identity(run: dict) -> Optional[tuple]:
    """A fallback configuration identity from comparison-relevant metadata,
    used ONLY when the source declares no ``configuration_id`` (AGR-12).

    Agent name alone cannot establish matching prompts, models, settings,
    tools, and task/environment versions, so the identity requires ``agent``,
    ``model``, AND ``harness_version`` to ALL be present and equal — a
    partial identity (any field missing) is not auditable and yields no
    identity at all, so it can never be treated as matching anything.
    """
    agent, model, harness = run.get("agent"), run.get("model"), run.get("harness_version")
    if agent and model and harness:
        return (agent, model, harness)
    return None


def _same_configuration(target: dict, candidate: dict) -> bool:
    """Whether ``candidate`` attempted the SAME configuration as ``target``
    (AGR-12) — a divergence report assumes both runs attempted the same
    thing; a run under a different configuration (a different prompt, tool
    set, or harness version) is not that, however tempting a coincidentally-
    passing run on the same task_id looks.

    "Missing configuration_id" is its own value here, not a wildcard: it is
    never treated as "equal by absence" (the same rule ``agr.versions``
    already applies to unresolved match keys). When EITHER side declares a
    configuration_id, only an exact, non-missing match on both sides counts —
    a target with no configuration_id must NEVER be paired with a candidate
    that DOES declare one, since that candidate's configuration is a known,
    specific thing the target's is not shown to match.

    When BOTH sides declare no configuration_id (e.g. two Harbor imports from
    a source that never stamps it — a common real case that must keep
    working), fall back to a derived identity from agent/model/harness_version
    metadata instead of admitting the pair unconditionally: two unknown
    configuration identities do not, by themselves, establish the same
    configuration (the real nginx-corpus case this closes: a Terminus-2
    failure has no business being compared against a mini-swe-agent pass just
    because neither declared a configuration_id).
    """
    t_cfg, c_cfg = target.get("configuration_id"), candidate.get("configuration_id")
    if t_cfg is not None or c_cfg is not None:
        return t_cfg is not None and c_cfg is not None and t_cfg == c_cfg
    t_identity = _derived_configuration_identity(target)
    return t_identity is not None and t_identity == _derived_configuration_identity(candidate)


def find_passing_sibling(store: Store, run_id: str) -> Optional[str]:
    """A PASSING run on the SAME task AND the same configuration as
    ``run_id`` (AGR-12; see ``_same_configuration``). Within whatever pool
    that leaves, prefer one that also shares its sweep_id (an intentionally
    paired run).

    Returns ``None`` when ``run_id`` is not in the store, has no task_id, or
    no comparable passing sibling exists — never a guess at "the closest
    other run".
    """
    runs = read.list_runs(store)
    target = next((r for r in runs if r["run_id"] == run_id), None)
    if target is None or not target.get("task_id"):
        return None
    task_id = target["task_id"]
    candidates = [
        r for r in runs
        if r["run_id"] != run_id
        and r.get("task_id") == task_id
        and (r.get("outcome") or {}).get("status") == "PASSED"
    ]
    if not candidates:
        return None
    pool = [r for r in candidates if _same_configuration(target, r)]
    if not pool:
        return None
    same_sweep = [r for r in pool if target.get("sweep_id") and r.get("sweep_id") == target.get("sweep_id")]
    pool = same_sweep or pool
    return pool[0]["run_id"]


def _phase_kind_by_event(phases: list[dict]) -> dict[str, str]:
    lookup: dict[str, str] = {}
    for p in phases:
        for eid in p.get("event_ids") or []:
            lookup[eid] = p.get("kind")
    return lookup


def _tool_call_sequence(store: Store, run_id: str, capture_id: str) -> tuple[list[tuple], list[dict]]:
    """The comparable sequence for one run: (phase_kind, tool, content,
    input_identity) for every ``tool_call`` event, in order, alongside the
    raw event dicts (same index) so a matched/diverged pair can be reported
    with its event_id.

    The full ``action_signature`` — including its structured
    ``input_identity`` — is part of the comparison key (AGR-13): dropping it
    made two Edits of the same path with DIFFERENT replacements compare
    equal (same phase/tool/path), so a real behavioural divergence between a
    failing and a passing run went undetected whenever it took the shape of
    "edited the same file differently" rather than "touched a different
    file" — exactly the case ``action_signature`` (_util.py) exists to
    distinguish (R2).
    """
    events = store.read_derived(run_id, capture_id, "events.json") or []
    phases = store.read_derived(run_id, capture_id, "phases.json") or []
    phase_kind = _phase_kind_by_event(phases)
    seq: list[tuple] = []
    refs: list[dict] = []
    for e in events:
        if e.get("event_type") != "tool_call":
            continue
        sig = action_signature(DerivedEvent(**e))
        seq.append((phase_kind.get(e["event_id"]), sig[0], sig[1], sig[2]))
        refs.append(e)
    return seq, refs


def _labeled_action(ref: dict, key: tuple) -> dict:
    action = {
        "event_id": ref.get("event_id"),
        "phase_kind": key[0],
        "tool": key[1],
        "content": key[2],
    }
    if key[3]:
        # AGR-13: surface WHAT distinguished this action beyond tool+path —
        # e.g. an Edit's old/new strings — so a reviewer sees why two same-
        # path actions were reported as a divergence rather than a match.
        action["input_identity"] = key[3]
    return action


def divergence_report(
    store: Store, run_id: str, sibling_run_id: Optional[str] = None,
) -> Optional[dict]:
    """Align ``run_id``'s tool_call timeline against a passing sibling's,
    and report the first point of divergence plus the full alignment.

    Returns ``None`` when ``run_id`` is not in the store, or no sibling is
    given/found — the caller decides how to present "no sibling available".
    """
    runs = {r["run_id"]: r for r in read.list_runs(store)}
    target = runs.get(run_id)
    if target is None:
        return None
    sib_id = sibling_run_id or find_passing_sibling(store, run_id)
    if sib_id is None or sib_id not in runs:
        return None

    seq_a, refs_a = _tool_call_sequence(store, run_id, target["capture_id"])
    seq_b, refs_b = _tool_call_sequence(store, sib_id, runs[sib_id]["capture_id"])
    matcher = difflib.SequenceMatcher(None, seq_a, seq_b, autojunk=False)

    aligned: list[dict] = []
    first_divergence: Optional[dict] = None
    matched_prefix = 0
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            for k in range(i2 - i1):
                aligned.append({
                    "tag": "equal",
                    "failed": _labeled_action(refs_a[i1 + k], seq_a[i1 + k]),
                    "passing": _labeled_action(refs_b[j1 + k], seq_b[j1 + k]),
                })
            matched_prefix += i2 - i1
            continue
        if first_divergence is None:
            first_divergence = {
                "matched_prefix_length": matched_prefix,
                "failed_action": _labeled_action(refs_a[i1], seq_a[i1]) if i1 < i2 else None,
                "passing_action": _labeled_action(refs_b[j1], seq_b[j1]) if j1 < j2 else None,
            }
        span = max(i2 - i1, j2 - j1)
        for k in range(span):
            aligned.append({
                "tag": tag,
                "failed": _labeled_action(refs_a[i1 + k], seq_a[i1 + k]) if i1 + k < i2 else None,
                "passing": _labeled_action(refs_b[j1 + k], seq_b[j1 + k]) if j1 + k < j2 else None,
            })

    return {
        "failed_run_id": run_id,
        "passing_run_id": sib_id,
        "task_id": target.get("task_id"),
        "first_divergence": first_divergence,
        "aligned": aligned,
    }
