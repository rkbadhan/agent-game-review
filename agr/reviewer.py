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
    ep_by_failure = {ep.failure_event_id: ep for ep in ctx.recoveries}
    sig_by_event = {
        e.event_id: action_signature(e) for e in ctx.events if e.event_type == "tool_call"
    }
    strat_after = _strategy_change_between(ctx.events)
    terminal_type = _terminal_event_type(ctx.events)

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
        elif ftype == "absence":
            artifact = fact.get("declared_artifact")
            recomputed = "observed" if artifact in observed_artifacts else "absent"
            passed = artifact not in observed_artifacts
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
            # Semantic-moment grounding: every named event must exist AND the
            # quoted span must actually appear in that event's text (normalised).
            quotes = fact.get("quotes") or []
            recomputed = []
            for q in quotes:
                eid = q.get("event_id")
                ev = next((e for e in ctx.events if e.event_id == eid), None)
                matched = ev is not None and _norm_text(q.get("quote", "")) in _norm_text(ev.text())
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
        return f"Requirement check {fact.get('check_id')} was still failing at submission{detail}."
    if ftype == "repetition":
        evs = fact.get("events", [])
        return f"The same action was repeated with no new information in between ({', '.join(evs)})."
    if ftype == "absence":
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


def _same_moment(a: ReviewMoment, b: ReviewMoment) -> bool:
    """Whether two cards are facets of the *same* decisive moment (spec §8.10 gate 4).

    Same polarity, and either they share an **affected check** (the same
    requirement — this is what collapses a chatty model reviewer's duplicate cards
    even when it anchors them differently), or they share a **primary anchor** while
    at least one carries no check (a check card and a behaviour/omission card about
    the same spot). Two cards with *different* checks are never merged just for
    sharing an anchor, so distinct requirements stay distinct and recall is preserved.
    """
    if a.polarity != b.polarity:
        return False
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
               and m.gate_results.get("contract_link") == "present"
               and m.gate_results.get("observability") == "supported"
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


def run_reviewer(ctx: ReviewerContext, reviewer: Optional[Reviewer] = None) -> list[ReviewMoment]:
    """Build the reviewed moments for one run (Stages G→H→I).

    Analogous to :func:`agr.detectors.run_detectors`. Every proposed candidate is
    validated (G, with the §8.8 return-once revise loop), given an attribution
    ceiling and a controlled rendering (H), enriched with the model's gated
    taxonomy judgement, then collectively gated, de-duplicated, and ranked (I).
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
        primary = cand.structured_facts[0] if cand.structured_facts else {}
        statement = render(primary, ceiling, cand.polarity)
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
        gate_results = {
            "fact_validation": "passed" if _facts_valid(validated) else "failed",
            "validation_attempts": attempts,
            "contract_link": contract_link,
            "observability": "supported",  # only evaluated detectors reach the envelope
            "attribution": "within_ceiling" if gate_ok else "overclaim",
            "better_action": better_action,
        }
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
    return moments
