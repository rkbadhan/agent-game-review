"""Attribution-benchmark adapters and scoring (EV-1, spec amendment).

The evidence-integrity audit (:mod:`agr.integrity`, EV-2) can say whether AGR's
*displayed* references and quotes are mechanically sound. It cannot say whether
AGR found the *important* moment — that needs reference labels. This module is
the EV-1 half: a small, explicit contract for reusing an existing,
externally-labelled **failure-attribution benchmark** instead of inventing new
labels.

Three pieces, deliberately separate:

* :class:`BenchmarkManifest` — the provenance and limits of a benchmark, and
  the rules AGR must declare *before* scoring: how it maps up-to-five moments
  onto the benchmark's single-prediction protocol, and what its agent/steps
  mean. A result without this record is not comparable to anything.
* :class:`BenchmarkCase` — one benchmark item: the trace converted to the ATIF
  document :mod:`agr.ingest` already understands, plus its reference label as a
  :class:`~agr.gold.GoldTrajectory` in **source-step coordinates**.
* scoring — a benchmark's own protocol, applied honestly. Who&When asks for one
  responsible agent and one decisive step; AGR proposes up to five moments, so
  the manifest declares the single-prediction rule (here: AGR's highest-ranked
  **negative** moment) *before* any run, and the scorer never lets AGR's best of
  five pose as one prediction.

Missing annotations are never evidence that no valid moment exists, and a
failure-only benchmark cannot establish correct abstention on clean runs — that
is why :attr:`BenchmarkManifest.limits` is part of the record, not a footnote.

Pure stdlib — no third-party imports, no model calls.
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Protocol, runtime_checkable
from urllib.parse import urlsplit

from . import version
from .gold import GoldTrajectory
from .schema import _clean


# --- provenance --------------------------------------------------------------


@dataclass
class BenchmarkManifest:
    """What a benchmark is, where its labels came from, and how AGR maps onto it.

    ``single_prediction_rule`` is the declared answer to "the benchmark expects
    one decisive step; what does AGR submit?" — fixed before scoring so the
    comparison is not tuned to the result. ``scoring_protocol`` names the
    benchmark's own metrics. ``coverage`` records what the dataset does and does
    not contain (e.g. ``{"outcomes": ["failed"], "abstention": False}``), and
    ``limits`` states the caveats a reader must carry with any number.
    """

    benchmark: str
    version: str
    source_url: str
    license: str
    label_provenance: str
    single_prediction_rule: str
    scoring_protocol: str
    coverage: dict[str, Any] = field(default_factory=dict)
    limits: list[str] = field(default_factory=list)
    manifest_version: str = version.BENCHMARK_MANIFEST_VERSION

    def to_dict(self) -> dict:
        return _clean({
            "benchmark": self.benchmark,
            "version": self.version,
            "manifest_version": self.manifest_version,
            "source_url": self.source_url,
            "license": self.license,
            "label_provenance": self.label_provenance,
            "single_prediction_rule": self.single_prediction_rule,
            "scoring_protocol": self.scoring_protocol,
            "coverage": self.coverage,
            "limits": self.limits,
        })


@dataclass
class BenchmarkCase:
    """One benchmark item: an ATIF trace plus its reference label.

    ``doc`` is the ATIF-shaped document ingest accepts; ``gold`` anchors on the
    same source step ids that document declares. ``meta`` carries the
    benchmark's own fields the scorer needs (responsible agent, split) without
    letting them into reviewer input.
    """

    case_id: str
    doc: dict
    gold: GoldTrajectory
    meta: dict[str, Any] = field(default_factory=dict)

    @property
    def run_id(self) -> str:
        return self.gold.run_id


@runtime_checkable
class BenchmarkAdapter(Protocol):
    """One labelled attribution benchmark -> ingestable cases.

    Adapters are stateless and pure: ``load_cases`` reads a directory and
    returns converted cases. New labels are never inferred — a record that
    cannot be faithfully mapped is skipped with a warning, not guessed.
    """

    name: str
    version: str
    manifest: BenchmarkManifest

    def load_cases(
        self, directory: str, *, limit: Optional[int] = None
    ) -> tuple[list[BenchmarkCase], list[str]]:
        """Return ``(cases, warnings)`` for every record under ``directory``."""
        ...

    def input_inventory(
        self, directory: str, *, limit: Optional[int] = None
    ) -> list[dict[str, str]]:
        """Return ordered source record names and hashes for a frozen run."""
        ...


# --- prediction --------------------------------------------------------------


@dataclass
class AttributionPrediction:
    """AGR's single submission for one case, under the declared rule.

    ``step_ids`` contains the one source step submitted under the manifest's
    selection rule; ``agent`` is AGR's attributed responsible agent for that
    step. ``abstained`` means AGR surfaced no qualifying negative moment —
    which is a real, scored outcome, not a missing row.
    """

    case_id: str
    step_ids: list[str] = field(default_factory=list)
    agent: Optional[str] = None
    moment_id: Optional[str] = None
    review_status: Optional[str] = None
    abstained: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return _clean({
            "case_id": self.case_id,
            "step_ids": self.step_ids,
            "agent": self.agent,
            "moment_id": self.moment_id,
            "review_status": self.review_status,
            "abstained": self.abstained,
            "notes": self.notes,
        })


# The review-status values that are FAILURES or early stops, never abstention
# (GR-1). AGR must not earn abstention credit for a provider error.
_NON_ABSTENTION_STATUSES = {"review_failed", "incomplete", "all_proposals_rejected"}


def predict_from_store(
    store,
    case: BenchmarkCase,
    *,
    reviewer_key: Optional[str] = None,
) -> AttributionPrediction:
    """AGR's single prediction for one case, from its served review.

    Declared rule (see :data:`WHO_WHEN_MANIFEST.single_prediction_rule`): the
    highest-ranked **negative** moment. Positive moments and the deterministic
    selection order are respected — the top of the reviewer's own ranking, not
    an incidental timeline position.
    """
    from . import read

    review = read.get_review(store, case.run_id, reviewer_key=reviewer_key)
    status = review.get("review_status")
    if status in _NON_ABSTENTION_STATUSES:
        return AttributionPrediction(
            case_id=case.case_id, review_status=status,
            notes=[f"review status {status!r} is a failure, not an abstention"],
        )

    moments = review.get("moments", [])
    negative = [m for m in moments if m.get("polarity") != "positive"]
    if not negative:
        return AttributionPrediction(
            case_id=case.case_id, review_status=status, abstained=True,
            notes=["no negative moment was selected"],
        )

    chosen = negative[0]
    forensic = read.get_forensic(store, case.run_id)
    step_of_event: dict[str, dict] = {}
    for row in forensic.get("steps", []):
        for eid in row.get("event_ids", []):
            step_of_event[eid] = row

    rows = [step_of_event[e] for e in chosen.get("anchor_event_ids", []) if e in step_of_event]
    # Who&When expects one decisive step. Select the earliest source step in
    # the chosen moment's anchors, with anchor order as a stable fallback.
    ordered_rows = sorted(
        rows,
        key=lambda row: (
            row.get("sequence")
            if isinstance(row.get("sequence"), int)
            else float("inf")
        ),
    )
    step_ids = _dedupe(r["step_id"] for r in ordered_rows)[:1]
    selected_rows = [r for r in ordered_rows if step_ids and r["step_id"] == step_ids[0]]
    return AttributionPrediction(
        case_id=case.case_id,
        step_ids=step_ids,
        agent=_resolve_agent(selected_rows, forensic.get("steps", [])),
        moment_id=chosen.get("moment_id"),
        review_status=status,
    )


def _dedupe(it) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for x in it:
        if x not in seen:
            seen.add(x)
            out.append(x)
    return out


# Actors that name a mechanism, not a responsible agent. A tool result's own
# actor is "tool"; attribution walks back to the agent that issued it.
_MECHANISM_ACTORS = {"tool", "harness", "environment", "unknown", ""}


def _resolve_agent(rows: list[dict], all_rows: list[dict]) -> Optional[str]:
    """AGR's attributed agent for a moment, by a declared, deterministic rule.

    1. If one of the moment's own anchor steps is an agent turn, use it.
    2. Otherwise (a tool/harness anchor) walk back to the nearest preceding
       agent turn — the agent that most recently spoke before the anchor.
    3. Normalize a delegation role ("Orchestrator (-> WebSurfer)") to the
       speaking agent, never the delegate.
    """
    direct = next((_agent_of(r) for r in rows if _is_agent_actor(r.get("actor"))), None)
    if direct:
        return direct
    if not rows:
        return None
    anchor_seq = min((r.get("sequence") or 0) for r in rows)
    earlier = [r for r in all_rows
               if isinstance(r.get("sequence"), int) and r["sequence"] < anchor_seq
               and _is_agent_actor(r.get("actor"))]
    if earlier:
        return _agent_of(max(earlier, key=lambda r: r["sequence"]))
    # No earlier agent: fall back to any agent anywhere (better than None).
    anywhere = [r for r in all_rows if _is_agent_actor(r.get("actor"))]
    return _agent_of(anywhere[0]) if anywhere else None


def _is_agent_actor(actor: Optional[str]) -> bool:
    if actor is None:
        return False
    if actor in _MECHANISM_ACTORS:
        return False
    return "terminal" not in actor.lower() and "computer" not in actor.lower()


def _agent_of(row: dict) -> Optional[str]:
    actor = row.get("actor")
    if not actor or actor in _MECHANISM_ACTORS:
        return None
    # "Orchestrator (-> WebSurfer)" is produced by the Orchestrator.
    if actor.startswith("Orchestrator"):
        return "Orchestrator"
    return actor


# --- scoring -----------------------------------------------------------------


@dataclass
class AttributionCaseScore:
    """One case's outcome under the benchmark's protocol.

    ``step_correct``/``agent_correct`` are ``None`` only when the benchmark's
    label cannot be scored for that case (see the adapter's warnings); a
    prediction that abstained scores ``False`` on both — it is in the
    denominator, never silently dropped.
    """

    case_id: str
    run_id: str
    step_correct: Optional[bool]
    agent_correct: Optional[bool]
    both_correct: Optional[bool]
    abstained: bool
    review_status: Optional[str]
    predicted_step_ids: list[str] = field(default_factory=list)
    predicted_agent: Optional[str] = None
    gold_step_ids: list[str] = field(default_factory=list)
    gold_agent: Optional[str] = None
    notes: list[str] = field(default_factory=list)
    task_id: Optional[str] = None

    def to_dict(self) -> dict:
        return _clean({
            "case_id": self.case_id,
            "run_id": self.run_id,
            "task_id": self.task_id,
            "step_correct": self.step_correct,
            "agent_correct": self.agent_correct,
            "both_correct": self.both_correct,
            "abstained": self.abstained,
            "review_status": self.review_status,
            "predicted_step_ids": self.predicted_step_ids,
            "predicted_agent": self.predicted_agent,
            "gold_step_ids": self.gold_step_ids,
            "gold_agent": self.gold_agent,
            "notes": self.notes,
        })


def score_case(prediction: AttributionPrediction, case: BenchmarkCase) -> AttributionCaseScore:
    """Score one prediction against its case's reference label.

    Step match compares the one submitted source step with the benchmark's one
    decisive step. Agent match
    compares AGR's attributed agent to the benchmark's labelled responsible
    agent. An abstention scores False on both — the plan's rule that errors and
    abstentions stay in the denominator.
    """
    annotation = case.gold.adjudicated_annotation()
    if annotation.no_decisive_moment or not annotation.moments:
        # A failure-only benchmark should not produce this; if a dataset ever
        # labels a clean run, the case is a no-moment case and AGR's correct
        # behaviour is to abstain.
        correct = prediction.abstained
        return AttributionCaseScore(
            case_id=case.case_id, run_id=case.run_id,
            task_id=str(case.meta.get("question_id") or case.meta.get("task_id") or case.run_id),
            step_correct=correct, agent_correct=correct, both_correct=correct,
            abstained=prediction.abstained, review_status=prediction.review_status,
            predicted_step_ids=prediction.step_ids, predicted_agent=prediction.agent,
            notes=["no decisive moment labelled: scored on abstention"],
        )

    gold = annotation.moments[0]
    gold_step = gold.anchor_step_ids[0] if gold.anchor_step_ids else None
    predicted_step = prediction.step_ids[0] if prediction.step_ids else None
    step_correct = predicted_step is not None and predicted_step == gold_step
    gold_agent = case.meta.get("mistake_agent")
    agent_correct = (
        prediction.agent is not None and gold_agent is not None
        and prediction.agent == gold_agent
    )
    both = step_correct and agent_correct
    return AttributionCaseScore(
        case_id=case.case_id, run_id=case.run_id,
        task_id=str(case.meta.get("question_id") or case.meta.get("task_id") or case.run_id),
        step_correct=step_correct, agent_correct=agent_correct, both_correct=both,
        abstained=prediction.abstained, review_status=prediction.review_status,
        predicted_step_ids=prediction.step_ids, predicted_agent=prediction.agent,
        gold_step_ids=gold.anchor_step_ids, gold_agent=gold_agent,
    )


@dataclass
class AttributionSetEval:
    """Micro-averaged attribution metrics over a case set (EV-2 group 1).

    Each metric is a rate over the *same* denominator (all scored cases), so a
    reader can compare them directly. ``abstention_rate`` is reported beside
    them because a high step score that came with many abstentions is a
    different result from a high step score on a full set.
    """

    scores: list[AttributionCaseScore] = field(default_factory=list)
    manifest: Optional[BenchmarkManifest] = None
    warnings: list[str] = field(default_factory=list)
    run_manifest: Optional[dict[str, Any]] = None

    def metrics(self) -> dict:
        n = len(self.scores)
        result = {
            "benchmark_eval_version": version.BENCHMARK_EVAL_VERSION,
            "benchmark": self.manifest.benchmark if self.manifest else None,
            "n_cases": n,
            "n_warnings": len(self.warnings),
            "step_accuracy": _rate(sum(1 for s in self.scores if s.step_correct is True), n),
            "agent_accuracy": _rate(sum(1 for s in self.scores if s.agent_correct is True), n),
            "both_accuracy": _rate(sum(1 for s in self.scores if s.both_correct is True), n),
            "abstention_rate": _rate(sum(1 for s in self.scores if s.abstained), n),
            "review_failed_count": sum(1 for s in self.scores if s.review_status == "review_failed"),
            "incomplete_count": sum(1 for s in self.scores if s.review_status == "incomplete"),
            "all_proposals_rejected_count": sum(
                1 for s in self.scores if s.review_status == "all_proposals_rejected"
            ),
            # Numerator/denominator kept explicit so a scoped claim can quote
            # "X of N" rather than only a rounded rate (EV-4 wording).
            "step_correct_count": sum(1 for s in self.scores if s.step_correct is True),
            "agent_correct_count": sum(1 for s in self.scores if s.agent_correct is True),
            "both_correct_count": sum(1 for s in self.scores if s.both_correct is True),
        }
        if self.manifest and self.manifest.benchmark == "trail":
            true_positives = sum(getattr(s, "true_positives", 0) for s in self.scores)
            predicted = sum(getattr(s, "predicted_count", 0) for s in self.scores)
            gold = sum(getattr(s, "gold_count", 0) for s in self.scores)
            critical_tp = sum(getattr(s, "critical_true_positives", 0) for s in self.scores)
            critical_gold = sum(getattr(s, "critical_gold_count", 0) for s in self.scores)
            precision = true_positives / predicted if predicted else 0.0
            recall = true_positives / gold if gold else 0.0
            result["trail_span_metrics"] = {
                "true_positives": true_positives, "predicted_spans": predicted,
                "gold_spans": gold, "span_precision": round(precision, 4),
                "span_recall": round(recall, 4),
                "span_f1": round(2 * precision * recall / (precision + recall), 4)
                if precision + recall else 0.0,
                "high_impact_recall": round(critical_tp / critical_gold, 4)
                if critical_gold else None,
                "high_impact_numerator": critical_tp,
                "high_impact_denominator": critical_gold,
            }
            result["step_accuracy"] = None
            result["agent_accuracy"] = None
            result["both_accuracy"] = None
            result["step_correct_count"] = None
            result["agent_correct_count"] = None
            result["both_correct_count"] = None
        return result

    def report(self) -> dict:
        return {
            "manifest": self.manifest.to_dict() if self.manifest else None,
            "run_manifest": self.run_manifest,
            "summary": self.metrics(),
            "cases": [s.to_dict() for s in self.scores],
            "warnings": self.warnings,
        }


def _rate(num: int, den: int):
    if den == 0:
        return None
    return round(num / den, 4)


def build_run_manifest(
    adapter: BenchmarkAdapter,
    directory: str,
    *,
    limit: Optional[int],
    provider: Optional[str],
    model: Optional[str],
    base_url: Optional[str] = None,
    protocol: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """Freeze the benchmark inputs and reviewer setup before scoring begins.

    Only source filenames and content hashes are recorded; benchmark records
    and labels stay local and are never copied into the run manifest.
    """
    records = adapter.input_inventory(directory, limit=limit)
    canonical_inputs = json.dumps(records, sort_keys=True, separators=(",", ":"))
    endpoint = None
    if base_url:
        parsed = urlsplit(base_url)
        if parsed.scheme and parsed.hostname:
            endpoint = f"{parsed.scheme}://{parsed.hostname}"
            if parsed.port:
                endpoint += f":{parsed.port}"

    reviewer: dict[str, Any] = {"kind": "model" if provider else "deterministic"}
    if provider:
        from .model_reviewer import SYSTEM_PROMPT

        reviewer.update({
            "provider": provider,
            "model": model,
            "endpoint_origin": endpoint,
            "reviewer_version": version.MODEL_REVIEWER_VERSION,
            "system_prompt_sha256": hashlib.sha256(
                SYSTEM_PROMPT.encode("utf-8")
            ).hexdigest(),
        })

    return {
        "run_manifest_version": version.BENCHMARK_RUN_MANIFEST_VERSION,
        "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "benchmark": adapter.manifest.to_dict(),
        "software": {
            "agr_version": version.AGR_VERSION,
            "benchmark_eval_version": version.BENCHMARK_EVAL_VERSION,
        },
        "reviewer": reviewer,
        "evaluation_protocol": protocol or {},
        "selection": {
            "directory_name": os.path.basename(os.path.normpath(directory)),
            "limit": limit,
            "record_count": len(records),
            "records": records,
            "records_sha256": hashlib.sha256(canonical_inputs.encode("utf-8")).hexdigest(),
        },
    }


# --- runner ------------------------------------------------------------------


def run_benchmark(
    adapter: BenchmarkAdapter,
    directory: str,
    store,
    *,
    limit: Optional[int] = None,
    reviewer_factory: Optional[Callable] = None,
    reviewer_key: Optional[str] = None,
    analyze_fn: Optional[Callable] = None,
    run_manifest: Optional[dict[str, Any]] = None,
) -> AttributionSetEval:
    """Convert, analyze and score a benchmark directory end to end.

    ``analyze_fn`` defaults to :func:`agr.pipeline.analyze`; it is injectable so
    a test can drive the scorers without a full store. ``reviewer_factory`` is
    the Stage F model reviewer when a model-scored run is wanted — ``None``
    keeps the deterministic envelope, whose moments are still real predictions.
    """
    if analyze_fn is None:
        from .pipeline import analyze as analyze_fn  # type: ignore[assignment]

    cases, warnings = adapter.load_cases(directory, limit=limit)
    scores: list[AttributionCaseScore] = []
    for case in cases:
        analyze_fn(case.doc, store, reviewer=reviewer_factory() if reviewer_factory else None)
        if hasattr(adapter, "score_case"):
            scores.append(adapter.score_case(store, case, reviewer_key=reviewer_key))
        else:
            prediction = predict_from_store(store, case, reviewer_key=reviewer_key)
            scores.append(score_case(prediction, case))
    return AttributionSetEval(
        scores=scores, manifest=adapter.manifest, warnings=warnings,
        run_manifest=run_manifest,
    )
