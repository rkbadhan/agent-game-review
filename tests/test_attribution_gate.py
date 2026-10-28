"""Table-driven checks for the Stage H wording gate (spec §8.9)."""
import pytest

from agr.reviewer import _render_requirement_status_group, attribution_gate, implied_attribution, render

CASES = [
    ("The edit caused the failure.", "direct"),
    ("The edit led to the failure.", "direct"),
    ("The edit was responsible for the failure.", "direct"),
    ("The failure happened due to the edit.", "direct"),
    ("Because of the edit, C3 failed.", "direct"),
    ("The edit broke the build.", "direct"),
    ("The edit triggered the failure.", "direct"),
    ("The edit made C3 fail.", "direct"),
    ("The failure resulted from the edit.", "direct"),
    ("Had the agent re-run tests, C3 would pass.", "counterfactually_supported"),
    ("If the agent had re-run tests, C3 passes.", "counterfactually_supported"),
    ("C3 would not have failed.", "counterfactually_supported"),
    ("Produced by chance, unrelated.", "hypothesized"),
    ("This was caused by accident when the agent deleted config.", "direct"),
    ("The failure was not caused by chance; the edit deleted the file.", "direct"),
    ("The edit makes C3 fail.", "direct"),
    ("The edit led C3 to fail.", "direct"),
    ("The root cause was the edit.", "direct"),
    ("As a result of the edit, C3 failed.", "direct"),
    ("The edit has broken the build.", "direct"),
    ("Without the edit, C3 passes.", "counterfactually_supported"),
    ("C3 could have passed.", "counterfactually_supported"),
    ("- Had the agent re-run tests, C3 passes.", "counterfactually_supported"),
    ("(Had the agent re-run tests, C3 passes.)", "counterfactually_supported"),
    ("Summary\nHad the agent re-run tests, C3 passes.", "counterfactually_supported"),
    ("Notably, had the agent re-run tests, C3 passes.", "counterfactually_supported"),
    ("C3 failed. Had no effect on C3.", "hypothesized"),
    ("Expected file; had 0 lines.", "hypothesized"),
    ("The agent changed config.yaml.", "hypothesized"),
    ("C3 wouldn\u2019t have failed.", "counterfactually_supported"),
    ("The failure wasn\u2019t caused by chance; the edit deleted it.", "direct"),
    ("If the agent re-ran tests, C3 passes.", "counterfactually_supported"),
    ("The failure wasnt caused by chance; the edit deleted it.", "direct"),
    ("The failure was never caused by chance, the edit deleted it.", "direct"),
    ("The failure was not  caused by chance.", "direct"),
    ("The failure happened due  to the edit.", "direct"),
    ("C3 couldn't have passed.", "counterfactually_supported"),
    ("C3 should not have failed.", "counterfactually_supported"),
    ("The edit is why C3 failed.", "direct"),
    ("Without this edit, C3 passes.", "counterfactually_supported"),
    ("On replay, C3 passes.", "counterfactually_supported"),
    ("The agent made a pass over the file.", "hypothesized"),
    ("The edit made C3 pass.", "direct"),
    ("The edit makes C3 pass.", "direct"),
    ("C3 is linked to the failed outcome.", "dependency_linked"),
    ("A likely explanation for the outcome.", "hypothesized"),
]


@pytest.mark.parametrize("text,level", CASES)
def test_implied_attribution(text, level):
    assert implied_attribution(text) == level


def test_gate_flags_overclaim():
    assert attribution_gate("The edit led to the failure.", "hypothesized") == (False, "direct")
    assert attribution_gate("The edit led to the failure.", "direct")[0]


@pytest.mark.parametrize("ceiling", ["hypothesized", "dependency_linked", "direct"])
@pytest.mark.parametrize("fact", [
    {"type": "absence", "declared_artifact": "out.txt"},
    {"type": "absence", "declared_artifact": "out.txt", "observation_scope": "not_observed_in_captured_evidence"},
    {"type": "state_transition", "tool": "bash", "failure_diagnostic": "ModuleNotFoundError"},
    {"type": "event_support", "quotes": [{"event_id": "evt_1"}]},
    {"type": "requirement_status", "check_id": "C3", "status": "failed"},
    {"type": "requirement_status", "check_id": "C3", "status": "failed", "expected": 1, "observed": 0,
     "agent_observed_failure": True},
    {"type": "requirement_status", "check_id": "C3", "status": "passed"},
    {"type": "requirement_status", "check_id": "C3", "status": "skipped"},
    {"type": "state_transition", "tool": "bash", "failure_diagnostic": "ModuleNotFoundError",
     "resolution_event": "evt_2"},
    {"type": "repetition", "events": ["evt_1", "evt_2"]},
    {"type": "termination", "expected": "run_timed_out"},
])
def test_renderer_templates_stay_within_ceiling(fact, ceiling):
    ok, implied = attribution_gate(render(fact, ceiling, "negative"), ceiling)
    assert ok, implied


def test_grouped_requirement_status_stays_within_ceiling():
    facts = [{"type": "requirement_status", "check_id": c, "status": "failed"} for c in ("C1", "C2")]
    assert attribution_gate(_render_requirement_status_group(facts), "hypothesized")[0]


def test_if_had_is_linear_on_long_input():
    import time
    t = time.time()
    implied_attribution("if " * 20000)
    assert time.time() - t < 2
