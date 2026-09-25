"""GR-3: the idea-category registry — a deterministic view over taxonomy tags.

The registry is the coverage spine: it maps each of the four categories from the
idea to the behaviour tags that realise it and to the detection routes that can
produce a finding. The model never names a category, so these invariants are what
keep the registry from drifting from the vocabulary it claims to cover.
"""

from __future__ import annotations

from agr import taxonomy


def test_every_registry_tag_is_a_real_tag_with_guidance():
    for category_id, spec in taxonomy.IDEA_CATEGORIES.items():
        for tag in spec["tags"]:
            assert tag in taxonomy.BEHAVIOUR_TAGS, (category_id, tag)
            assert tag in taxonomy.BEHAVIOUR_TAG_GUIDANCE, (category_id, tag)


def test_every_category_has_at_least_one_detection_route():
    for category_id, spec in taxonomy.IDEA_CATEGORIES.items():
        assert spec["detection"], category_id


def test_a_tag_belongs_to_at_most_one_category():
    seen: dict[str, str] = {}
    for category_id, spec in taxonomy.IDEA_CATEGORIES.items():
        for tag in spec["tags"]:
            assert tag not in seen, f"{tag} in {seen.get(tag)} and {category_id}"
            seen[tag] = category_id


def test_category_for_tag_and_tags():
    assert taxonomy.category_for_tag("poor_query") == "bad_query_in_tool_call"
    assert taxonomy.category_for_tag("good_recovery") == "good_recovery_from_failed_plan"
    # A real tag outside the idea's four categories is deliberately uncategorised.
    assert taxonomy.category_for_tag("destructive_action") is None
    assert taxonomy.category_for_tags(["good_recovery", "effective_replan"]) == \
        ["good_recovery_from_failed_plan"]
    assert taxonomy.category_for_tags(["destructive_action"]) == []


def test_category_coverage_reports_each_route():
    coverage = {c["category_id"]: c for c in taxonomy.category_coverage()}
    assert set(coverage) == set(taxonomy.IDEA_CATEGORIES)
    planning = coverage["mistake_in_planning"]
    assert planning["model_guided"] is True and planning["has_detector"] is False
    query = coverage["bad_query_in_tool_call"]
    assert query["has_argument_shapes"] is True
    recovery = coverage["good_recovery_from_failed_plan"]
    assert recovery["has_detector"] is True and recovery["model_guided"] is True
    # The submission detector surfaces the situation but cannot mechanically
    # assign the tag, so it is model-only with a surfaced_by note.
    victory = coverage["claiming_victory_before_verifying"]
    assert victory["has_detector"] is False and victory["model_guided"] is True
    assert any(d["name"] == "unresolved_requirement_at_submission"
               for d in victory["surfaced_by"])


def test_detector_tag_mapping_is_inside_the_vocabulary_and_a_category():
    for detector, tags in taxonomy.DETECTOR_BEHAVIOUR_TAGS.items():
        for tag in tags:
            assert tag in taxonomy.BEHAVIOUR_TAGS, (detector, tag)
            assert taxonomy.category_for_tag(tag) is not None, (detector, tag)


def test_submission_detector_cannot_mechanically_claim_victory():
    """PR #92 review: the detector proves a failing check at submission, not a
    victory claim — so it must not map to a tag and must have a neutral label."""
    assert "unresolved_requirement_at_submission" not in taxonomy.DETECTOR_BEHAVIOUR_TAGS
    label = taxonomy.DETECTOR_NEUTRAL_LABELS["unresolved_requirement_at_submission"]
    assert "victory" not in label.lower()
    assert "premature_submission" not in label
