"""The reviewer's typed input packet (spec §8.7) — run_shape scaffolding.

``run_shape`` is the deterministic budget-allocation strip that lets the model
reviewer *discover* prolonged-drift patterns without eyeballing every digest
entry. These tests pin its arithmetic and its presence in the packet.
"""

from agr.model_packet import _run_shape, build_packet
from agr.reviewer import ReviewerContext
from agr.schema import DerivedEvent


def _event(eid, etype="tool_call", seq=None, phase=None, payload=None):
    return DerivedEvent(
        event_id=eid, run_id="r", source_capture_id="c", sequence=seq or 1,
        source_step_ids=[f"h_{eid}"], event_type=etype, actor="main_agent",
        phase_id=phase,
        payload={"content": "work", **(payload or {})},
    )


def test_run_shape_counts_phases_tools_and_terminal():
    evs = [
        _event("evt_001", etype="task_received", seq=1, phase="ph_01"),
        _event("evt_002", etype="tool_call", seq=2, phase="ph_01",
               payload={"content": "", "tool": "bash"}),
        _event("evt_003", etype="tool_call", seq=3, phase="ph_02",
               payload={"content": "", "tool": "bash"}),
        _event("evt_004", etype="artifact_observation", seq=4, phase="ph_02",
               payload={"content": "", "path": "/app/out.txt"}),
        _event("evt_005", etype="run_timed_out", seq=5, phase="ph_02"),
    ]
    shape = _run_shape(evs)
    assert shape["total_events"] == 5
    assert shape["work_events"] == 2  # two tool_calls
    assert shape["tools"] == {"bash": 2}
    assert shape["artifacts_observed"] == ["/app/out.txt"]
    assert shape["submission_event_id"] is None
    assert shape["terminal"] == {"event_id": "evt_005", "event_type": "run_timed_out"}
    # phase shares are arithmetic over all events, first/last ids preserved
    ph = {p["phase_id"]: p for p in shape["phases"]}
    assert ph["ph_01"]["events"] == 2 and ph["ph_01"]["share"] == 0.4
    assert ph["ph_02"]["events"] == 3 and ph["ph_02"]["share"] == 0.6
    assert ph["ph_01"]["first_event_id"] == "evt_001"
    assert ph["ph_02"]["last_event_id"] == "evt_005"


def test_run_shape_flags_missing_deliverable_and_submission():
    """A timed-out run with zero artifact observations reports exactly that:
    empty artifacts_observed, no submission event."""
    evs = [
        _event("evt_001", etype="model_output", seq=1),
        _event("evt_002", etype="tool_call", seq=2, payload={"tool": "bash"}),
        _event("evt_003", etype="final_submission", seq=3),
    ]
    shape = _run_shape(evs)
    assert shape["artifacts_observed"] == []
    assert shape["submission_event_id"] == "evt_003"


def test_run_shape_reaches_the_packet():
    ctx = ReviewerContext(
        run_id="r", source_capture_id="c", candidates=[], slices=[],
        checks=[], events=[
            _event("evt_001", etype="task_received", seq=1, phase="ph_01"),
            _event("evt_002", etype="run_timed_out", seq=2, phase="ph_01"),
        ],
    )
    packet, _ = build_packet(ctx)
    assert packet["run_shape"]["total_events"] == 2
    assert packet["run_shape"]["terminal"]["event_type"] == "run_timed_out"
