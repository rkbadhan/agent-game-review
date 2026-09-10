"""Matched version comparison — version A vs B on one task slice (§4.16, §6.12, §12.3).

This is the comparison the spec means by "comparison surface": *harness/model
version A versus B on the same matched task slice*. It is distinct from
:mod:`agr.compare`, which diffs two **reviews of one run** (a reviewer-vs-reviewer
tool in the §15.3 family). Both are "comparisons"; only this one compares versions.

The module is built around a single refusal: **a comparison must not look valid
when the slice cannot support it.** Everything else follows from that.

- Runs are matched on exact keys (§4.16.1). A key the source never declared is
  *unresolved*, never "equal by absence" — an unresolved version key makes the
  whole comparison ``invalid`` and the ``Matched`` label unavailable.
- Every run that does not enter the matched slice is excluded *with a reason*,
  grouped and countable, so the match report can show where the slice came from.
- If more than the declared axis changed between the two sides, the result is
  labelled a **configuration comparison**: still shown, but explicitly unable to
  isolate the declared component (§4.16.1).
- Aggregates are task-clustered, never run-pooled: repeated runs of one task
  cannot inflate a rate, which is the Simpson's-paradox guard §12.3 requires.
- Every metric carries its numerator, denominator, task/run counts, review
  coverage, and uncertainty method — never a bare percentage (§6.12).
- A slice too thin to estimate variance is labelled ``insufficient_evidence``
  rather than given a confident direction (§12.3).

Pure stdlib and deterministic: the same store and definition always produce the
same pairing, the same exclusions, and the same numbers. Narration is templated
and explicitly labelled interpretation — no model call is made here.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Iterable, Optional

from . import read
from .store import Store

MATCH_KEY_VERSION = "match-keys-1.0"
STATISTICS_VERSION = "paired-stats-1.0"

# Default exact match keys (§4.16.1), in the order the match report walks them.
DEFAULT_MATCH_KEYS = ("task_id", "task_version", "verifier_version",
                      "environment_image_digest", "task_parameters", "seed")

# Keys whose absence blocks the ``Matched`` label. The spec blocks on an
# unresolved *version* field; ``seed`` is explicitly conditional ("when
# meaningful and available") and environment/parameters degrade to a caveat.
VERSION_KEYS = ("task_version", "verifier_version")

# Run-source field -> the change axis it represents (§4.16.1 step 3). Identity
# fields (``sweep_id``, ``configuration_id``) are deliberately absent: they name
# the side rather than describe it, and always differ between two sides, so
# reading them as a changed axis would make every comparison unisolable.
AXIS_OF_FIELD = {
    "harness_version": "evaluation_harness",
    "model": "model",
    "agent": "agent_scaffold",
    "environment_image_digest": "environment",
}
# ``complete_configuration`` is declarable but never *observed* from a field: it
# is the honest declaration for "everything moved", and it never earns `Matched`.
COMPLETE_CONFIGURATION = "complete_configuration"
CHANGE_AXES = tuple(sorted(set(AXIS_OF_FIELD.values()) | {COMPLETE_CONFIGURATION}))

# Fields a pinned configuration is identified by, most specific first.
CONFIG_FIELDS = ("sweep_id", "configuration_id", "harness_version", "model", "agent")

_Z95 = 1.959963984540054

# Fewest matched tasks a *directional* claim is allowed to rest on. Two tasks can
# produce an interval that excludes zero while resting on two observations; §12.2
# sanctions gating sparse results by an initial default, provided the default is
# declared rather than treated as proof. Below this the row still shows its
# numbers — it just is not allowed to say "improved" or "regressed".
MIN_TASKS_FOR_DIRECTION = 3


class ComparisonError(ValueError):
    """Raised when a comparison definition cannot be built at all."""


# --- selection ---------------------------------------------------------------


def _runs(store: Store) -> list[dict]:
    """Every run's review, keyed for comparison. One read pass over the store."""
    out = []
    for summary in read.list_runs(store):
        out.append(read.get_review(store, summary["run_id"]))
    return out


def _matches_selector(run_source: dict, selector: dict) -> bool:
    return all(run_source.get(field) == value for field, value in selector.items())


def _select(runs: list[dict], selector: dict) -> list[dict]:
    if not selector:
        raise ComparisonError("a side selector must name at least one field")
    unknown = set(selector) - set(CONFIG_FIELDS)
    if unknown:
        raise ComparisonError(f"cannot select a configuration on {sorted(unknown)}")
    return [r for r in runs if _matches_selector(r.get("run") or {}, selector)]


def list_configurations(store: Store) -> list[dict]:
    """The pinned configurations present in the store (§4.16.1 steps 1–2).

    A *configuration* is the sweep or version identity a side of a comparison is
    selected by. Each entry carries the selector that reproduces it, so the
    construct flow never has to invent one.
    """
    groups: dict[tuple, dict] = {}
    for run in _runs(store):
        rs = run.get("run") or {}
        key = tuple(rs.get(f) for f in CONFIG_FIELDS)
        entry = groups.setdefault(key, {
            "configuration": {f: rs.get(f) for f in CONFIG_FIELDS if rs.get(f) is not None},
            "label": _config_label(rs),
            "run_count": 0,
            "task_ids": set(),
            # Every axis value seen in this group, so a caller can tell which axes
            # separate two configurations. Kept apart from ``configuration`` (the
            # selector fields) because an axis like the environment digest
            # describes a configuration without identifying it.
            "_axis_values": {field: set() for field in AXIS_OF_FIELD},
        })
        entry["run_count"] += 1
        entry["task_ids"].add(rs.get("task_id"))
        for field in AXIS_OF_FIELD:
            entry["_axis_values"][field].add(rs.get(field))
    out = []
    for entry in groups.values():
        task_ids = entry.pop("task_ids")
        entry["task_count"] = len(task_ids)
        # Carried so a caller can pick a *comparable* default pair rather than
        # two arbitrary configurations: a store often holds more than two, and
        # the wrong pair produces a valid-looking comparison of unrelated slices.
        entry["task_ids"] = sorted(t for t in task_ids if t)
        values = entry.pop("_axis_values")
        # A value only describes the group when the whole group agrees on it.
        entry["axes"] = {field: (next(iter(seen)) if len(seen) == 1 else None)
                         for field, seen in values.items()}
        entry["selector"] = _narrowest_selector(entry["configuration"])
        out.append(entry)
    out.sort(key=lambda e: e["label"])
    return out


def default_pair(configurations: list[dict]) -> tuple[Optional[dict], Optional[dict]]:
    """The two configurations that make the most comparable slice (§4.16.1).

    Ranked by *isolation first*: a pair separated by exactly one axis is the
    comparison the surface exists to make, and is preferred even to a pair that
    shares more tasks. A bigger slice across two changed axes can only ever be a
    configuration comparison, so offering it by default would open on a result
    that cannot isolate anything. Slice size is the tie-break. Returns
    ``(None, None)`` when no two configurations share a task, which is the honest
    answer: there is no default comparison to offer.
    """
    best, best_score = (None, None), ()
    for i, left in enumerate(configurations):
        for right in configurations[i + 1:]:
            shared = len(set(left["task_ids"]) & set(right["task_ids"]))
            if not shared:
                continue
            axes = sum(1 for field in AXIS_OF_FIELD
                       if left["axes"].get(field) != right["axes"].get(field))
            score = (axes == 1, -axes, shared, left["run_count"] + right["run_count"])
            if score > best_score:
                best, best_score = (left, right), score
    return best


def _config_label(rs: dict) -> str:
    for field in CONFIG_FIELDS:
        if rs.get(field):
            return str(rs[field])
    return "unidentified configuration"


def _narrowest_selector(configuration: dict) -> dict:
    """The most specific single-field selector that identifies a configuration."""
    for field in CONFIG_FIELDS:
        if configuration.get(field) is not None:
            return {field: configuration[field]}
    return {}


# --- match keys --------------------------------------------------------------


def _key_value(run_source: dict, key: str) -> Any:
    value = run_source.get(key)
    if key == "task_parameters" and isinstance(value, dict):
        return json.dumps(value, sort_keys=True)
    return value


def _unresolved_keys(runs: Iterable[dict], keys: Iterable[str]) -> list[str]:
    """Keys no run on a side declares — unresolvable, so unusable for matching."""
    runs = list(runs)
    return [k for k in keys
            if any(_key_value(r.get("run") or {}, k) is None for r in runs)]


def _match_tuple(run: dict, keys: Iterable[str]) -> tuple:
    rs = run.get("run") or {}
    return tuple(_key_value(rs, k) for k in keys)


# --- pairing -----------------------------------------------------------------


def _exclusion(run: dict, side: str, reason: str) -> dict:
    return {"run_id": run.get("run", {}).get("run_id"), "side": side, "reason": reason}


def _first_mismatched_key(run: dict, others: list[dict], keys: list[str]) -> Optional[str]:
    """The earliest key on which a run differs from every same-task counterpart.

    Drives the exclusion *reason*: a run kept out of the slice says which key
    kept it out, rather than a generic "no match".
    """
    best: Optional[int] = None
    for other in others:
        for i, key in enumerate(keys):
            if _key_value(run.get("run") or {}, key) != _key_value(other.get("run") or {}, key):
                best = i if best is None else min(best, i)
                break
    return keys[best] if best is not None else None


def _reason_for_key(key: str) -> str:
    if key == "environment_image_digest":
        return "environment_mismatch"
    return f"{key}_mismatch"


def _pair_runs(baseline: list[dict], candidate: list[dict],
               keys: list[str]) -> tuple[list[dict], list[dict], int]:
    """Pair the two sides on the resolvable match keys (§4.16.1 steps 4–5).

    Returns ``(pairs, exclusions, strata)``. Runs sharing one key tuple on a side
    are a *repeated-run stratum*: they pair in run-id order up to the smaller
    count, and the surplus is excluded rather than silently averaged in.
    """
    def by_key(runs):
        grouped: dict[tuple, list[dict]] = {}
        for r in runs:
            grouped.setdefault(_match_tuple(r, keys), []).append(r)
        for group in grouped.values():
            group.sort(key=lambda r: r.get("run", {}).get("run_id") or "")
        return grouped

    left, right = by_key(baseline), by_key(candidate)
    tasks_left = {(r.get("run") or {}).get("task_id") for r in baseline}
    tasks_right = {(r.get("run") or {}).get("task_id") for r in candidate}

    pairs: list[dict] = []
    exclusions: list[dict] = []
    strata = 0
    for key in sorted(set(left) | set(right), key=lambda k: tuple(str(v) for v in k)):
        lrs, rrs = left.get(key, []), right.get(key, [])
        if lrs and rrs:
            if len(lrs) > 1 or len(rrs) > 1:
                strata += 1
            for i in range(min(len(lrs), len(rrs))):
                pairs.append({
                    "pair_id": f"pair_{len(pairs) + 1:03d}",
                    "task_id": (lrs[i].get("run") or {}).get("task_id"),
                    "match_key": [None if v is None else str(v) for v in key],
                    "baseline_run_id": (lrs[i].get("run") or {}).get("run_id"),
                    "candidate_run_id": (rrs[i].get("run") or {}).get("run_id"),
                    "_baseline": lrs[i],
                    "_candidate": rrs[i],
                })
            for surplus in lrs[min(len(lrs), len(rrs)):]:
                exclusions.append(_exclusion(surplus, "baseline", "unpaired_repeated_run"))
            for surplus in rrs[min(len(lrs), len(rrs)):]:
                exclusions.append(_exclusion(surplus, "candidate", "unpaired_repeated_run"))
            continue
        # One side only: name the key that kept these runs out.
        for run in lrs:
            exclusions.append(_side_exclusion(run, "baseline", candidate, tasks_right, keys))
        for run in rrs:
            exclusions.append(_side_exclusion(run, "candidate", baseline, tasks_left, keys))
    return pairs, exclusions, strata


def _side_exclusion(run: dict, side: str, others: list[dict],
                    other_tasks: set, keys: list[str]) -> dict:
    task_id = (run.get("run") or {}).get("task_id")
    if task_id not in other_tasks:
        return _exclusion(run, side, "missing_counterpart")
    same_task = [o for o in others if (o.get("run") or {}).get("task_id") == task_id]
    key = _first_mismatched_key(run, same_task, keys)
    return _exclusion(run, side, _reason_for_key(key) if key else "missing_counterpart")


# --- statistics (paired-stats-1.0) -------------------------------------------


def _mean(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _paired_difference(diffs: list[float]) -> dict:
    """Task-clustered paired difference with a normal-approximation interval.

    One observation per *task*, never per run: repeated runs of the same task are
    averaged into that task's rate first, so a task with many runs cannot count
    more than a task with one (§12.3). With fewer than two tasks there is no
    variance to estimate, and the interval is withheld rather than faked.
    """
    n = len(diffs)
    mean = _mean(diffs)
    if n < 2:
        return {"difference": mean, "n_tasks": n, "interval": None,
                "method": STATISTICS_VERSION, "estimable": False}
    variance = sum((d - mean) ** 2 for d in diffs) / (n - 1)
    se = math.sqrt(variance / n)
    half = _Z95 * se
    return {"difference": mean, "n_tasks": n,
            "interval": [mean - half, mean + half],
            "standard_error": se, "method": STATISTICS_VERSION, "estimable": True}


def _label_change(stats: dict, higher_is_better: bool,
                  baseline_eligible: int, candidate_eligible: int) -> str:
    """Improved / Regressed / Within uncertainty / Insufficient evidence / one-sided."""
    if baseline_eligible == 0 and candidate_eligible == 0:
        return "insufficient_evidence"
    if baseline_eligible == 0 or candidate_eligible == 0:
        return "observed_only_on_one_side"
    if not stats.get("estimable") or stats.get("n_tasks", 0) < MIN_TASKS_FOR_DIRECTION:
        return "insufficient_evidence"
    low, high = stats["interval"]
    if low <= 0 <= high:
        return "within_uncertainty"
    improved = (stats["difference"] > 0) == higher_is_better
    return "improved" if improved else "regressed"


# --- per-run measurements ----------------------------------------------------


def _passed(run: dict) -> float:
    return 1.0 if (run.get("outcome") or {}).get("status") == "PASSED" else 0.0


def _behaviour_observations(run: dict) -> dict[str, tuple[int, int, int]]:
    """``ability -> (successes, evaluated, eligible-but-unevaluated)`` (§12.1).

    Restricted to *opportunity-gated* abilities — the ones the run created a
    chance to observe (§4.6). The rest of the Task Ability Signature is derived
    from verifier checks; rating those as "behaviour" would re-express the pass
    rate under a second name and drown the two rates that are actually
    opportunity-normalized.

    Eligibility is the denominator, but only where the review *evaluated* the
    window: an opportunity that occurred and was not evaluated is counted
    separately rather than folded in, since silently treating it as a failure
    would make thin review coverage look like worse behaviour. The deterministic
    signature emits one row per ability per run, so the unit is runs carrying an
    eligible opportunity, not individual windows.
    """
    signature = {r.get("ability"): r for r in run.get("signature") or []}
    out: dict[str, tuple[int, int, int]] = {}
    for opportunity in run.get("opportunity_rows") or []:
        ability = opportunity.get("ability")
        row = signature.get(ability)
        successes, evaluated, unevaluated = out.get(ability, (0, 0, 0))
        if opportunity.get("status") == "measured" and row is not None and row.get("measured"):
            out[ability] = (successes + (1 if row.get("result") == "Successful" else 0),
                            evaluated + 1, unevaluated)
        else:
            out[ability] = (successes, evaluated, unevaluated + 1)
    return out


def _failure_observations(run: dict) -> dict[str, int]:
    """``detector -> count of concern candidates`` mechanically observed in one run.

    Counted from the detector results, *not* from the selected moment cards.
    Card selection (§8.10) ranks and caps what a reviewer is shown; if a rate were
    computed from it, a run whose cards were crowded out would read as a run where
    the behaviour did not happen — turning a presentation choice into a measured
    version difference. The detector output is the mechanical observation.
    """
    out: dict[str, int] = {}
    for result in run.get("detector_results") or []:
        if not result.get("evaluated"):
            continue
        concerns = sum(1 for c in result.get("candidates", [])
                       if c.get("polarity", "negative") != "positive")
        if concerns:
            out[result["detector"]] = out.get(result["detector"], 0) + concerns
    return out


def _evaluated_detectors(run: dict) -> set[str]:
    return {d.get("detector") for d in run.get("detector_results") or [] if d.get("evaluated")}


def _duration(run: dict) -> Optional[float]:
    return read._duration_seconds(run.get("run") or {})


def _token_total(usage: Optional[dict]) -> Optional[float]:
    """Sum a captured usage dict (input/output/cache token counts, …) to one
    comparable number. ``None``/empty means usage was never captured — kept
    distinct from a run that genuinely used 0 tokens (never happens, but the
    same "absent is not zero" rule applies as everywhere else in this file)."""
    if not usage:
        return None
    return sum(v for v in usage.values() if isinstance(v, (int, float)))


# --- metric assembly ---------------------------------------------------------


def _by_task(pairs: list[dict]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for pair in pairs:
        grouped.setdefault(pair["task_id"], []).append(pair)
    return grouped


def _clustered_metric(pairs: list[dict], observe, *, metric_id: str, label: str,
                      higher_is_better: bool, **extra) -> dict:
    """One task-clustered rate row, from a per-run ``observe`` function.

    ``observe(run)`` returns ``(numerator, denominator)`` for one run — passed/1
    for the outcome, successes/eligible-opportunities for a behaviour,
    concerns/detector-ran for a failure mode. Everything above that is identical
    for every row, and is written once here so the clustering rule (§12.3: one
    observation per *task*, so repeated runs cannot outvote a single-run task)
    has a single definition rather than one per metric family.

    A pair contributes to the row only when *both* sides observed it, which is
    what keeps a one-sided window out of a paired difference and gives the row
    the pair ids that actually fed it.
    """
    diffs, contributing = [], []
    totals = {"baseline": [0, 0], "candidate": [0, 0]}
    task_count = 0
    for task_pairs in _by_task(pairs).values():
        rates: dict[str, list[float]] = {"baseline": [], "candidate": []}
        task_contributing = []
        for pair in task_pairs:
            seen = 0
            for side, key in (("baseline", "_baseline"), ("candidate", "_candidate")):
                numerator, denominator = observe(pair[key])
                totals[side][0] += numerator
                totals[side][1] += denominator
                if denominator:
                    seen += 1
                    rates[side].append(numerator / denominator)
            if seen == 2:
                task_contributing.append(pair["pair_id"])
        if rates["baseline"] and rates["candidate"]:
            task_count += 1
            contributing.extend(task_contributing)
            diffs.append(_mean(rates["candidate"]) - _mean(rates["baseline"]))
    stats = _paired_difference(diffs)
    row = {
        "metric_id": metric_id,
        "label": label,
        "higher_is_better": higher_is_better,
        "baseline": {"numerator": totals["baseline"][0], "denominator": totals["baseline"][1]},
        "candidate": {"numerator": totals["candidate"][0], "denominator": totals["candidate"][1]},
        "task_count": task_count,
        "run_pairs": len(contributing),
        # Milestone 5: every aggregate traces to the run pairs behind *it* — not
        # to the whole slice, which would over-claim what fed a gated row.
        "contributing_pair_ids": contributing,
        "statistics": stats,
        "change": _label_change(stats, higher_is_better,
                                totals["baseline"][1], totals["candidate"][1]),
    }
    row.update(extra)
    return row


def _pass_rate_metric(pairs: list[dict]) -> dict:
    return _clustered_metric(pairs, lambda run: (int(_passed(run)), 1),
                             metric_id="pass_rate", label="Pass rate",
                             higher_is_better=True)


def _behaviour_metrics(pairs: list[dict]) -> list[dict]:
    """Opportunity-normalized behaviour rows (§12.1) with explicit denominators."""
    rows = []
    for ability in sorted({a for p in pairs for side in ("_baseline", "_candidate")
                           for a in _behaviour_observations(p[side])}):
        def observe(run, ability=ability):
            successes, evaluated, _ = _behaviour_observations(run).get(ability, (0, 0, 0))
            return successes, evaluated

        row = _clustered_metric(pairs, observe, metric_id="ability:" + ability,
                                label=ability, higher_is_better=True,
                                kind="opportunity_normalized",
                                denominator_unit="runs_with_eligible_opportunity")
        # Opportunities that occurred but were not evaluated ride alongside the
        # denominator rather than inside it (see _behaviour_observations).
        for side, key in (("baseline", "_baseline"), ("candidate", "_candidate")):
            row[side]["eligible_not_evaluated"] = sum(
                _behaviour_observations(p[key]).get(ability, (0, 0, 0))[2] for p in pairs)
        rows.append(row)
    return rows


def _failure_mode_metrics(pairs: list[dict]) -> list[dict]:
    """Failure-mode rows, with the one-sided case named honestly (§4.16.2).

    ``new_failure_mode`` is claimable only when the baseline side could have
    surfaced the mode at all — the detector ran there. Otherwise the row says
    ``observed_only_in_candidate``, which is what the evidence supports.
    """
    rows = []
    for detector in sorted({d for p in pairs for side in ("_baseline", "_candidate")
                            for d in _failure_observations(p[side])}):
        def observe(run, detector=detector):
            # Share of runs exhibiting the mode, not mean occurrences per run: a
            # detector firing twice in one run is still one run that showed it.
            if detector not in _evaluated_detectors(run):
                return 0, 0
            return (1 if _failure_observations(run).get(detector, 0) else 0), 1

        row = _clustered_metric(pairs, observe, metric_id="failure_mode:" + detector,
                                label=detector.replace("_", " "), higher_is_better=False,
                                kind="failure_mode")
        base, cand = row["baseline"], row["candidate"]
        if row["change"] == "observed_only_on_one_side":
            row["change"] = ("observed_only_in_candidate" if base["denominator"] == 0
                             else "observed_only_in_baseline")
        elif base["denominator"] and base["numerator"] == 0 and cand["numerator"]:
            row["change"] = "new_failure_mode"
        rows.append(row)
    return rows


def _cost_metrics(pairs: list[dict]) -> list[dict]:
    """Token / cost / latency change — reported only where the capture has it."""
    rows = []
    for metric_id, label, getter in (
        ("duration_s", "Run duration (s)", _duration),
        ("cost", "Cost", lambda r: (r.get("run") or {}).get("cost")),
        ("tokens", "Tokens", lambda r: _token_total((r.get("run") or {}).get("tokens"))),
    ):
        base = [getter(p["_baseline"]) for p in pairs]
        cand = [getter(p["_candidate"]) for p in pairs]
        paired = [(b, c) for b, c in zip(base, cand) if b is not None and c is not None]
        if not paired:
            rows.append({"metric_id": metric_id, "label": label, "kind": "resource",
                         "captured": False, "change": "not_captured"})
            continue
        stats = _paired_difference([c - b for b, c in paired])
        rows.append({
            "metric_id": metric_id, "label": label, "kind": "resource", "captured": True,
            "baseline": {"mean": _mean([b for b, _ in paired]), "n": len(paired)},
            "candidate": {"mean": _mean([c for _, c in paired]), "n": len(paired)},
            "contributing_pair_ids": [p["pair_id"] for p, (b, c) in
                                      zip(pairs, zip(base, cand)) if b is not None and c is not None],
            "statistics": stats,
            "change": _label_change(stats, False, len(paired), len(paired)),
        })
    return rows


def _review_coverage(runs: list[dict]) -> dict:
    modes: dict[str, int] = {}
    for run in runs:
        mode = run.get("review_mode") or "deterministic_only"
        modes[mode] = modes.get(mode, 0) + 1
    return {"runs": len(runs), "by_review_mode": modes,
            "model_enriched": modes.get("model_enriched", 0)}


# --- narration ---------------------------------------------------------------


_ONE_SIDED_WORDS = {
    "new_failure_mode": "newly observed",
    "observed_only_in_candidate": "observed only in the candidate",
    "observed_only_in_baseline": "observed only in the baseline",
}
_DIRECTIONAL = ("improved", "regressed")


def _phrase(row: dict) -> Optional[str]:
    """How one moved row reads in the synthesis.

    Direction comes from the sign of the measured difference, never from the
    improved/regressed label: on a failure-mode row lower *is* improved, so
    reading the label as "higher" would invert what the numbers say.
    """
    change = row.get("change")
    if change in _ONE_SIDED_WORDS:
        return f"{row['label'].lower()} {_ONE_SIDED_WORDS[change]}"
    if change not in _DIRECTIONAL:
        return None
    difference = (row.get("statistics") or {}).get("difference") or 0
    return f"{row['label'].lower()} {'higher' if difference > 0 else 'lower'}"


def _interpretation(result: dict) -> dict:
    """A templated, correlational synthesis — labelled interpretation (§4.16.2).

    Deterministic and mechanical: it restates the rows that carry a direction and
    then states the limit of what the design can support. It never claims cause,
    and it never speaks for rows labelled within-uncertainty or insufficient.
    """
    phrases = [p for p in (_phrase(row) for row in
                           [result["pass_rate"]] + result["behaviours"] + result["failure_modes"])
               if p]
    if phrases:
        summary = ("On this matched slice, the candidate is associated with "
                   + "; ".join(phrases) + ".")
    else:
        summary = ("On this matched slice, no measured behaviour moved beyond the "
                   "uncertainty this design can resolve.")
    limit = ("This comparison does not isolate causality"
             if result["label"] == "matched"
             else "More than the declared axis changed, so no single component is isolated")
    return {
        "kind": "interpretation",
        "summary": summary,
        "limit": (f"{limit}; the association holds for the matched slice only "
                  f"({result['report']['exact_matched_tasks']} tasks, "
                  f"{result['report']['matched_run_pairs']} run pairs)."),
    }


# --- public API --------------------------------------------------------------


def comparison_id(baseline: dict, candidate: dict, axis: str, keys: Iterable[str]) -> str:
    """A stable id for one comparison definition, so a saved link is shareable."""
    payload = json.dumps({"baseline": baseline, "candidate": candidate,
                          "axis": axis, "keys": list(keys)}, sort_keys=True)
    return "comparison_" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def compare_versions(store: Store, baseline_selector: dict, candidate_selector: dict,
                     declared_change_axis: str,
                     match_keys: Iterable[str] = DEFAULT_MATCH_KEYS,
                     definition_revision: int = 1,
                     result_revision: int = 1) -> dict:
    """Build the matched comparison of two configurations (§4.16 / §6.12).

    Returns the frozen definition (sides, declared axis, match keys, included
    pairs, exclusions with reasons, match status) together with the computed
    result (pass rate, opportunity-normalized behaviours, failure modes,
    resources, review coverage, interpretation). Nothing is stored as a rendered
    percentage: every row keeps its numerator, denominator, and counts.
    """
    if declared_change_axis not in CHANGE_AXES:
        raise ComparisonError(
            f"declared_change_axis must be one of {list(CHANGE_AXES)}, got {declared_change_axis!r}")
    keys = list(match_keys)
    unknown = set(keys) - set(DEFAULT_MATCH_KEYS)
    if unknown:
        raise ComparisonError(f"unknown match keys {sorted(unknown)}")

    runs = _runs(store)
    baseline = _select(runs, baseline_selector)
    candidate = _select(runs, candidate_selector)
    if not baseline or not candidate:
        raise ComparisonError("both sides must select at least one run")

    unresolved = sorted(set(_unresolved_keys(baseline, keys)) | set(_unresolved_keys(candidate, keys)))
    usable_keys = [k for k in keys if k not in unresolved]
    pairs, exclusions, strata = _pair_runs(baseline, candidate, usable_keys)

    observed_axes = _observed_axes(baseline, candidate)
    blocked = _blocked_reasons(unresolved, pairs)
    match_status = "invalid" if blocked else "valid"
    # `Matched` is the narrow claim: the slice is exact *and* only the declared
    # axis moved. Declaring the whole configuration is honest but never earns it.
    if blocked:
        label = "unmatched"
    elif declared_change_axis == COMPLETE_CONFIGURATION or set(observed_axes) - {declared_change_axis}:
        label = "configuration_comparison"
    else:
        label = "matched"

    matched_tasks = sorted({p["task_id"] for p in pairs})
    result = {
        "comparison_id": comparison_id(baseline_selector, candidate_selector,
                                       declared_change_axis, keys),
        "definition_revision": definition_revision,
        "result_revision": result_revision,
        "baseline": _side_summary(baseline_selector, baseline),
        "candidate": _side_summary(candidate_selector, candidate),
        "declared_change_axis": declared_change_axis,
        "observed_change_axes": observed_axes,
        "match_keys": keys,
        "match_keys_used": usable_keys,
        "match_key_version": MATCH_KEY_VERSION,
        "unresolved_keys": unresolved,
        "match_status": match_status,
        "label": label,
        "blocked_reasons": blocked,
        "caveats": _caveats(unresolved, baseline, candidate, matched_tasks),
        "statistics_version": STATISTICS_VERSION,
        "report": {
            "exact_matched_tasks": len(matched_tasks),
            "matched_run_pairs": len(pairs),
            "repeated_run_strata": strata,
            "excluded_baseline_runs": sum(1 for e in exclusions if e["side"] == "baseline"),
            "excluded_candidate_runs": sum(1 for e in exclusions if e["side"] == "candidate"),
            "exclusions_by_reason": _group_exclusions(exclusions),
        },
        "included_pair_ids": [p["pair_id"] for p in pairs],
        "pairs": [{k: v for k, v in p.items() if not k.startswith("_")} for p in pairs],
        "exclusions": exclusions,
        "review_coverage": {
            "baseline": _review_coverage([p["_baseline"] for p in pairs]),
            "candidate": _review_coverage([p["_candidate"] for p in pairs]),
        },
    }
    result["pass_rate"] = _pass_rate_metric(pairs)
    result["behaviours"] = _behaviour_metrics(pairs)
    result["failure_modes"] = _failure_mode_metrics(pairs)
    result["resources"] = _cost_metrics(pairs)
    result["interpretation"] = _interpretation(result)
    return result


# --- saved definitions (§4.16.1 step 6) --------------------------------------


def save_comparison(store: Store, baseline_selector: dict, candidate_selector: dict,
                    declared_change_axis: str, match_keys: Iterable[str] = DEFAULT_MATCH_KEYS,
                    name: Optional[str] = None) -> dict:
    """Freeze a comparison definition so its URL is shareable and reproducible.

    Only the *definition* is stored — sides, axis, match keys. Results are
    recomputed on read, so a saved comparison over a store that has since gained
    runs reports the new slice rather than a stale number, and the exclusions
    explain the difference. Saving again advances both ``definition_revision``
    and ``result_revision`` (§4.16.1: editing a definition creates a new result
    revision); reading never writes, so a ``GET`` stays side-effect free.
    """
    keys = list(match_keys)
    cid = comparison_id(baseline_selector, candidate_selector, declared_change_axis, keys)
    previous = store.read_comparison(cid) if store.has_comparison(cid) else None
    definition = {
        "comparison_id": cid,
        "definition_revision": (previous or {}).get("definition_revision", 0) + 1,
        "result_revision": (previous or {}).get("result_revision", 0) + 1,
        "name": name or (previous or {}).get("name"),
        "baseline": baseline_selector,
        "candidate": candidate_selector,
        "declared_change_axis": declared_change_axis,
        "match_keys": keys,
        "match_key_version": MATCH_KEY_VERSION,
        "statistics_version": STATISTICS_VERSION,
    }
    store.write_comparison(cid, definition)
    return definition


def load_comparison(store: Store, cid: str) -> dict:
    """Recompute a saved comparison's result from its frozen definition."""
    if not store.has_comparison(cid):
        raise ComparisonError(f"no saved comparison {cid!r}")
    d = store.read_comparison(cid)
    result = compare_versions(store, d["baseline"], d["candidate"], d["declared_change_axis"],
                              match_keys=d.get("match_keys", DEFAULT_MATCH_KEYS),
                              definition_revision=d.get("definition_revision", 1),
                              result_revision=d.get("result_revision", 1))
    result["name"] = d.get("name")
    result["saved"] = True
    return result


def list_saved_comparisons(store: Store) -> list[dict]:
    return [store.read_comparison(cid) for cid in store.list_comparisons()]


def _side_summary(selector: dict, runs: list[dict]) -> dict:
    rs = [(r.get("run") or {}) for r in runs]

    def unique(field):
        values = {r.get(field) for r in rs} - {None}
        return values.pop() if len(values) == 1 else None

    return {
        "selector": selector,
        "label": _config_label(rs[0]) if rs else "—",
        "run_count": len(runs),
        "task_count": len({r.get("task_id") for r in rs}),
        # Only reported when the whole side agrees; a side spanning two sweeps
        # must not be labelled with one of them.
        "sweep_id": unique("sweep_id"),
        "configuration_id": unique("configuration_id"),
    }


def _observed_axes(baseline: list[dict], candidate: list[dict]) -> list[str]:
    """Every configuration axis that actually differs between the two sides."""
    axes = []
    for field, axis in AXIS_OF_FIELD.items():
        left = {(r.get("run") or {}).get(field) for r in baseline}
        right = {(r.get("run") or {}).get(field) for r in candidate}
        if left != right and not (left <= {None} and right <= {None}):
            axes.append(axis)
    return sorted(set(axes))


def _blocked_reasons(unresolved: list[str], pairs: list[dict]) -> list[str]:
    reasons = []
    for key in unresolved:
        if key in VERSION_KEYS:
            reasons.append(f"unresolved_version_field:{key}")
    if not pairs:
        reasons.append("no_exactly_matched_task")
    return reasons


def _caveats(unresolved: list[str], baseline: list[dict], candidate: list[dict],
             matched_tasks: list[str]) -> list[str]:
    out = []
    for key in unresolved:
        if key not in VERSION_KEYS:
            out.append(f"{key} is not declared on both sides; runs were matched without it.")
    for side, runs in (("baseline", baseline), ("candidate", candidate)):
        total = len({(r.get("run") or {}).get("task_id") for r in runs})
        if total and len(matched_tasks) < total:
            out.append(f"The matched slice covers {len(matched_tasks)} of {total} {side} tasks; "
                       f"results describe the matched slice only.")
    return out


def _group_exclusions(exclusions: list[dict]) -> list[dict]:
    grouped: dict[tuple, list[str]] = {}
    for e in exclusions:
        grouped.setdefault((e["side"], e["reason"]), []).append(e["run_id"])
    return [{"side": side, "reason": reason, "count": len(run_ids), "run_ids": sorted(run_ids)}
            for (side, reason), run_ids in sorted(grouped.items())]
