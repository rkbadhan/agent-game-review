"""Task contract construction and human confirmation (spec §8.2, §6.4, §6.5).

Stage B assembles a *draft* task contract from the structured sources the
adapter provides — author-declared requirements, the environment/artifact
configuration, reference-solution assumptions, and the verifier's atomic
checks — and cross-checks them against each other. Disagreements are surfaced
as warnings rather than silently resolved (spec §3.1).

Two deliberate boundaries keep this inside the deterministic core:

* **No natural-language extraction.** The full product's contract builder is
  model-assisted: it reads the free-text instruction and reference solution.
  This module consumes only *structured* declarations (``task.requirements``,
  ``task.artifacts``, ``task.reference_solution.assumptions``) plus the
  verifier's structured checks. The free-text ``instruction`` is retained as a
  source pointer, never parsed. What the builder does deterministically —
  mapping every verifier check to an item and cross-checking coverage — is
  exactly the part the spec marks as mechanical.
* **No silent confirmation.** Items start ``unconfirmed``. A contract only
  reaches ``human_confirmed`` when an explicit set of human decisions confirms
  every required item; until then any review built on it is watermarked.
"""

from __future__ import annotations

from typing import Optional

from . import version
from .schema import (
    CONTRACT_OBSERVATION_STATUSES,
    CONTRACT_POLARITIES,
    CONTRACT_SOURCE_TYPES,
    IMPORTANCE_LEVELS,
    ITEM_HUMAN_STATUSES,
    ContractItem,
    ContractObservation,
    ContractWarning,
    DerivedEvent,
    RunSource,
    TaskContract,
    VerifierCheck,
)


class ContractError(ValueError):
    pass


# --- Draft construction ------------------------------------------------------

def _declared_items(doc: dict) -> list[ContractItem]:
    """Author/environment/reference declared items (structured sources only).

    Reads ``task.requirements`` (stated requirements and inferred assumptions),
    ``task.environment.preconditions`` (environment preconditions), and
    ``task.reference_solution.assumptions`` (reference-only assumptions). Each
    entry may name its own ``source_type``; sensible per-source defaults apply.
    """
    task = doc.get("task") or {}
    items: list[ContractItem] = []
    seen: set[str] = set()

    def add(raw: dict, default_source: str, default_pointer: str) -> None:
        item_id = raw.get("id")
        if not item_id:
            raise ContractError(f"declared contract item missing id: {raw!r}")
        if item_id in seen:
            raise ContractError(f"duplicate contract item id {item_id!r}")
        source_type = raw.get("source_type", default_source)
        if source_type not in CONTRACT_SOURCE_TYPES:
            raise ContractError(f"invalid source_type {source_type!r} on {item_id!r}")
        importance = raw.get("importance", "required")
        if importance not in IMPORTANCE_LEVELS:
            raise ContractError(f"invalid importance {importance!r} on {item_id!r}")
        polarity = raw.get("polarity", "require")
        if polarity not in CONTRACT_POLARITIES:
            raise ContractError(f"invalid polarity {polarity!r} on {item_id!r}")
        seen.add(item_id)
        items.append(ContractItem(
            id=item_id,
            description=raw.get("description", ""),
            source_type=source_type,
            source_pointers=list(raw.get("source_pointers", [])) or [default_pointer],
            importance=importance,
            verification_mode=raw.get("verification_mode"),
            mapped_checks=list(raw.get("mapped_checks", [])),
            target=raw.get("target"),
            polarity=polarity,
        ))

    for raw in task.get("requirements", []):
        add(raw, "stated_requirement", "instruction")
    env = task.get("environment") or {}
    for raw in env.get("preconditions", []):
        add(raw, "environment_precondition", "environment")
    ref = task.get("reference_solution") or {}
    for raw in ref.get("assumptions", []):
        add(raw, "reference_assumption", "reference_solution")
    return items


def _synth_verifier_item(check: VerifierCheck, item_id: str) -> ContractItem:
    """Turn a verifier check the author never declared into a contract item."""
    return ContractItem(
        id=item_id,
        description=check.name or f"Requirement enforced by {check.check_id}",
        source_type="verifier_enforced",
        source_pointers=list(check.source_pointers) or [f"verifier:{check.check_id}"],
        importance="required",
        mapped_checks=[check.check_id],
    )


def _link_checks(items: list[ContractItem], checks: list[VerifierCheck]) -> list[ContractWarning]:
    """Map every verifier check to one or more contract items (spec §8.2).

    A check whose declared ``contract_item_ids`` all resolve to declared items
    is linked to them. A check that names no item, or names an item the author
    never declared, yields a synthesised ``verifier_enforced`` item plus a
    ``verifier_only_requirement`` warning — the verifier is enforcing something
    the author did not state. Every structured check therefore ends mapped to at
    least one item.
    """
    by_id = {i.id: i for i in items}
    warnings: list[ContractWarning] = []

    for check in checks:
        declared_ids = [cid for cid in check.contract_item_ids if cid in by_id]
        undeclared_ids = [cid for cid in check.contract_item_ids if cid not in by_id]

        for cid in declared_ids:
            item = by_id[cid]
            if check.check_id not in item.mapped_checks:
                item.mapped_checks.append(check.check_id)

        # A check with no resolvable declared item is a verifier-only
        # requirement: synthesise an item under the id it referenced (or a
        # stable id derived from the check) and surface it.
        if not declared_ids:
            new_id = undeclared_ids[0] if undeclared_ids else f"V-{check.check_id}"
            item = _synth_verifier_item(check, new_id)
            by_id[new_id] = item
            items.append(item)
            warnings.append(ContractWarning(
                warning_type="verifier_only_requirement",
                message=(f"Verifier check {check.check_id} enforces a requirement "
                         f"not declared by the task author (item {new_id})."),
                item_ids=[new_id],
                check_ids=[check.check_id],
            ))
    return warnings


def _coverage_warnings(items: list[ContractItem], checks: list[VerifierCheck], doc: dict) -> list[ContractWarning]:
    """Surface items with no verification coverage and un-checked artifacts."""
    warnings: list[ContractWarning] = []
    for item in items:
        if item.mapped_checks:
            continue
        if item.source_type == "reference_assumption":
            warnings.append(ContractWarning(
                warning_type="reference_only_assumption",
                message=(f"Item {item.id} is present only in the reference solution "
                         f"and is not enforced by any verifier check."),
                item_ids=[item.id],
            ))
        elif item.importance in ("required", "optional"):
            warnings.append(ContractWarning(
                warning_type="prompt_only_unverified_requirement",
                message=(f"Item {item.id} is stated but no verifier check covers it; "
                         f"the requirement is unverified."),
                item_ids=[item.id],
            ))

    # Every explicit declared artifact should be referenced by some check.
    task = doc.get("task") or {}
    referenced = " ".join(
        (c.name or "") + " " + " ".join(c.source_pointers) for c in checks
    ) + " " + " ".join(i.target or "" for i in items if i.mapped_checks)
    for artifact in task.get("artifacts", []):
        path = artifact.get("path")
        if path and path not in referenced:
            warnings.append(ContractWarning(
                warning_type="uncovered_artifact_requirement",
                message=f"Declared artifact {path} is not referenced by any verifier check.",
            ))
    return warnings


def _contradiction_warnings(items: list[ContractItem]) -> list[ContractWarning]:
    """Two items on the same target with opposing polarity contradict."""
    warnings: list[ContractWarning] = []
    by_target: dict[str, list[ContractItem]] = {}
    for item in items:
        if item.target:
            by_target.setdefault(item.target, []).append(item)
    for target, group in by_target.items():
        requires = [i for i in group if i.polarity == "require"]
        forbids = [i for i in group if i.polarity == "forbid"]
        if requires and forbids:
            warnings.append(ContractWarning(
                warning_type="contradiction",
                message=(f"Target {target} is both required and forbidden by "
                         f"different contract items."),
                item_ids=sorted(i.id for i in requires + forbids),
            ))
    return warnings


def build_contract(doc: dict, run_source: RunSource, checks: list[VerifierCheck]) -> TaskContract:
    """Assemble a draft task contract from all structured sources (spec §8.2).

    The returned contract is always version 1 with status ``draft`` and every
    item ``unconfirmed``. Applying human decisions (``confirm_contract``) is what
    produces a finalised, non-watermarked contract.
    """
    items = _declared_items(doc)
    warnings: list[ContractWarning] = []
    warnings += _link_checks(items, checks)
    warnings += _coverage_warnings(items, checks, doc)
    warnings += _contradiction_warnings(items)

    task = doc.get("task") or {}
    _ = task.get("instruction")  # retained as source, never parsed here

    return TaskContract(
        task_id=run_source.task_id,
        task_version=run_source.task_version,
        contract_version=1,
        status="draft",
        items=items,
        warnings=warnings,
        builder_version=version.CONTRACT_BUILDER_VERSION,
    )


# --- Human confirmation ------------------------------------------------------

def confirm_contract(
    contract: TaskContract,
    decisions: dict[str, str],
    confirmed_by: Optional[str] = None,
    confirmed_at: Optional[str] = None,
) -> TaskContract:
    """Apply human decisions, producing a new superseding contract version.

    ``decisions`` maps item id -> one of ``ITEM_HUMAN_STATUSES``. The result is
    a new contract whose version is one higher than the input's. It reaches
    ``human_confirmed`` only when every ``required`` item is ``confirmed`` (an
    edited item must be re-confirmed to count); otherwise it is ``provisional``
    and a review built on it stays watermarked. Warnings are carried forward
    unchanged — confirmation records a human judgement, it does not erase a
    surfaced disagreement.
    """
    for item_id, decision in decisions.items():
        if decision not in ITEM_HUMAN_STATUSES:
            raise ContractError(f"invalid human decision {decision!r} for {item_id!r}")
        if contract.item(item_id) is None:
            raise ContractError(f"decision references unknown item {item_id!r}")

    new_items = [
        ContractItem(
            id=i.id, description=i.description, source_type=i.source_type,
            source_pointers=list(i.source_pointers), importance=i.importance,
            verification_mode=i.verification_mode, mapped_checks=list(i.mapped_checks),
            human_status=decisions.get(i.id, i.human_status),
            target=i.target, polarity=i.polarity,
        )
        for i in contract.items
    ]

    required = [i for i in new_items if i.importance == "required"]
    finalised = bool(required) and all(i.human_status == "confirmed" for i in required)
    # With no required items, confirmation still requires an explicit decision
    # on every item before it is considered final.
    if not required:
        finalised = bool(new_items) and all(i.human_status == "confirmed" for i in new_items)
    status = "human_confirmed" if finalised else "provisional"

    return TaskContract(
        task_id=contract.task_id,
        task_version=contract.task_version,
        contract_version=contract.contract_version + 1,
        status=status,
        items=new_items,
        warnings=[ContractWarning(w.warning_type, w.message, list(w.item_ids), list(w.check_ids))
                  for w in contract.warnings],
        supersedes_contract_version=contract.contract_version,
        confirmed_by=confirmed_by if finalised else None,
        confirmed_at=confirmed_at if finalised else None,
        builder_version=contract.builder_version,
    )


def confirmation_decisions(contract: TaskContract, confirmation: dict) -> dict[str, str]:
    """Resolve a confirmation record into an explicit per-item decision map.

    ``{"confirm_all": true}`` is shorthand for confirming every current item;
    otherwise ``decisions`` is used verbatim.
    """
    if confirmation.get("confirm_all"):
        return {i.id: "confirmed" for i in contract.items}
    return dict(confirmation.get("decisions", {}))


def apply_confirmation(contract: TaskContract, confirmation: Optional[dict]) -> TaskContract:
    """Apply a confirmation record (from the doc or a stored decision) if present.

    A task version that has been through human confirmation records its
    decisions so the stored review is not watermarked. Absent a record, the
    draft is returned unchanged (and any review remains watermarked).
    """
    if not confirmation:
        return contract
    return confirm_contract(
        contract,
        confirmation_decisions(contract, confirmation),
        confirmed_by=confirmation.get("confirmed_by"),
        confirmed_at=confirmation.get("confirmed_at"),
    )


def declared_confirmation(doc: dict) -> Optional[dict]:
    """The confirmation record embedded in the source doc, if any."""
    return (doc.get("task") or {}).get("contract_confirmation")


# --- Per-run contract observation (spec §6.5) --------------------------------

def _final_event_id(events: list[DerivedEvent]) -> Optional[str]:
    for e in reversed(events):
        if e.event_type == "final_submission":
            return e.event_id
    return events[-1].event_id if events else None


def derive_observations(
    contract: TaskContract,
    checks: list[VerifierCheck],
    events: list[DerivedEvent],
    run_id: str,
) -> list[ContractObservation]:
    """Derive each item's satisfaction from its mapped verifier checks (spec §6.5).

    Satisfaction is an evidence-backed *observation*, not assumed ground truth.
    The deterministic derivation is intentionally conservative:

    * any mapped check failed        -> ``evidenced_violated``
    * all mapped checks passed        -> ``evidenced_satisfied``
    * mapped checks only skipped/error/unknown -> ``unknown``
    * no mapped check at all           -> ``not_observed``
    """
    by_id = {c.check_id: c for c in checks}
    at_event = _final_event_id(events)
    observations: list[ContractObservation] = []
    for item in contract.items:
        mapped = [by_id[cid] for cid in item.mapped_checks if cid in by_id]
        if not mapped:
            status, derivation = "not_observed", "no_mapped_check"
        elif any(c.status == "failed" for c in mapped):
            status, derivation = "evidenced_violated", "mapped_check_failed"
        elif all(c.status == "passed" for c in mapped):
            status, derivation = "evidenced_satisfied", "mapped_checks_passed"
        else:
            status, derivation = "unknown", "mapped_check_inconclusive"
        assert status in CONTRACT_OBSERVATION_STATUSES
        observations.append(ContractObservation(
            run_id=run_id,
            contract_item_id=item.id,
            status=status,
            at_event_id=at_event,
            evidence=[c.check_id for c in mapped],
            derivation=derivation,
            derivation_version=version.CONTRACT_OBSERVATION_VERSION,
        ))
    return observations
