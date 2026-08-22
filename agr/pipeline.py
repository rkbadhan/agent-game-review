"""Deterministic analysis pipeline orchestration.

Ties together Stage A (ingest + events + checks), Stage C1-adjacent opportunity
detection, the recovery state machine (§8.6), Stage D (evidence slices for
failed checks + omission slices), Stage E (deterministic detectors), and the
reviewer envelope (Stages G/H/I). The reviewer defaults to the deterministic one
(no model call); ``analyze(doc, store, reviewer=...)`` swaps in a Stage F model
reviewer, whose facts are still recomputed here. All outputs are persisted next
to the immutable source capture.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from .audit import build_audit
from .checks import extract_checks, outcome
from .contract import (
    apply_confirmation,
    build_contract,
    declared_confirmation,
    derive_observations,
)
from .detectors import DetectorContext, run_detectors
from .evidence import slice_distributed, slice_external, slice_for_check, slice_omission
from .events import derive_events
from .ingest import IngestResult, ingest
from .opportunities import detect_opportunities
from .phases import segment_phases
from .recovery import UNRECOVERED, classify_recoveries
from .reviewer import ReviewerContext, run_reviewer
from .signature import SignatureRow, build_signature
from .schema import (
    AuditFinding,
    Candidate,
    CapabilityProfile,
    ContractObservation,
    DerivedEvent,
    DetectorResult,
    EvidenceSlice,
    Opportunity,
    Phase,
    RecoveryEpisode,
    ReviewMoment,
    RunSource,
    TaskContract,
    VerifierCheck,
)
from .store import Store


@dataclass
class Analysis:
    run_source: RunSource
    capabilities: CapabilityProfile
    events: list[DerivedEvent]
    phases: list[Phase]
    checks: list[VerifierCheck]
    outcome: dict
    contract: TaskContract
    contract_observations: list[ContractObservation]
    opportunities: list[Opportunity]
    recoveries: list[RecoveryEpisode]
    evidence_slices: list[EvidenceSlice]
    detector_results: list[DetectorResult]
    signature: list[SignatureRow]
    idempotent: bool
    review_moments: list[ReviewMoment] = field(default_factory=list)
    audit: list[AuditFinding] = field(default_factory=list)

    @property
    def candidates(self) -> list[Candidate]:
        out: list[Candidate] = []
        for r in self.detector_results:
            out.extend(r.candidates)
        return out

    @property
    def watermark(self) -> str | None:
        """Human-readable watermark when the contract is not yet confirmed.

        Spec §6.4: a review built on a ``draft`` or ``provisional`` contract is
        watermarked. A confirmed contract clears it (returns ``None``).
        """
        if not self.contract.watermarked:
            return None
        return (f"PROVISIONAL REVIEW — based on a {self.contract.status} task "
                f"contract (v{self.contract.contract_version}); not human-confirmed.")


def analyze(doc: dict, store: Store, reviewer=None) -> Analysis:
    result: IngestResult = ingest(doc, store)
    rs = result.run_source
    profile = result.capabilities

    events = derive_events(doc, rs)
    # Stage C1 — deterministic phase segmentation; stamps each event's phase_id.
    phases = segment_phases(events, rs.run_id, rs.source_capture_id)
    checks = extract_checks(doc, rs)
    run_outcome = outcome(checks)

    # Stage B — build the task contract, apply any recorded human confirmation,
    # and connect it to the atomic checks. Verifier-only checks that mapped to a
    # synthesised contract item are backfilled onto the check so downstream
    # evidence slicing and the signature see a consistent check<->item mapping.
    draft = build_contract(doc, rs, checks)
    # A confirmation persisted by `agr confirm` takes precedence over any
    # confirmation declared in the (immutable) source doc.
    confirmation = declared_confirmation(doc)
    if store.has_derived(rs.run_id, rs.source_capture_id, "contract_confirmation.json"):
        confirmation = store.read_derived(rs.run_id, rs.source_capture_id, "contract_confirmation.json")
    contract = apply_confirmation(draft, confirmation)
    item_for_check: dict[str, list[str]] = {}
    for item in contract.items:
        for cid in item.mapped_checks:
            item_for_check.setdefault(cid, []).append(item.id)
    for c in checks:
        if not c.contract_item_ids and c.check_id in item_for_check:
            c.contract_item_ids = list(item_for_check[c.check_id])
    contract_observations = derive_observations(contract, checks, events, rs.run_id)

    opportunities = detect_opportunities(events, rs.run_id, rs.source_capture_id)
    recoveries = classify_recoveries(events, rs.run_id, rs.source_capture_id)

    # Standard-branch slices for failed checks.
    slices = [slice_for_check(c, events) for c in checks if c.status == "failed"]

    ctx = DetectorContext(
        run_id=rs.run_id, capture_id=rs.source_capture_id, events=events, checks=checks,
        doc=doc, profile=profile, recoveries=recoveries, opportunities=opportunities,
    )
    detector_results = run_detectors(ctx)

    # Omission-branch slices for absence candidates (e.g. required artifact absent).
    verify_opp = next((o for o in opportunities if o.trigger == "required_artifact_exists"), None)
    fs_supported = profile.meets("filesystem", "checkpoint_only")
    for r in detector_results:
        for cand in r.candidates:
            if cand.kind != "omission" or not cand.structured_facts:
                continue
            if cand.structured_facts[0].get("type") != "absence":
                continue
            opp = verify_opp or Opportunity(
                opportunity_id="opp_run_span", run_id=rs.run_id, source_capture_id=rs.source_capture_id,
                ability="artifact_management", trigger="run_span",
                start_event_id=events[0].event_id, end_event_id=events[-1].event_id,
            )
            slices.append(slice_omission(
                rs.run_id, rs.source_capture_id, cand.candidate_id, cand.affected_contract_items,
                opp, cand.anchor_event_ids[0], fs_supported,
            ))

    # Distributed-branch slices: repeated-action patterns anchor an interval.
    for r in detector_results:
        if r.detector != "repeated_action_no_new_info":
            continue
        for cand in r.candidates:
            slices.append(slice_distributed(
                rs.run_id, rs.source_capture_id, cand.candidate_id,
                cand.anchor_event_ids, "repeated identical action without new information",
            ))

    # External-branch slices: a failed check may be better explained by an
    # unrecovered tool/environment failure on the path than by an agent decision.
    failed_checks = [c for c in checks if c.status == "failed"]
    unrecovered = [ep for ep in recoveries if ep.classification == UNRECOVERED]
    if failed_checks and unrecovered:
        fail_event = unrecovered[0].failure_event_id
        for c in failed_checks:
            slices.append(slice_external(rs.run_id, rs.source_capture_id, c, fail_event, "tool"))

    # Stage G/H/I — the deterministic reviewer envelope (§8.8–§8.10). It validates
    # every candidate's facts, assigns attribution language no stronger than the
    # evidence slice licenses, and selects/de-duplicates the final cards. No model
    # call: in deterministic_only mode the candidates it reviews are the detector
    # candidates. Stage F's model reviewer plugs into the same seam later.
    review_moments = run_reviewer(ReviewerContext(
        run_id=rs.run_id,
        source_capture_id=rs.source_capture_id,
        candidates=[c for r in detector_results if r.evaluated for c in r.candidates],
        slices=slices,
        checks=checks,
        events=events,
        recoveries=recoveries,
        contract=contract,
    ), reviewer=reviewer)
    # The stable slot key under which this reviewer's snapshot is persisted so a
    # later review by a different reviewer coexists rather than overwrites it
    # (the reviewer-diff view). The default reviewer's key is
    # ``"deterministic"``; a Stage F model reviewer's is ``"model:<source>"``.
    reviewer_key = getattr(reviewer, "reviewer_key", "deterministic")

    partial = Analysis(
        run_source=rs, capabilities=profile, events=events, phases=phases, checks=checks,
        outcome=run_outcome, contract=contract, contract_observations=contract_observations,
        opportunities=opportunities, recoveries=recoveries, evidence_slices=slices,
        detector_results=detector_results, signature=[], idempotent=result.idempotent,
    )
    signature = build_signature(partial)

    # §11 — Task & Verifier Audit: categorical qualifications of what this run's
    # result can legitimately imply. Deterministic; never overrides the result.
    audit = build_audit(partial)

    # Identity + capability profile as first-class derived records so the read
    # layer (and the forensic view's capability badge, §4.5/§6.2) can consume
    # them without re-parsing the raw source or recomputing the pipeline.
    store.write_derived(rs.run_id, rs.source_capture_id, "run_source.json", rs.to_dict())
    store.write_derived(rs.run_id, rs.source_capture_id, "capabilities.json", profile.to_dict())
    store.write_derived(rs.run_id, rs.source_capture_id, "signature.json", [s.to_dict() for s in signature])
    store.write_derived(rs.run_id, rs.source_capture_id, "audit.json", [f.to_dict() for f in audit])
    store.write_derived(rs.run_id, rs.source_capture_id, "phases.json", [p.to_dict() for p in phases])
    # events.json is written after phase segmentation so each event carries its phase_id.
    store.write_derived(rs.run_id, rs.source_capture_id, "events.json", [e.to_dict() for e in events])
    store.write_derived(rs.run_id, rs.source_capture_id, "checks.json", [c.to_dict() for c in checks])
    store.write_derived(rs.run_id, rs.source_capture_id, "outcome.json", run_outcome)
    store.write_derived(rs.run_id, rs.source_capture_id, "opportunities.json", [o.to_dict() for o in opportunities])
    store.write_derived(rs.run_id, rs.source_capture_id, "recoveries.json", [r.to_dict() for r in recoveries])
    store.write_derived(rs.run_id, rs.source_capture_id, "evidence_slices.json", [s.to_dict() for s in slices])
    store.write_derived(
        rs.run_id, rs.source_capture_id, "detector_results.json", [r.to_dict() for r in detector_results]
    )
    # Each reviewer writes only its own slot, so a model pass no longer
    # destroys the deterministic baseline. The legacy ``review_moments.json`` is
    # still mirrored as the most-recent review for backward compatibility with
    # older readers and the CLI eval harness until those are updated.
    review_payload = [m.to_dict() for m in review_moments]
    store.write_review(rs.run_id, rs.source_capture_id, reviewer_key, review_payload)
    store.write_derived(
        rs.run_id, rs.source_capture_id, "review_moments.json", review_payload
    )
    store.write_derived(rs.run_id, rs.source_capture_id, "contract.json", contract.to_dict())
    # Version trail: the draft, and the confirmed version that superseded it.
    history = []
    if contract.contract_version != draft.contract_version:
        superseded = draft.to_dict()
        superseded["status"] = "superseded"
        history.append(superseded)
    history.append(contract.to_dict())
    store.write_derived(rs.run_id, rs.source_capture_id, "contract_history.json", history)
    store.write_derived(
        rs.run_id, rs.source_capture_id, "contract_observations.json",
        [o.to_dict() for o in contract_observations],
    )

    return Analysis(
        run_source=rs, capabilities=profile, events=events, phases=phases, checks=checks,
        outcome=run_outcome, contract=contract, contract_observations=contract_observations,
        opportunities=opportunities, recoveries=recoveries, evidence_slices=slices,
        detector_results=detector_results, signature=signature, audit=audit,
        idempotent=result.idempotent, review_moments=review_moments,
    )
