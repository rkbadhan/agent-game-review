"""Who&When attribution-benchmark adapter (EV-1).

[Who&When](https://github.com/ag2ai/Agents_Failure_Attribution) (ICML 2025) is
184 annotated **failure** tasks from two multi-agent systems — AutoGen's
CaptainAgent (algorithm-generated) and Microsoft's Magnetic-One (hand-crafted) —
over GAIA / AssistantBench queries. Each record labels the failure-responsible
agent (``mistake_agent``), the decisive error step (``mistake_step``, a 0-based
index into ``history``), and a natural-language explanation (``mistake_reason``).

The adapter does two faithful conversions:

1. **Trace -> ATIF.** Every ``history`` entry becomes exactly one ATIF step, in
   order, so ``mistake_step = k`` maps to source step ``s{k+1}`` with no
   ambiguity. A turn whose speaker is a terminal executor becomes a
   ``tool_result`` (actor ``"tool"``); a ``human`` turn is the ``task_received``;
   everything else is a ``model_output``. The speaker is the entry's ``name``
   when present (algorithm-generated) or its ``role`` otherwise (hand-crafted),
   with an ``"Orchestrator (-> X)"`` delegation normalised to the speaking
   ``Orchestrator`` — the decision-maker, not the delegate.

2. **Label -> gold.** ``mistake_step`` becomes a ``GoldMoment`` anchored on that
   source step (``critical``, negative, ``decision``); ``mistake_agent`` is kept
   in ``case.meta`` for the agent half of the benchmark's protocol. Reference
   labels never enter the ATIF document, so they cannot reach reviewer input.

No verifier is supplied: Who&When records none, and AGR must not invent a
pass/fail signal it was never given. The run therefore ingests as
"Task success unverified", and the benchmark's own ``is_correct`` field is kept
in ``meta`` as the dataset's outcome label, not as verifier evidence.

Pure stdlib — no third-party imports, no model calls.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from typing import Any, Optional

from .adapter import apply_capability_defaults
from .benchmark import BenchmarkCase, BenchmarkManifest
from .gold import GoldAnnotation, GoldMoment, GoldTrajectory

WHO_WHEN_VERSION = "who-and-when-2025-05"

WHO_WHEN_MANIFEST = BenchmarkManifest(
    benchmark="who_and_when",
    version=WHO_WHEN_VERSION,
    source_url="https://github.com/ag2ai/Agents_Failure_Attribution",
    license="MIT",
    label_provenance=(
        "Expert human annotation by the Who&When authors: failure-responsible "
        "agent, decisive error step and a written explanation, following the "
        "public annotation guide."
    ),
    single_prediction_rule=(
        "AGR's highest-ranked NEGATIVE selected moment. Its anchor step is the "
        "earliest source step among the moment's anchors (lowest trace sequence); "
        "that step alone is submitted, and its attributed agent is responsible. "
        "This rule is declared before scoring and never uses AGR's other moments."
    ),
    scoring_protocol=(
        "Who&When's own protocol, applied per case: step accuracy (the single "
        "predicted decisive step equals the labelled decisive step), agent accuracy "
        "(the labelled responsible agent equals the prediction's attributed "
        "agent), and both. Errors and abstentions stay in the denominator with "
        "zero credit."
    ),
    coverage={
        "records": 184,
        "splits": {"algorithm_generated": 126, "hand_crafted": 58},
        "outcomes": ["failed"],
        "labels": ["responsible_agent", "decisive_step", "reason"],
        "abstention": False,
        "verifier_results": False,
    },
    limits=[
        "Failure-only dataset: it cannot demonstrate correct abstention on clean "
        "runs, and missing annotations are never evidence that no valid moment exists.",
        "Multi-agent web/QA tasks (GAIA, AssistantBench), not coding traces; "
        "the decisive step may be a coordination error with no local tool failure.",
        "No per-check verifier results, so AGR's verifier-driven detectors cannot "
        "fire; only observed execution findings are available.",
        "The labelled responsible agent is sometimes a different agent from the "
        "speaker at the decisive step, so agent accuracy is a harder, partly "
        "independent label than step accuracy.",
    ],
)

# A speaker that names a terminal/executor, in either split's naming
# ("Computer_terminal" algorithm-generated, "ComputerTerminal" hand-crafted).
_EXECUTOR_RE = re.compile(r"terminal|computer", re.IGNORECASE)
_DELEGATION_RE = re.compile(r"^Orchestrator \(-> ([^)]+)\)$")
# Who&When's terminal outputs declare their own exit code in the text
# ("exitcode: 1 (execution failed) ..."). Reading it out is faithful, not a
# guess — it is the only signal AGR's recovery detectors can use on a source
# that carries no structured tool metadata.
_EXIT_CODE_RE = re.compile(r"exitcode:\s*(-?\d+)", re.IGNORECASE)


def _speaker(entry: dict) -> str:
    """The agent that produced a history entry.

    ``name`` is the AutoGen speaker for the algorithm-generated split; the
    hand-crafted split uses ``role`` as the speaker. An ``"Orchestrator (-> X)"``
    role is the Orchestrator's own turn, so its speaker is the Orchestrator.
    """
    name = entry.get("name")
    if name:
        return str(name)
    role = str(entry.get("role") or "")
    if _DELEGATION_RE.match(role):
        # The Orchestrator's own turn delegating to a specialist; the
        # decision-maker is the Orchestrator, not the delegate.
        return "Orchestrator"
    return role


def _kind_and_actor(speaker: str, role: str) -> tuple[str, str]:
    if _EXECUTOR_RE.search(speaker):
        return "tool_result", "tool"
    if role == "human":
        return "task_received", "human"
    return "model_output", speaker


def convert_record(record: dict, *, case_id: str, split: str) -> BenchmarkCase:
    """Convert one Who&When record into an ingestable :class:`BenchmarkCase`.

    Raises :class:`ValueError` on a record the adapter cannot faithfully map
    (missing history, an out-of-range ``mistake_step``) rather than guessing.
    """
    history = record.get("history")
    if not isinstance(history, list) or not history:
        raise ValueError(f"{case_id}: no history[] to convert")
    try:
        mistake_step = int(record["mistake_step"])
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError(f"{case_id}: missing or non-integer mistake_step") from exc
    if not 0 <= mistake_step < len(history):
        raise ValueError(
            f"{case_id}: mistake_step {mistake_step} outside history of {len(history)}"
        )
    question_id = str(record.get("question_ID") or case_id)
    run_id = f"whowhen__{split}__{_slug(case_id)}"

    steps: list[dict[str, Any]] = []
    for i, entry in enumerate(history):
        if not isinstance(entry, dict):
            raise ValueError(f"{case_id}: history[{i}] is not an object")
        speaker = _speaker(entry)
        role = str(entry.get("role") or "")
        kind, actor = _kind_and_actor(speaker, role)
        content = entry.get("content") or ""
        step: dict[str, Any] = {
            "step_id": f"s{i + 1}",
            "kind": kind,
            "actor": actor,
            "content": content,
            "provenance": "observed",
        }
        if kind == "tool_result":
            step["tool"] = "terminal"
            match = _EXIT_CODE_RE.search(content)
            if match:
                step["exit_code"] = int(match.group(1))
        steps.append(step)

    question = record.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError(f"{case_id}: missing question; record skipped to avoid exposing ground_truth")
    instruction = question.strip()
    doc: dict[str, Any] = {
        "atif_version": "1.7",
        "source_type": "who_and_when",
        "adapter_version": WHO_WHEN_VERSION,
        # Execution transcript and terminal outputs are captured; structured
        # tool calls, filesystem/process state and verifier results are not.
        "capture_completeness": "partial",
        "run": {
            "logical_run_id": run_id,
            "task_id": question_id,
            "agent": "multi-agent",
            "harness_version": WHO_WHEN_VERSION,
        },
        # Every tool result the agent saw is present as terminal output, so
        # tool_results is complete; the CALLS themselves are not structured
        # (no arguments/ids), so tool_calls is partial and call↔result pairing
        # is inferred from transcript order.
        "capabilities": apply_capability_defaults({
            "messages": "complete",
            "tool_calls": "partial",
            "tool_results": "complete",
        }),
        "task": {
            "instruction": instruction,
            "artifacts": [],
            "requirements": [],
        },
        "steps": steps,
    }

    gold = GoldTrajectory(
        run_id=run_id,
        task_id=question_id,
        annotations=[GoldAnnotation(
            annotator="who_and_when",
            moments=[GoldMoment(
                moment_id="wm_1",
                anchor_type="decision",
                anchor_step_ids=[f"s{mistake_step + 1}"],
                polarity="negative",
                critical=True,
                attribution_ceiling="dependency_linked",
            )],
        )],
        label_source="human",
        frozen=True,
    )
    meta = {
        "split": split,
        "mistake_agent": record.get("mistake_agent"),
        "mistake_reason": record.get("mistake_reason"),
        "question_id": question_id,
        # Who&When is a failure benchmark; the raw flags are preserved as the
        # dataset's outcome label, never turned into a verifier signal.
        "outcome": "failed",
        "is_correct": record.get("is_correct"),
        "is_corrected": record.get("is_corrected"),
        "level": record.get("level"),
    }
    return BenchmarkCase(case_id=case_id, doc=doc, gold=gold, meta=meta)


def _slug(value: str) -> str:
    """A run-id-safe segment from a file stem (digits/hyphens already are)."""
    slug = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(value)).strip("-")
    return slug or "case"


def _split_of(directory: str) -> str:
    name = os.path.basename(os.path.normpath(directory)).lower()
    return name.replace(" ", "-").replace("_", "-") or "who-and-when"


def load_cases(
    directory: str, *, limit: Optional[int] = None
) -> tuple[list[BenchmarkCase], list[str]]:
    """Load every ``*.json`` record under ``directory`` as a case.

    Returns ``(cases, warnings)``; a record that fails to map is recorded in
    ``warnings`` with its reason and skipped — never silently dropped and never
    guessed at.
    """
    if not os.path.isdir(directory):
        return [], [f"not a directory: {directory}"]
    split = _split_of(directory)
    names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    if limit is not None:
        names = names[:limit]
    cases: list[BenchmarkCase] = []
    warnings: list[str] = []
    for name in names:
        path = os.path.join(directory, name)
        case_id = name[:-5]
        try:
            with open(path, encoding="utf-8") as fh:
                record = json.load(fh)
            cases.append(convert_record(record, case_id=case_id, split=split))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            warnings.append(f"{name}: skipped ({exc})")
    return cases, warnings


class WhoWhenAdapter:
    """The Who&When adapter, exposed through the shared benchmark protocol."""

    name = "who-and-when"
    version = WHO_WHEN_VERSION
    manifest = WHO_WHEN_MANIFEST

    def input_inventory(
        self, directory: str, *, limit: Optional[int] = None
    ) -> list[dict[str, str]]:
        """Hash the exact ordered JSON record selection without copying labels."""
        if not os.path.isdir(directory):
            raise ValueError(f"not a directory: {directory}")
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
        if limit is not None:
            names = names[:limit]
        inventory = []
        for name in names:
            path = os.path.join(directory, name)
            with open(path, "rb") as fh:
                digest = hashlib.sha256(fh.read()).hexdigest()
            inventory.append({"record_id": name[:-5], "sha256": digest})
        return inventory

    def load_cases(
        self, directory: str, *, limit: Optional[int] = None
    ) -> tuple[list[BenchmarkCase], list[str]]:
        return load_cases(directory, limit=limit)


WHO_WHEN_ADAPTER = WhoWhenAdapter()
