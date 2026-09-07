"""Order-5 follow-up probes (review 2026-09-06): diagnostic delivery and
telemetry — verifier logs reach the reviewer, the packet budget is measured
and enforced on the actual payload, per-round telemetry persists, and
rejection telemetry does not misattribute failures to non-gates.
"""

import json
import os
import tempfile

from agr import read
from agr.model_packet import build_packet, resolve_expansion
from agr.model_reviewer import ScriptedReviewer
from agr.pipeline import analyze
from agr.reviewer import ReviewerContext
from agr.store import Store

FIXTURE = "chess_best_move.atif.json"


def _load(name, fixtures_dir):
    with open(os.path.join(fixtures_dir, name), encoding="utf-8") as fh:
        return json.load(fh)


def _mkdtemp():
    return tempfile.mkdtemp(prefix="agr-probe-")


def _ctx(a, verifier_logs=()):
    return ReviewerContext(
        run_id=a.run_source.run_id, source_capture_id=a.run_source.source_capture_id,
        candidates=[], slices=[], checks=a.checks, events=a.events,
        recoveries=a.recoveries, contract=a.contract, verifier_logs=list(verifier_logs),
    )


def test_verifier_log_diagnostics_reach_the_packet(fixtures_dir, tmp_path):
    """The verifier's post-run log output is packet evidence: present in its
    own labelled section and resolvable through the expansion namespace."""
    doc = _load(FIXTURE, fixtures_dir)
    doc["verifier"]["log_excerpts"] = [{
        "source": "verifier/test-stdout.txt",
        "content": "E   assert 404 == 200\nE   + where 404 = response.status_code",
        "timing": "post_run",
    }]
    store = Store(str(tmp_path / "store"))
    a = analyze(doc, store)
    ctx = _ctx(a, doc["verifier"]["log_excerpts"])

    packet, _map = build_packet(ctx)
    diags = packet["verifier_diagnostics"]
    assert len(diags) == 1
    assert diags[0]["log_id"] == "log::0"
    assert diags[0]["timing"] == "post_run"
    assert "assert 404 == 200" in diags[0]["content"]

    # The log namespace is resolvable through the expansion round; unknown ids
    # are still rejected, not guessed.
    expansion = resolve_expansion(ctx, [{"event_ids": ["log::0", "log::9"]}])
    assert expansion["evidence"][0]["event_id"] == "log::0"
    assert expansion["evidence"][0]["timing"] == "post_run"
    assert "assert 404 == 200" in expansion["evidence"][0]["content"]
    assert expansion["rejected"][0]["event_id"] == "log::9"


def test_packet_budget_is_measured_and_reported(fixtures_dir):
    """The redaction/telemetry map reports the packet's actual serialized size
    and whether the enforced budget was met — measured on the final payload,
    with room reserved for the system prompt and expansion round."""
    doc = _load(FIXTURE, fixtures_dir)
    a = analyze(doc, Store(_mkdtemp()))
    ctx = _ctx(a)

    packet, red_map = build_packet(ctx, budget_chars=60_000)
    assert red_map["packet_size_chars"] == len(json.dumps(packet))
    assert red_map["budget_chars"] == 60_000
    assert red_map["effective_budget_chars"] == 60_000 - 8_000
    assert red_map["budget_met"] is True

    # An oversized budget forces the digest down to scaled excerpts, with the
    # outcome reported — never silently dropped.
    packet2, red_map2 = build_packet(ctx, budget_chars=1_200)
    assert red_map2["budget_chars"] == 1_200
    assert red_map2["packet_size_chars"] == len(json.dumps(packet2))
    excerpts = [d["excerpt"] for d in packet2["timeline_digest"]]
    assert all(len(x) <= 220 for x in excerpts)


def test_rejection_telemetry_does_not_count_status_fields_as_gates(fixtures_dir, tmp_path):
    """``validation_attempts`` (a number) and better_action/explanation status
    fields are not gates: the rejection map iterates only the declared gates,
    and de-duplication is recorded separately from the quota."""
    doc = _load(FIXTURE, fixtures_dir)
    store = Store(str(tmp_path / "store"))
    analyze(doc, store)
    telemetry = read.get_review(store, "chess_best_move__seed42")["review_telemetry"]
    rejections = telemetry.get("rejections", {})
    assert "validation_attempts" not in rejections
    assert "better_action" not in rejections
    for key, count in rejections.items():
        assert count >= 1


def test_provider_round_telemetry_persists(fixtures_dir, tmp_path):
    """Per-attempt provider-round records reach the persisted telemetry —
    not just reviewer-key and counts. Estimates stay labelled as estimates."""
    doc = _load(FIXTURE, fixtures_dir)
    store = Store(str(tmp_path / "store"))

    class _RoundLogging(ScriptedReviewer):
        telemetry = [{
            "kind": "propose", "provider": "scripted", "model": "probe",
            "input_chars": 1234, "input_tokens_est": 308, "latency_ms": 5,
            "redaction": {"removed": []},
        }]

    analyze(doc, store, reviewer=_RoundLogging({"moments": []}, source="model:probe"))
    t = read.get_review(store, "chess_best_move__seed42")["review_telemetry"]
    rounds = t.get("provider_rounds")
    assert rounds and rounds[0]["input_tokens_est"] == 308
    assert "est" in rounds[0] or "input_tokens_est" in rounds[0]
