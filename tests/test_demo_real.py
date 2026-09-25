"""GR-4: the offline demo — real runs + pre-computed model reviews.

The point of GR-4 is that ``agr demo`` shows real model reviews with **no API
key and no model call**. These tests exercise the committed dataset, the
offline rebuild, and the bake→rebuild round trip.
"""

from __future__ import annotations

import json
import os

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
    assert len(runs) >= 10 and len(reviews) == len(runs)
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
    assert definition["model_reviews"] == definition["real_runs"]
    landing = definition["landing_run"]
    assert landing

    review = read.get_review(store, landing)
    # No model was called: the served review is the baked snapshot.
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
    review is installed, not re-validated, so this is the guard that the derivation
    identity (and the source text) still lines up."""
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
