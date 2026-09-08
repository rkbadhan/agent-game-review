"""Corpus manifest — reproducible provenance and counting definitions (AGR-01)."""

from __future__ import annotations

import json
import os

from agr import corpus_manifest, version

REPO_ROOT = os.path.join(os.path.dirname(__file__), "..")
EVAL_RUNS = os.path.join(REPO_ROOT, "eval-runs")
REVIEWED_COMMIT = "dea7af10a0093642362d80abbc7637b2c3c12324"


def _build():
    return corpus_manifest.build_manifest(EVAL_RUNS, reviewed_commit=REVIEWED_COMMIT)


# --- against the real published corpus ---------------------------------------


def test_counts_reconcile():
    m = _build()
    c = m["counts"]
    assert c["eligible_trials"] + c["excluded_trials"] == c["discovered_trials"]
    assert c["logical_runs"] == c["active_captures"]


def test_pinned_corpus_size_is_22_logical_runs():
    # The reviewed corpus this backlog is derived from is 22 logical runs
    # (spec: "Treat these 22 runs as development/regression data"). Pinning
    # the exact count here means a future change to discovery, exclusion, or
    # run-id derivation that silently changes what counts as "the 22 runs" is
    # caught immediately rather than drifting unnoticed.
    m = _build()
    assert m["counts"]["logical_runs"] == 22
    assert m["counts"]["eligible_trials"] == 22


def test_run_ids_are_unique_and_match_the_count():
    m = _build()
    assert len(m["logical_run_ids"]) == len(set(m["logical_run_ids"]))
    assert len(m["logical_run_ids"]) == m["counts"]["logical_runs"]


def test_every_excluded_trial_carries_a_reason():
    m = _build()
    for t in m["trials"]:
        if not t["eligible"]:
            assert t["exclusion_reason"]
        else:
            assert t["exclusion_reason"] is None


def test_eligible_trials_carry_checksums_and_identity():
    m = _build()
    eligible = [t for t in m["trials"] if t["eligible"]]
    assert eligible
    for t in eligible:
        assert t["file_checksums"]["trajectory.json"] or t["file_checksums"]["result.json"]
        assert t["run_id"]
        assert t["capture_id"]
        assert t["source_hash"].startswith("sha256:")


def test_provenance_fields_present():
    m = _build()
    assert m["reviewed_commit"] == REVIEWED_COMMIT
    assert m["manifest_version"] == version.CORPUS_MANIFEST_VERSION
    assert m["derivation_versions"]["GOLD_SCHEMA_VERSION"] == version.GOLD_SCHEMA_VERSION
    assert set(corpus_manifest.COUNTING_STAGES) == {
        "discovered_trials", "eligible_trials", "excluded_trials",
        "ingested_captures", "logical_runs", "active_captures",
        "generated_candidates", "validated_candidates",
        "selected_moments", "displayed_moments",
    }


def test_manifest_is_deterministic_modulo_timestamp():
    a, b = _build(), _build()
    a.pop("generated_at")
    b.pop("generated_at")
    assert a == b


def test_write_manifest_round_trips(tmp_path):
    m = _build()
    out = tmp_path / "manifest.json"
    corpus_manifest.write_manifest(m, str(out))
    with open(out, encoding="utf-8") as fh:
        reloaded = json.load(fh)
    assert reloaded["counts"] == m["counts"]


# --- exclusion / conversion-error accounting on a synthetic layout -----------


def test_empty_job_directory_is_excluded_not_dropped(tmp_path):
    root = tmp_path / "corpus"
    (root / "empty-job").mkdir(parents=True)
    m = corpus_manifest.build_manifest(str(root), reviewed_commit=REVIEWED_COMMIT)
    assert m["counts"]["discovered_trials"] == 1
    assert m["counts"]["excluded_trials"] == 1
    assert m["counts"]["eligible_trials"] == 0
    assert m["trials"][0]["exclusion_reason"] == "empty job directory"
