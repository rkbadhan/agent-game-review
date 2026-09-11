"""The deterministic reviewer envelope — Stages G, H, and I (spec §8.8–§8.10).

Milestone 4 is "the model reviewer". The whole product is built foundation-first,
so the *safety envelope* around the model is built and tested **before** any model
call — with zero model spend:

* **Stage G — fact validation (§8.8).** Every structured fact a candidate carries is
  recomputed from the source-derived records. A fact whose recomputed value
  contradicts the claim fails validation and the candidate is dropped. (The spec
  returns a failed fact once to the model and drops on a second failure; here there
  is no model to re-ask, so a failed recompute drops immediately.)
* **Stage H — attribution gate (§8.9).** Each moment's ``attribution_ceiling`` is the
  strongest attribution its supporting evidence slice licenses, capped at
  ``dependency_linked`` (the deterministic core never claims more). The rendered
  card statement is built from controlled templates whose causal language matches
  that ceiling, and the gate rejects any statement whose implied causal strength
  exceeds it.
* **Stage I — moment selection (§8.10).** Hard gates, de-duplication of cards that are
  facets of the same moment, a deterministic total order standing in for the model's
  pairwise ranking, and the review quota (≤3 negative, ≤2 positive, one recovery,
  one task/verifier concern, 3–5 total).

No taxonomy *verdict* is authored here — a verdict is the model reviewer's job
(Stage F). This envelope runs in ``deterministic_only`` mode and reserves
``ReviewMoment.taxonomy_verdict`` for Stage F to fill behind the same view.

The single entry point is :func:`run_reviewer`, analogous to
:func:`agr.detectors.run_detectors`. It accepts an optional ``reviewer`` implementing
the :class:`Reviewer` seam; the default :class:`DeterministicReviewer` wraps the
existing detector candidates, so the model reviewer drops in later with no rewiring.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional, Protocol

from . import version
from ._util import action_signature, is_mutation, is_tool_failure, paired_result, structured_input
from .recovery import GOOD_RECOVERY, UNRECOVERED
from .schema import (
    ATTRIBUTION_LEVELS,
    Candidate,
    CapabilityProfile,
    DerivedEvent,
    EvidenceSlice,
    RecoveryEpisode,
    ReviewMoment,
    TaskContract,
    VerifierCheck,
)

_ATTR_RANK = {level: i for i, level in enumerate(ATTRIBUTION_LEVELS)}
# The deterministic core never licenses attribution language above this level
# (README "relevance is not causality"; §8.4). The gate caps every ceiling here.
_DET_CEILING = "dependency_linked"

# Review quota (spec §8.10).
_MAX_NEGATIVE = 3
_MAX_POSITIVE = 2


@dataclass
class ReviewerContext:
    """Everything the reviewer envelope reads. All records are already derived."""

    run_id: str
    source_capture_id: str
    candidates: list[Candidate]
    slices: list[EvidenceSlice]
    checks: list[VerifierCheck]
    events: list[DerivedEvent]
    recoveries: list[RecoveryEpisode] = field(default_factory=list)
    contract: Optional[TaskContract] = None
    # What the capture actually observed (§6.2) and what the task declared.
    # Model discoveries pass the same capability requirements deterministic
    # detectors meet before their findings publish (AGR-03).
    profile: Optional[CapabilityProfile] = None
    declared_artifacts: list[str] = field(default_factory=list)
    # AGR-04: the complete task instruction, kept as its own reviewer input —
    # never a truncated timeline excerpt. Requirements are diagnosed against
    # what the task actually asked for, in full.
    task_instruction: Optional[str] = None
    # F1 follow-up (diagnostic delivery): the verifier's own post-run log
    # output, as attached by ingestion (``verifier.log_excerpts``). This is
    # explicitly post-run diagnostic evidence — labelled as such so the reviewer
    # never mistakes assertion output for something the agent observed.
    verifier_logs: list[dict] = field(default_factory=list)


@dataclass
class Enrichment:
    """The model reviewer's judgement for one candidate (spec §8.7).

    All fields are model-authored interpretation — kept, but ``source`` labels them
    model-generated so the UI can mark them (spec §8.8). Vocabulary fields are
    filtered to the active taxonomy in :func:`run_reviewer` before they reach a
    :class:`~agr.schema.ReviewMoment`; anything outside the active version is
    dropped, never rendered (spec §8.7).
    """

    taxonomy_verdict: Optional[str] = None
    behaviour_tags: list[str] = field(default_factory=list)
    phase: Optional[str] = None
    consequence: Optional[str] = None
    micro_abilities: list[str] = field(default_factory=list)
    root_cause_candidates: list[dict] = field(default_factory=list)
    better_action: Optional[str] = None
    instructional_value: Optional[str] = None
    eval_lesson_recommended: bool = False
    # Optional quoted spans the model cites as observational support for its
    # explanation prose. Each is verified against the recorded event text before
    # an explanation may be labelled evidence-linked (AGR follow-up F7: identifier
    # existence alone is reference validity, not semantic support).
    quotes: list[dict] = field(default_factory=list)
    source: Optional[str] = None  # e.g. "model:claude-opus-4-8"


@dataclass
class ProposedMoment:
    """A candidate the reviewer puts forward, with optional model enrichment.

    The deterministic reviewer proposes bare candidates (``enrichment=None``); a
    model reviewer proposes the same shape plus its taxonomy judgement. Either
    way the candidate's facts are recomputed (Stage G) before anything is published.
    """

    candidate: Candidate
    enrichment: Optional[Enrichment] = None


# --- Stage G: fact validation ------------------------------------------------


def validate_facts(candidate: Candidate, ctx: ReviewerContext) -> list[dict]:
    """Recompute each of a candidate's structured facts (spec §8.8).

    Returns the facts annotated with ``validation`` ("passed"/"failed") and the
    ``recomputed`` value. A fact type the envelope does not know how to recompute
    is marked ``unrecomputable``; a moment whose facts are ALL unrecomputable is
    ungrounded and fails Stage G (a semantic claim must carry at least one fact
    deterministic code actually verified).
    """
    check_by_id = {c.check_id: c for c in ctx.checks}
    events_by_id = {e.event_id: e for e in ctx.events}
    event_ids = {e.event_id for e in ctx.events}
    observed_artifacts = {
        e.payload.get("artifact_path")
        for e in ctx.events
        if e.event_type == "artifact_observation"
    }
    declared_artifacts = set(ctx.declared_artifacts)
    ep_by_failure = {ep.failure_event_id: ep for ep in ctx.recoveries}
    sig_by_event = {
        e.event_id: action_signature(e) for e in ctx.events if e.event_type == "tool_call"
    }
    strat_after = _strategy_change_between(ctx.events)
    terminal_type = _terminal_event_type(ctx.events)
    # Whether the agent's own trace shows it observing this check failing
    # before the run ended — the only basis for "still failing at submission"
    # phrasing. A post-run verifier result is not something the agent saw.
    # The map carries the agent's LAST observed status per check id (F1
    # follow-up): a captured "C1 PASSED" is a pass observation, and a later
    # observation supersedes an earlier one in order.
    observed_check_statuses = _agent_observed_check_statuses(ctx.events, terminal_type)

    out: list[dict] = []
    for fact in candidate.structured_facts:
        ftype = fact.get("type")
        annotated = dict(fact)
        if ftype == "requirement_status":
            check = check_by_id.get(fact.get("check_id"))
            claimed = fact.get("status_at_submission") or fact.get("status")
            # Exact status equality (AGR follow-up F1): a claim must match the
            # check's recorded status precisely. The old failed/nonfailed
            # agreement let a passing check submitted as ``passed`` validate and
            # then render as a failure.
            passed = check is not None and claimed == check.status
            recomputed = check.status if check else None
            # AGR-08 (review 82cc113): an in-session check (agr.verifier_synth,
            # source == "output_interpretation", timing == "during_run") is
            # built DIRECTLY from the agent's own trace — its source_pointers
            # already ARE the agent's observation of this result. It never
            # needs the check-id/status text pattern below, which only ever
            # matches when the agent's OWN OUTPUT happens to echo a check id
            # like "C1 FAILED" — never a synthesized id like insession_pytest_1,
            # so that pattern can never match an in-session check and always
            # reported "no agent-observed failure" for one, even with the
            # failing pytest/cargo/go output sitting right in the trace.
            in_session = check is not None and check.source == "output_interpretation" \
                and check.timing == "during_run"
            if in_session:
                annotated["status_basis"] = "in_session_observation"
                annotated["agent_observed_failure"] = bool(
                    passed and claimed == "failed" and check.status == "failed")
                annotated["agent_observed_status"] = check.status if check else None
            else:
                # What the recomputation can honestly support: the FINAL
                # verifier status is always known; "at submission" is only
                # known when the agent's own trace observed the failure
                # before the run ended (AGR-03: a later failing check is not
                # an agent-observed failure unless the trace supports that
                # timing and visibility).
                annotated["status_basis"] = "final_verifier"
                annotated["agent_observed_failure"] = bool(
                    passed and claimed == "failed"
                    and observed_check_statuses.get(fact.get("check_id")) == "failed")
                # The agent's last observed status for this check, if any —
                # the UI uses it to word the timing honestly (observed-pass
                # vs never seen).
                annotated["agent_observed_status"] = observed_check_statuses.get(
                    fact.get("check_id"))
            if check is not None:
                # Canonical fields (AGR follow-up F1): the displayed status and
                # expected/observed values are sourced from the check record,
                # never from the proposal — an invented observation cannot
                # survive inside a validated fact.
                annotated["status"] = check.status
                annotated["expected"] = check.expected
                annotated["observed"] = check.observed
        elif ftype == "absence":
            artifact = fact.get("declared_artifact")
            if artifact not in declared_artifacts:
                # Nothing in the task declares this artifact required — "absent"
                # is meaningless without a requirement to be absent from (AGR-03:
                # undeclared-artifact claims do not validate).
                annotated["validation"] = "failed"
                annotated["recomputed"] = "undeclared"
                out.append(annotated)
                continue
            in_scope = ctx.profile.meets("filesystem", "complete") if ctx.profile else True
            if artifact in observed_artifacts:
                recomputed = "observed"
                passed = False
            elif in_scope:
                # Filesystem was captured at checkpoints or better: not seeing
                # the artifact there is evidence it was absent from the run.
                recomputed = "absent_from_run"
                passed = True
            else:
                # Filesystem never captured: this is only "not observed in the
                # available evidence", never a claim about the environment.
                recomputed = "not_observed_in_captured_evidence"
                passed = True
            annotated["observation_scope"] = recomputed
        elif ftype == "repetition":
            evs = fact.get("events", [])
            sigs = [sig_by_event.get(e) for e in evs]
            recomputed = [list(s) if s else None for s in sigs]
            # Shared semantics with the detector (AGR follow-up F1): identical
            # action signatures alone are not "no new information" — the
            # recorded outputs must also be captured and equivalent. Two
            # identical polls returning different results are an observation.
            # AGR-10: a repeated mutation (Edit/Write) cannot be validated as
            # "no new information" from acknowledgement text alone — "ok"
            # says nothing about what the file became. detectors.py never
            # emits this claim for a mutation for exactly that reason; the
            # validator must refuse to pass a model-proposed one too, or a
            # repeated-mutation moment slips through Stage G on ack-text
            # equivalence alone.
            cited = [events_by_id.get(e) for e in evs]
            any_mutation = any(ev is not None and is_mutation(ev) for ev in cited)
            passed = (
                len(evs) >= 2
                and all(s is not None for s in sigs)
                and len(set(sigs)) == 1
                and not strat_after.get(tuple(evs[:2]), False)
                and not any_mutation
                and _outputs_equivalent(ctx.events, evs)
            )
        elif ftype == "state_transition":
            fev = fact.get("failure_event")
            ep = ep_by_failure.get(fev)
            claims_resolution = bool(fact.get("resolution_event"))
            if ep is None:
                # No derived episode indexes this failure: bare event existence
                # is NOT a validated transition (AGR follow-up F1). The claim
                # needs the actual failure→resolution relationship, which only
                # a recovery episode records.
                recomputed = "no_recovery_episode"
                passed = False
            else:
                recomputed = ep.classification
                if claims_resolution:
                    # A positive claim must name the episode's ACTUAL resolution
                    # event — not merely some event that exists.
                    passed = (ep.classification == GOOD_RECOVERY
                              and fact.get("resolution_event") == ep.resolution_event_id)
                else:  # unresolved-failure claim
                    passed = ep.classification == UNRECOVERED
                # Canonical fields (AGR follow-up F1): recovery evidence comes
                # from the episode, not the proposal.
                if claims_resolution:
                    annotated["resolution_event"] = ep.resolution_event_id
                annotated["strategy_changed"] = ep.strategy_changed
                annotated["changed_action"] = ep.changed_action
        elif ftype == "event_support":
            # Semantic-moment grounding: every named event must exist AND a
            # nonempty quoted span must actually appear in that event's text
            # (normalised). An empty quote matches everything, so it is
            # rejected, not silently matched (AGR-03).
            quotes = fact.get("quotes") or []
            recomputed = []
            for q in quotes:
                eid = q.get("event_id")
                quote = _norm_text(q.get("quote") or "")
                if not quote:
                    recomputed.append({"event_id": eid, "matched": False,
                                       "event_present": eid in event_ids,
                                       "reason": "empty_quote"})
                    continue
                ev = next((e for e in ctx.events if e.event_id == eid), None)
                matched = ev is not None and quote in _norm_text(_quotable_text(ev))
                recomputed.append({"event_id": eid, "matched": matched,
                                   "event_present": ev is not None})
            passed = bool(quotes) and all(r["matched"] for r in recomputed)
        elif ftype == "termination":
            passed = terminal_type == fact.get("expected")
            recomputed = terminal_type
        else:
            annotated["validation"] = "unrecomputable"
            out.append(annotated)
            continue
        annotated["validation"] = "passed" if passed else "failed"
        annotated["recomputed"] = recomputed
        out.append(annotated)
    return out


def _norm_text(s: str) -> str:
    """Normalise for quote matching: lowercase, collapse whitespace."""
    return " ".join((s or "").lower().split())


def _quotable_text(ev: DerivedEvent) -> str:
    """Everything a quote may legitimately be copied from for this event.

    ``DerivedEvent.text()`` only hoists a few display fields (content/data/
    path/tool/summary) — it does NOT include the structured ``tool_input``
    (an Edit's old/new strings, a Write's content) that the reviewer packet
    and expansion round hand the model as this event's retained evidence
    (R2). A quote copied verbatim from that structured input is authentic
    evidence and must not fail recomputation just because it is absent from
    the narrower display text.
    """
    si = structured_input(ev, max_chars=10**9)
    return ev.text() if si is None else f"{ev.text()} {si}"


def _outputs_equivalent(events: list[DerivedEvent], event_ids: list[str]) -> bool:
    """Whether the recorded tool results of repeated calls are equivalent.

    The same condition the deterministic repetition detector requires (AGR
    follow-up F1: detector and validator must share repetition semantics): each
    named call must have a captured result, and every result's text must match.
    Differing outputs — a poll whose report changed — mean new information.

    Review 2026-09-07 (R3): results are paired to their calls through the ONE
    shared id-based index (``paired_result``), not a second adjacency walk —
    so reordered parallel results pair correctly and unmatched ids stay
    unpaired, identical to the detector and recovery paths.
    """
    ids = list(event_ids)
    if len(ids) < 2:
        return False
    idx_by_id = {e.event_id: i for i, e in enumerate(events)}
    results: list[DerivedEvent] = []
    for eid in ids:
        i = idx_by_id.get(eid)
        if i is None:
            return False
        r = paired_result(events, i)
        if r is None:
            return False  # outputs not captured — the claim is unsupported
        results.append(r)
    base = " ".join(results[0].text().split())
    return all(" ".join(r.text().split()) == base for r in results[1:])


# Canonical observed-status vocabulary (review 2026-09-07): the parser emits
# one normalized vocabulary consumed identically by fact validation and
# rendering — a real ``C1 FAILED`` observation must yield
# ``agent_observed_failure: true`` end to end, never a case-mismatched miss.
_CANONICAL_STATUS = {
    "PASS": "passed", "PASSED": "passed",
    "FAIL": "failed", "FAILED": "failed",
    "ERROR": "error", "SKIPPED": "skipped",
}


def _agent_observed_check_statuses(events: list[DerivedEvent], terminal_type: str | None) -> dict[str, str]:
    """Check-id -> the agent's LAST observed status, from the agent's own trace.

    Only tool results / observations inside the trajectory can be something the
    agent saw; the verifier result is computed after the run. A check-id token
    alone is NOT an observation of its failure (F1 follow-up): an observed
    status requires the id AND a status word together, and the most recent
    observation wins — a check captured failing and later passing is not
    "still failing at submission". Values are the canonical lowercase status
    vocabulary (``passed`` / ``failed`` / ``error`` / ``skipped``).
    """
    statuses: dict[str, str] = {}
    for e in events:
        if e.event_type == terminal_type:
            break
        if e.event_type not in ("tool_result", "environment_observation"):
            continue
        # Raw text: check ids are conventionally uppercase, so normalization
        # (lowercasing) would hide them.
        for cid, word in _CHECK_STATUS_PATTERN.findall(e.text()):
            statuses[cid.upper()] = _CANONICAL_STATUS.get(word.upper(), word.lower())
    return statuses


# Bare check-id tokens like C3, T2, CHK12 — how tool output usually names them.
_CHECK_ID_PATTERN = re.compile(r"\b([A-Z]{1,4}\d{1,3})\b")

# An observed check status: the id and a status word close together on one
# line (e.g. "C1 PASSED", "C3: FAILED"). The id alone is not evidence about
# the check's outcome.
_CHECK_STATUS_PATTERN = re.compile(
    r"\b([A-Z]{1,4}\d{1,3})\b[^\n]{0,40}?\b(PASSED|FAILED|FAIL|PASS|ERROR|SKIPPED)\b")


def validate_references(candidate: Candidate, ctx: ReviewerContext) -> dict:
    """Structural reference validation (AGR-03): no phantom or dangling pointers.

    Every anchor event, affected check, and affected contract item must resolve
    against the actual capture. A reference to an entity that does not exist is
    malformed evidence — the finding may still be *about* something real, but as
    authored it points nowhere, so it cannot publish until it points somewhere.
    """
    event_ids = {e.event_id for e in ctx.events}
    check_ids = {c.check_id for c in ctx.checks}
    item_ids = {i.id for i in ctx.contract.items} if ctx.contract else None

    dangling: dict[str, list[str]] = {}
    anchors = candidate.anchor_event_ids
    if not anchors:
        dangling["anchor_event_ids"] = ["<empty>"]
    else:
        missing = [a for a in anchors if a not in event_ids]
        if missing:
            dangling["anchor_event_ids"] = missing
    missing_checks = [c for c in candidate.affected_checks if c not in check_ids]
    if missing_checks:
        dangling["affected_checks"] = missing_checks
    if item_ids is not None:
        missing_items = [i for i in candidate.affected_contract_items if i not in item_ids]
        if missing_items:
            dangling["affected_contract_items"] = missing_items

    if not dangling:
        return {"status": "resolved"}
    return {"status": "dangling", "dangling": dangling}


def observability_for(candidate: Candidate, ctx: ReviewerContext) -> str:
    """Computed observability gate (AGR-03) — never a hardcoded "supported".

    Deterministic detectors are capability-gated before they reach the envelope
    (``evaluated=False`` otherwise), so their findings publish only when the
    profile met the requirement. Model discoveries have no detector gate, so
    the requirements implied by the fact types they assert are checked here
    against the same capability profile. A candidate whose facts need evidence
    the capture does not contain is "unsupported", never published as if the
    capture backed it.
    """
    if candidate.detector != "model":
        return "supported"  # the detector's own capability gate already ran
    profile = ctx.profile
    if profile is None:
        return "unverifiable"
    required: dict[tuple[str, str], None] = {}
    for fact in candidate.structured_facts:
        ftype = fact.get("type")
        if ftype == "event_support":
            required[("messages", "complete")] = None
        elif ftype in ("repetition", "state_transition"):
            required[("tool_results", "complete")] = None
        elif ftype == "requirement_status":
            required[("messages", "complete")] = None
        elif ftype == "absence":
            required[("filesystem", "checkpoint_only")] = None
        elif ftype == "termination":
            required[("messages", "complete")] = None
    unmet = [f"{cap}<{minimum}" for cap, minimum in required
             if not profile.meets(cap, minimum)]
    if unmet:
        return "unsupported: " + ", ".join(sorted(unmet))
    return "supported"


# Event types that can terminate a run (adapter 0.4 semantics).
_TERMINAL_EVENT_TYPES = {
    "final_submission", "run_completed", "run_timed_out", "run_failed", "run_finished",
}


def _terminal_event_type(events: list[DerivedEvent]) -> str | None:
    """The run's terminal event type, or None if no terminal event exists."""
    for e in reversed(events):
        if e.event_type in _TERMINAL_EVENT_TYPES:
            return e.event_type
    return None


def _strategy_change_between(events: list[DerivedEvent]) -> dict:
    """Map (e1, e2) -> whether a strategy_change occurred between them in order."""
    order = {e.event_id: i for i, e in enumerate(events)}
    changes = [i for i, e in enumerate(events) if e.event_type == "strategy_change"]
    out: dict = {}
    for i, a in enumerate(events):
        for b in events[i + 1 :]:
            lo, hi = order[a.event_id], order[b.event_id]
            out[(a.event_id, b.event_id)] = any(lo < c < hi for c in changes)
    return out


def _facts_valid(validated: list[dict]) -> bool:
    """A candidate survives Stage G only if grounded: at least one *passed*
    recomputable fact, and no recomputable fact false.

    An all-unrecomputable candidate asserts things no deterministic code could
    verify — it is ungrounded by definition and fails (spec §8.8: "if it does
    not recompute, the claim is dropped").
    """
    if any(f.get("validation") == "failed" for f in validated):
        return False
    return any(f.get("validation") == "passed" for f in validated)


def _fact_errors(validated: list[dict]) -> list[dict]:
    """The validation errors to return once to the reviewer (spec §8.8 step 4)."""
    return [
        {"type": f.get("type"), "recomputed": f.get("recomputed"), "claim": f}
        for f in validated
        if f.get("validation") == "failed"
    ]


# --- Stage H: attribution gate + controlled rendering ------------------------


def _linked_slices(candidate: Candidate, slices: list[EvidenceSlice]) -> list[EvidenceSlice]:
    """Evidence slices supporting a candidate.

    Omission/distributed slices are keyed on the candidate id (that is how
    ``pipeline.analyze`` builds them); standard/external slices are keyed on the
    check id, so a candidate links via its ``affected_checks``.
    """
    checks = set(candidate.affected_checks)
    return [s for s in slices if s.check_id == candidate.candidate_id or s.check_id in checks]


def attribution_ceiling_for(candidate: Candidate, slices: list[EvidenceSlice]) -> str:
    """Strongest attribution the candidate's slices license, capped deterministically."""
    linked = _linked_slices(candidate, slices)
    if not linked:
        # No failed-check slice: a positive recovery still rests on an observed
        # failure→resolution path; a bare negative without a slice stays weakest.
        return _DET_CEILING if candidate.polarity == "positive" else "hypothesized"
    best = max(linked, key=lambda s: _ATTR_RANK.get(s.attribution_ceiling, 0))
    ceiling = best.attribution_ceiling
    if _ATTR_RANK.get(ceiling, 0) > _ATTR_RANK[_DET_CEILING]:
        return _DET_CEILING
    return ceiling


# Causal phrases and the minimum attribution level each one implies (spec §8.9).
_IMPLIES = {
    "counterfactually_supported": ("would have", "in the replay", "had it ", "changed c"),
    "direct": ("produced", "caused", "resulted in", "because it", "made the"),
    "dependency_linked": ("linked to", "linked with"),
}


def implied_attribution(statement: str) -> str:
    """The strongest attribution level the wording of ``statement`` implies."""
    low = statement.lower()
    for level in ("counterfactually_supported", "direct", "dependency_linked"):
        if any(phrase in low for phrase in _IMPLIES[level]):
            return level
    return "hypothesized"


def attribution_gate(statement: str, ceiling: str) -> tuple[bool, str]:
    """Reject wording whose implied causal strength exceeds ``ceiling`` (spec §8.9).

    Returns ``(ok, implied_level)``. The deterministic renderer builds statements
    at or below the ceiling by construction, so this always passes for its own
    output; it is the enforcement point a future model statement must clear.
    """
    implied = implied_attribution(statement)
    ok = _ATTR_RANK.get(implied, 0) <= _ATTR_RANK.get(ceiling, 0)
    return ok, implied


_LINK_CLAUSE = {
    "dependency_linked": ", and is linked to the failed outcome",
    "hypothesized": ", a likely explanation for the outcome",
}


def render(fact: dict, ceiling: str, polarity: str, observation_scope: bool = False) -> str:
    """Render a card statement from controlled templates, gated by ``ceiling``.

    Causal language is chosen to match the attribution level: ``dependency_linked``
    yields "linked to", never "produced". Pure status facts carry no causal verb
    and read the same at any ceiling.

    ``observation_scope`` (review 2026-09-07 R1) marks a negative finding whose
    support is recorded tool evidence but which carries NO check/contract link
    (typically: no verifier exists). The template states the observation and
    its coverage limit explicitly — task correctness stays unknown, and the
    final implementation is never asserted incorrect.
    """
    ftype = fact.get("type")
    link = _LINK_CLAUSE.get(ceiling, _LINK_CLAUSE["hypothesized"])
    if ftype == "requirement_status":
        # Canonical fields only: status/expected/observed were sourced from the
        # check record during validation (F1), and a fact that survives carries
        # the check's actual status — so a passing check can never render as a
        # failure, and claimed detail never outruns the record.
        status = fact.get("status")
        detail = ""
        if fact.get("expected") is not None or fact.get("observed") is not None:
            detail = f" (expected {fact.get('expected')}, observed {fact.get('observed')})"
        # AGR-08 (review 82cc113): an in-session check (agr.verifier_synth) is
        # not the run's post-run verifier — it is a test invocation the agent
        # itself ran and observed mid-trace. Naming it "the run's final
        # verifier" misdescribes where the evidence came from.
        source_label = ("an in-session observation" if fact.get("status_basis") == "in_session_observation"
                        else "the run's final verifier")
        if status == "passed":
            return f"Requirement check {fact.get('check_id')} passed {source_label}."
        if status not in ("failed", None):
            return (f"Requirement check {fact.get('check_id')} ended with status '{status}' in "
                    f"{source_label}; the outcome is undetermined.")
        if fact.get("agent_observed_failure"):
            # The agent's own trace shows it seeing this failure before the run
            # ended, so "still failing at submission" is evidence-backed.
            return f"Requirement check {fact.get('check_id')} was still failing at submission{detail}."
        # A post-run verifier result is not something the agent saw (AGR-03:
        # final verifier status ≠ status known at submission).
        return (f"Requirement check {fact.get('check_id')} failed {source_label}"
                f"{detail}; the agent's trace records no observation of this check.")
    if ftype == "repetition":
        evs = fact.get("events", [])
        return f"The same action was repeated with no new information in between ({', '.join(evs)})."
    if ftype == "absence":
        if fact.get("observation_scope") == "not_observed_in_captured_evidence":
            # Filesystem was never captured: absence is only observational.
            return (f"The declared artifact {fact.get('declared_artifact')} was not observed in the "
                    f"captured evidence; the capture does not establish it was absent from the "
                    f"environment{link}.")
        return f"The declared artifact {fact.get('declared_artifact')} was never observed in the run{link}."
    if ftype == "state_transition":
        # Item 32 follow-up: lead with WHAT failed (the tool, and its own
        # diagnostic text) instead of only WHERE (an anonymous event id) — the
        # same identity fleet.py already groups these episodes by, restated
        # here so a reader gets "bash failed: ModuleNotFoundError: No module
        # named 'numpy'" instead of "a tool failure at evt_012". Falls back to
        # a bare tool name when the failure text carried no recognised
        # diagnostic (an opaque fallback signature — the fleet view's
        # "Unclassified" case) rather than showing a meaningless line.
        tool = fact.get("tool")
        diag = fact.get("failure_diagnostic") or fact.get("error_signature")
        diag_usable = diag and fact.get("error_signature_basis") != "fallback_last_nonempty"
        lead = (f"{tool} failed: {diag}" if (tool and diag_usable)
                else f"The {tool} call failed" if tool else "A tool call failed")
        if fact.get("resolution_event"):
            return f"{lead}, then recovered via a strategy change before submission."
        if observation_scope:
            # R1: no check/contract link exists (usually no verifier). The
            # observation states what the capture recorded and its limit —
            # never that the task outcome or final implementation is wrong.
            return (f"{lead}, and no resolving check appears in the available capture; "
                    f"whether the task's final state is correct remains unknown.")
        return f"{lead}, and was left unresolved before submission{link}."
    if ftype == "event_support":
        evs = [q.get("event_id") for q in (fact.get("quotes") or []) if q.get("event_id")]
        return f"Grounded in quoted run evidence ({', '.join(evs)}){link}."
    if ftype == "termination":
        expected = fact.get("expected", "unknown")
        label = {
            "run_timed_out": "The run reached its time limit",
            "run_failed": "The run ended in an error",
            "run_completed": "The run ended without a submission signal",
            "final_submission": "The agent submitted",
        }.get(expected, f"The run terminated via {expected}")
        return f"{label}."
    return fact.get("type", "candidate")


def _render_requirement_status_group(facts: list[dict]) -> str:
    """One statement covering EVERY requirement_status fact on an aggregate
    terminal-failure candidate (AGR-08), instead of the per-check sentence
    :func:`render` produces for a single fact — otherwise the same terminal
    statement would repeat once per failing check, crowding out other
    findings on the run.

    Only called with facts that already passed validation (canonical
    status/expected/observed sourced from the check record — see
    ``validate_facts``), so this only groups and phrases them; it invents no
    status of its own. Facts are grouped by exactly the same distinction
    :func:`render` draws for a single fact: agent-observed-at-submission vs.
    final-verifier-only vs. a non-failed/undetermined status.
    """
    observed = [f["check_id"] for f in facts if f.get("status") == "failed" and f.get("agent_observed_failure")]
    unobserved = [f["check_id"] for f in facts if f.get("status") == "failed" and not f.get("agent_observed_failure")]
    other = [f for f in facts if f.get("status") not in ("failed", None)]
    total = next((f.get("total_checks") for f in facts if f.get("total_checks") is not None), None)
    denominator = f" ({len(facts)} of {total} checks)" if total is not None else ""
    clauses = []
    if observed:
        clauses.append(f"still failing at submission: {', '.join(sorted(observed))}")
    if unobserved:
        # AGR-08 (review 82cc113): "the run's final verifier" only describes a
        # post-run check — an in-session check the agent ran itself never
        # reaches this bucket for a status the agent actually observed
        # (see validate_facts), but the wording still names its real source
        # rather than assuming every failing check came from a post-run verifier.
        by_check = {f["check_id"]: f for f in facts}
        unobs_source = ("an in-session observation"
                        if all(by_check[cid].get("status_basis") == "in_session_observation" for cid in unobserved)
                        else "the run's final verifier")
        clauses.append(
            f"failing {unobs_source}, with no agent-observed failure before the run "
            f"ended: {', '.join(sorted(unobserved))}")
    for f in other:
        clauses.append(f"{f.get('check_id')} ended with status '{f.get('status')}' (undetermined)")
    if not clauses:
        return "No deterministic evidence supports this finding."
    statement = f"Requirement checks{denominator} were " + "; and ".join(clauses) + "."
    if facts and facts[0].get("last_agent_action_captured") is False:
        statement += (" No agent action was captured before this terminal event; the anchor "
                      "reflects only the run's terminal state, not a specific action.")
    return statement


# --- Stage I: moment selection ------------------------------------------------


# Declared selection gates, in evaluation order, with the passing value each
# must carry. Anything outside this list (validation_attempts, better_action,
# explanation_*) is status metadata, not a rejection gate.
_REJECTION_GATES = {
    "fact_validation": "passed",
    "references": "resolved",
    "contract_link": "present",
    "observability": "supported",
    "attribution": "within_ceiling",
}


def _link_gate(m: "ReviewMoment") -> bool:
    """The §8.10 concern gate, with the R1 observation path.

    Passes when the moment carries a check/contract concern — or, failing
    that, when it is a supported observation backed by recorded tool evidence
    (review 2026-09-07 R1). Shared by selection and rejection accounting so
    the two never disagree.
    """
    if m.gate_results.get("contract_link") == "present":
        return True
    return (m.gate_results.get("contract_link") == "absent"
            and m.gate_results.get("observation_basis") == "tool_evidence")


def _observation_basis(cand: Candidate, validated: list[dict], ctx: ReviewerContext,
                       refs: dict, has_concern: bool) -> str:
    """R1 supported-observation gate, scoped by evidence type (R4).

    A negative finding with no check/contract link (typically: no verifier
    exists) is selectable as a supported OBSERVATION only when a validated
    fact's evidence actually establishes the proposed negative *tool*
    behaviour — not merely because some fact validated and references
    resolved. Returns ``tool_evidence`` | ``unsupported`` | ``n/a``.

    Supported observation types and their required evidence:
      * ``state_transition`` — an observed failed tool: the named
        ``failure_event`` must be a tool-result/error event the capture shows
        failing (the failed-tool/no-verifier card).
      * ``repetition`` — correctly paired equivalent actions with no new
        information (the detector and validator already enforce id-based
        pairing and output equivalence).

    Authentic quotation (``event_support``) establishes neither a negative
    behaviour nor an explanation of an outcome, so a message-quote-only
    negative finding is ``unsupported`` and does not publish. Message-only
    concerns can be supported too, but need their own stated evidentiary rule;
    none is defined yet, so they stay unsupported.
    """
    if not (cand.polarity == "negative" and cand.kind != "recovery"
            and not has_concern and not ctx.checks):
        return "n/a"
    if not (_facts_valid(validated) and refs.get("status") == "resolved"):
        return "unsupported"
    events_by_id = {e.event_id: e for e in ctx.events}
    for f in validated:
        if f.get("validation") != "passed":
            continue
        ftype = f.get("type")
        if ftype == "state_transition":
            fev = events_by_id.get(f.get("failure_event"))
            if fev is not None and is_tool_failure(fev):
                return "tool_evidence"
        elif ftype == "repetition":
            return "tool_evidence"
        # event_support / absence / requirement_status / termination: a quote or
        # a bare status/termination does not establish a negative tool
        # behaviour, so it cannot carry an observation-scoped negative card.
    return "unsupported"


def _value_key(m: ReviewMoment) -> tuple:
    """Deterministic stand-in for the model's pairwise ranking (spec §8.10).

    Higher is better: touches a failed check (outcome relevance), then evidence
    strength (attribution rank), then a real contract item, then earlier in the
    timeline. No score is stored or displayed — only the resulting order.
    """
    return (
        1 if m.affected_checks else 0,
        _ATTR_RANK.get(m.attribution_ceiling, 0),
        1 if m.affected_contract_items else 0,
        -(m.sequence if m.sequence is not None else 1_000_000),
    )


def _primary_anchor(m: ReviewMoment) -> str:
    return m.anchor_event_ids[0] if m.anchor_event_ids else m.moment_id


def _fact_subject(m: ReviewMoment) -> str | None:
    """The mechanical subject of a card's first determinable validated fact —
    the issue identity its evidence supports (AGR-05). ``requirement_status``
    → its check, ``state_transition`` → the failing event, ``absence`` → the
    artifact, ``repetition`` → the action signature, ``event_support`` → the
    quoted events. None when no fact determines a subject."""
    for f in m.validated_facts:
        t = f.get("type")
        if t == "requirement_status" and f.get("check_id"):
            return f"check:{f['check_id']}"
        if t == "state_transition" and f.get("failure_event"):
            return f"failure:{f['failure_event']}"
        if t == "absence" and f.get("declared_artifact"):
            return f"artifact:{f['declared_artifact']}"
        if t == "repetition" and f.get("signature"):
            return "repetition:" + "|".join(str(x) for x in f["signature"])
        if t == "event_support" and f.get("quotes"):
            ids = sorted(q.get("event_id", "") for q in f["quotes"] if isinstance(q, dict))
            if ids:
                return "quote:" + ",".join(ids)
    return None


def _same_moment(a: ReviewMoment, b: ReviewMoment) -> bool:
    """Whether two cards are facets of the *same* decisive moment (spec §8.10 gate 4).

    AGR-05: deduplication runs on supported issue identity, not merely on a
    shared affected check. Same polarity and the same fact subject (same
    requirement, same failing event, same artifact…) collapse — this folds a
    chatty model reviewer's duplicate cards even when it anchors them
    differently. Cards sharing a check but asserting DIFFERENT subjects are
    distinct contributing problems and both survive — even against a shared
    aggregate check. When neither card's facts determine a subject, fall back
    to the anchor rule: a shared primary anchor while at least one carries no
    check. Two cards with different subjects are never merged for sharing an
    anchor, so distinct issues stay distinct and recall is preserved.
    """
    if a.polarity != b.polarity:
        return False
    subject_a, subject_b = _fact_subject(a), _fact_subject(b)
    if subject_a is not None and subject_b is not None:
        return subject_a == subject_b
    checks_a, checks_b = set(a.affected_checks), set(b.affected_checks)
    if checks_a & checks_b:
        return True
    if _primary_anchor(a) == _primary_anchor(b) and (not checks_a or not checks_b):
        return True
    return False


def select_moments(moments: list[ReviewMoment]) -> list[ReviewMoment]:
    """Apply the hard gates, de-duplicate, rank, and enforce the quota (spec §8.10).

    Mutates each moment's ``selected`` / ``selection_rank`` / ``superseded_by`` and
    returns the same list. De-duplication is the precision win: cards that are facets
    of the *same* moment (see :func:`_same_moment`) collapse to the higher-value one,
    so a model reviewer that surfaces the same moment twice does not cost precision.
    Recall is preserved because distinct requirements are never merged and a card is
    only ever superseded by a higher-value card for the same moment.

    Review 2026-09-07 (R1): a negative finding with NO check/contract link can
    still be selected as a supported OBSERVATION — its ``observation_basis``
    gate must show the claim is backed by recorded tool evidence (facts
    validated, references resolved, observability supported). The stronger
    failed-task/causal claim still requires the contract link; the
    observation-scoped card renders with its coverage limits and never asserts
    the task outcome.
    """
    def _passes_link(m: ReviewMoment) -> bool:
        return _link_gate(m)

    passing = [m for m in moments if m.gate_results.get("fact_validation") == "passed"
               and m.gate_results.get("references", {}).get("status") == "resolved"
               and m.gate_results.get("observability") == "supported"
               and _passes_link(m)
               and m.gate_results.get("attribution") == "within_ceiling"]

    # Union-find over "same moment": transitively group facets so a card that links
    # to two others (by check and by anchor) pulls them into one group.
    parent = list(range(len(passing)))

    def _find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(passing)):
        for j in range(i + 1, len(passing)):
            if _same_moment(passing[i], passing[j]):
                parent[_find(j)] = _find(i)

    grouped: dict[int, list[ReviewMoment]] = {}
    for i, m in enumerate(passing):
        grouped.setdefault(_find(i), []).append(m)
    survivors: list[ReviewMoment] = []
    for group in grouped.values():
        group.sort(key=_value_key, reverse=True)
        winner = group[0]
        survivors.append(winner)
        for loser in group[1:]:
            loser.superseded_by = winner.moment_id

    # Rank (negatives before positives, then by value) and apply the quota.
    survivors.sort(key=lambda m: (m.polarity == "positive", *(-v for v in _value_key(m))))
    n_neg = n_pos = 0
    rank = 0
    for m in survivors:
        if m.polarity == "positive":
            if n_pos >= _MAX_POSITIVE:
                continue
            n_pos += 1
        else:
            if n_neg >= _MAX_NEGATIVE:
                continue
            n_neg += 1
        m.selected = True
        m.selection_rank = rank
        rank += 1
    return moments


# --- the reviewer seam -------------------------------------------------------


class Reviewer(Protocol):
    """The Stage F plug point.

    ``propose`` returns the moments (candidate + optional enrichment) the reviewer
    puts forward. The deterministic default returns the detector candidates with no
    enrichment; a model reviewer returns the same shape plus its taxonomy judgement.
    Every downstream stage (validation, attribution gate, selection) is identical.

    ``revise`` implements the spec §8.8 return-once loop: when a candidate's facts
    fail recomputation, it is handed back once with the errors for a corrected
    candidate; a second failure drops it. The deterministic reviewer has no model
    to re-ask, so its ``revise`` returns ``None`` (drop immediately).

    ``reviewer_key`` is the stable slot name (``"deterministic"`` or
    ``"model:<source>"``) under which this reviewer's snapshot is persisted so
    the reviewer-diff view can address it alongside other reviews of the same
    capture (spec §6.12).
    """

    review_mode: str
    reviewer_key: str

    def propose(self, ctx: ReviewerContext) -> list[ProposedMoment]:
        ...

    def revise(self, candidate: Candidate, errors: list[dict],
               ctx: ReviewerContext) -> Optional[Candidate]:
        ...


class DeterministicReviewer:
    """Default reviewer: the detector candidates, no model call (spec §8.7 note).

    In ``deterministic_only`` mode Stages C2 and F are skipped, so the candidates
    the envelope validates are exactly the deterministic detector candidates, with
    no taxonomy verdict authored.
    """

    review_mode = "deterministic_only"
    reviewer_key = "deterministic"

    def propose(self, ctx: ReviewerContext) -> list[ProposedMoment]:
        return [ProposedMoment(candidate=c) for c in ctx.candidates]

    def revise(self, candidate, errors, ctx):
        return None  # no model to re-ask — a failed fact simply drops


def _quote_authenticity(enr: Enrichment, ctx: ReviewerContext) -> str:
    """Whether the explanation's quoted spans actually appear in the named
    events' recorded text (review 2026-09-07): its own dimension, separate
    from whether the prose around it is supported. ``authentic`` | ``dangling``
    | ``none``."""
    quotes = enr.quotes or []
    if not quotes:
        return "none"
    for q in quotes:
        eid = q.get("event_id")
        quote = _norm_text(q.get("quote") or "")
        ev = next((e for e in ctx.events if e.event_id == eid), None)
        if not quote or ev is None or quote not in _norm_text(_quotable_text(ev)):
            return "dangling"
    return "authentic"


def _explanation_support(enr: Enrichment, ctx: ReviewerContext) -> str:
    """Semantic support status for model explanations (AGR-03; F1 follow-up).

    Three dimensions, never conflated (review 2026-09-07: matching a quote
    establishes QUOTE AUTHENTICITY — it does not support the accompanying
    factual assertions, so an authentic quote no longer upgrades invented
    explanation prose to evidence-linked):

    * *reference validity* — an identifier the prose names must exist, else
      ``dangling_references``;
    * *quote authenticity* — reported separately (``quote_authenticity``):
      every quoted span must actually appear in the named event's recorded
      text; a failed quote is misrepresentation and fails the explanation;
    * *explanation support* — prose is ``evidence_linked`` only when the
      explanation states nothing beyond the quoted evidence (each nonempty
      prose field's text is contained in the quoted spans). Everything else
      is ``interpretation_only`` — invented database/outage rationales stay
      interpretation, however true an unrelated quoted span is. It stays
      visible, labelled as interpretation.
    """
    texts = [enr.consequence or "", enr.instructional_value or ""]
    texts += [str(rc.get("rationale") or "") for rc in enr.root_cause_candidates]
    check_ids = {c.check_id for c in ctx.checks}
    event_ids = {e.event_id for e in ctx.events}
    for text in texts:
        for cid in _CHECK_ID_PATTERN.findall(text):
            if cid.upper() not in check_ids and cid not in event_ids:
                return "dangling_references"
    # Quote authenticity: every quoted span must actually appear in the
    # recorded event text, exactly like an event_support fact (empty quotes
    # and non-matching spans fail — never silently match).
    quoted: list[str] = []
    for q in (enr.quotes or []):
        eid = q.get("event_id")
        quote = _norm_text(q.get("quote") or "")
        ev = next((e for e in ctx.events if e.event_id == eid), None)
        if not quote or ev is None or quote not in _norm_text(_quotable_text(ev)):
            return "dangling_references"
        quoted.append(quote)
    if not quoted:
        return "interpretation_only"
    # Evidence-linked prose only when it says nothing beyond the quotes: every
    # nonempty prose field must be contained in the quoted evidence itself.
    joined = " ".join(quoted)
    if all((not t.strip()) or (_norm_text(t) and _norm_text(t) in joined) for t in texts):
        return "evidence_linked"
    return "interpretation_only"


def _explanation_attribution(enr: Enrichment, ceiling: str) -> str:
    """Attribution check applied to every displayed explanation field (AGR-03).

    Phrase lists cannot guarantee causal correctness, so this is recorded and
    displayed, not treated as proof: an explanation whose wording implies more
    causality than the evidence ceiling licenses is flagged "overclaim".
    """
    texts = [enr.better_action or "", enr.consequence or ""]
    texts += [str(rc.get("rationale") or "") for rc in enr.root_cause_candidates]
    for text in texts:
        implied = implied_attribution(text)
        if _ATTR_RANK.get(implied, 0) > _ATTR_RANK.get(ceiling, 0):
            return "overclaim"
    return "within_ceiling"


def _apply_enrichment(moment: ReviewMoment, enr: Enrichment) -> None:
    """Copy an :class:`Enrichment` onto a moment, filtered to the active taxonomy.

    Anything outside the controlled vocabulary is dropped, never rendered
    (spec §8.7). Free-text interpretations are kept and marked model-generated.
    """
    from . import taxonomy

    moment.taxonomy_verdict = enr.taxonomy_verdict
    moment.behaviour_tags = [t for t in enr.behaviour_tags if t in taxonomy.BEHAVIOUR_TAGS]
    moment.phase = enr.phase if enr.phase in taxonomy.PHASES else None
    moment.consequence = enr.consequence if enr.consequence in taxonomy.CONSEQUENCES else None
    moment.micro_abilities = [m for m in enr.micro_abilities if m in taxonomy.MICRO_ABILITIES]
    moment.root_cause_candidates = [
        rc for rc in enr.root_cause_candidates if rc.get("locus") in taxonomy.ROOT_CAUSE_LOCI
    ]
    moment.better_action = enr.better_action
    moment.instructional_value = enr.instructional_value
    moment.eval_lesson_recommended = bool(enr.eval_lesson_recommended)
    moment.enrichment_source = enr.source
    moment.taxonomy_version = version.TAXONOMY_VERSION


def run_reviewer(ctx: ReviewerContext, reviewer: Optional[Reviewer] = None,
                 telemetry: Optional[dict] = None) -> list[ReviewMoment]:
    """Build the reviewed moments for one run (Stages G→H→I).

    Analogous to :func:`agr.detectors.run_detectors`. Every proposed candidate is
    validated (G, with the §8.8 return-once revise loop), given an attribution
    ceiling and a controlled rendering (H), enriched with the model's gated
    taxonomy judgement, then collectively gated, de-duplicated, and ranked (I).

    ``telemetry``, when given, is filled with measurable review-cost records
    (AGR-06): how many proposals were rejected and by which gate.
    """
    reviewer = reviewer or DeterministicReviewer()
    seq_of = {e.event_id: e.sequence for e in ctx.events}
    phase_of = {e.event_id: e.phase_id for e in ctx.events}

    moments: list[ReviewMoment] = []
    for proposal in reviewer.propose(ctx):
        cand = proposal.candidate
        validated = validate_facts(cand, ctx)
        # Stage G return-once loop (§8.8): a failed fact goes back to the reviewer
        # once; a corrected candidate is re-validated; a second failure is left
        # failed (and therefore unselected).
        attempts = 1
        if not _facts_valid(validated):
            revised = reviewer.revise(cand, _fact_errors(validated), ctx)
            if revised is not None:
                cand = revised
                validated = validate_facts(cand, ctx)
                attempts = 2

        ceiling = attribution_ceiling_for(cand, ctx.slices)
        anchor = cand.anchor_event_ids[0] if cand.anchor_event_ids else None
        linked = _linked_slices(cand, ctx.slices)
        # Gate 2 (§8.10) requires a relevant contract/verifier concern for a
        # *negative* decisive moment. Positive moments and recoveries are surfaced
        # under their own quota lines (a reinforcing behaviour, gate 5), so the
        # concern requirement does not apply to them.
        has_concern = bool(cand.affected_checks or cand.affected_contract_items or linked)
        contract_link = "present" if (has_concern or cand.polarity == "positive"
                                       or cand.kind == "recovery") else "absent"
        # Review 2026-09-07 (R1/R4): a negative finding with no check/contract
        # link (typically: no verifier exists at all) is a SUPPORTED
        # OBSERVATION only when a validated fact's evidence actually
        # establishes the proposed negative *tool* behaviour — not merely
        # because some fact validated and references resolved. With a verifier
        # present, concern linkage is establishable — the §8.10 gate keeps
        # applying and unlinked negatives stay unselected.
        refs = validate_references(cand, ctx)
        obs_basis = _observation_basis(cand, validated, ctx, refs, has_concern)
        observation_scope = obs_basis == "tool_evidence"
        # Render from the fact(s) that actually PASSED validation — never
        # from a failed or unrecomputable fact, whose "recomputed" basis does
        # not exist (AGR-03: unknown fact types must not become validated prose).
        passed_facts = [f for f in validated if f.get("validation") == "passed"]
        requirement_status_facts = [f for f in passed_facts if f.get("type") == "requirement_status"]
        if len(requirement_status_facts) > 1:
            # AGR-08: an aggregate terminal-failure candidate carries one
            # requirement_status fact per failing check — one joint statement,
            # not the per-fact render() repeated once per check.
            statement = _render_requirement_status_group(requirement_status_facts)
        else:
            primary = passed_facts[0] if passed_facts else None
            statement = render(primary, ceiling, cand.polarity,
                               observation_scope=observation_scope) if primary else \
                "No deterministic evidence supports this finding."
        gate_ok, _ = attribution_gate(statement, ceiling)
        enr = proposal.enrichment
        better_action = (
            "model_provided" if (enr and enr.better_action) else "not_available_deterministic"
        )
        gate_results = {
            "fact_validation": "passed" if _facts_valid(validated) else "failed",
            "validation_attempts": attempts,
            "references": refs,  # resolved | dangling (+ dangling detail)
            "contract_link": contract_link,
            # R1/R4: for an observation-scoped negative finding, whether the
            # claim is backed by recorded tool evidence of the specific
            # negative behaviour (the thing that makes it selectable without
            # a contract link). ``n/a`` when a contract link already carries
            # the moment; ``unsupported`` when the evidence type does not
            # establish a tool-behaviour observation (e.g. a quote-only
            # finding).
            "observation_basis": obs_basis,
            # Computed from the capability profile for model discoveries;
            # deterministic detectors were capability-gated before this envelope.
            "observability": observability_for(cand, ctx),
            "attribution": "within_ceiling" if gate_ok else "overclaim",
            "better_action": better_action,
        }
        if enr is not None:
            # Model explanations are interpretation until evidence links them:
            # semantic support gets its own review status (AGR-03), separate
            # from the validated facts above. Review 2026-09-07: quote
            # authenticity is reported as its OWN dimension — a matching quote
            # never upgrades the accompanying prose to evidence-linked.
            gate_results["explanation_support"] = _explanation_support(enr, ctx)
            gate_results["quote_authenticity"] = _quote_authenticity(enr, ctx)
            gate_results["explanation_attribution"] = _explanation_attribution(enr, ceiling)
        moment = ReviewMoment(
            moment_id=f"mom_{cand.candidate_id}",
            run_id=ctx.run_id,
            source_capture_id=ctx.source_capture_id,
            candidate_id=cand.candidate_id,
            detector=cand.detector,
            kind=cand.kind,
            polarity=cand.polarity,
            anchor_event_ids=list(cand.anchor_event_ids),
            affected_checks=list(cand.affected_checks),
            affected_contract_items=list(cand.affected_contract_items),
            validated_facts=validated,
            attribution_ceiling=ceiling,
            rendered_statement=statement,
            gate_results=gate_results,
            sequence=seq_of.get(anchor),
            phase_id=phase_of.get(anchor),
            review_mode=reviewer.review_mode,
            reviewer_version=version.REVIEWER_VERSION,
        )
        # Enrichment only survives on a moment whose facts validated — a card the
        # envelope will not publish never carries a model verdict.
        if enr is not None and _facts_valid(validated):
            _apply_enrichment(moment, enr)
        moments.append(moment)

    select_moments(moments)
    if telemetry is not None:
        # AGR-06: measurable rejection reasons — which gate dropped each
        # unselected moment (facts, references, observability, …). F1 follow-up:
        # the loop iterates a DECLARED ordered list of real gates (never every
        # gate_results entry — the numeric ``validation_attempts`` field and
        # status flags were being misread as failed gates), and records
        # de-duplication separately from the quota.
        rejections: dict[str, int] = {}
        for m in moments:
            if m.selected:
                continue
            if m.superseded_by:
                rejections["deduplicated"] = rejections.get("deduplicated", 0) + 1
                continue
            for gate in _REJECTION_GATES:
                result = m.gate_results.get(gate)
                ok = result == "passed" if gate == "fact_validation" \
                    else (isinstance(result, dict) and result.get("status") == "resolved"
                          if gate == "references"
                          else _link_gate(m) if gate == "contract_link"
                          else result == _REJECTION_GATES[gate])
                if not ok:
                    rejections[gate] = rejections.get(gate, 0) + 1
                    break
            else:
                rejections["quota"] = rejections.get("quota", 0) + 1
        telemetry["rejections"] = rejections
        telemetry["proposed"] = len(moments)
        telemetry["selected"] = sum(1 for m in moments if m.selected)
    return moments
