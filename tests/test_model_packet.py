"""The reviewer's typed input packet (spec §8.7) — run_shape scaffolding.

``run_shape`` is the deterministic budget-allocation strip that lets the model
reviewer *discover* prolonged-drift patterns without eyeballing every digest
entry. These tests pin its arithmetic and its presence in the packet.
"""

import json

from agr.model_packet import _run_shape, build_packet
from agr.reviewer import ReviewerContext
from agr.schema import Candidate, DerivedEvent


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


# --- P1 finding 1: no credential format reaches the packet --------------------
#
# Fabricated tokens only: correct prefix and shape, no real key.

_PACKET_SECRETS = [
    "sk-proj-AbCdEfGhIj_KlMnOp-QrStUvWxYz0123456789abcdEFGH",
    "sk-svcacct-AbCdEfGhIj_KlMnOp-QrStUvWxYz0123456789abcdEFGH",
    "sk-or-v1-" + "0123456789abcdef" * 4,
    "sk-lf-12345678-abcd-4ef0-9876-0123456789ab",
    "github_" + "pat_11ABCDEFG0abcdefghijkl_ZYXWVUTSRQPONMLKJIHGFEDCBA0123456789abcdefghijklmnopqrstuvw",
    "xox" + "b-123456789012-abcdefABCDEF",
    "xox" + "p-123456789012-abcdefABCDEF",
    "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
    "hf_aBcDeFgHiJkLmNoPqRsTuVwXyZ01234567",
    "dXNlcjpmYWtlLXBhc3N3b3JkLTEyMzQ=",       # Basic auth credential
    "f4k3-Pa55w0rd",                          # URL password
    "hunter2-fake",                           # assigned password / env value
]
_PACKET_TEXT = " ".join([
    "sk-proj-AbCdEfGhIj_KlMnOp-QrStUvWxYz0123456789abcdEFGH",
    "sk-svcacct-AbCdEfGhIj_KlMnOp-QrStUvWxYz0123456789abcdEFGH",
    "sk-or-v1-" + "0123456789abcdef" * 4,
    "sk-lf-12345678-abcd-4ef0-9876-0123456789ab",
    "github_" + "pat_11ABCDEFG0abcdefghijkl_ZYXWVUTSRQPONMLKJIHGFEDCBA0123456789abcdefghijklmnopqrstuvw",
    "xox" + "b-123456789012-abcdefABCDEF xox" + "p-123456789012-abcdefABCDEF",
    "AIzaSyA1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q",
    "hf_aBcDeFgHiJkLmNoPqRsTuVwXyZ01234567",
    "Authorization: Basic dXNlcjpmYWtlLXBhc3N3b3JkLTEyMzQ=",
    "postgres://app:f4k3-Pa55w0rd@db.test:5432/app",
    "password=hunter2-fake",
])


def test_no_credential_format_reaches_the_packet():
    ev = _event("evt_001", etype="tool_call", seq=1, phase="ph_01", payload={
        "content": f"ran with {_PACKET_TEXT}",
        "tool": "bash",
        "tool_input": {
            "command": f"export OPENAI_API_KEY=hunter2-fake; echo {_PACKET_TEXT}",
            "env": {"password": "hunter2-fake", "sk-proj-AbCdEfGhIj_KlMnOp-QrStUvWxYz0123456789abcdEFGH": "x"},
        },
    })
    ctx = ReviewerContext(
        run_id="r", source_capture_id="c", candidates=[], slices=[], checks=[],
        events=[ev], task_instruction=f"Deploy using {_PACKET_TEXT}",
        verifier_logs=[{"content": f"log: {_PACKET_TEXT}"}],
    )
    packet, red_map = build_packet(ctx)
    dumped = json.dumps(packet, ensure_ascii=False)
    for secret in _PACKET_SECRETS:
        assert secret not in dumped, secret
    assert "[REDACTED:openai_key]" in packet["task_instruction"]
    assert {"openai_key", "openrouter_key", "langfuse_key", "github_token", "slack_token",
            "google_api_key", "huggingface_token", "basic_auth", "url_credentials",
            "credential_assignment"} <= {e["reason"] for e in red_map["removed"]}


def test_secret_past_the_input_bound_is_redacted_before_truncation():
    """The packet bounds structured tool input; redaction must run on the FULL
    text first, or a secret straddling the bound survives as a partial prefix."""
    from agr._util import _PACKET_INPUT_CHARS
    token = "sk-proj-AbCdEfGhIj_KlMnOp-QrStUvWxYz0123456789abcdEFGH"
    filler = "x" * (_PACKET_INPUT_CHARS - 13 - 18 - 1)  # only 10 body chars fit under the bound
    ev = _event("evt_001", etype="tool_call", seq=1, phase="ph_01", payload={
        "tool": "bash", "tool_input": {"command": f"{filler} {token}"},
    })
    cand = Candidate(candidate_id="cand_1", run_id="r", source_capture_id="c",
                     detector="model", kind="behaviour", anchor_event_ids=["evt_001"])
    ctx = ReviewerContext(
        run_id="r", source_capture_id="c", candidates=[cand], slices=[], checks=[], events=[ev],
    )
    packet, _ = build_packet(ctx)
    tool_input = packet["deterministic_candidates"][0]["evidence"][0]["tool_input"]
    assert "[REDACTED" in tool_input  # the marker itself may be cut by the bound
    assert "sk-proj" not in tool_input
    assert "sk-proj-AbCd" not in json.dumps(packet, ensure_ascii=False)


def test_multiline_tool_input_secrets_reach_the_packet_redacted():
    ev = _event("evt_001", etype="tool_call", seq=1, phase="ph_01", payload={
        "tool": "Write",
        "tool_input": {"content": "HF_HOME=/x\nhf_aBcDeFgHiJkLmNoPqRsTuVwXyZ01234567\nPASSWORD=hunter2-fake\n"},
    })
    ctx = ReviewerContext(
        run_id="r", source_capture_id="c", candidates=[], slices=[], checks=[], events=[ev],
    )
    packet, _ = build_packet(ctx)
    dumped = json.dumps(packet, ensure_ascii=False)
    assert "hf_aBcDeFg" not in dumped and "hunter2-fake" not in dumped


def test_in_bounds_tool_input_is_not_truncated_by_longer_redaction_markers():
    """Markers are longer than the short secrets they replace; an input that
    fit under the bound before redaction must keep its tail afterwards."""
    from agr._util import _PACKET_INPUT_CHARS
    command = " ".join(f"K{i}_TOKEN=abcd{i:02d}" for i in range(60)) + " && echo TAIL_EVIDENCE_MARKER"
    assert len(command) < _PACKET_INPUT_CHARS
    ev = _event("evt_001", etype="tool_call", seq=1, phase="ph_01", payload={
        "tool": "bash", "tool_input": {"command": command},
    })
    cand = Candidate(candidate_id="cand_1", run_id="r", source_capture_id="c",
                     detector="model", kind="behaviour", anchor_event_ids=["evt_001"])
    ctx = ReviewerContext(
        run_id="r", source_capture_id="c", candidates=[cand], slices=[], checks=[], events=[ev],
    )
    packet, _ = build_packet(ctx)
    tool_input = packet["deterministic_candidates"][0]["evidence"][0]["tool_input"]
    assert "TAIL_EVIDENCE_MARKER" in tool_input and "[truncated]" not in tool_input
    assert "abcd00" not in tool_input


def test_escaped_newline_command_credentials_do_not_reach_the_packet_or_expansion():
    from agr.model_packet import resolve_expansion
    ev = _event("evt_001", etype="tool_call", seq=1, phase="ph_01", payload={
        "tool": "bash", "tool_input": {"command": "cd /app\ncurl -u admin:Sup3rSecretPw http://x.test"},
    })
    cand = Candidate(candidate_id="cand_1", run_id="r", source_capture_id="c",
                     detector="model", kind="behaviour", anchor_event_ids=["evt_001"])
    ctx = ReviewerContext(
        run_id="r", source_capture_id="c", candidates=[cand], slices=[], checks=[], events=[ev],
    )
    packet, _ = build_packet(ctx)
    assert "Sup3rSecretPw" not in json.dumps(packet)
    assert "Sup3rSecretPw" not in json.dumps(resolve_expansion(ctx, [{"event_ids": ["evt_001"]}]))
