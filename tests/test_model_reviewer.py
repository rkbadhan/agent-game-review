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
    ModelOutputError,
    OpenAIReviewer,
    ScriptedReviewer,
    make_reviewer,
)
from agr.model_reviewer import _LazyModelReviewer
from agr.pipeline import analyze
from agr.reviewer import ReviewerContext, run_reviewer
from agr.schema import CapabilityProfile, DerivedEvent
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


def _ctx(candidates, checks=(), slices=(), profile=None, declared_artifacts=(), anchor_ids=(), events=None):
    # AGR-03: synthesise an event for every referenced anchor so hand-built
    # candidates resolve, and default to a full-capability profile so model
    # candidates pass the computed observability gate unless a test says otherwise.
    events = list(events or [])
    supplied = {e.event_id for e in events}
    for aid in anchor_ids:
        if aid in supplied:
            continue
        events.append(DerivedEvent(
            event_id=aid, run_id="r", source_capture_id="c",
            sequence=len(supplied) + 1, source_step_ids=[aid],
            event_type="tool_call", actor="agent"))
        supplied.add(aid)
    for cand in candidates:
        for aid in cand.anchor_event_ids:
            if aid not in supplied:
                events.append(DerivedEvent(
                    event_id=aid, run_id="r", source_capture_id="c",
                    sequence=len(supplied) + 1, source_step_ids=[aid],
                    event_type="tool_call", actor="agent"))
                supplied.add(aid)
    if profile is None:
        profile = CapabilityProfile(
            run_id="r", source_capture_id="c",
            capabilities={"messages": "complete", "tool_calls": "complete",
                          "tool_results": "complete", "filesystem": "checkpoint_only"})
    return ReviewerContext(run_id="r", source_capture_id="c", candidates=list(candidates),
                           slices=list(slices), checks=list(checks), events=events, recoveries=[],
                           profile=profile, declared_artifacts=list(declared_artifacts))


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
             checks=checks, slices=[_slice("C3", "dependency_linked")],
             anchor_ids=["evt_008", "evt_004"]),
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


def _install_fake_sdk(monkeypatch, name):
    """Make ``import <name>`` succeed without the real optional extra installed."""
    import sys
    import types
    monkeypatch.setitem(sys.modules, name, types.ModuleType(name))


def test_make_reviewer_selects_provider(monkeypatch):
    _install_fake_sdk(monkeypatch, "anthropic")
    _install_fake_sdk(monkeypatch, "openai")
    assert isinstance(make_reviewer("anthropic"), AnthropicReviewer)
    assert isinstance(make_reviewer("openai"), OpenAIReviewer)
    assert make_reviewer("anthropic").model == "claude-opus-4-8"
    with pytest.raises(ValueError):
        make_reviewer("gemini")


def test_make_reviewer_fails_fast_when_sdk_missing(monkeypatch):
    # P0-3: the SDK is checked at construction time, before the pipeline's own
    # catch-all could swallow the failure into a silent deterministic fallback.
    # Simulate the missing import explicitly so the result does not depend on
    # whether the optional provider SDKs happen to be installed in this
    # developer/CI environment (None in sys.modules makes ``import`` raise
    # ModuleNotFoundError).
    import sys
    for name in ("anthropic", "openai"):
        monkeypatch.setitem(sys.modules, name, None)
    with pytest.raises(RuntimeError, match="anthropic"):
        make_reviewer("anthropic")
    with pytest.raises(RuntimeError, match="openai"):
        make_reviewer("openai")


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


def test_base_url_makes_any_openai_compatible_model_usable(monkeypatch):
    # The 'use any model' path: point the OpenAI adapter at any compatible endpoint.
    rev = OpenAIReviewer(model="llama-3.1-70b", base_url="http://localhost:11434/v1")
    assert rev.base_url == "http://localhost:11434/v1"
    assert rev.model == "llama-3.1-70b"
    _install_fake_sdk(monkeypatch, "openai")
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

    def fake_make(provider, model, base_url, **kwargs):
        captured["model"] = model
        captured["kwargs"] = kwargs
        raise SystemExit(99)  # stop before the (network) analyze call

    monkeypatch.setattr(model_reviewer, "make_reviewer", fake_make)

    # env fallback used when --model is absent
    monkeypatch.setenv("AGR_REVIEW_MODEL", "env-model")
    with pytest.raises(SystemExit):
        cli.cmd_review(_review_args(tmp_path, load_fixture, None))
    assert captured["model"] == "env-model"
    # GR-1: unset budget flags resolve to None, so the reviewer defaults apply.
    assert captured["kwargs"] == {"cost_budget_usd": None, "time_budget_s": None}

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


def test_eval_provider_missing_sdk_exits_cleanly_not_a_traceback(tmp_path, monkeypatch, capsys):
    """`agr eval --provider` used to only catch ValueError around make_reviewer,
    so the RuntimeError make_reviewer now raises for a missing SDK (P0-3's
    fail-fast check) propagated as an unhandled traceback instead of the
    same clean 'cannot run model reviewer' / exit 3 every other command
    gives for this exact failure."""
    from agr import cli, model_reviewer

    def _boom(*a, **k):
        raise RuntimeError("The OpenAI model reviewer requires the 'openai' SDK.")

    monkeypatch.setattr(model_reviewer, "make_reviewer", _boom)
    archive = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic")
    rc = cli.main(["--store", str(tmp_path / "store"), "eval",
                   "--gold", os.path.join(archive, "gold"),
                   "--fixtures", os.path.join(archive, "fixtures"),
                   "--provider", "openai"])
    err = capsys.readouterr().err
    assert rc == 3
    assert "cannot run model reviewer" in err and "openai" in err.lower()


def test_lenient_json_parse_tolerates_prose():
    from agr.model_reviewer import _loads_lenient
    assert _loads_lenient('Here is the review:\n{"moments": []}\ndone') == {"moments": []}


def test_malformed_model_output_is_an_explicit_error_not_empty():
    """AGR-06: unparsable output raises ModelOutputError — parsing failure
    must not silently become 'no decisive moment'. F1 follow-up: an EMPTY
    response is also malformed — only an explicit {\"moments\": []} means
    'no findings', and a parseable non-review object (a refusal) is not a
    zero-moment review either."""
    import pytest

    from agr.model_reviewer import ModelOutputError, _loads_lenient, _validate_envelope
    with pytest.raises(ModelOutputError):
        _loads_lenient("not json at all")
    with pytest.raises(ModelOutputError):
        _loads_lenient("")
    with pytest.raises(ModelOutputError):
        _loads_lenient(None)
    # An explicit empty review is valid; a refusal object is not a review.
    assert _loads_lenient('{"moments": []}') == {"moments": []}
    with pytest.raises(ModelOutputError):
        _validate_envelope({"error": "model refused"})
    with pytest.raises(ModelOutputError):
        _validate_envelope({"moments": ["not an object"]})


@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"),
                    reason="no ANTHROPIC_API_KEY — live model validation is opt-in")
def test_anthropic_live_beats_or_holds_baseline(tmp_path, load_fixture):
    store = Store(str(tmp_path / "store"))
    analyze(load_fixture("chess_best_move.atif.json"), store,
            reviewer=AnthropicReviewer())
    review = read.get_review(store, "chess_best_move__seed42")
    assert review["review_mode"] == "model_enriched"
    assert review["moments"], "the live reviewer surfaced no moment"


# --- AGR-06: reviewer access with explicit failure states ----------------------

_TOKEN = "AKIAABCDEFGHIJKLMNOP"  # synthetic AWS-shaped secret the redactor knows


def test_no_outbound_string_escapes_redaction():
    """Acceptance (AGR-06): a synthetic token recognized by the redactor cannot
    survive in an `expected` field or ANY other outbound field of the packet."""
    check = VerifierCheck(check_id="C9", run_id="r", source_capture_id="c",
                          name=f"credential is {_TOKEN}", status="failed",
                          source="native_structured", expected=[_TOKEN],
                          observed=[f"leaked {_TOKEN}"])
    cand = _cand("cand_x", affected_checks=["C9"],
                 structured_facts=[{"type": "requirement_status", "check_id": "C9",
                                    "status_at_submission": "failed", "note": _TOKEN}])
    packet, _red = build_packet(_ctx([cand], checks=[check]))
    serialized = json.dumps(packet)
    assert _TOKEN not in serialized
    # The traversal pass also redacts nested values inside facts and errors.
    from agr.redaction import redact_value
    obj, _m = redact_value({"expected": [_TOKEN], "nested": {"deep": _TOKEN}})
    assert _TOKEN not in json.dumps(obj)


def test_small_trace_keeps_full_text_large_trace_gets_excerpts():
    """AGR-06: traces that fit the budget are not truncated unnecessarily."""
    long_text = "x" * 900  # > the 220-char excerpt budget
    ev = DerivedEvent(event_id="evt_big", run_id="r", source_capture_id="c",
                      sequence=1, source_step_ids=["s"], event_type="tool_call",
                      actor="agent", payload={"content": long_text})
    small = _ctx([], events=[ev])
    packet, _ = build_packet(small)
    digest = packet["timeline_digest"][0]["excerpt"]
    assert digest == long_text  # full text — no unnecessary truncation
    # An absurdly small budget forces the 220-char excerpt fallback.
    tiny = build_packet(small, budget_chars=10)
    assert len(tiny[0]["timeline_digest"][0]["excerpt"]) == 220


class _FakeProvider(_LazyModelReviewer):
    """Offline stand-in with canned responses and a captured outbound log."""

    provider = "fake"

    def __init__(self, responses):
        super().__init__("fake-model")
        self._responses = list(responses)
        self.sent = []

    def _complete(self, system, user_json):
        self.sent.append(user_json)
        return self._responses.pop(0)


def test_expansion_round_returns_full_redacted_evidence():
    """Acceptance (AGR-06): a bounded second pass retrieves the FULL text of
    requested events (redacted), and unknown ids are rejected explicitly."""
    full_cmd = "nginx -t -c /etc/nginx/nginx.conf && echo config-ok-details-" + "y" * 300
    ev = DerivedEvent(event_id="evt_nginx", run_id="r", source_capture_id="c",
                      sequence=1, source_step_ids=["s"], event_type="tool_call",
                      actor="agent", payload={"content": full_cmd})
    ctx = _ctx([], events=[ev], anchor_ids=["evt_nginx"])
    rev = _FakeProvider([
        {"expansion_requests": [{"event_ids": ["evt_nginx", "evt_ghost"], "reason": "need the command"}]},
        {"moments": []},
    ])
    rev.propose(ctx)
    # Two rounds were spent; the second carried the FULL event text.
    assert len(rev.sent) == 2
    second = json.loads(rev.sent[1])
    ev_block = second["expansion"]["evidence"][0]
    assert ev_block["event_id"] == "evt_nginx"
    assert ev_block["content"] == full_cmd
    # Unknown id rejected with an explicit reason, never guessed.
    assert second["expansion"]["rejected"] == [
        {"event_id": "evt_ghost", "reason": "unknown event id — not in the captured evidence"}
    ]
    # Telemetry records the requested evidence and both rounds; every provider
    # round carries its cost measurements.
    kinds = [t["kind"] for t in rev.telemetry]
    assert kinds.count("propose") == 1 and kinds.count("expansion") == 1
    assert all("latency_ms" in t for t in rev.telemetry if t["kind"].endswith(("propose", "propose_expanded", "revise")))


def test_pipeline_preserves_deterministic_baseline_on_model_failure(tmp_path, load_fixture):
    """Acceptance (AGR-06): invalid model output produces an explicit error
    state; the deterministic baseline is preserved and served."""
    class _Broken(ScriptedReviewer):
        review_mode = "model_enriched"

        def propose(self, ctx):
            raise ModelOutputError("model response was not parsable as a JSON object")

    store = Store(str(tmp_path / "store"))
    a = analyze(load_fixture("chess_best_move.atif.json"), store, reviewer=_Broken({}, source="model:broken"))
    # Moments still served — the deterministic baseline.
    assert any(m.selected for m in a.review_moments)
    # The error state is explicit, separate, and names the reviewer.
    errors = store.read_derived(a.run_source.run_id, a.run_source.source_capture_id, "review_errors.json")
    assert errors and errors[-1]["error_type"] == "ModelOutputError"
    assert errors[-1]["reviewer_key"] == "model:broken"
    # The model's review slot was never written — no failure disguised as a review.
    assert store.list_reviews(a.run_source.run_id, a.run_source.source_capture_id) == ["deterministic"]
    # The read view names the state.
    view = read.get_review(store, a.run_source.run_id)
    assert view["review_status"] == "review_failed"
    assert view["review_errors"]


def test_rejection_reasons_recorded_in_telemetry():
    """AGR-06: measurable rejections — which gate dropped each unselected card."""
    checks = [_check("C3", "failed")]
    facts = [{"type": "requirement_status", "check_id": "C3", "status_at_submission": "failed"}]
    payload = {"moments": [_moment("cand_C3", ["C3"], facts,
                                   root_cause_candidates=[{"locus": "agent_policy", "rank": 1, "rationale": "gave up"}],
                                   better_action="try again")]}
    telemetry: dict = {}
    run_reviewer(_ctx([_cand("cand_C3", affected_checks=["C3"])],
                      checks=checks, slices=[_slice("C3", "dependency_linked")]),
                 ScriptedReviewer(payload), telemetry=telemetry)
    assert telemetry["selected"] >= 1
    assert "rejections" in telemetry


# --- F2: a private key never survives to the outbound provider payload ------

_SYNTHETIC_KEY = (
    "-----BEGIN RSA PRIVATE KEY-----\n"
    "MIIEpAIBAAKCAQEAsynthetic0000000000000000000000000000000000000\n"
    "QKBgQCthisIsNotARealKeyJustSyntheticBytesForTestingRedactionOnly\n"
    "-----END RSA PRIVATE KEY-----"
)
_KEY_BODY_FRAGMENT = "MIIEpAIBAAKCAQEAsynthetic0000000000000000000000000000000000000"


def test_private_key_never_reaches_provider_in_initial_packet():
    """No real credentials or network calls: the 'model' is _FakeProvider, an
    offline stub that only records what it was sent."""
    ev = DerivedEvent(event_id="evt_leak", run_id="r", source_capture_id="c",
                      sequence=1, source_step_ids=["s"], event_type="tool_call",
                      actor="agent", payload={"content": f"cat id_rsa\n{_SYNTHETIC_KEY}"})
    ctx = _ctx([], events=[ev], anchor_ids=["evt_leak"])
    rev = _FakeProvider([{"moments": []}])
    rev.propose(ctx)

    assert len(rev.sent) == 1
    assert _KEY_BODY_FRAGMENT not in rev.sent[0]
    assert "-----BEGIN RSA PRIVATE KEY-----" not in rev.sent[0]
    assert "[REDACTED:private_key]" in rev.sent[0]


def test_private_key_never_reaches_provider_in_evidence_expansion():
    ev = DerivedEvent(event_id="evt_leak", run_id="r", source_capture_id="c",
                      sequence=1, source_step_ids=["s"], event_type="tool_call",
                      actor="agent", payload={"content": f"cat id_rsa\n{_SYNTHETIC_KEY}"})
    ctx = _ctx([], events=[ev], anchor_ids=["evt_leak"])
    rev = _FakeProvider([
        {"expansion_requests": [{"event_ids": ["evt_leak"], "reason": "need the file"}]},
        {"moments": []},
    ])
    rev.propose(ctx)

    assert len(rev.sent) == 2
    for sent in rev.sent:
        assert _KEY_BODY_FRAGMENT not in sent
        assert "-----BEGIN RSA PRIVATE KEY-----" not in sent
    second = json.loads(rev.sent[1])
    assert second["expansion"]["evidence"][0]["event_id"] == "evt_leak"
    assert "[REDACTED:private_key]" in second["expansion"]["evidence"][0]["content"]


def test_private_key_never_reaches_provider_in_revision_request():
    cand = _cand("cand_1", structured_facts=[
        {"type": "requirement_status", "check_id": "C1", "status_at_submission": "failed"},
    ])
    ctx = _ctx([cand])
    rev = _FakeProvider([{"moments": []}])

    errors = [{"reason": "bad fact", "detail": f"leaked key in log: {_SYNTHETIC_KEY}"}]
    rev.revise(cand, errors, ctx)

    assert len(rev.sent) == 1
    assert _KEY_BODY_FRAGMENT not in rev.sent[0]
    assert "-----BEGIN RSA PRIVATE KEY-----" not in rev.sent[0]
    assert "[REDACTED:private_key]" in rev.sent[0]


def test_propose_never_calls_the_provider_when_budget_is_not_met(monkeypatch):
    """AGR-10/AGR-11: build_packet's own enforced-budget result must gate the
    provider call — a packet that does not fit the effective budget even at
    the excerpt floor (budget_met: False) must never reach the provider."""
    from agr import model_reviewer as mr

    real_build_packet = mr.build_packet
    monkeypatch.setattr(mr, "build_packet",
                        lambda ctx: real_build_packet(ctx, budget_chars=50))

    rev = _FakeProvider([{"moments": []}])
    ev = DerivedEvent(event_id="evt_sub", run_id="r", source_capture_id="c",
                      sequence=1, source_step_ids=["s"], event_type="tool_call",
                      actor="agent", payload={"content": "x" * 2000})
    ctx = _ctx([_cand("cand_1")], events=[ev])

    with pytest.raises(mr.PacketBudgetExceededError):
        rev.propose(ctx)
    assert rev.sent == []  # the provider stub was never called
    assert rev.telemetry[-1]["kind"] == "propose_skipped"
    assert rev.telemetry[-1]["reason"] == "packet_budget_exceeded"


def test_revise_returns_none_without_calling_the_provider_when_budget_is_not_met(monkeypatch):
    """The revise round honours the same gate, but degrades to 'no correction'
    (None) rather than raising — a skipped revise must not abort the whole
    review and lose every other already-proposed moment with it."""
    from agr import model_reviewer as mr

    real_build_packet = mr.build_packet
    monkeypatch.setattr(mr, "build_packet",
                        lambda ctx: real_build_packet(ctx, budget_chars=50))

    rev = _FakeProvider([])
    ev = DerivedEvent(event_id="evt_sub", run_id="r", source_capture_id="c",
                      sequence=1, source_step_ids=["s"], event_type="tool_call",
                      actor="agent", payload={"content": "x" * 2000})
    cand = _cand("cand_1")
    ctx = _ctx([cand], events=[ev])

    result = rev.revise(cand, [{"reason": "bad fact"}], ctx)
    assert result is None
    assert rev.sent == []  # the provider stub was never called
    assert rev.telemetry[-1]["kind"] == "revise_skipped"


def test_expanded_request_never_exceeds_budget_even_when_the_initial_packet_did(monkeypatch):
    """Follow-up review finding: the initial-packet budget gate does not
    protect the SECOND (expanded) request. A packet can fit the budget on
    its own while the expansion round's evidence — up to its own
    _EXPANSION_MAX_CHARS on top — pushes the combined request well past it.
    That combined request must never reach the provider."""
    from agr import model_reviewer as mr

    # 40 events ~32k chars: the initial packet fits the effective budget, while
    # the expansion round's grant (~16k) pushes the combined request past it.
    # (The count tracks the builder's reserve; GR-1 raised it to cover the
    # larger system prompt, so the fitting packet is smaller than before.)
    events = [
        DerivedEvent(event_id=f"evt_{i}", run_id="r", source_capture_id="c",
                     sequence=i + 1, source_step_ids=[f"s{i}"], event_type="tool_call",
                     actor="agent", payload={"content": "x" * 700, "tool": "Bash"})
        for i in range(40)
    ]
    ctx = _ctx([], events=events)
    packet, red = mr.build_packet(ctx)
    assert red["budget_met"] is True  # the initial packet fits on its own

    rev = _FakeProvider([
        {"expansion_requests": [{"event_ids": [f"evt_{i}" for i in range(20)], "reason": "need detail"}]},
    ])

    with pytest.raises(mr.PacketBudgetExceededError):
        rev.propose(ctx)
    # The first (fitting) call went out; the oversized expanded call did not.
    assert len(rev.sent) == 1
    assert rev.telemetry[-1]["kind"] == "propose_expanded_skipped"
    assert rev.telemetry[-1]["reason"] == "packet_budget_exceeded"


def test_revise_request_never_exceeds_budget_even_when_the_bare_packet_did_not():
    """AGR-11 (2026-09-08 review): revise()'s pre-check only measures the bare
    packet before its own 'revise' fields (candidate_id, validation_errors,
    candidate_structured_facts, instruction) are added. A packet that
    comfortably fits the budget on its own can still, once those fields are
    added, produce a real outbound request well past the declared budget —
    the reproduction found a 49,083-char packet plus revision fields and the
    system prompt totalling 70,483 chars against a 60,000-char budget, sent
    with no budget error raised. The shared _call() boundary must catch this
    too, not just the bare-packet pre-check."""
    from agr import model_reviewer as mr

    rev = _FakeProvider([])
    ev = DerivedEvent(event_id="evt_sub", run_id="r", source_capture_id="c",
                      sequence=1, source_step_ids=["s"], event_type="tool_call",
                      actor="agent", payload={"content": "small"})
    cand = _cand("cand_1")
    ctx = _ctx([cand], events=[ev])

    _, redaction = mr.build_packet(ctx)
    assert redaction["budget_met"] is True  # the bare packet comfortably fits

    huge_errors = [{"reason": "x" * 70_000}]
    result = rev.revise(cand, huge_errors, ctx)
    assert result is None
    assert rev.sent == []  # the provider stub was never called
    assert rev.telemetry[-1]["kind"] == "revise_skipped"
    assert rev.telemetry[-1]["reason"] == "packet_budget_exceeded"


# --- GR-1: general per-tag guidance replaces the run-specific drift block -----


def test_system_prompt_has_no_run_specific_drift_block():
    """GR-1: the raman-specific "DRIFT TEST" instructions are gone."""
    from agr.model_reviewer import SYSTEM_PROMPT

    assert "DRIFT TEST" not in SYSTEM_PROMPT
    assert "raman" not in SYSTEM_PROMPT.lower()
    assert "prolonged off-task investigation" not in SYSTEM_PROMPT


def test_system_prompt_renders_guidance_for_every_behaviour_tag():
    """GR-1: every taxonomy tag the reviewer may emit has prompt guidance."""
    from agr import taxonomy
    from agr.model_reviewer import SYSTEM_PROMPT

    assert set(taxonomy.BEHAVIOUR_TAG_GUIDANCE) == taxonomy.BEHAVIOUR_TAGS
    missing = [t for t in sorted(taxonomy.BEHAVIOUR_TAGS) if t not in SYSTEM_PROMPT]
    assert not missing, f"behaviour tags with no prompt guidance: {missing}"


def test_system_prompt_blesses_abstention_and_five_moment_cap():
    """GR-1: abstention is first-class and the total cap is five (spec §8.10)."""
    from agr.model_reviewer import SYSTEM_PROMPT

    low = SYSTEM_PROMPT.lower()
    assert "no decisive moment established" in low
    assert '{"moments": []}' in SYSTEM_PROMPT
    assert "at most five" in low


def test_packet_reserve_covers_system_prompt():
    """GR-1: the packet builder's reserve must cover the real system prompt plus
    slack, or a packet built to the advertised effective budget and then sent
    with the prompt would blow _call()'s hard request gate."""
    from agr import model_packet
    from agr.model_reviewer import SYSTEM_PROMPT

    assert len(SYSTEM_PROMPT) + 1_500 <= model_packet._BUDGET_RESERVE_CHARS


def test_a_quote_that_matches_only_summary_prose_is_dropped_and_counted():
    """GR-1: a chunk summary is never evidence. A moment whose only quote does
    not appear in the original event text is dropped, and the drop is counted
    as ``summary_not_evidence`` (not a generic fact failure)."""
    ev = DerivedEvent(event_id="evt_1", run_id="r", source_capture_id="c",
                      sequence=1, source_step_ids=["s"], event_type="tool_call",
                      actor="agent", payload={"content": "the real original event text"})
    cand = _cand("cand_1", anchor_event_ids=["evt_1"], structured_facts=[
        {"type": "event_support",
         "quotes": [{"event_id": "evt_1", "quote": "a claim copied from the chunk summary"}]}])
    ctx = _ctx([cand], events=[ev])
    telemetry = {}
    moments = run_reviewer(ctx, telemetry=telemetry)
    assert not any(m.selected for m in moments)
    assert telemetry["rejections"] == {"summary_not_evidence": 1}


# --- GR-3: category coverage prompt block + run-scoped argument shapes --------


def test_system_prompt_carries_category_coverage_and_judging_rule():
    from agr import taxonomy
    from agr.model_reviewer import SYSTEM_PROMPT

    assert "CATEGORY COVERAGE" in SYSTEM_PROMPT
    # Every idea-category tag is named, so the reviewer is prompted to consider it.
    for spec in taxonomy.IDEA_CATEGORIES.values():
        for tag in spec["tags"]:
            assert tag in SYSTEM_PROMPT, tag
    # The judging-vs-suggesting distinction is explicit.
    assert "information cutoff constrains only a SUGGESTED alternative" in SYSTEM_PROMPT
    assert "argument_shapes" in SYSTEM_PROMPT


def test_packet_carries_run_scoped_argument_shapes():
    call = DerivedEvent(
        event_id="evt_c", run_id="r", source_capture_id="c", sequence=1,
        source_step_ids=["s1"], event_type="tool_call", actor="agent",
        payload={"tool": "Edit", "tool_use_id": "t1",
                 "tool_input": {"file_path": "/a.py", "old_string": "x", "new_string": "y"}})
    result = DerivedEvent(
        event_id="evt_r", run_id="r", source_capture_id="c", sequence=2,
        source_step_ids=["s2"], event_type="tool_result", actor="tool",
        payload={"status": "error", "tool_use_id": "t1",
                 "content": "String to replace not found in file."})
    packet, _red = build_packet(_ctx([], events=[call, result]))
    groups = packet["argument_shapes"]
    assert groups, "the run's failing-call shape distribution must be in the packet"
    assert groups[0]["key"][0] == "Edit"
    assert groups[0]["total_failing_calls"] == 1
    # Structure only — the retained value never travels in the packet.
    assert "/a.py" not in json.dumps(groups)
