"""Five-minute-path integration test.

Verifies that the one-command demo flow (``agr demo``, equivalently
``agr demo-store`` + ``agr serve``) produces a working store and serves
a complete evidence-browser API without errors, watermarks, or missing
endpoints.

The test builds the demo store in a temp directory using the same
``demo.build_demo_store()`` call the CLI uses, then exercises the read API
via FastAPI's TestClient (no real server needed).  It does not test the
uvicorn bind step — that is a one-line framework call we trust.

This test depends on the optional ``api`` extra (fastapi + httpx) and
skips cleanly when it is not installed, exactly like ``test_api.py``.
FastAPI is *not* a runtime dependency of the deterministic core.
"""

from __future__ import annotations

import json
import os

import pytest

fastapi = pytest.importorskip("fastapi")
pytest.importorskip("httpx")
from fastapi.testclient import TestClient  # noqa: E402

from agr import demo  # noqa: E402
from agr.api import create_app  # noqa: E402
from agr.store import Store  # noqa: E402

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")

_RUN_COUNT = 12          # 5 matched pairs + 1 baseline-extra + 1 candidate-extra
_BASELINE_SWEEP = "sweep_141"
_CANDIDATE_SWEEP = "sweep_142"
_BASELINE_RUNS = 6       # 5 matched + 1 exclusive
_CANDIDATE_RUNS = 6      # 5 matched + 1 exclusive
_MATCHED_RUN_COUNT = 10  # 5 pairs × 2 sides
_MATCHED_TASK_COUNT = 5


def _demo_store(tmp_path) -> Store:
    """Build a demo store in a temp directory and return it."""
    root = str(tmp_path / "agr-demo")
    store = Store(root)
    demo.build_demo_store(store, FIXTURES)
    return store


def _client(tmp_path) -> TestClient:
    """Build a demo store and return a TestClient bound to it."""
    store = _demo_store(tmp_path)
    return TestClient(create_app(store.root))


# ---------------------------------------------------------------------------
# Store-level verification  (no HTTP)
# ---------------------------------------------------------------------------


class TestDemoStoreBuilt:
    """The store created by ``demo.build_demo_store()`` has the right shape."""

    def test_runs_exist(self, tmp_path):
        store = _demo_store(tmp_path)
        summary = store.read_index("chess_best_move__seed42__b")
        assert len(summary) == 1
        entry = summary[0]
        assert not entry.get("idempotent", True)  # was freshly ingested
        assert entry["source_hash"].startswith("sha256:")

    def test_all_runs_listed_in_read_model(self, tmp_path):
        store = _demo_store(tmp_path)
        from agr import read
        summaries = read.list_runs(store)
        assert len(summaries) == _RUN_COUNT
        run_ids = {s["run_id"] for s in summaries}
        # Spot-check: every expected suffix pattern is present
        assert any("__b" in rid and "__c" not in rid for rid in run_ids)   # baseline
        assert any("__c" in rid and "__c_v4" not in rid for rid in run_ids)  # matched candidate
        assert any("__c_v4" in rid for rid in run_ids)                     # unmatched candidate
        assert any("clean_pass__b" in rid for rid in run_ids)              # baseline extra

    def test_every_contract_is_human_confirmed(self, tmp_path):
        """The demo auto-confirms every contract — no watermarks."""
        store = _demo_store(tmp_path)
        for run_dir in os.listdir(os.path.join(store.root, "runs")):
            capture_id = store.latest_capture_id(run_dir)
            assert capture_id is not None, f"no capture for {run_dir}"
            contract = store.read_derived(run_dir, capture_id, "contract.json")
            assert contract["status"] == "human_confirmed", (
                f"{run_dir} contract is {contract['status']}, not confirmed"
            )

    def test_sweep_ids_are_correct(self, tmp_path):
        store = _demo_store(tmp_path)
        from agr import queue
        summary = queue.sweep_summary(store)
        assert set(summary["sweep_ids"]) == {_BASELINE_SWEEP, _CANDIDATE_SWEEP}

    def test_configurations_exist(self, tmp_path):
        store = _demo_store(tmp_path)
        from agr import versions
        configs = versions.list_configurations(store)
        labels = {c["label"] for c in configs}
        assert labels == {_BASELINE_SWEEP, _CANDIDATE_SWEEP}
        for c in configs:
            count = _BASELINE_RUNS if c["label"] == _BASELINE_SWEEP else _CANDIDATE_RUNS
            assert c["run_count"] == count, f"{c['label']} has {c['run_count']} runs, expected {count}"

    def test_comparison_preview_works(self, tmp_path):
        """A construction preview returns matched pairs + exclusions."""
        store = _demo_store(tmp_path)
        from agr import versions
        result = versions.compare_versions(
            store, {"sweep_id": _BASELINE_SWEEP}, {"sweep_id": _CANDIDATE_SWEEP},
            "evaluation_harness",
        )
        assert result["comparison_id"].startswith("comparison_")
        assert result["declared_change_axis"] == "evaluation_harness"
        assert result["observed_change_axes"] == ["evaluation_harness"]
        # Matched pairs = 5 tasks × 1 run each side = 10 runs total
        assert len(result["pairs"]) == _MATCHED_TASK_COUNT, (
            f"expected {_MATCHED_TASK_COUNT} matched tasks, got {len(result['pairs'])}"
        )
        # Exclusions: 1 per side
        assert len(result["exclusions"]) == 2
        sides = [e["side"] for e in result["exclusions"]]
        assert "baseline" in sides
        assert "candidate" in sides


# ---------------------------------------------------------------------------
# HTTP-level verification  (via TestClient)
# ---------------------------------------------------------------------------


class TestDemoAPI:
    """The HTTP API serves the expected endpoints for a demo store."""

    def test_healthz(self, tmp_path):
        client = _client(tmp_path)
        resp = client.get("/healthz")
        assert resp.status_code == 200
        assert resp.json()["status"] == "ok"

    def test_index_serves_spa(self, tmp_path):
        client = _client(tmp_path)
        resp = client.get("/")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]
        # Core SPA markers
        assert 'id="run-list"' in resp.text
        assert "Agent Game Review" in resp.text

    def test_sweep_endpoint(self, tmp_path):
        client = _client(tmp_path)
        resp = client.get("/sweep")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total_runs"] == _RUN_COUNT
        assert set(body["sweep_ids"]) == {_BASELINE_SWEEP, _CANDIDATE_SWEEP}

    def test_runs_endpoint_returns_all_runs(self, tmp_path):
        client = _client(tmp_path)
        resp = client.get("/runs")
        assert resp.status_code == 200
        runs = resp.json()
        assert len(runs) == _RUN_COUNT

    def test_queue_endpoint(self, tmp_path):
        client = _client(tmp_path)
        resp = client.get("/queue")
        assert resp.status_code == 200
        body = resp.json()
        assert "queue_view_id" in body
        assert len(body["runs"]) == _RUN_COUNT

    def test_review_for_specific_run(self, tmp_path):
        client = _client(tmp_path)
        resp = client.get("/runs/chess_best_move__seed42__b")
        assert resp.status_code == 200
        body = resp.json()
        assert body["outcome"]["passed"] == 5
        assert body["outcome"]["total"] == 6
        # Contract must be confirmed — no watermark in the demo
        assert body["contract"]["status"] == "human_confirmed"
        assert body["contract"]["confirmed_by"] == "demo-user"

    def test_review_for_candidate_run_is_improved(self, tmp_path):
        client = _client(tmp_path)
        # chess_best_move improves from FAILED 5/6 → PASSED 6/6 on candidate side
        resp = client.get("/runs/chess_best_move__seed42__c")
        assert resp.status_code == 200
        body = resp.json()
        assert body["outcome"]["status"] == "PASSED"
        assert body["outcome"]["passed"] == 6

    def test_forensic_view(self, tmp_path):
        client = _client(tmp_path)
        resp = client.get("/runs/chess_best_move__seed42__b/forensic")
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["steps"]) >= 1
        assert "capability_badge" in body

    def test_source_endpoint(self, tmp_path):
        client = _client(tmp_path)
        resp = client.get("/runs/chess_best_move__seed42__b/source")
        assert resp.status_code == 200
        body = resp.json()
        assert body["source"]["run"]["logical_run_id"] == "chess_best_move__seed42__b"
        assert body["verified"] is True

    def test_configurations_endpoint(self, tmp_path):
        client = _client(tmp_path)
        resp = client.get("/configurations")
        assert resp.status_code == 200
        body = resp.json()
        labels = {c["label"] for c in body["configurations"]}
        assert labels == {_BASELINE_SWEEP, _CANDIDATE_SWEEP}
        assert body["default_pair"] == [_BASELINE_SWEEP, _CANDIDATE_SWEEP]

    def test_comparisons_preview(self, tmp_path):
        client = _client(tmp_path)
        resp = client.get(
            "/comparisons/preview",
            params={
                "baseline": f"sweep_id={_BASELINE_SWEEP}",
                "candidate": f"sweep_id={_CANDIDATE_SWEEP}",
                "axis": "evaluation_harness",
            },
        )
        assert resp.status_code == 200
        body = resp.json()
        assert len(body["pairs"]) == _MATCHED_TASK_COUNT
        assert len(body["exclusions"]) == 2
        sides = [e["side"] for e in body["exclusions"]]
        assert "baseline" in sides
        assert "candidate" in sides

    def test_save_and_load_comparison(self, tmp_path):
        client = _client(tmp_path)
        # Save a comparison definition
        save_resp = client.post(
            "/comparisons",
            json={
                "baseline": {"sweep_id": _BASELINE_SWEEP},
                "candidate": {"sweep_id": _CANDIDATE_SWEEP},
                "axis": "evaluation_harness",
                "name": "demo comparison",
            },
        )
        assert save_resp.status_code == 200
        saved = save_resp.json()
        cid = saved["comparison_id"]
        assert cid.startswith("comparison_")
        # Load it back
        load_resp = client.get(f"/comparisons/{cid}")
        assert load_resp.status_code == 200
        loaded = load_resp.json()
        assert loaded["comparison_id"] == cid
        assert len(loaded["pairs"]) == _MATCHED_TASK_COUNT

    def test_next_unhandled_run(self, tmp_path):
        client = _client(tmp_path)
        # Get any run to use as the current position
        runs_resp = client.get("/runs")
        first_run = runs_resp.json()[0]["run_id"]
        resp = client.get(f"/runs/{first_run}/next")
        assert resp.status_code == 200
        body = resp.json()
        assert body["run_id"] == first_run
        # Should return a different run as "next unhandled"
        assert body["next_unhandled"] is not None
        assert body["next_unhandled"] != first_run

    def test_reviews_list(self, tmp_path):
        client = _client(tmp_path)
        resp = client.get("/runs/chess_best_move__seed42__b/reviews")
        assert resp.status_code == 200
        body = resp.json()
        assert "reviews" in body
        assert "deterministic" in body["reviews"]

    def test_static_file_served(self, tmp_path):
        """The SPA's static assets (JS modules, CSS) are served."""
        client = _client(tmp_path)
        resp = client.get("/static/index.html")
        assert resp.status_code == 200
        assert "text/html" in resp.headers["content-type"]

    def test_404_for_missing_run(self, tmp_path):
        client = _client(tmp_path)
        resp = client.get("/runs/nonexistent_run")
        assert resp.status_code == 404


