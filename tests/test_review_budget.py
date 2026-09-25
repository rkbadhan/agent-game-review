"""GR-1: per-review cost and latency, and the budget that produces 'incomplete'.

Costs are ESTIMATES from character-derived token counts against a documented
list-price table; the provider's actual usage is not captured, so these tests
pin the estimate and the stop behaviour, never a fabricated exact figure.
"""

import json
import os
import time

import pytest

from agr import read
from agr.model_reviewer import (
    _LazyModelReviewer,
    _estimate_cost_usd,
    ScriptedReviewer,
)
from agr.pipeline import analyze
from agr.reviewer import ReviewBudgetExceededError, ReviewerContext
from agr.schema import CapabilityProfile, DerivedEvent
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")
FIXTURE = "chess_best_move.atif.json"


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _event(event_id="evt_1"):
    return DerivedEvent(event_id=event_id, run_id="r", source_capture_id="c",
                        sequence=1, source_step_ids=["s"], event_type="tool_call",
                        actor="agent", payload={"content": "x" * 100})


def _ctx(events):
    profile = CapabilityProfile(
        run_id="r", source_capture_id="c",
        capabilities={"messages": "complete", "tool_calls": "complete",
                      "tool_results": "complete", "filesystem": "checkpoint_only"})
    return ReviewerContext(run_id="r", source_capture_id="c", candidates=[],
                           slices=[], checks=[], events=list(events), recoveries=[],
                           profile=profile, declared_artifacts=[])


class _Fake(_LazyModelReviewer):
    """Offline stand-in with canned responses, an optional sleep and captured log."""

    provider = "fake"

    def __init__(self, responses, sleep_s=0.0, **kw):
        super().__init__("fake-model", **kw)
        self._responses = list(responses)
        self._sleep_s = sleep_s
        self.sent = []

    def _complete(self, system, user_json):
        self.sent.append(user_json)
        if self._sleep_s:
            time.sleep(self._sleep_s)
        return self._responses.pop(0)


class _BudgetBlown(ScriptedReviewer):
    """A model reviewer that stops early on its own budget."""

    review_mode = "model_enriched"

    def propose(self, ctx):
        raise ReviewBudgetExceededError(
            "timeout", "review exceeded its time budget",
            {"elapsed_s": 95.0, "time_budget_s": 90.0})


# --- the estimate ------------------------------------------------------------


def test_cost_estimate_uses_the_model_price_table():
    # gpt-4o-mini list price: $0.15/M in, $0.60/M out.
    assert _estimate_cost_usd("gpt-4o-mini", 1_000_000, 1_000_000) == pytest.approx(0.75)
    # A local/unknown model falls back to the documented mid-size default ($3/$15).
    assert _estimate_cost_usd("some-local-llama", 1_000_000, 0) == pytest.approx(3.0)
    assert _estimate_cost_usd("some-local-llama", 0, 1_000_000) == pytest.approx(15.0)


def test_budget_defaults_and_env_overrides(monkeypatch):
    monkeypatch.delenv("AGR_REVIEW_COST_BUDGET_USD", raising=False)
    monkeypatch.delenv("AGR_REVIEW_TIME_BUDGET_S", raising=False)
    rev = _Fake([])
    assert rev.cost_budget_usd == 0.15
    assert rev.time_budget_s == 90.0

    monkeypatch.setenv("AGR_REVIEW_COST_BUDGET_USD", "2.5")
    monkeypatch.setenv("AGR_REVIEW_TIME_BUDGET_S", "12")
    rev = _Fake([])
    assert rev.cost_budget_usd == 2.5
    assert rev.time_budget_s == 12.0


# --- stopping early ----------------------------------------------------------


def test_time_budget_stops_before_the_second_round():
    rev = _Fake([{"expansion_requests": [{"event_ids": ["evt_1"], "reason": "need it"}]}],
                sleep_s=0.05, time_budget_s=0.01, cost_budget_usd=0)
    with pytest.raises(ReviewBudgetExceededError) as excinfo:
        rev.propose(_ctx([_event()]))
    assert excinfo.value.reason == "timeout"
    assert rev.incomplete["reason"] == "timeout"
    assert rev.incomplete["time_budget_s"] == 0.01
    assert rev.telemetry[-1]["kind"] == "propose_expanded_skipped"
    assert rev.telemetry[-1]["reason"] == "review_time_budget"


def test_cost_budget_stops_before_the_second_round():
    rev = _Fake([{"expansion_requests": [{"event_ids": ["evt_1"], "reason": "need it"}]}],
                cost_budget_usd=1e-9, time_budget_s=0)
    with pytest.raises(ReviewBudgetExceededError) as excinfo:
        rev.propose(_ctx([_event()]))
    assert excinfo.value.reason == "cost_budget"
    assert rev.incomplete["reason"] == "cost_budget"
    assert rev.incomplete["cost_estimate_usd"] > rev.incomplete["cost_budget_usd"]
    assert rev.telemetry[-1]["kind"] == "propose_expanded_skipped"
    assert rev.telemetry[-1]["reason"] == "review_cost_budget"


def test_a_finished_review_is_not_discarded_for_cost_it_already_incurred():
    # The budget is checked BEFORE a round: a review that completes in one call
    # is served even if that call's estimate is over budget (there is no work
    # left to stop), and the cost is still recorded.
    rev = _Fake([{"moments": []}], cost_budget_usd=1e-9, time_budget_s=0)
    assert rev.propose(_ctx([_event()])) == []
    assert rev.incomplete is None
    assert rev.telemetry[-1]["kind"] == "propose_response"
    assert rev.telemetry[-1]["cost_estimate_usd"] > 0


def test_a_new_review_resets_the_previous_stop_state():
    rev = _Fake([])
    rev.mark_incomplete("missing_chunks", {"chunks": ["phase_2"]})
    assert rev.incomplete["reason"] == "missing_chunks"
    rev2 = _Fake([{"moments": []}])
    rev2.incomplete = {"reason": "stale"}
    rev2.propose(_ctx([_event()]))
    assert rev2.incomplete is None


# --- pipeline + read integration ---------------------------------------------


def test_early_stop_is_recorded_as_incomplete_not_failed(tmp_path):
    store = Store(str(tmp_path / "store"))
    analysis = analyze(_load(FIXTURE), store,
                       reviewer=_BudgetBlown({"moments": []}, source="model:probe"))
    assert analysis.review_error is None
    assert analysis.review_incomplete["reason"] == "timeout"

    view = read.get_review(store, analysis.run_source.run_id)
    assert view["served_reviewer_key"] == "deterministic"
    assert "model:probe" not in view["available_reviews"]  # no partial snapshot written
    assert view["review_status"] == "incomplete"
    assert view["review_status_success"] is False
    assert view["review_attempts"][-1]["outcome"] == "incomplete"
    assert view["review_telemetry"]["incomplete"]["reason"] == "timeout"
    assert view["review_telemetry"]["reviewer_key"] == "model:probe"


def test_review_cost_and_latency_are_recorded_per_review(tmp_path):
    store = Store(str(tmp_path / "store"))
    analysis = analyze(_load(FIXTURE), store,
                       reviewer=_Fake([{"moments": []}]))
    view = read.get_review(store, analysis.run_source.run_id)
    telemetry = view["review_telemetry"]
    assert telemetry["cost_estimate_usd"] > 0
    assert telemetry["latency_ms"] >= 0
    assert telemetry["cost_budget_usd"] == 0.15
    assert telemetry["time_budget_s"] == 90.0
    assert view["review_status"] == "no_decisive_moment"


class _PacketBlown(ScriptedReviewer):
    """A model reviewer whose packet cannot fit the enforced budget."""

    review_mode = "model_enriched"

    def propose(self, ctx):
        from agr.model_reviewer import PacketBudgetExceededError
        raise PacketBudgetExceededError("packet exceeds the enforced budget")


def test_packet_budget_stop_is_incomplete_not_failed(tmp_path):
    # A packet-budget stop is a budget stop: 'incomplete' with reason
    # 'packet_budget', never a provider 'review_failed'.
    store = Store(str(tmp_path / "store"))
    analysis = analyze(_load(FIXTURE), store,
                       reviewer=_PacketBlown({"moments": []}, source="model:probe"))
    assert analysis.review_error is None
    assert analysis.review_incomplete["reason"] == "packet_budget"
    view = read.get_review(store, analysis.run_source.run_id)
    assert view["review_status"] == "incomplete"
    assert view["review_telemetry"]["incomplete"]["reason"] == "packet_budget"


def test_an_incomplete_attempt_does_not_serve_an_older_model_slot(tmp_path):
    """Finding 4: after an incomplete retry, the reviewer's older successful
    snapshot is stale — the default serves the deterministic baseline, not old
    model cards under an Incomplete status."""
    store = Store(str(tmp_path / "store"))
    analyze(_load(FIXTURE), store,
            reviewer=ScriptedReviewer({"moments": []}, source="model:probe"))
    second = analyze(_load(FIXTURE), store,
                     reviewer=_BudgetBlown({"moments": []}, source="model:probe"))
    view = read.get_review(store, second.run_source.run_id)
    assert view["served_reviewer_key"] == "deterministic"
    assert view["review_status"] == "incomplete"
    # The old slot is retained and explicitly addressable, just not the default.
    assert "model:probe" in view["available_reviews"]
