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
from ._util import action_signature
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
    observed_check_failures = _agent_observed_check_failures(ctx.events, terminal_type)

    out: list[dict] = []
    for fact in candidate.structured_facts:
        ftype = fact.get("type")
        annotated = dict(fact)
        if ftype == "requirement_status":
            check = check_by_id.get(fact.get("check_id"))
            recomputed = check.status if check else None
            # "status" accepted as an alias for "status_at_submission".
            claimed_failed = (fact.get("status_at_submission") or fact.get("status")) == "failed"
            passed = check is not None and (check.status == "failed") == claimed_failed
            # What the recomputation can honestly support: the FINAL verifier
            # status is always known; "at submission" is only known when the
            # agent's own trace observed the failure before the run ended (AGR-03:
            # a later failing check is not an agent-observed failure unless the
            # trace supports that timing and visibility).
            annotated["status_basis"] = "final_verifier"
            annotated["agent_observed_failure"] = bool(
                passed and claimed_failed and fact.get("check_id") in observed_check_failures)
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
            passed = (
                len(evs) >= 2
                and all(s is not None for s in sigs)
                and len(set(sigs)) == 1
                and not strat_after.get(tuple(evs[:2]), False)
            )
        elif ftype == "state_transition":
            fev = fact.get("failure_event")
            ep = ep_by_failure.get(fev)
            recomputed = ep.classification if ep else ("present" if fev in event_ids else None)
            if fact.get("resolution_event"):  # positive recovery claim
                passed = ep is not None and ep.classification == GOOD_RECOVERY
            else:  # unresolved-failure claim
                passed = ep is not None and ep.classification == UNRECOVERED
            # Fall back to bare existence when no episode indexes this failure.
            if ep is None:
                passed = fev in event_ids
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
                matched = ev is not None and quote in _norm_text(ev.text())
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


def _agent_observed_check_failures(events: list[DerivedEvent], terminal_type: str | None) -> set[str]:
    """Check ids whose failure the agent's OWN trace shows before the run ended.

    Only tool results / observations inside the trajectory can be something the
    agent saw. The verifier result is computed after the run; a failure that
    appears only there was never observed by the agent (AGR-03 timing rule).
    """
    observed: set[str] = set()
    for e in events:
        if e.event_type == terminal_type:
            break
        if e.event_type not in ("tool_result", "environment_observation"):
            continue
        # Raw text: check ids are conventionally uppercase, so normalization
        # (lowercasing) would hide them.
        for cid in _CHECK_ID_PATTERN.findall(e.text()):
            observed.add(cid.upper())
    return observed


# Bare check-id tokens like C3, T2, CHK12 — how tool output usually names them.
_CHECK_ID_PATTERN = re.compile(r"\b([A-Z]{1,4}\d{1,3})\b")


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


def render(fact: dict, ceiling: str, polarity: str) -> str:
    """Render a card statement from controlled templates, gated by ``ceiling``.

    Causal language is chosen to match the attribution level: ``dependency_linked``
    yields "linked to", never "produced". Pure status facts carry no causal verb
    and read the same at any ceiling.
    """
    ftype = fact.get("type")
    link = _LINK_CLAUSE.get(ceiling, _LINK_CLAUSE["hypothesized"])
    if ftype == "requirement_status":
        detail = ""
        if fact.get("expected") is not None:
            detail = f" (expected {fact.get('expected')}, observed {fact.get('observed')})"
        if fact.get("agent_observed_failure"):
            # The agent's own trace shows it seeing this failure before the run
            # ended, so "still failing at submission" is evidence-backed.
            return f"Requirement check {fact.get('check_id')} was still failing at submission{detail}."
        # A post-run verifier result is not something the agent saw (AGR-03:
        # final verifier status ≠ status known at submission).
        return (f"Requirement check {fact.get('check_id')} failed the run's final verifier"
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
        if fact.get("resolution_event"):
            return (
                f"A failure at {fact.get('failure_event')} was recovered via a strategy "
                f"change at {fact.get('resolution_event')}."
            )
        return f"A tool failure at {fact.get('failure_event')} was left unresolved before submission{link}."
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


# --- Stage I: moment selection ------------------------------------------------


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
    """
    passing = [m for m in moments if m.gate_results.get("fact_validation") == "passed"
               and m.gate_results.get("references", {}).get("status") == "resolved"
               and m.gate_results.get("observability") == "supported"
               and m.gate_results.get("contract_link") == "present"
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


def _explanation_support(enr: Enrichment, ctx: ReviewerContext) -> str:
    """Semantic support status for model explanations (AGR-03).

    Separates factual assertions inside explanation fields from interpretation:
    an explanation that references concrete run entities (check ids, event ids)
    is "evidence_linked" only when every referenced entity exists; references
    to entities that do not exist make it "dangling_references"; an explanation
    that references nothing is "interpretation_only" — kept and labelled, but
    never rendered as if the run's evidence proved it (the invented-database-
    outage class of claim).
    """
    texts = [enr.consequence or "", enr.instructional_value or ""]
    texts += [str(rc.get("rationale") or "") for rc in enr.root_cause_candidates]
    check_ids = {c.check_id for c in ctx.checks}
    event_ids = {e.event_id for e in ctx.events}
    referenced = False
    for text in texts:
        for cid in _CHECK_ID_PATTERN.findall(text):
            referenced = True
            if cid.upper() not in check_ids and cid not in event_ids:
                return "dangling_references"
    if not referenced:
        return "interpretation_only"
    return "evidence_linked"


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
        # Render from the first fact that actually PASSED validation — never
        # from a failed or unrecomputable fact, whose "recomputed" basis does
        # not exist (AGR-03: unknown fact types must not become validated prose).
        primary = next((f for f in validated if f.get("validation") == "passed"), None)
        statement = render(primary, ceiling, cand.polarity) if primary else \
            "No deterministic evidence supports this finding."
        gate_ok, _ = attribution_gate(statement, ceiling)
        anchor = cand.anchor_event_ids[0] if cand.anchor_event_ids else None
        linked = _linked_slices(cand, ctx.slices)
        # Gate 2 (§8.10) requires a relevant contract/verifier concern for a
        # *negative* decisive moment. Positive moments and recoveries are surfaced
        # under their own quota lines (a reinforcing behaviour, gate 5), so the
        # concern requirement does not apply to them.
        has_concern = bool(cand.affected_checks or cand.affected_contract_items or linked)
        contract_link = "present" if (has_concern or cand.polarity == "positive"
                                       or cand.kind == "recovery") else "absent"
        enr = proposal.enrichment
        better_action = (
            "model_provided" if (enr and enr.better_action) else "not_available_deterministic"
        )
        refs = validate_references(cand, ctx)
        gate_results = {
            "fact_validation": "passed" if _facts_valid(validated) else "failed",
            "validation_attempts": attempts,
            "references": refs,  # resolved | dangling (+ dangling detail)
            "contract_link": contract_link,
            # Computed from the capability profile for model discoveries;
            # deterministic detectors were capability-gated before this envelope.
            "observability": observability_for(cand, ctx),
            "attribution": "within_ceiling" if gate_ok else "overclaim",
            "better_action": better_action,
        }
        if enr is not None:
            # Model explanations are interpretation until evidence links them:
            # semantic support gets its own review status (AGR-03), separate
            # from the validated facts above.
            gate_results["explanation_support"] = _explanation_support(enr, ctx)
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
        # unselected moment (facts, references, observability, …).
        rejections: dict[str, int] = {}
        for m in moments:
            if m.selected:
                continue
            for gate, result in m.gate_results.items():
                ok = result == "passed" if gate == "fact_validation" \
                    else (result.get("status") == "resolved" if gate == "references"
                          else result in ("present", "supported", "within_ceiling"))
                if not ok:
                    rejections[gate] = rejections.get(gate, 0) + 1
                    break
            else:
                rejections["not_selected"] = rejections.get("not_selected", 0) + 1
        telemetry["rejections"] = rejections
        telemetry["proposed"] = len(moments)
        telemetry["selected"] = sum(1 for m in moments if m.selected)
    return moments
