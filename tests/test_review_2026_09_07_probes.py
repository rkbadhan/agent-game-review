"""Review 2026-09-07 acceptance probes.

R1 — verifier-optional supported observations: importing a failed-test Claude
session WITHOUT a verifier must surface the supported observation (tool
evidence, coverage limits) while the overall task outcome stays unknown, and
it must never assert the final implementation is incorrect.

R4 — reviewer selection/error isolation: a healthy reviewer must be served
over a stale errored slot; the deterministic baseline must be served (not a
stale model snapshot) when the model slot is failing; and an explicitly
requested healthy reviewer must not read as failed because a DIFFERENT
reviewer has an active error.
"""

import json
import os

from agr import read
from agr.ingest_claude import convert
from agr.model_reviewer import ScriptedReviewer
from agr.pipeline import analyze
from agr.read import _default_reviewer_key
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")
FIXTURE = "chess_best_move.atif.json"


def _claude_failed_test_session(tmp_path):
    """Task, a failed pytest call, then 'Done' — no verifier anywhere."""
    lines = [
        {"type": "user", "sessionId": "sess-r1-1",
         "message": {"role": "user", "content": "Fix the failing test in tests/."}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-4-8",
         "content": [{"type": "tool_use", "id": "t1", "name": "Bash",
                      "input": {"command": "python -m pytest tests/"}}]}},
        {"type": "user", "message": {"role": "user", "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": "E   assert 404 == 200",
             "is_error": True}]}},
        {"type": "assistant", "message": {"role": "assistant", "model": "claude-opus-4-8",
         "content": [{"type": "text", "text": "Done."}]}},
    ]
    return convert(_write(tmp_path, lines)).doc


def _write(tmp_path, lines):
    p = tmp_path / "session.jsonl"
    p.write_text("\n".join(json.dumps(l) for l in lines), encoding="utf-8")
    return p


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _tmpdir():
    import tempfile
    return tempfile.mkdtemp(prefix="agr-probe-")


def _mirror_payload():
    scratch = Store(_tmpdir())
    a = analyze(_load(FIXTURE), scratch)
    moments = read.get_review(scratch, a.run_source.run_id)["review_moments"]
    return {"moments": [{
        "candidate_id": m["candidate_id"],
        "anchor_event_ids": m["anchor_event_ids"],
        "kind": m["kind"],
        "polarity": m["polarity"],
        "affected_checks": m["affected_checks"],
        "structured_facts": [{k: v for k, v in f.items()
                              if k not in ("validation", "recomputed")}
                             for f in m.get("validated_facts", [])],
    } for m in moments if m.get("selected")]}


# --- R1: the supported observation without a task-level eval -----------------

def test_failed_test_session_surfaces_a_supported_observation(tmp_path):
    doc = _claude_failed_test_session(tmp_path)
    store = Store(str(tmp_path / "store"))
    a = analyze(doc, store)
    run_id = a.run_source.run_id

    # Overall task outcome stays unknown — no verifier, no claims.
    view = read.get_review(store, run_id)
    assert view["outcome"]["status"] == "UNVERIFIED"

    # The supported failure concern IS selected: a tool-evidence-backed
    # observation, with the coverage limit stated — no check/contract link
    # exists, and the card says so instead of pretending otherwise.
    selected = [m for m in view["review_moments"] if m["selected"]]
    obs = [m for m in selected
           if m["gate_results"].get("observation_basis") == "tool_evidence"]
    assert obs, "the failed-tool observation must be selected without a verifier"
    m = obs[0]
    assert m["polarity"] == "negative"
    assert m["gate_results"]["contract_link"] == "absent"
    statement = m["rendered_statement"].lower()
    assert "failed" in statement
    # Coverage limit + outcome honesty: the capture does not know whether the
    # task's final state is correct, and the card never claims it does.
    assert "no resolving check" in statement
    assert "remains unknown" in statement
    assert "was still failing at submission" not in statement
    assert "was left unresolved before submission" not in statement


def test_with_a_verifier_the_unlinked_negative_stays_gated(tmp_path):
    """The observation path fills the NO-verifier gap only: with a verifier
    present, concern linkage is establishable and §8.10 keeps applying."""
    doc = _claude_failed_test_session(tmp_path)
    doc["verifier"] = {
        "raw_output": "C1 FAILED",
        "checks": [{"check_id": "C1", "name": "tests pass", "status": "failed",
                    "source": "native_structured"}],
    }
    store = Store(str(tmp_path / "store"))
    a = analyze(doc, store)
    view = read.get_review(store, a.run_source.run_id)
    unlinked = [m for m in view["review_moments"]
                if m["gate_results"].get("contract_link") == "absent"
                and m["gate_results"].get("observation_basis") == "tool_evidence"]
    assert not unlinked


# --- R4: reviewer selection and error isolation ------------------------------


def test_default_serves_the_baseline_not_a_stale_errored_model_slot(tmp_path):
    """Success → failure with both baseline and model slots present: the
    default serves the deterministic baseline (the stale model snapshot is
    not silently served while the UI says the baseline is shown), and the
    state names the enrichment failure."""
    store = Store(str(tmp_path / "store"))
    ok = ScriptedReviewer(_mirror_payload(), source="model:a")

    class _Broken(ScriptedReviewer):
        review_mode = "model_enriched"

        def propose(self, ctx):
            raise RuntimeError("provider unavailable")

    analyze(_load(FIXTURE), store)                            # deterministic baseline
    analyze(_load(FIXTURE), store, reviewer=ok)               # model:a succeeds
    analyze(_load(FIXTURE), store, reviewer=_Broken({}, source="model:a"))  # then fails

    view = read.get_review(store, "chess_best_move__seed42")
    assert view["served_reviewer_key"] == "deterministic"
    assert view["requested_reviewer_key"] is None
    # The served snapshot content IS the deterministic baseline: no model
    # enrichment on any served card.
    assert all(not m.get("enrichment_source") for m in view["moments"])
    assert view["review_status"] == "failed"
    assert [e["reviewer_key"] for e in view["review_errors"]] == ["model:a"]


def test_requested_healthy_reviewer_is_not_contaminated_by_another_models_error(tmp_path):
    """model:A has an active error; model:B succeeds. Explicitly requesting
    model:B must serve B's snapshot with review_status ok — A's error stays
    visible as A's status, never as the viewed review's state."""
    store = Store(str(tmp_path / "store"))
    a_ok = ScriptedReviewer(_mirror_payload(), source="model:a")
    b_ok = ScriptedReviewer(_mirror_payload(), source="model:b")

    class _Broken(ScriptedReviewer):
        review_mode = "model_enriched"

        def propose(self, ctx):
            raise RuntimeError("provider unavailable")

    analyze(_load(FIXTURE), store)
    analyze(_load(FIXTURE), store, reviewer=a_ok)
    analyze(_load(FIXTURE), store, reviewer=_Broken({}, source="model:a"))
    analyze(_load(FIXTURE), store, reviewer=b_ok)

    view = read.get_review(store, "chess_best_move__seed42", reviewer_key="model:b")
    assert view["served_reviewer_key"] == "model:b"
    assert view["requested_reviewer_key"] == "model:b"
    assert view["review_status"] == "ok"
    assert view["review_errors"] == []
    # A's active error is preserved as A's status — visible, not contaminating.
    assert [e["reviewer_key"] for e in view["other_reviewer_errors"]] == ["model:a"]


def test_default_prefers_a_healthy_model_slot_over_the_baseline(tmp_path):
    """model:A fails, model:B succeeds, no explicit request: the default
    serves the healthy model:B review — not the baseline, not a stale slot."""
    store = Store(str(tmp_path / "store"))
    b_ok = ScriptedReviewer(_mirror_payload(), source="model:b")

    class _Broken(ScriptedReviewer):
        review_mode = "model_enriched"

        def propose(self, ctx):
            raise RuntimeError("provider unavailable")

    analyze(_load(FIXTURE), store)
    analyze(_load(FIXTURE), store, reviewer=_Broken({}, source="model:a"))
    analyze(_load(FIXTURE), store, reviewer=b_ok)

    view = read.get_review(store, "chess_best_move__seed42")
    assert view["served_reviewer_key"] == "model:b"
    assert view["review_status"] == "ok"
    assert view["review_errors"] == []
    assert [e["reviewer_key"] for e in view["other_reviewer_errors"]] == ["model:a"]


def test_errored_slot_served_only_when_no_baseline_exists():
    """The last-resort fallback: with NO deterministic baseline at all, an
    errored model slot is still served (an errored slot beats nothing)."""
    class _FakeStore:
        def list_reviews(self, run_id, capture_id):
            return ["model:a"]

        def read_review_slot(self, run_id, capture_id, key):
            return []

    class _FakeRead:
        @staticmethod
        def _read(store, run_id, capture_id, name, default):
            return default

    import agr.read as r
    original = r._read
    r._read = lambda store, run_id, capture_id, name, default: default
    try:
        assert r._default_reviewer_key(_FakeStore(), "r", "c") == "model:a"
    finally:
        r._read = original
