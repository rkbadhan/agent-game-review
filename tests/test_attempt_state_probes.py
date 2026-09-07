"""F5 follow-up probes (review 2026-09-06): model attempts must have correct
persistent state — a successful retry resolves its active error, history is
kept separately, and the served snapshot matches the reported state.
"""

import json
import os

import pytest

from agr import read
from agr.model_reviewer import ScriptedReviewer
from agr.pipeline import analyze
from agr.store import Store

FIXTURES = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures")
FIXTURE = "chess_best_move.atif.json"


class _Broken(ScriptedReviewer):
    """A reviewer whose provider call fails."""
    review_mode = "model_enriched"

    def propose(self, ctx):
        raise RuntimeError("provider unavailable")


def _load(name):
    with open(os.path.join(FIXTURES, name), encoding="utf-8") as fh:
        return json.load(fh)


def _mirror_payload():
    """A scripted model payload mirroring the deterministic selected moments."""
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


def _tmpdir():
    import tempfile
    return tempfile.mkdtemp(prefix="agr-probe-")


def test_success_then_failure_then_successful_retry_resolves_state(tmp_path):
    """The review's three-attempt table: after success → failure → successful
    retry, the retry's review must serve as ``ok`` with no active error — not
    stuck at ``failed`` with history counted as current state."""
    run_id = "chess_best_move__seed42"
    store = Store(str(tmp_path / "store"))
    ok = ScriptedReviewer(_mirror_payload(), source="model:probe")

    # Attempt 1 — success: the model slot is written.
    analyze(_load(FIXTURE), store, reviewer=ok)
    view = read.get_review(store, run_id)
    assert view["reviewer_key"] == "model:probe"
    assert view["review_status"] == "ok"
    assert view["review_errors"] == []

    # Attempt 2 — provider failure: explicit error state; the deterministic
    # baseline is served (the errored slot is not silently served as default).
    analyze(_load(FIXTURE), store, reviewer=_Broken({}, source="model:probe"))
    view = read.get_review(store, run_id)
    assert view["review_status"] == "failed"
    assert view["review_errors"]
    assert view["review_errors"][0]["reviewer_key"] == "model:probe"

    # Attempt 3 — successful retry: the active error is RESOLVED. The served
    # snapshot is the fresh model review and the state says so.
    analyze(_load(FIXTURE), store, reviewer=ok)
    view = read.get_review(store, run_id)
    assert view["reviewer_key"] == "model:probe"
    assert view["review_status"] == "ok"
    assert view["review_errors"] == []

    # History is retained: the attempt log shows all three, in order.
    attempts = view["review_attempts"]
    assert [a["outcome"] for a in attempts] == ["ok", "failed", "ok"]
    assert attempts[1]["reviewer_key"] == "model:probe"
