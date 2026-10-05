"""TRAIL span-level error benchmark adapter (EV-1).

TRAIL records are locally supplied JSON/JSONL exports with ``trace`` and
``labels`` fields. This adapter intentionally does not download or redistribute
the gated dataset. High-impact labels are the predeclared critical subset;
AGR's highest-ranked five negative moments are compared as a set to labeled
source spans (span recall, precision, F1, and high-impact recall).
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass
from typing import Any, Optional

from .benchmark import BenchmarkManifest, _NON_ABSTENTION_STATUSES
from .gold import GoldAnnotation, GoldMoment, GoldTrajectory
from .ingest_otel import convert

TRAIL_VERSION = "trail-2025-05"
TRAIL_MANIFEST = BenchmarkManifest(
    benchmark="trail",
    version=TRAIL_VERSION,
    source_url="https://huggingface.co/datasets/PatronusAI/TRAIL",
    license="MIT (dataset card; gated records; redistribution restrictions apply)",
    label_provenance="Expert span-level error annotations distributed by PatronusAI.",
    single_prediction_rule=(
        "Submit AGR's highest-ranked five negative moments as a set; map each "
        "anchor event to its source step and then to its original OTel span id."
    ),
    scoring_protocol=(
        "Micro span precision, recall and F1 against all labeled error spans; "
        "high-impact recall treats TRAIL impact=High as the predeclared critical "
        "subset. A prediction matches when it maps to the same source span."
    ),
    coverage={"splits": ["gaia", "swe_bench"], "outcomes": ["errors present"],
              "abstention": False, "labels": ["span_id", "category", "evidence", "impact"]},
    limits=[
        "Failure/error-only benchmark; it cannot establish correct abstention on clean runs.",
        "High impact is a proxy for criticality, not a dataset-provided decisive-moment label.",
        "The gated source records remain local; this adapter supports user-provided exports only.",
    ],
)


def _json_value(value: Any, field: str) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{field} is not valid JSON: {exc}") from exc
    return value


def _label_rows(value: Any) -> list[dict]:
    value = _json_value(value, "labels")
    if isinstance(value, dict):
        for key in ("errors", "labels", "annotations"):
            if isinstance(value.get(key), list):
                value = value[key]
                break
        else:
            value = [value]
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise ValueError("labels must be a list of error objects")
    return value


def _records(directory: str, limit: Optional[int]) -> list[tuple[str, dict]]:
    if not os.path.isdir(directory):
        raise ValueError(f"not a directory: {directory}")
    files = sorted(n for n in os.listdir(directory) if n.endswith((".json", ".jsonl")))
    out = []
    for name in files:
        path = os.path.join(directory, name)
        with open(path, encoding="utf-8-sig") as fh:
            if name.endswith(".jsonl"):
                rows = [json.loads(line) for line in fh if line.strip()]
            else:
                obj = json.load(fh)
                rows = obj if isinstance(obj, list) else obj.get("records", [obj])
        for index, row in enumerate(rows):
            rid = str(row.get("id") or row.get("task_id") or f"{name}:{index}")
            out.append((rid, row))
    return out[:limit] if limit is not None else out


@dataclass
class TrailScore:
    case_id: str
    run_id: str
    task_id: str
    true_positives: int
    predicted_count: int
    gold_count: int
    critical_true_positives: int
    critical_gold_count: int
    review_status: str | None
    predicted_span_ids: list[str]
    gold_span_ids: list[str]

    @property
    def step_correct(self):
        return self.true_positives > 0

    @property
    def agent_correct(self):
        return self.true_positives > 0

    @property
    def both_correct(self):
        return self.true_positives > 0

    @property
    def abstained(self):
        return self.predicted_count == 0 and self.review_status not in _NON_ABSTENTION_STATUSES

    def to_dict(self) -> dict:
        precision = self.true_positives / self.predicted_count if self.predicted_count else 0.0
        recall = self.true_positives / self.gold_count if self.gold_count else 0.0
        return {"case_id": self.case_id, "run_id": self.run_id, "task_id": self.task_id,
                "span_precision": round(precision, 4), "span_recall": round(recall, 4),
                "true_positives": self.true_positives, "predicted_count": self.predicted_count,
                "gold_count": self.gold_count, "critical_true_positives": self.critical_true_positives,
                "critical_gold_count": self.critical_gold_count, "review_status": self.review_status,
                # Keep gated annotation identifiers out of portable reports.
                "predicted_span_count": self.predicted_count,
        }


class TrailAdapter:
    name = "trail"
    version = TRAIL_VERSION
    manifest = TRAIL_MANIFEST

    def input_inventory(self, directory: str, *, limit: Optional[int] = None) -> list[dict[str, str]]:
        records = []
        for rid, row in _records(directory, limit):
            raw = json.dumps(row, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
            records.append({"record_id": _anon_id(rid), "sha256": hashlib.sha256(raw.encode()).hexdigest()})
        return records

    def load_cases(self, directory: str, *, limit: Optional[int] = None):
        from .benchmark import BenchmarkCase

        cases, warnings = [], []
        for raw_case_id, record in _records(directory, limit):
            case_id = _anon_id(raw_case_id)
            try:
                trace = _json_value(record.get("trace"), "trace")
                if not isinstance(trace, dict):
                    raise ValueError("trace must decode to an OTLP JSON object")
                labels = _label_rows(record.get("labels"))
                converted = convert(trace, task_id=case_id, run_id=f"trail__{_slug(case_id)}")
                span_map = converted.meta.get("source_span_steps", {})
                moments, mapped = [], {}
                for n, label in enumerate(labels):
                    span_id = str(label.get("span_id") or label.get("spanId") or label.get("location") or "")
                    if not span_id:
                        raise ValueError(f"label {n + 1} has no span id; case is unscorable")
                    step_ids = span_map.get(span_id, [])
                    impact = str(label.get("impact") or "").lower()
                    mapped[span_id] = mapped.get(span_id, False) or impact == "high"
                    if not step_ids:
                        warnings.append(f"{case_id}: label span {span_id!r} has no source-step mapping")
                        continue
                    moments.append(GoldMoment(
                        moment_id=f"trail_{n + 1}", anchor_type="decision",
                        anchor_step_ids=list(step_ids), evidence_span_step_ids=list(step_ids),
                        polarity="negative", critical=impact == "high",
                        attribution_ceiling="dependency_linked"))
                if not labels:
                    raise ValueError("case has no error labels")
                gold = GoldTrajectory(run_id=converted.doc["run"]["logical_run_id"],
                                      task_id=case_id,
                                      annotations=[GoldAnnotation(annotator="trail", moments=moments)],
                                      label_source="human", frozen=True)
                step_span = {step: span for span, steps in span_map.items() for step in steps}
                cases.append(BenchmarkCase(case_id, converted.doc, gold,
                                           {"task_id": case_id,
                                            "trail_spans": list(mapped.items()),
                                            "step_span_ids": step_span,
                                            "split": record.get("split")}))
            except (ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
                warnings.append(f"{case_id}: skipped ({exc})")
        return cases, warnings

    def score_case(self, store, case, *, reviewer_key=None):
        from . import read
        review = read.get_review(store, case.run_id, reviewer_key=reviewer_key)
        gold_pairs = case.meta["trail_spans"]
        gold = [span for span, _ in gold_pairs]
        gold_critical = {span for span, critical in gold_pairs if critical}
        status = review.get("review_status")
        if status in _NON_ABSTENTION_STATUSES:
            # A fallback snapshot may still carry moments after model failure.
            # It is not a prediction: give it no numerator or predicted set.
            return TrailScore(case.case_id, case.run_id, case.meta["task_id"],
                              0, 0, len(set(gold)), 0, len(gold_critical),
                              status, [], gold)
        forensic = read.get_forensic(store, case.run_id)
        # The analysis stores original span ids in event.source_span_id after
        # this adapter has attached the mapping to its source steps (see loader).
        source_map = case.meta.get("step_span_ids", {})
        event_step = {}
        for row in forensic.get("steps", []):
            for eid in row.get("event_ids", []):
                event_step[eid] = row.get("step_id")
        predictions = []
        for moment in [m for m in review.get("moments", []) if m.get("polarity") != "positive"][:5]:
            for event_id in moment.get("anchor_event_ids", []):
                sid = event_step.get(event_id)
                span = source_map.get(sid)
                if span:
                    predictions.append(span)
        predictions = list(dict.fromkeys(predictions))
        predicted_set = set(predictions)
        return TrailScore(case.case_id, case.run_id, case.meta["task_id"],
                          len(predicted_set & set(gold)), len(predicted_set), len(set(gold)),
                          len(predicted_set & gold_critical), len(gold_critical),
                          review.get("review_status"), predictions, gold)


def _slug(value: str) -> str:
    import re
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", value).strip("-") or "case"


def _anon_id(value: str) -> str:
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:16]


TRAIL_ADAPTER = TrailAdapter()
