"""Direct tests for the §6.12 matched-comparison algorithm (spec §4.16).

The API tests in ``test_api.py`` exercise the compare endpoint end-to-end but
only ever fire the ``matched`` branch (the scripted reviewer mirrors the
deterministic anchors exactly). This module pins the alignment logic directly —
``matched`` / ``added`` / ``removed`` / ``redundant`` — plus the field-level
diffs and attribution direction, so the subtlest code in the package is not
relying on indirect coverage.
"""

from agr.compare import compare_reviews, _align, _field_diff, _set_field_diff, _attribution_direction
from agr.reviewer_eval import PredictedMoment

# A shared forensic view mapping event ids onto source step ids. ``moments_from_review``
# uses this to translate event-id anchors into the source-step coordinates the
# matcher works in.
FORENSIC = {"steps": [
    {"step_id": "s1", "event_ids": ["e1"]},
    {"step_id": "s2", "event_ids": ["e2"]},
    {"step_id": "s3", "event_ids": ["e3"]},
    {"step_id": "s9", "event_ids": ["e9"]},
]}


def _moment(mid, anchor, **kw):
    base = {"moment_id": mid, "anchor_event_ids": [anchor], "polarity": "negative",
            "affected_checks": [], "attribution_ceiling": "hypothesized",
            "summary": mid, "behaviour_tags": []}
    base.update(kw)
    return base


def _review(moments, mode="deterministic_only"):
    return {"review_mode": mode, "moments": moments}


# --- _align: the four statuses ----------------------------------------------


def test_align_matched_when_anchors_overlap():
    left = [PredictedMoment("L1", ["s1"])]
    right = [PredictedMoment("R1", ["s1"])]
    pairs = _align(left, right)
    assert len(pairs) == 1
    assert pairs[0]["status"] == "matched"
    assert pairs[0]["left"].moment_id == "L1"
    assert pairs[0]["right"].moment_id == "R1"


def test_align_added_when_right_only_has_no_overlap():
    left = [PredictedMoment("L1", ["s1"])]
    right = [PredictedMoment("R1", ["s1"]), PredictedMoment("R2", ["s9"])]
    pairs = _align(left, right)
    by_status = {p["status"] for p in pairs}
    assert by_status == {"matched", "added"}
    added = [p for p in pairs if p["status"] == "added"][0]
    assert added["left"] is None
    assert added["right"].moment_id == "R2"


def test_align_removed_when_left_only_has_no_overlap():
    left = [PredictedMoment("L1", ["s1"]), PredictedMoment("L2", ["s2"])]
    right = [PredictedMoment("R1", ["s1"])]
    pairs = _align(left, right)
    by_status = {p["status"] for p in pairs}
    assert by_status == {"matched", "removed"}
    removed = [p for p in pairs if p["status"] == "removed"][0]
    assert removed["left"].moment_id == "L2"
    assert removed["right"] is None


def test_align_redundant_when_right_overlaps_already_matched_left():
    # L1 overlaps both R1 and R3. Greedy unmatched-first takes R1 as the match;
    # R3 then overlaps an already-matched left moment (L1) -> redundant, not added.
    left = [PredictedMoment("L1", ["s1"]), PredictedMoment("L2", ["s2"])]
    right = [PredictedMoment("R1", ["s1"]), PredictedMoment("R3", ["s1"]),
             PredictedMoment("R2", ["s9"])]
    pairs = _align(left, right)
    by_status = {p["status"] for p in pairs}
    assert by_status == {"matched", "redundant", "removed", "added"}
    redundant = [p for p in pairs if p["status"] == "redundant"][0]
    assert redundant["left"].moment_id == "L1"
    assert redundant["right"].moment_id == "R3"


def test_align_counts_sum_to_inputs():
    left = [PredictedMoment("L1", ["s1"]), PredictedMoment("L2", ["s2"]),
            PredictedMoment("L3", ["s3"])]
    right = [PredictedMoment("R1", ["s1"]), PredictedMoment("R2", ["s9"])]
    pairs = _align(left, right)
    n_left = sum(1 for p in pairs if p["left"] is not None)
    n_right = sum(1 for p in pairs if p["right"] is not None)
    assert n_left == len(left)
    assert n_right == len(right)


# --- field diffs ------------------------------------------------------------


def test_field_diff_flags_changed_scalars():
    d = _field_diff({"attribution_ceiling": "hypothesized", "summary": "a"},
                    {"attribution_ceiling": "direct", "summary": "a"})
    assert d["attribution_ceiling"]["changed"] is True
    assert d["summary"]["changed"] is False


def test_set_field_diff_reports_added_and_removed():
    d = _set_field_diff({"behaviour_tags": ["a", "b"]}, {"behaviour_tags": ["b", "c"]})
    assert d["behaviour_tags"]["added"] == ["c"]
    assert d["behaviour_tags"]["removed"] == ["a"]
    assert d["behaviour_tags"]["changed"] is True


def test_attribution_direction_up_down_same_unknown():
    assert _attribution_direction("hypothesized", "direct") == "up"
    assert _attribution_direction("direct", "hypothesized") == "down"
    assert _attribution_direction("direct", "direct") == "same"
    assert _attribution_direction(None, None) == "unknown"


# --- compare_reviews: end-to-end payload ------------------------------------


def test_compare_reviews_matched_pair_carries_diffs_and_direction():
    left = _review([_moment("L1", "e1", attribution_ceiling="hypothesized")])
    right = _review([_moment("R1", "e1", attribution_ceiling="direct",
                             behaviour_tags=["failed_to_replan"],
                             better_action="retry", summary="R1 enriched")],
                    mode="model_enriched")
    cmp = compare_reviews(left, right, FORENSIC, "deterministic", "model:test")
    assert cmp["counts"] == {"matched": 1, "added": 0, "removed": 0, "redundant": 0}
    p = cmp["pairs"][0]
    assert p["status"] == "matched"
    assert p["attribution_direction"] == "up"
    assert p["diffs"]["attribution_ceiling"]["changed"] is True
    assert p["diffs"]["better_action"]["changed"] is True
    assert p["set_diffs"]["behaviour_tags"]["added"] == ["failed_to_replan"]


def test_compare_reviews_added_removed_redundant_all_fire():
    left = _review([_moment("L1", "e1"), _moment("L2", "e2"), _moment("L3", "e3")])
    right = _review([_moment("R1", "e1"), _moment("R3", "e1"),  # R3 redundant to L1
                     _moment("R2", "e9")], mode="model_enriched")
    cmp = compare_reviews(left, right, FORENSIC, "deterministic", "model:test")
    assert cmp["counts"]["matched"] == 1
    assert cmp["counts"]["redundant"] == 1
    assert cmp["counts"]["added"] == 1   # R2, no left overlap
    assert cmp["counts"]["removed"] == 2  # L2, L3 have no right overlap


def test_compare_reviews_is_deterministic_and_idempotent():
    left = _review([_moment("L1", "e1"), _moment("L2", "e2")])
    right = _review([_moment("R1", "e1"), _moment("R2", "e9")], mode="model_enriched")
    a = compare_reviews(left, right, FORENSIC, "deterministic", "model:test")
    b = compare_reviews(left, right, FORENSIC, "deterministic", "model:test")
    assert a == b  # same inputs -> identical pairing + diffs
    assert a["compare_id"] == b["compare_id"]


def test_compare_reviews_identical_reviews_produce_no_diffs():
    m = _moment("M1", "e1", summary="same")
    cmp = compare_reviews(_review([m]), _review([m]), FORENSIC, "deterministic", "deterministic")
    assert cmp["counts"]["matched"] == 1
    p = cmp["pairs"][0]
    assert all(not v["changed"] for v in p["diffs"].values())
    assert p["attribution_direction"] == "same"
