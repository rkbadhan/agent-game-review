"""Reviewer-vs-reviewer diff of two reviews of the same capture.

A side-by-side view of two reviews of the *same run* — the deterministic
baseline and a Stage F model reviewer's pass, or two model passes. It aligns the
two snapshots into matched / added / removed / redundant pairs with field-level
diffs, so the UI renders deltas without re-deriving anything.

**This is not the §4.16 comparison surface.** §4.16 / §6.12 / §12.3 compare two
*versions* (harness/model A vs B) across a matched task slice; that lives in
:mod:`agr.versions`. Both answer "what changed", but this one holds the run fixed
and varies the reviewer, which makes it a reviewer-quality tool in the §15.3
family rather than a version measurement.

It is the generalization of the §15.3 prediction-vs-gold scoring in
:mod:`agr.reviewer_eval` from *prediction vs adjudicated gold* to *review vs
review*. Both sides are now reviewers; neither is assumed adjudicated. The
alignment algorithm is reused verbatim: anchor-overlap in source-step coordinates
(:func:`agr.gold.anchor_overlap`) with the greedy unmatched-first ordering
(:func:`agr.reviewer_eval._best_gold`), so a moment that overlaps both a taken
and a free moment takes the free one before any is flagged redundant.

Pure stdlib, no model calls, deterministic and idempotent: the same two inputs
always produce the same pairing and the same diffs (no timestamps). The
``compare_id`` folds ``(left_key, right_key)`` so a shared link is stable.
"""

from __future__ import annotations

import hashlib
from typing import Optional

from .gold import anchor_overlap
from .reviewer_eval import PredictedMoment, moments_from_review
from .schema import ATTRIBUTION_LEVELS

_ATTR_RANK = {level: i for i, level in enumerate(ATTRIBUTION_LEVELS)}

# Fields diffed on a matched pair. Each is reported as {left, right, changed}.
_DIFF_FIELDS = (
    "attribution_ceiling",
    "polarity",
    "kind",
    "summary",
    "rendered_statement",
    "better_action",
    "enrichment_source",
)

# Set-valued fields diffed as added/removed tokens rather than whole-value swap.
_SET_DIFF_FIELDS = ("behaviour_tags", "root_cause_candidates")


def _best_overlap(pred: PredictedMoment, candidates: list[PredictedMoment],
                  matched_ids: set[str], threshold: float = 0.0) -> Optional[PredictedMoment]:
    """The candidate moment a prediction best overlaps, or None.

    Greedy unmatched-first: an already-matched candidate is only chosen when no
    unmatched overlapping candidate remains — so a prediction overlapping both a
    taken and a free moment takes the free one, mirroring
    :func:`agr.reviewer_eval._best_gold` exactly.
    """
    scored = []
    for cand in candidates:
        if not anchor_overlap(pred.anchor_set, cand.anchor_set, threshold):
            continue
        inter = len(pred.anchor_set & cand.anchor_set)
        union = len(pred.anchor_set | cand.anchor_set) or 1
        scored.append(((cand.moment_id in matched_ids), -(inter / union), cand))
    if not scored:
        return None
    scored.sort(key=lambda t: (t[0], t[1]))
    return scored[0][2]


def _field_diff(left: dict, right: dict) -> dict:
    """Per-field {left, right, changed} for the scalar diff fields."""
    diffs = {}
    for field in _DIFF_FIELDS:
        lv, rv = left.get(field), right.get(field)
        diffs[field] = {"left": lv, "right": rv, "changed": lv != rv}
    return diffs


def _set_field_diff(left: dict, right: dict) -> dict:
    """Per-field {left, right, added, removed} for the set-valued diff fields."""
    diffs = {}
    for field in _SET_DIFF_FIELDS:
        lv = list(left.get(field) or [])
        rv = list(right.get(field) or [])
        ls, rs = set(_hashable(x) for x in lv), set(_hashable(x) for x in rv)
        diffs[field] = {
            "left": lv,
            "right": rv,
            "added": [x for x in rv if _hashable(x) not in ls],
            "removed": [x for x in lv if _hashable(x) not in rs],
            "changed": ls != rs,
        }
    return diffs


def _hashable(x):
    """Make a value hashable for set membership (dicts → a sorted frozenset of items)."""
    if isinstance(x, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in x.items()))
    if isinstance(x, list):
        return tuple(_hashable(i) for i in x)
    return x


def _attribution_direction(left_ceiling: Optional[str], right_ceiling: Optional[str]) -> str:
    """``"up"``, ``"down"``, ``"same"`` or ``"unknown"`` — did the right review claim stronger causality?"""
    lr = _ATTR_RANK.get(left_ceiling, -1)
    rr = _ATTR_RANK.get(right_ceiling, -1)
    if lr < 0 and rr < 0:
        return "unknown"
    if rr > lr:
        return "up"
    if rr < lr:
        return "down"
    return "same"


def _align(left_moments: list[PredictedMoment], right_moments: list[PredictedMoment],
           threshold: float = 0.0) -> list[dict]:
    """Pair left and right moments by anchor overlap.

    Returns a list of pair records:
    - ``{status: "matched", left, right}`` — both sides have an overlapping moment.
    - ``{status: "added", left: None, right}`` — right-only (e.g. a model card the
      baseline missed).
    - ``{status: "removed", left, right: None}`` — left-only (baseline dropped by
      the model).
    - ``{status: "redundant", left, right}`` — a right moment overlaps a left moment
      already paired to a different right moment; it adds nothing new. Mirrors the
      ``_best_gold`` greedy unmatched-first ordering from §15.3.
    """
    pairs: list[dict] = []
    matched_right: set[str] = set()
    matched_left: set[str] = set()

    # First pass: greedy-match each left moment to its best *unmatched* right
    # overlap. A left moment whose only overlaps are already-taken right moments
    # is removed (the model dropped it); it does not steal another left's match.
    for lm in left_moments:
        rm = _best_overlap(lm, right_moments, matched_right, threshold)
        if rm is None or rm.moment_id in matched_right:
            pairs.append({"status": "removed", "left": lm, "right": None})
            continue
        matched_right.add(rm.moment_id)
        matched_left.add(lm.moment_id)
        pairs.append({"status": "matched", "left": lm, "right": rm})

    # Second pass: any right moment not claimed by a match is either redundant
    # (it overlaps an already-matched left moment, adding nothing new) or added
    # (no left overlap at all — a genuinely new card).
    for rm in right_moments:
        if rm.moment_id in matched_right:
            continue
        overlaps_left = any(anchor_overlap(rm.anchor_set, lm.anchor_set, threshold)
                            for lm in left_moments if lm.moment_id in matched_left)
        if overlaps_left:
            # Attach it to the left moment it best overlaps for display.
            host = _best_overlap(rm, left_moments, set(), threshold)
            pairs.append({"status": "redundant",
                          "left": host, "right": rm})
        else:
            pairs.append({"status": "added", "left": None, "right": rm})

    return pairs


def _compare_id(left_key: str, right_key: str) -> str:
    """Stable id for a comparison so a shared link is reproducible."""
    h = hashlib.sha1(f"{left_key}\n{right_key}".encode("utf-8")).hexdigest()
    return "cmp_" + h[:12]


def compare_reviews(left_review: dict, right_review: dict, forensic: dict,
                    left_key: str, right_key: str, threshold: float = 0.0) -> dict:
    """Align two review snapshots of one run into matched pairs with field diffs.

    ``left_review`` / ``right_review`` are :func:`agr.read.get_review` payloads
    (each already scoped to its ``reviewer_key``). ``forensic`` is the shared
    :func:`agr.read.get_forensic` payload — the same source steps route both
    sides' anchors, so only one forensic view is needed. The ``threshold`` is the
    Jaccard floor for anchor overlap (default ``0.0`` = any shared step).

    The returned payload is what the reviewer-diff view renders: ``pairs``, each
    carrying the matched/added/removed/redundant status, the two moment views,
    and (for matched pairs) field-level diffs including an attribution
    ``direction`` (up/down/same) so the UI can flag a stronger or weaker causal
    claim without re-deriving it.
    """
    left_preds = moments_from_review(left_review, forensic)
    right_preds = moments_from_review(right_review, forensic)

    # Index the raw moment dicts by PredictedMoment.moment_id for diff lookups.
    left_by_id = {m.get("moment_id"): m for m in left_review.get("moments", [])}
    right_by_id = {m.get("moment_id"): m for m in right_review.get("moments", [])}

    raw_pairs = _align(left_preds, right_preds, threshold)

    pairs_out: list[dict] = []
    counts = {"matched": 0, "added": 0, "removed": 0, "redundant": 0}
    for p in raw_pairs:
        status = p["status"]
        counts[status] += 1
        lp, rp = p["left"], p["right"]
        entry = {"status": status}

        if lp is not None:
            entry["left"] = left_by_id.get(lp.moment_id, {"moment_id": lp.moment_id})
        else:
            entry["left"] = None

        if rp is not None:
            entry["right"] = right_by_id.get(rp.moment_id, {"moment_id": rp.moment_id})
        else:
            entry["right"] = None

        if status == "matched" and entry["left"] and entry["right"]:
            lm, rm = entry["left"], entry["right"]
            entry["diffs"] = _field_diff(lm, rm)
            entry["set_diffs"] = _set_field_diff(lm, rm)
            entry["attribution_direction"] = _attribution_direction(
                lm.get("attribution_ceiling"), rm.get("attribution_ceiling"))

        pairs_out.append(entry)

    return {
        "compare_id": _compare_id(left_key, right_key),
        "left_key": left_key,
        "right_key": right_key,
        "left_mode": left_review.get("review_mode", "deterministic_only"),
        "right_mode": right_review.get("review_mode", "deterministic_only"),
        "threshold": threshold,
        "counts": counts,
        "pairs": pairs_out,
    }
