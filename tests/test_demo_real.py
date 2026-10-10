"""GR-4: the offline demo — real runs + pre-computed model reviews.

The point of GR-4 is that ``agr demo`` shows real model reviews with **no API
key and no model call**. These tests exercise the committed dataset, the
offline rebuild, and the bake→rebuild round trip.
"""

from __future__ import annotations

import json
import os

import pytest

from agr import demo, read
from agr.model_reviewer import ScriptedReviewer
from agr.pipeline import analyze
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")


def _fixture(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def test_the_committed_real_demo_dataset_is_present_and_well_formed():
    real_dir = demo.find_real_demo_dir()
    assert real_dir is not None, "the GR-4 real demo dataset must ship with the package"
    runs = demo._load_json_dir(os.path.join(real_dir, "runs"))
    reviews = demo._load_json_dir(os.path.join(real_dir, "reviews"))
    assert len(runs) >= 10 and len(reviews) >= 10
    # Every baked review belongs to a shipped run. Not every run needs one: the
    # tau3 set ships a passing sibling per failing run so the divergence view
    # has a passing run to compare against (checked in the next test).
    run_ids = {doc["run"]["logical_run_id"] for doc in runs}
    assert {review["run_id"] for review in reviews} <= run_ids
    for doc in runs:
        # The exported source is a real ATIF document the pipeline can analyze.
        assert isinstance(doc.get("run"), dict) and doc.get("steps")
    for review in reviews:
        assert review["run_id"] and review["reviewer_key"].startswith("model:")
        # Reviewer model + date travel with every pre-computed review.
        assert review["model"] and review["reviewed_at"]
        assert isinstance(review["moments"], list)


def test_build_real_demo_store_serves_a_precomputed_model_review(tmp_path):
    store = Store(str(tmp_path / "store"))
    definition = demo.build_real_demo_store(store)
    assert definition["real_runs"] >= 10
    assert 10 <= definition["model_reviews"] <= definition["real_runs"]
    # A run ships without a baked review only as a passing comparison sibling;
    # every non-passing run carries its pre-computed model review.
    real_dir = demo.find_real_demo_dir()
    real_ids = {doc["run"]["logical_run_id"]
                for doc in demo._load_json_dir(os.path.join(real_dir, "runs"))}
    for row in read.list_runs(store):
        # The synthetic comparison slice never carries baked reviews.
        if row["run_id"] not in real_ids or (row["outcome"] or {}).get("status") == "PASSED":
            continue
        keys = store.list_reviews(row["run_id"], row["capture_id"])
        assert any(k.startswith("model:") for k in keys), row["run_id"]
    landing = definition["landing_run"]
    assert landing

    review = read.get_review(store, landing)
    # No model was called: the baked proposals passed current offline validation.
    assert review["review_mode"] == "model_enriched"
    assert review["review_status"] in ("moments_found", "no_decisive_moment")
    assert review["served_reviewer_key"].startswith("model:")
    assert review["review_model"] and review["reviewed_at"]
    # The landing run is a real failed run (the demo's whole framing).
    assert (review.get("outcome") or {}).get("status") != "PASSED"
    assert review["moments"]


def test_every_installed_review_resolves_against_its_freshly_ingested_run(tmp_path):
    """A baked moment's anchors and quotes must name real events in the store the
    demo builds — and a quote must actually appear in that event's text. The
    review is revalidated offline, including references and quote authenticity,
    so its derivation identity and source text must still line up."""
    from agr.reviewer import _norm_text, _quotable_text
    from agr.schema import DerivedEvent

    store = Store(str(tmp_path / "store"))
    demo.build_real_demo_store(store, include_comparison=False)
    for row in read.list_runs(store):
        run_id, capture_id = row["run_id"], row["capture_id"]
        model_keys = [k for k in store.list_reviews(run_id, capture_id) if k.startswith("model:")]
        if not model_keys:
            continue
        raw = store.read_derived(run_id, capture_id, "events.json") or []
        events = [DerivedEvent(**e) for e in raw]
        by_id = {e.event_id: e for e in events}
        for key in model_keys:
            for moment in store.read_review_slot(run_id, capture_id, key):
                refs = list(moment.get("anchor_event_ids") or [])
                for fact in moment.get("validated_facts") or []:
                    refs += list(fact.get("events") or [])
                    refs += [q.get("event_id") for q in (fact.get("quotes") or [])]
                missing = [r for r in refs if r and r not in by_id]
                assert not missing, f"{run_id} {key}: {missing}"
                # The quote text itself must still be present in the event.
                for fact in moment.get("validated_facts") or []:
                    for q in fact.get("quotes") or []:
                        ev = by_id.get(q.get("event_id"))
                        quote = _norm_text(q.get("quote") or "")
                        assert ev is not None and quote and quote in _norm_text(_quotable_text(ev)), \
                            f"{run_id} {key}: quote no longer present in {q.get('event_id')}"


def test_a_review_whose_source_changed_is_rejected_not_served(tmp_path):
    """PR #93 review: installed, not re-validated — so a baked review whose
    source_hash does not match the freshly-ingested run must be dropped."""
    source = Store(str(tmp_path / "source"))
    analyze(_fixture("chess_best_move.atif.json"), source,
            reviewer=ScriptedReviewer({"moments": []},
                                      source="model:accounts/fireworks/models/kimi-k3"))
    out = str(tmp_path / "baked")
    demo.bake_reviews(source, out)
    (review_path,) = [os.path.join(out, "reviews", n) for n in os.listdir(os.path.join(out, "reviews"))]
    review = json.load(open(review_path, encoding="utf-8"))
    review["source_hash"] = "sha256:0000000000000000000000000000000000000000000000000000000000000000"
    with open(review_path, "w", encoding="utf-8") as fh:
        json.dump(review, fh)

    rebuilt = Store(str(tmp_path / "rebuilt"))
    definition = demo.build_real_demo_store(rebuilt, real_dir=out, include_comparison=False)
    assert definition["stale_reviews"] == 1 and definition["model_reviews"] == 0
    (row,) = read.list_runs(rebuilt)
    assert read.get_review(rebuilt, row["run_id"])["served_reviewer_key"] == "deterministic"


def test_a_review_with_a_missing_hash_is_rejected(tmp_path):
    """PR #93 review: an unverifiable review (no hash on either side) must not be
    installed — a matching, non-empty hash is required."""
    source = Store(str(tmp_path / "source"))
    analyze(_fixture("chess_best_move.atif.json"), source,
            reviewer=ScriptedReviewer({"moments": []},
                                      source="model:accounts/fireworks/models/kimi-k3"))
    out = str(tmp_path / "baked")
    demo.bake_reviews(source, out)
    (path,) = [os.path.join(out, "reviews", n) for n in os.listdir(os.path.join(out, "reviews"))]
    review = json.load(open(path, encoding="utf-8"))
    review["source_hash"] = None
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(review, fh)
    rebuilt = Store(str(tmp_path / "rebuilt"))
    definition = demo.build_real_demo_store(rebuilt, real_dir=out, include_comparison=False)
    assert definition["stale_reviews"] == 1 and definition["model_reviews"] == 0


def test_a_rejected_review_does_not_leave_a_previous_slot_served(tmp_path):
    """PR #93 review: rebuilding into the SAME store must clear an older model
    slot so a review rejected as stale cannot remain served."""
    source = Store(str(tmp_path / "source"))
    analyze(_fixture("chess_best_move.atif.json"), source,
            reviewer=ScriptedReviewer({"moments": []},
                                      source="model:accounts/fireworks/models/kimi-k3"))
    out = str(tmp_path / "baked")
    demo.bake_reviews(source, out)

    store = Store(str(tmp_path / "reused"))
    first = demo.build_real_demo_store(store, real_dir=out, include_comparison=False)
    assert first["model_reviews"] == 1
    (row,) = read.list_runs(store)
    key = "model:accounts/fireworks/models/kimi-k3"
    assert key in store.list_reviews(row["run_id"], row["capture_id"])

    # Corrupt the baked hash and rebuild into the SAME store.
    (path,) = [os.path.join(out, "reviews", n) for n in os.listdir(os.path.join(out, "reviews"))]
    review = json.load(open(path, encoding="utf-8"))
    review["source_hash"] = "sha256:1111111111111111111111111111111111111111111111111111111111111111"
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(review, fh)
    second = demo.build_real_demo_store(store, real_dir=out, include_comparison=False)
    assert second["stale_reviews"] == 1 and second["model_reviews"] == 0
    assert key not in store.list_reviews(row["run_id"], row["capture_id"])
    assert read.get_review(store, row["run_id"])["served_reviewer_key"] == "deterministic"


def test_an_empty_rebake_keeps_the_existing_dataset(tmp_path):
    """PR #93 review: a re-bake with nothing eligible must not wipe the dataset."""
    out = str(tmp_path / "dataset")
    store = Store(str(tmp_path / "source"))
    analyze(_fixture("chess_best_move.atif.json"), store,
            reviewer=ScriptedReviewer({"moments": []},
                                      source="model:accounts/fireworks/models/kimi-k3"))
    demo.bake_reviews(store, out)
    before = sorted(os.listdir(os.path.join(out, "reviews")))
    assert before

    empty = Store(str(tmp_path / "empty"))
    analyze(_fixture("ignored_failure.atif.json"), empty)  # no model review
    result = demo.bake_reviews(empty, out)
    assert result["runs"] == 0
    assert sorted(os.listdir(os.path.join(out, "reviews"))) == before


def test_real_demo_never_mutates_the_source_and_clears_the_watermark_via_a_record(tmp_path):
    """PR #93 review: no demo confirmation is injected into the real source."""
    store = Store(str(tmp_path / "store"))
    definition = demo.build_real_demo_store(store, include_comparison=False)
    run_id = definition["landing_run"]
    capture_id = store.latest_capture_id(run_id)
    doc = store.read_source(run_id, capture_id)
    assert "contract_confirmation" not in (doc.get("task") or {})
    assert store.has_derived(run_id, capture_id, "contract_confirmation.json")
    # Cleared under its OWN status — never human_confirmed (PR #93 review).
    assert store.read_derived(run_id, capture_id, "contract.json")["status"] == "demo_confirmed"
    review = read.get_review(store, run_id)
    assert review["contract_status"] == "demo_confirmed"
    assert review["contract_demo_override"] is True
    # No item claims a human decision: the demo override is contract-level only.
    assert review["contract"]["items"]
    assert all(i.get("human_status") != "confirmed"
               for i in review["contract"]["items"])


def test_bake_reviews_skips_a_reviewer_whose_latest_attempt_failed(tmp_path):
    """PR #93 review: a failed/incomplete retry leaves the older slot on disk; the
    exporter must not resurrect it."""
    store = Store(str(tmp_path / "source"))
    analyze(_fixture("chess_best_move.atif.json"), store,
            reviewer=ScriptedReviewer({"moments": []},
                                      source="model:accounts/fireworks/models/kimi-k3"))
    (row,) = read.list_runs(store)
    key = "model:accounts/fireworks/models/kimi-k3"
    store.write_derived(row["run_id"], row["capture_id"], "review_attempts.json", [
        {"attempt_id": "att_0001", "reviewer_key": key, "outcome": "ok"},
        {"attempt_id": "att_0002", "reviewer_key": key, "outcome": "failed"},
    ])
    assert demo.bake_reviews(store, str(tmp_path / "stale"))["runs"] == 0

    # An active error also disqualifies the reviewer.
    store.write_derived(row["run_id"], row["capture_id"], "review_attempts.json", [
        {"attempt_id": "att_0001", "reviewer_key": key, "outcome": "ok"}])
    store.write_derived(row["run_id"], row["capture_id"], "review_errors.json", [
        {"reviewer_key": key, "error_type": "ModelOutputError", "message": "boom"}])
    assert demo.bake_reviews(store, str(tmp_path / "errored"))["runs"] == 0


def test_rebaking_replaces_the_dataset_and_drops_stale_files(tmp_path):
    """PR #93 review: a re-bake must not leave files from a previous corpus behind."""
    out = str(tmp_path / "dataset")
    a = Store(str(tmp_path / "a"))
    analyze(_fixture("chess_best_move.atif.json"), a,
            reviewer=ScriptedReviewer({"moments": []},
                                      source="model:accounts/fireworks/models/kimi-k3"))
    demo.bake_reviews(a, out)
    first = set(os.listdir(os.path.join(out, "reviews")))

    b = Store(str(tmp_path / "b"))
    analyze(_fixture("ignored_failure.atif.json"), b,
            reviewer=ScriptedReviewer({"moments": []},
                                      source="model:accounts/fireworks/models/kimi-k3"))
    demo.bake_reviews(b, out)
    second = set(os.listdir(os.path.join(out, "reviews")))
    assert first and second and first != second
    assert not (first & second), "a re-bake left the previous corpus's files behind"


def test_bake_reviews_round_trips_a_store_into_an_offline_demo(tmp_path):
    """bake-reviews exports runs + reviews; build_real_demo_store rebuilds them."""
    source_store = Store(str(tmp_path / "source"))
    analyze(_fixture("chess_best_move.atif.json"), source_store,
            reviewer=ScriptedReviewer({"moments": []},
                                      source="model:accounts/fireworks/models/kimi-k3"))

    out = str(tmp_path / "baked")
    result = demo.bake_reviews(source_store, out)
    assert result["runs"] == 1
    assert os.path.isdir(os.path.join(out, "runs")) and os.path.isdir(os.path.join(out, "reviews"))

    rebuilt = Store(str(tmp_path / "rebuilt"))
    definition = demo.build_real_demo_store(rebuilt, real_dir=out, include_comparison=False)
    assert definition["real_runs"] == 1 and definition["model_reviews"] == 1

    (row,) = read.list_runs(rebuilt)
    review = read.get_review(rebuilt, row["run_id"])
    assert review["served_reviewer_key"].startswith("model:")
    assert review["review_model"] == "accounts/fireworks/models/kimi-k3"
    assert review["reviewed_at"]


def test_demo_store_cli_builds_the_real_demo_offline(tmp_path, capsys):
    from agr.cli import main
    rc = main(["--store", str(tmp_path / "store"), "demo-store"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "real demo store" in out
    assert "pre-computed" in out


def test_bake_selects_full_configuration_key_and_keeps_plain_model_name(tmp_path):
    import pytest
    model = "gpt-4o"
    keys = ["model:gpt-4o#aaaaaaaaaaaa", "model:gpt-4o#bbbbbbbbbbbb"]
    store = Store(str(tmp_path / "source"))
    doc = _fixture("chess_best_move.atif.json")
    for key in keys:
        analyze(doc, store, reviewer=ScriptedReviewer({"moments": []}, source=key))
    with pytest.raises(ValueError, match="full reviewer key"):
        demo.bake_reviews(store, str(tmp_path / "ambiguous"), model=model)
    out = str(tmp_path / "baked-config")
    assert demo.bake_reviews(store, out, model=keys[1])["runs"] == 1
    (review,) = demo._load_json_dir(os.path.join(out, "reviews"))
    assert review["reviewer_key"] == keys[1]
    assert review["model"] == model
    assert demo.bake_reviews(store, str(tmp_path / "partial-model"), model="gpt-4")["runs"] == 0


def test_matching_source_hash_does_not_preserve_obsolete_validation_or_selection(tmp_path):
    """A snapshot's old 'passed' flags cannot authenticate fabricated evidence."""
    source = Store(str(tmp_path / "source"))
    analyze(_fixture("chess_best_move.atif.json"), source,
            reviewer=ScriptedReviewer({"moments": []}, source="model:offline"))
    out = str(tmp_path / "baked")
    demo.bake_reviews(source, out, model="offline")
    (path,) = [os.path.join(out, "reviews", n)
               for n in os.listdir(os.path.join(out, "reviews"))]
    with open(path, encoding="utf-8") as fh:
        snapshot = json.load(fh)
    (row,) = read.list_runs(source)
    (anchor, *_) = source.read_derived(row["run_id"], row["capture_id"], "events.json")
    snapshot["moments"] = [{
        "candidate_id": "sem_fabricated", "kind": "behaviour", "polarity": "positive",
        "anchor_event_ids": [anchor["event_id"]], "selected": True,
        "rendered_statement": "This old rendering must not be served",
        "gate_results": {"fact_validation": "passed"},
        "taxonomy_verdict": "Excellent move",
        "validated_facts": [{"type": "event_support", "validation": "passed",
                             "quotes": [{"event_id": anchor["event_id"],
                                         "quote": "A fabricated quote absent from the capture"}]}],
    }]
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh)
    rebuilt = Store(str(tmp_path / "rebuilt"))
    definition = demo.build_real_demo_store(rebuilt, real_dir=out, include_comparison=False)
    assert definition["stale_reviews"] == 0 and definition["model_reviews"] == 1
    served = read.get_review(rebuilt, row["run_id"])
    assert served["moments"] == [] and served["review_status"] == "all_proposals_rejected"
    (rejected,) = served["review_moments"]
    assert rejected["gate_results"]["fact_validation"] == "failed"
    assert rejected["taxonomy_verdict"] is None


@pytest.mark.parametrize("incomplete", [False, True])
def test_failed_replay_never_saves_fallback_under_model_name(tmp_path, monkeypatch, incomplete):
    from agr.reviewer import ReviewBudgetExceededError
    source = Store(str(tmp_path / "source"))
    analyze(_fixture("chess_best_move.atif.json"), source,
            reviewer=ScriptedReviewer({"moments": []}, source="model:offline"))
    baked = str(tmp_path / "baked")
    demo.bake_reviews(source, baked, model="offline")
    store = Store(str(tmp_path / "rebuilt"))
    demo.build_real_demo_store(store, real_dir=baked, include_comparison=False)
    (row,) = read.list_runs(store)
    assert "model:offline" in store.list_reviews(row["run_id"], row["capture_id"])

    def fail(self, ctx):
        if incomplete:
            raise ReviewBudgetExceededError("missing_chunks", "Incomplete baked review")
        raise ValueError("Broken baked review")
    monkeypatch.setattr(ScriptedReviewer, "propose", fail)
    result = demo.build_real_demo_store(store, real_dir=baked, include_comparison=False)
    assert result["model_reviews"] == 0 and result["failed_reviews"] == 1
    assert "model:offline" not in store.list_reviews(row["run_id"], row["capture_id"])
    errors = store.read_derived(row["run_id"], row["capture_id"], "review_errors.json")
    assert errors[0]["reviewer_key"] == "model:offline"
    assert errors[0]["origin"] == "precomputed_revalidation"
    assert bool(errors[0].get("incomplete")) is incomplete


def test_replay_reuses_analysis_writes_model_once_and_adds_no_fake_attempts(tmp_path, monkeypatch):
    source = Store(str(tmp_path / "source"))
    analyze(_fixture("chess_best_move.atif.json"), source,
            reviewer=ScriptedReviewer({"moments": []}, source="model:offline"))
    baked = str(tmp_path / "baked")
    demo.bake_reviews(source, baked, model="offline")
    store = Store(str(tmp_path / "rebuilt"))
    calls, writes = [], []
    original_analyze, original_write = demo.analyze, store.write_review
    def tracked_analyze(*args, **kwargs):
        calls.append(kwargs.get("reviewer"))
        return original_analyze(*args, **kwargs)
    def tracked_write(run_id, capture_id, key, moments):
        writes.append(key)
        return original_write(run_id, capture_id, key, moments)
    monkeypatch.setattr(demo, "analyze", tracked_analyze)
    monkeypatch.setattr(store, "write_review", tracked_write)
    for _ in range(2):
        calls.clear()
        writes.clear()
        result = demo.build_real_demo_store(store, real_dir=baked, include_comparison=False)
        assert result["model_reviews"] == 1
        assert calls == [None, None]
        assert writes.count("model:offline") == 1
    (row,) = read.list_runs(store)
    attempts = store.read_derived(row["run_id"], row["capture_id"], "review_attempts.json")
    assert len(attempts) == 4
    assert all(attempt["reviewer_key"] == "deterministic" for attempt in attempts)

    # A replayed snapshot can be baked again without inventing a model call.
    rebaked = str(tmp_path / "rebaked")
    exported = demo.bake_reviews(store, rebaked, model="offline")
    assert exported["runs"] == 1
    original_review = demo._load_json_dir(os.path.join(baked, "reviews"))[0]
    new_review = demo._load_json_dir(os.path.join(rebaked, "reviews"))[0]
    assert new_review["reviewed_at"] == original_review["reviewed_at"]
