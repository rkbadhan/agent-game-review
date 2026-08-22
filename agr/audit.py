"""Task & Verifier Audit (spec §11).

Produces one categorical, evidence-backed finding per audit dimension
(§11.1). The audit qualifies what a run result can legitimately imply — it
never overrides the result and never emits scores.

The stage is deterministic: it maps already-derived signals (contract items
and warnings, verifier-check outcomes, opportunities, recovery episodes) onto
the ten dimensions. Every concern carries exact supporting evidence as ids
pointing into the derived records. Where the supporting inputs for a judgement
do not exist, the dimension reports ``insufficient_evidence`` — surfaced as
"cannot say", never silently as "no concern" (the same not-evaluated rule the
detectors follow, spec §6.2 / §8.5).
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import version
from .recovery import UNRECOVERED
from .schema import AUDIT_DIMENSIONS, CONCERN_ASSESSMENTS, AuditFinding

if TYPE_CHECKING:  # pragma: no cover - duck-typed at runtime, like build_signature
    from .pipeline import Analysis

# Check statuses that mean the verifier itself did not run cleanly on this run.
_UNCLEAN_CHECK_STATUSES = {"error", "skipped", "unknown"}

# The weakest check source: verdicts read out of free text are the most fragile
# verification a task can rely on.
_INTERPRETIVE_SOURCE = "output_interpretation"


def _finding(analysis, dimension: str, assessment: str, statement: str,
             events=(), checks=(), items=()) -> AuditFinding:
    assert assessment in CONCERN_ASSESSMENTS
    return AuditFinding(
        dimension=dimension,
        assessment=assessment,
        statement=statement,
        run_id=analysis.run_source.run_id,
        source_capture_id=analysis.run_source.source_capture_id,
        evidence_event_ids=list(events),
        evidence_check_ids=list(checks),
        evidence_item_ids=list(items),
        audit_version=version.AUDIT_VERSION,
    )


def _instruction_clarity(a) -> AuditFinding:
    contradictions = [w for w in a.contract.warnings if w.warning_type == "contradiction"]
    if contradictions:
        return _finding(
            a, "instruction_clarity", "supported_concern",
            f"{len(contradictions)} contract contradiction(s): requirements disagree on "
            f"the same target.",
            items=sorted({i for w in contradictions for i in w.item_ids}),
        )
    return _finding(
        a, "instruction_clarity", "insufficient_evidence",
        "No mechanical signal about instruction clarity in this run; clarity needs human review.",
    )


def _coverage(a) -> AuditFinding:
    if a.outcome.get("status") == "UNVERIFIED":
        return _finding(
            a, "contract_verifier_coverage", "supported_concern",
            "No verifier evidence was captured for this run (zero atomic checks); "
            "the outcome is unverified.",
        )
    required = [i for i in a.contract.items if i.importance == "required"]
    uncovered = [i for i in required if not i.mapped_checks]
    artifact_warnings = [
        w for w in a.contract.warnings if w.warning_type == "uncovered_artifact_requirement"
    ]
    if not required and not artifact_warnings:
        return _finding(
            a, "contract_verifier_coverage", "insufficient_evidence",
            "No required contract item was extracted from the task sources; "
            "coverage cannot be assessed.",
        )
    if uncovered or artifact_warnings:
        parts = [
            f"{len(uncovered)} of {len(required)} required contract item(s) have no mapped check"
        ]
        parts += [w.message for w in artifact_warnings]
        return _finding(
            a, "contract_verifier_coverage", "supported_concern",
            "; ".join(parts) + ".",
            items=[i.id for i in uncovered],
        )
    return _finding(
        a, "contract_verifier_coverage", "no_concern_detected",
        f"All {len(required)} required contract item(s) map to at least one verifier check.",
    )


def _hidden_requirements(a) -> AuditFinding:
    warnings = [w for w in a.contract.warnings if w.warning_type == "verifier_only_requirement"]
    if warnings:
        return _finding(
            a, "hidden_verifier_requirements", "supported_concern",
            f"{len(warnings)} verifier-enforced requirement(s) are not declared by the task "
            f"author; the contract synthesised them so they stay visible.",
            checks=sorted({c for w in warnings for c in w.check_ids}),
            items=sorted({i for w in warnings for i in w.item_ids}),
        )
    if not a.checks:
        return _finding(
            a, "hidden_verifier_requirements", "insufficient_evidence",
            "No checks were captured; undeclared enforcement cannot be ruled out.",
        )
    return _finding(
        a, "hidden_verifier_requirements", "no_concern_detected",
        "Every verifier-enforced requirement is declared in the task contract.",
    )


def _prompt_only(a) -> AuditFinding:
    warnings = [
        w for w in a.contract.warnings if w.warning_type == "prompt_only_unverified_requirement"
    ]
    if warnings:
        return _finding(
            a, "prompt_only_unverified_requirements", "supported_concern",
            f"{len(warnings)} stated requirement(s) have no verifier check; success on "
            f"them is unverified.",
            items=sorted({i for w in warnings for i in w.item_ids}),
        )
    if not any(i.source_type == "stated_requirement" for i in a.contract.items):
        return _finding(
            a, "prompt_only_unverified_requirements", "insufficient_evidence",
            "No stated requirement was extracted from the instruction; nothing to cross-check.",
        )
    return _finding(
        a, "prompt_only_unverified_requirements", "no_concern_detected",
        "Every stated requirement maps to at least one verifier check.",
    )


def _reference_assumptions(a) -> AuditFinding:
    reference_items = [i for i in a.contract.items if i.source_type == "reference_assumption"]
    if not reference_items:
        return _finding(
            a, "reference_solution_assumptions", "insufficient_evidence",
            "No reference-solution assumption surfaced in the contract; absence of an "
            "oracle is not mechanically confirmable.",
        )
    warned = {i for w in a.contract.warnings
              if w.warning_type == "reference_only_assumption" for i in w.item_ids}
    unchecked = [i for i in reference_items if i.id in warned]
    checked = [i for i in reference_items if i.id not in warned]
    if unchecked:
        return _finding(
            a, "reference_solution_assumptions", "supported_concern",
            f"{len(unchecked)} requirement(s) exist only in the reference solution and are "
            f"not enforced; runs can pass without satisfying them.",
            items=[i.id for i in unchecked],
        )
    return _finding(
        a, "reference_solution_assumptions", "possible_concern",
        f"{len(checked)} contract item(s) originate in the reference solution; success may "
        f"depend on oracle-specific behaviour even though checks cover them.",
        items=[i.id for i in reference_items],
    )


def _verifier_stability(a) -> AuditFinding:
    unclean = [c for c in a.checks if c.status in _UNCLEAN_CHECK_STATUSES]
    if unclean:
        return _finding(
            a, "verifier_stability", "supported_concern",
            f"{len(unclean)} check(s) did not run to a verdict; the verifier is not "
            f"stable on this run.",
            checks=[c.check_id for c in unclean],
        )
    if a.checks and all(c.source == _INTERPRETIVE_SOURCE for c in a.checks):
        return _finding(
            a, "verifier_stability", "possible_concern",
            f"All {len(a.checks)} check(s) are interpretive ({_INTERPRETIVE_SOURCE}); verdicts "
            f"depend on reading output rather than asserting state.",
            checks=[c.check_id for c in a.checks],
        )
    if not a.checks:
        return _finding(
            a, "verifier_stability", "insufficient_evidence",
            "No checks were captured; stability cannot be assessed.",
        )
    return _finding(
        a, "verifier_stability", "no_concern_detected",
        f"All {len(a.checks)} check(s) ran to a passed/failed verdict.",
    )


def _exploitability(a) -> AuditFinding:
    return _finding(
        a, "exploitability", "insufficient_evidence",
        "Exploitability needs adversarial analysis or replay; no deterministic signal exists.",
    )


def _environment_realism(a) -> AuditFinding:
    unrecovered = [ep for ep in a.recoveries if ep.classification == UNRECOVERED]
    if unrecovered:
        return _finding(
            a, "environment_realism", "possible_concern",
            f"{len(unrecovered)} failure(s) went unrecovered and may be environmental rather "
            f"than a capability signal.",
            events=[ep.failure_event_id for ep in unrecovered],
        )
    return _finding(
        a, "environment_realism", "insufficient_evidence",
        "No unrecovered environment failure in this run; realism cannot be judged from a "
        "single clean pass.",
    )


def _capability_contamination(a) -> AuditFinding:
    by_ability: dict[str, object] = {}
    for o in a.opportunities:
        by_ability.setdefault(o.ability, o)
    if len(by_ability) >= 2:
        anchors: list[str] = []
        for o in by_ability.values():
            anchors += [o.start_event_id, o.end_event_id]
        names = ", ".join(sorted(by_ability))
        return _finding(
            a, "capability_contamination", "possible_concern",
            f"success also depends on {len(by_ability)} distinct abilities ({names}); "
            f"the task is not a pure measure of any single one.",
            events=anchors,
        )
    return _finding(
        a, "capability_contamination", "insufficient_evidence",
        "Too few observable ability windows to tell whether success depends on more than "
        "the nominal task focus.",
    )


def _product_relevance(a) -> AuditFinding:
    return _finding(
        a, "product_relevance", "insufficient_evidence",
        "Product relevance is a human judgement; the deterministic core cannot assess it.",
    )


_DIMENSION_RULES = {
    "instruction_clarity": _instruction_clarity,
    "contract_verifier_coverage": _coverage,
    "hidden_verifier_requirements": _hidden_requirements,
    "prompt_only_unverified_requirements": _prompt_only,
    "reference_solution_assumptions": _reference_assumptions,
    "verifier_stability": _verifier_stability,
    "exploitability": _exploitability,
    "environment_realism": _environment_realism,
    "capability_contamination": _capability_contamination,
    "product_relevance": _product_relevance,
}


def build_audit(analysis) -> list[AuditFinding]:
    """One finding per §11.1 dimension — always all ten, in display order."""
    findings = [_DIMENSION_RULES[d](analysis) for d in AUDIT_DIMENSIONS]
    assert all(f.dimension == d for f, d in zip(findings, AUDIT_DIMENSIONS))
    return findings
