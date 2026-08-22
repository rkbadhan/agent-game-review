"""Stage F — the model reviewer envelope (spec §8.7, §8.8, §7.4).

Everything here runs offline: the whole pipeline is driven by ``ScriptedReviewer``
(a canned model payload), and the provider adapters are exercised by monkeypatching
their ``_complete`` so no network call or spend occurs. A key-gated live test is
included but skips without ``ANTHROPIC_API_KEY``.
"""

import json
import os

import pytest

from agr import read, reviewer
from agr.model_packet import build_packet
from agr.model_reviewer import (
    AnthropicReviewer,
    OpenAIReviewer,
    ScriptedReviewer,
    make_reviewer,
)
from agr.pipeline import analyze
from agr.reviewer import ReviewerContext, run_reviewer
from agr.schema import Candidate, EvidenceSlice, VerifierCheck
from agr.store import Store


# --- small builders (isolated units) -----------------------------------------


def _check(cid, status, expected=None, observed=None):
    return VerifierCheck(check_id=cid, run_id="r", source_capture_id="c", name=cid,
                         status=status, source="native_structured",
                         expected=expected, observed=observed)


def _cand(cid, **kw):
    kw.setdefault("kind", "omission")
    kw.setdefault("anchor_event_ids", ["evt_sub"])
    return Candidate(candidate_id=cid, run_id="r", source_capture_id="c",
                     detector=kw.pop("detector", "det"), **kw)


def _slice(cid, ceiling):
    return EvidenceSlice(slice_id=f"s_{cid}", run_id="r", source_capture_id="c",
                         check_id=cid, contract_item_ids=[], branch="standard",
                         event_ids=[], attribution_ceiling=ceiling, rationale="")


def _ctx(candidates, checks=(), slices=()):
    return ReviewerContext(run_id="r", source_capture_id="c", candidates=list(candidates),
                           slices=list(slices), checks=list(checks), events=[], recoveries=[])


def _moment(cid, checks, facts, **enr):
    return {"candidate_id": cid, "kind": "omission", "polarity": "negative",
            "anchor_event_ids": ["evt_sub"], "affected_checks": list(checks),
            "structured_facts": facts, **enr}


# --- enrichment + gating ------------------------------------------------------


def test_scripted_reviewer_enriches_and_selects():
    checks = [_check("C3", "failed")]
    facts = [{"type": "requirement_status", "check_id": "C3", "status_at_submission": "failed"}]
    payload = {"moments": [_moment(
        "cand_C3", ["C3"], facts,
        taxonomy_verdict="Mistake", behaviour_tags=["stopped_enumeration"],
        phase="submission", consequence="requirement_failed",
        root_cause_candidates=[{"locus": "agent_policy", "rank": 1, "rationale": "gave up"}],
        better_action="enumerate all winning moves before submitting",
    )]}
    moments = run_reviewer(_ctx([_cand("cand_C3", affected_checks=["C3"])],
                                checks=checks, slices=[_slice("C3", "dependency_linked")]),
                           ScriptedReviewer(payload))
    (m,) = [x for x in moments if x.selected]
    assert m.taxonomy_verdict == "Mistake"
    assert m.behaviour_tags == ["stopped_enumeration"]
    assert m.better_action.startswith("enumerate")
    assert m.enrichment_source == "model:scripted"
    assert m.review_mode == "model_enriched"
    assert m.gate_results["better_action"] == "model_provided"


def test_out_of_vocabulary_enrichment_is_dropped():
    checks = [_check("C3", "failed")]
    facts = [{"type": "requirement_status", "check_id": "C3", "status_at_submission": "failed"}]
    payload = {"moments": [_moment(
        "cand_C3", ["C3"], facts,
        behaviour_tags=["stopped_enumeration", "not_a_real_tag"],
        phase="not_a_phase", consequence="bogus",
        micro_abilities=["exhaustive_completion", "nope"],
        root_cause_candidates=[{"locus": "agent_policy", "rank": 1, "rationale": "x"},
                               {"locus": "made_up", "rank": 2, "rationale": "y"}],
    )]}
    (m,) = [x for x in run_reviewer(
        _ctx([_cand("cand_C3", affected_checks=["C3"])], checks=checks,
             slices=[_slice("C3", "hypothesized")]), ScriptedReviewer(payload)) if x.selected]
    assert m.behaviour_tags == ["stopped_enumeration"]
    assert m.phase is None and m.consequence is None
    assert m.micro_abilities == ["exhaustive_completion"]
    assert [rc["locus"] for rc in m.root_cause_candidates] == ["agent_policy"]


# --- prompt-injection / fabrication defence (spec §7.4, §16.1) ----------------


def test_fabricated_fact_is_rejected_and_carries_no_verdict():
    # A reviewer fooled by injected trace content fabricates a moment claiming a
    # PASSING check failed. Stage G recomputes the value, the card is not selected,
    # and no model verdict is published — injection cannot change the review.
    checks = [_check("C1", "passed")]
    facts = [{"type": "requirement_status", "check_id": "C1", "status_at_submission": "failed"}]
    payload = {"moments": [_moment("cand_bad", ["C1"], facts,
                                   taxonomy_verdict="Perfect run", better_action="ship it")]}
    moments = run_reviewer(_ctx([_cand("cand_bad", affected_checks=["C1"])], checks=checks),
                           ScriptedReviewer(payload))
    m = next(x for x in moments if x.candidate_id == "cand_bad")
    assert m.gate_results["fact_validation"] == "failed"
    assert m.selected is False
    assert m.taxonomy_verdict is None       # enrichment withheld from an unpublished card
    assert m.better_action is None


# --- precision: a chatty model's duplicate cards collapse (spec §8.10 gate 4) --


def test_two_model_cards_for_one_check_collapse_even_at_different_anchors():
    # The eval regression: the model surfaces the SAME failed requirement twice,
    # anchored differently. Sharing the affected check must collapse them to one,
    # so precision is not dragged down.
    checks = [_check("C3", "failed")]
    facts = [{"type": "requirement_status", "check_id": "C3", "status_at_submission": "failed"}]
    m1 = {"candidate_id": "c1", "kind": "omission", "polarity": "negative",
          "anchor_event_ids": ["evt_008"], "affected_checks": ["C3"],
          "structured_facts": facts, "taxonomy_verdict": "incomplete_execution"}
    m2 = {"candidate_id": "c2", "kind": "behaviour", "polarity": "negative",
          "anchor_event_ids": ["evt_004"], "affected_checks": ["C3"],
          "structured_facts": facts, "taxonomy_verdict": "premature_submission"}
    moments = run_reviewer(
        _ctx([_cand("c1", affected_checks=["C3"]), _cand("c2", affected_checks=["C3"])],
             checks=checks, slices=[_slice("C3", "dependency_linked")]),
        ScriptedReviewer({"moments": [m1, m2]}))
    assert len([m for m in moments if m.selected]) == 1


def test_distinct_checks_are_not_merged_even_at_same_anchor():
    # Two DIFFERENT failed requirements at the same submission anchor must both
    # survive — merging them would drop recall.
    checks = [_check("C2", "failed"), _check("C3", "failed")]
    def f(cid):
        return [{"type": "requirement_status", "check_id": cid, "status_at_submission": "failed"}]
    m1 = {"candidate_id": "a", "polarity": "negative", "anchor_event_ids": ["evt_sub"],
          "affected_checks": ["C2"], "structured_facts": f("C2")}
    m2 = {"candidate_id": "b", "polarity": "negative", "anchor_event_ids": ["evt_sub"],
          "affected_checks": ["C3"], "structured_facts": f("C3")}
    moments = run_reviewer(
        _ctx([_cand("a", affected_checks=["C2"]), _cand("b", affected_checks=["C3"])],
             checks=checks, slices=[_slice("C2", "hypothesized"), _slice("C3", "hypothesized")]),
        ScriptedReviewer({"moments": [m1, m2]}))
    assert len([m for m in moments if m.selected]) == 2


# --- Stage G return-once loop (spec §8.8) -------------------------------------


def test_return_once_loop_corrects_then_selects():
    checks = [_check("C3", "failed")]
    bad = [{"type": "requirement_status", "check_id": "C3", "status_at_submission": "passed"}]
    good = [{"type": "requirement_status", "check_id": "C3", "status_at_submission": "failed"}]
    payload = {"moments": [_moment("cand_C3", ["C3"], bad, taxonomy_verdict="Mistake")]}
    revisions = {"cand_C3": _moment("cand_C3", ["C3"], good, taxonomy_verdict="Mistake")}
    (m,) = [x for x in run_reviewer(
        _ctx([_cand("cand_C3", affected_checks=["C3"])], checks=checks,
             slices=[_slice("C3", "hypothesized")]),
        ScriptedReviewer(payload, revisions=revisions)) if x.selected]
    assert m.gate_results["fact_validation"] == "passed"
    assert m.gate_results["validation_attempts"] == 2
    assert m.taxonomy_verdict == "Mistake"


def test_return_once_loop_second_failure_drops():
    checks = [_check("C3", "failed")]
    bad = [{"type": "requirement_status", "check_id": "C3", "status_at_submission": "passed"}]
    payload = {"moments": [_moment("cand_C3", ["C3"], bad, taxonomy_verdict="Mistake")]}
    # revision still wrong -> second failure -> unselected
    revisions = {"cand_C3": _moment("cand_C3", ["C3"], bad, taxonomy_verdict="Mistake")}
    moments = run_reviewer(
        _ctx([_cand("cand_C3", affected_checks=["C3"])], checks=checks),
        ScriptedReviewer(payload, revisions=revisions))
    m = next(x for x in moments if x.candidate_id == "cand_C3")
    assert m.gate_results["validation_attempts"] == 2
    assert m.selected is False


# --- integration over a real fixture + read badge ----------------------------


def _analysis(tmp_path, load_fixture, name, reviewer_obj=None):
    return analyze(load_fixture(name), Store(str(tmp_path / "store")), reviewer=reviewer_obj)


def test_deterministic_run_reports_deterministic_only(tmp_path, load_fixture):
    store = Store(str(tmp_path / "store"))
    analyze(load_fixture("chess_best_move.atif.json"), store)
    review = read.get_review(store, "chess_best_move__seed42")
    assert review["review_mode"] == "deterministic_only"
    assert all(m.get("taxonomy_verdict") is None for m in review["moments"])


def test_model_run_reports_model_enriched(tmp_path, load_fixture):
    store = Store(str(tmp_path / "store"))
    # First deterministically, to read the real candidate id + facts for the script.
    a = analyze(load_fixture("chess_best_move.atif.json"), store)
    ctx = ReviewerContext(
        run_id=a.run_source.run_id, source_capture_id=a.run_source.source_capture_id,
        candidates=[c for r in a.detector_results if r.evaluated for c in r.candidates],
        slices=a.evidence_slices, checks=a.checks, events=a.events,
        recoveries=a.recoveries, contract=a.contract)
    packet, _ = build_packet(ctx)
    pc = packet["deterministic_candidates"][0]
    payload = {"moments": [_moment(
        pc["candidate_id"], pc["affected_checks"], pc["structured_facts"],
        taxonomy_verdict="Mistake", behaviour_tags=["stopped_enumeration"])]}
    payload["moments"][0]["anchor_event_ids"] = pc["anchor_event_ids"]
    analyze(load_fixture("chess_best_move.atif.json"), store, reviewer=ScriptedReviewer(payload))
    review = read.get_review(store, "chess_best_move__seed42")
    assert review["review_mode"] == "model_enriched"
    assert review["moments"][0]["taxonomy_verdict"] == "Mistake"
    assert review["moments"][0]["enrichment_source"] == "model:scripted"


# --- provider adapters (offline: monkeypatch the SDK call) --------------------


def test_make_reviewer_selects_provider():
    assert isinstance(make_reviewer("anthropic"), AnthropicReviewer)
    assert isinstance(make_reviewer("openai"), OpenAIReviewer)
    assert make_reviewer("anthropic").model == "claude-opus-4-8"
    with pytest.raises(ValueError):
        make_reviewer("gemini")


def test_anthropic_adapter_parses_completion(monkeypatch):
    rev = AnthropicReviewer()
    facts = [{"type": "requirement_status", "check_id": "C3", "status_at_submission": "failed"}]
    monkeypatch.setattr(rev, "_complete", lambda system, user: {
        "moments": [_moment("cand_C3", ["C3"], facts, taxonomy_verdict="Mistake")]})
    checks = [_check("C3", "failed")]
    (m,) = [x for x in run_reviewer(
        _ctx([_cand("cand_C3", affected_checks=["C3"])], checks=checks,
             slices=[_slice("C3", "hypothesized")]), rev) if x.selected]
    assert m.taxonomy_verdict == "Mistake"
    assert m.enrichment_source == "model:claude-opus-4-8"


def test_base_url_makes_any_openai_compatible_model_usable():
    # The 'use any model' path: point the OpenAI adapter at any compatible endpoint.
    rev = OpenAIReviewer(model="llama-3.1-70b", base_url="http://localhost:11434/v1")
    assert rev.base_url == "http://localhost:11434/v1"
    assert rev.model == "llama-3.1-70b"
    made = make_reviewer("openai", "mistral-large", "https://openrouter.ai/api/v1")
    assert isinstance(made, OpenAIReviewer)
    assert made.base_url == "https://openrouter.ai/api/v1"
    assert made.model == "mistral-large"


def test_openai_base_url_falls_back_to_env(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "http://example.test/v1")
    assert OpenAIReviewer().base_url == "http://example.test/v1"
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert OpenAIReviewer().base_url is None


def test_anthropic_base_url_is_optional():
    assert AnthropicReviewer().base_url is None
    assert AnthropicReviewer(base_url="https://gw.example/v1").base_url == "https://gw.example/v1"


def _review_args(tmp_path, load_fixture, model):
    import argparse
    store_dir = str(tmp_path / "store")
    analyze(load_fixture("chess_best_move.atif.json"), Store(store_dir))
    return argparse.Namespace(store=store_dir, run_id="chess_best_move__seed42",
                              provider="openai", model=model, base_url=None)


def test_review_model_resolves_flag_then_env_then_default(tmp_path, load_fixture, monkeypatch):
    from agr import cli, model_reviewer

    captured = {}

    def fake_make(provider, model, base_url):
        captured["model"] = model
        raise SystemExit(99)  # stop before the (network) analyze call

    monkeypatch.setattr(model_reviewer, "make_reviewer", fake_make)

    # env fallback used when --model is absent
    monkeypatch.setenv("AGR_REVIEW_MODEL", "env-model")
    with pytest.raises(SystemExit):
        cli.cmd_review(_review_args(tmp_path, load_fixture, None))
    assert captured["model"] == "env-model"

    # explicit --model wins over the env var
    with pytest.raises(SystemExit):
        cli.cmd_review(_review_args(tmp_path, load_fixture, "flag-model"))
    assert captured["model"] == "flag-model"

    # neither set -> None, so make_reviewer applies the provider default
    monkeypatch.delenv("AGR_REVIEW_MODEL", raising=False)
    with pytest.raises(SystemExit):
        cli.cmd_review(_review_args(tmp_path, load_fixture, None))
    assert captured["model"] is None


def test_cli_loads_env_file_without_overriding_real_env(tmp_path, monkeypatch):
    pytest.importorskip("dotenv")  # ships with the model extras
    from agr import cli

    env_path = tmp_path / ".env"
    env_path.write_text("OPENAI_API_KEY=from-file\nAGR_REVIEW_MODEL=file-model\n", encoding="utf-8")

    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setenv("AGR_REVIEW_MODEL", "already-set")  # real env must win
    cli._load_env(str(env_path))
    assert os.environ["OPENAI_API_KEY"] == "from-file"       # filled from the file
    assert os.environ["AGR_REVIEW_MODEL"] == "already-set"   # not overridden


def test_eval_comparison_renders_and_verdicts(tmp_path, load_fixture, capsys):
    # Offline check of `agr eval --provider` rendering + M4 acceptance logic:
    # compared against itself, a reviewer never regresses -> VERDICT PASS.
    import glob

    from agr import cli
    from agr import reviewer_eval as rev
    from agr.gold import load_gold_set

    store = Store(str(tmp_path / "store"))
    fixtures_dir = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")
    for p in sorted(glob.glob(os.path.join(fixtures_dir, "*.atif.json"))):
        with open(p, encoding="utf-8") as fh:
            analyze(json.load(fh), store)
    gs = load_gold_set(os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "gold"))
    result = rev.evaluate_store(store, gs)

    cli._print_eval_comparison(result, result, "openai", "demo-model")
    out = capsys.readouterr().out
    assert "baseline vs model reviewer (openai · demo-model)" in out
    assert "M4 acceptance" in out
    assert "VERDICT: PASS" in out  # identical to itself -> no regression on any gate


def test_lenient_json_parse_tolerates_prose():
    from agr.model_reviewer import _loads_lenient
    assert _loads_lenient('Here is the review:\n{"moments": []}\ndone') == {"moments": []}
    assert _loads_lenient("not json at all") == {"moments": []}


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"),
                    reason="no ANTHROPIC_API_KEY — live model validation is opt-in")
def test_anthropic_live_beats_or_holds_baseline(tmp_path, load_fixture):
    store = Store(str(tmp_path / "store"))
    analyze(load_fixture("chess_best_move.atif.json"), store,
            reviewer=AnthropicReviewer())
    review = read.get_review(store, "chess_best_move__seed42")
    assert review["review_mode"] == "model_enriched"
    assert review["moments"], "the live reviewer surfaced no moment"
