"""Deterministic candidate detectors (spec §8.5).

Detectors emit structured facts, not prose, and never assign causal
interpretation. Each declares the observability it needs; when the capability
profile does not meet it, the detector reports ``evaluated=False`` and is
surfaced as "not evaluated", never as "no problem found" (spec §6.2).

The five MVP detectors (spec §8.5) are implemented, plus a capability-gated
context detector to exercise the "not evaluated" path.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import version
from ._util import action_signature
from .recovery import GOOD_RECOVERY, UNRECOVERED
from .schema import (
    Candidate,
    CapabilityProfile,
    DerivedEvent,
    DetectorResult,
    Opportunity,
    RecoveryEpisode,
    VerifierCheck,
)


@dataclass
class DetectorContext:
    run_id: str
    capture_id: str
    events: list[DerivedEvent]
    checks: list[VerifierCheck]
    doc: dict
    profile: CapabilityProfile
    recoveries: list[RecoveryEpisode] = field(default_factory=list)
    opportunities: list[Opportunity] = field(default_factory=list)

    def submission(self) -> Optional[DerivedEvent]:
        return next((e for e in self.events if e.event_type == "final_submission"), None)


class Detector:
    name: str = ""
    required_capabilities: dict[str, str] = {}
    # AGR-05: a registered-but-not-implemented detector is distinct from an
    # evaluated detector that found no issue. Placeholders report
    # evaluated=False with placeholder=True even when capabilities would allow
    # a run — "no candidates" from a stub must never read as "no problem".
    implemented: bool = True

    def _run(self, ctx: DetectorContext) -> list[Candidate]:
        raise NotImplementedError

    def run(self, ctx: DetectorContext) -> DetectorResult:
        unmet = [c for c, m in self.required_capabilities.items() if not ctx.profile.meets(c, m)]
        if not self.implemented:
            # A stub can never evaluate, whatever the capabilities say — report
            # the placeholder status (and the unmet capabilities, for candour).
            return DetectorResult(detector=self.name, evaluated=False,
                                  unmet_capabilities=unmet, placeholder=True)
        if unmet:
            return DetectorResult(detector=self.name, evaluated=False, unmet_capabilities=unmet)
        return DetectorResult(detector=self.name, evaluated=True, candidates=self._run(ctx))

    def _candidate(self, ctx: DetectorContext, key: str, **kw) -> Candidate:
        return Candidate(
            candidate_id=f"cand_{self.name}_{key}",
            run_id=ctx.run_id,
            source_capture_id=ctx.capture_id,
            detector=self.name,
            detector_version=version.DETECTOR_VERSION,
            **kw,
        )


class UnresolvedRequirementAtSubmission(Detector):
    """A required contract item is still failing when the run submits (§8.5 #4)."""

    name = "unresolved_requirement_at_submission"
    required_capabilities = {"messages": "complete", "tool_results": "complete"}

    def _run(self, ctx):
        submit = ctx.submission()
        if submit is None:
            return []
        out = []
        for check in ctx.checks:
            if check.status != "failed":
                continue
            out.append(self._candidate(
                ctx, check.check_id, kind="omission", anchor_event_ids=[submit.event_id],
                affected_checks=[check.check_id], affected_contract_items=list(check.contract_item_ids),
                structured_facts=[{
                    "type": "requirement_status", "check_id": check.check_id,
                    "status_at_submission": "failed",
                    "expected": check.expected, "observed": check.observed,
                }],
            ))
        return out


class IgnoredToolFailure(Detector):
    """A tool failure left unresolved through submission (§8.5 #1)."""

    name = "ignored_tool_failure"
    required_capabilities = {"tool_results": "complete"}

    def _run(self, ctx):
        submit = ctx.submission()
        out = []
        for ep in ctx.recoveries:
            if ep.classification != UNRECOVERED:
                continue
            anchor = [ep.failure_event_id] + ([submit.event_id] if submit else [])
            out.append(self._candidate(
                ctx, ep.failure_event_id, kind="behaviour", anchor_event_ids=anchor,
                structured_facts=[{
                    "type": "state_transition", "failure_event": ep.failure_event_id,
                    "resolved_before_submission": False,
                }],
            ))
        return out


class RepeatedActionNoNewInfo(Detector):
    """Same action repeated, producing the same output again (§8.5 #2).

    AGR-05: a repeated call is an observation, not automatically a defect. The
    "no new information" claim is made only when the mechanical evidence
    supports it — both calls' tool results are captured and their outputs are
    equivalent. Differing outputs (e.g. a poll whose report changed) are
    observation with new information and emit nothing; unobserved results
    cannot support the claim and emit nothing either.
    """

    name = "repeated_action_no_new_info"
    required_capabilities = {"tool_calls": "complete", "tool_results": "complete"}

    @staticmethod
    def _norm(text: str) -> str:
        return " ".join(text.split())

    def _run(self, ctx):
        out = []
        # First walk: attach each tool_call to its own tool_result (the next
        # result before the next call) and remember whether a strategy change
        # separated it from the previous call.
        calls: list[dict] = []
        strategy_since = False
        pending: DerivedEvent | None = None
        for ev in ctx.events:
            if ev.event_type == "strategy_change":
                strategy_since = True
                continue
            if ev.event_type == "tool_call":
                pending = ev
                calls.append({"call": ev, "result": None, "strategy_since": strategy_since})
                strategy_since = False
            elif ev.event_type == "tool_result" and pending is not None:
                calls[-1]["result"] = ev
                pending = None
        # Second walk: consecutive identical calls with no strategy change —
        # emit only when BOTH outputs are captured and equivalent.
        for a, b in zip(calls, calls[1:]):
            if action_signature(a["call"]) != action_signature(b["call"]):
                continue
            if a["strategy_since"] or b["strategy_since"]:
                continue
            ra, rb = a["result"], b["result"]
            if ra is None or rb is None:
                continue  # outputs not captured — the claim is unsupported
            if self._norm(ra.text()) != self._norm(rb.text()):
                continue  # outputs differ — observation with new information
            out.append(self._candidate(
                ctx, b["call"].event_id, kind="behaviour",
                anchor_event_ids=[a["call"].event_id, b["call"].event_id],
                structured_facts=[{
                    "type": "repetition", "signature": list(action_signature(b["call"])),
                    "events": [a["call"].event_id, b["call"].event_id],
                    "result_event_ids": [ra.event_id, rb.event_id],
                    "outputs_compared": True,
                    "outputs_equivalent": True,
                }],
            ))
        return out


class RequiredArtifactAbsent(Detector):
    """A declared task artifact never appears in any observation (§8.5 #3, omission)."""

    name = "required_artifact_absent"
    required_capabilities = {"filesystem": "checkpoint_only"}

    def _run(self, ctx):
        declared = [a.get("path") for a in ctx.doc.get("task", {}).get("artifacts", []) if a.get("path")]
        observed = {e.payload.get("artifact_path") for e in ctx.events if e.event_type == "artifact_observation"}
        submit = ctx.submission()
        closing = submit.event_id if submit else (ctx.events[-1].event_id if ctx.events else "")
        out = []
        for path in declared:
            if path in observed:
                continue
            out.append(self._candidate(
                ctx, path.strip("/").replace("/", "_"), kind="omission",
                anchor_event_ids=[closing],
                structured_facts=[{
                    "type": "absence",
                    "predicate": {"event_type": "artifact_observation", "artifact_path": path},
                    "declared_artifact": path,
                }],
            ))
        return out


class SuccessfulRecoveryViaStrategyChange(Detector):
    """Positive: a qualifying failure recovered via a real strategy change (§8.5 #5)."""

    name = "successful_recovery_via_strategy_change"
    required_capabilities = {"tool_results": "complete"}

    def _run(self, ctx):
        out = []
        for ep in ctx.recoveries:
            if ep.classification != GOOD_RECOVERY:
                continue
            anchor = [ep.failure_event_id] + ([ep.resolution_event_id] if ep.resolution_event_id else [])
            out.append(self._candidate(
                ctx, ep.failure_event_id, kind="recovery", polarity="positive",
                anchor_event_ids=anchor,
                structured_facts=[{
                    "type": "state_transition", "failure_event": ep.failure_event_id,
                    "resolution_event": ep.resolution_event_id,
                    "strategy_changed": ep.strategy_changed, "changed_action": ep.changed_action,
                }],
            ))
        return out


class CompactionRequirementLoss(Detector):
    """Requirement lost across a compaction (context family). Capability-gated.

    AGR-05: registered as a placeholder — the compaction-context evidence
    family is not implemented yet. It reports ``evaluated=False,
    placeholder=True`` so the UI can say "not implemented" instead of the
    "evaluated, no issue found" a stub's empty candidate list would fake.
    """

    name = "compaction_requirement_loss"
    required_capabilities = {"pre_post_compaction_context": "complete"}
    implemented = False

    def _run(self, ctx):
        return []


DETECTORS: list[Detector] = [
    UnresolvedRequirementAtSubmission(),
    IgnoredToolFailure(),
    RepeatedActionNoNewInfo(),
    RequiredArtifactAbsent(),
    SuccessfulRecoveryViaStrategyChange(),
    CompactionRequirementLoss(),
]


def run_detectors(ctx: DetectorContext, detectors: Optional[list[Detector]] = None) -> list[DetectorResult]:
    detectors = detectors if detectors is not None else DETECTORS
    return [d.run(ctx) for d in detectors]
