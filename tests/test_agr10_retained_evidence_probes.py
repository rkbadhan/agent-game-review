"""AGR-10 acceptance probes (review 82cc113): retained evidence/validation.

Three reproducible gaps in the model-reviewer safety envelope:

1. Long structured tool_input was silently re-bounded to the smaller
   always-sent packet excerpt during the "full evidence" expansion round
   (covered by test_expansion_returns_full_structured_tool_input in
   test_review_2026_09_07_followup_probes.py).
2. A quote copied verbatim from an event's retained structured tool_input
   (an Edit's old/new strings, a Write's content) failed Stage G
   recomputation because validation only checked DerivedEvent.text(), which
   never carries structured tool_input.
3. A model-proposed "repetition" fact over two mutation events (Edit/Write)
   validated as "no new information" from identical acknowledgement text
   alone — exactly the claim detectors.py refuses to emit for a mutation,
   because "ok" says nothing about what the file became.
"""

from agr.reviewer import ReviewerContext, validate_facts
from agr.schema import Candidate, DerivedEvent


def _event(eid, etype="tool_call", seq=None, payload=None):
    return DerivedEvent(
        event_id=eid, run_id="r", source_capture_id="c", sequence=seq or 1,
        source_step_ids=[f"h_{eid}"], event_type=etype, actor="main_agent",
        payload=dict(payload or {}),
    )


def _ctx(events):
    return ReviewerContext(
        run_id="r", source_capture_id="c", candidates=[], slices=[],
        checks=[], events=events, recoveries=[], contract=None, verifier_logs=[],
    )


def test_event_support_quote_from_structured_tool_input_validates():
    """A quote copied verbatim from an Edit's retained new_string — evidence
    the reviewer packet and expansion round both hand the model — must
    validate; it is not present in DerivedEvent.text() alone."""
    evs = [_event("c1", payload={
        "tool": "Edit", "content": "app.py",
        "tool_input": {"file_path": "app.py",
                       "old_string": "return False", "new_string": "return True"}})]
    cand = Candidate(candidate_id="cand_1", run_id="r", source_capture_id="c",
                     detector="model", kind="behaviour", anchor_event_ids=["c1"],
                     structured_facts=[{"type": "event_support",
                                        "quotes": [{"event_id": "c1", "quote": "return True"}]}])
    out = validate_facts(cand, _ctx(evs))
    assert out[0]["validation"] == "passed"
    assert out[0]["recomputed"][0]["matched"] is True


def test_event_support_quote_matching_neither_text_nor_input_still_fails():
    """The widened quote match must not become "matches anything" — a quote
    absent from both the display text and the structured input still fails."""
    evs = [_event("c1", payload={
        "tool": "Edit", "content": "app.py",
        "tool_input": {"file_path": "app.py",
                       "old_string": "return False", "new_string": "return True"}})]
    cand = Candidate(candidate_id="cand_1", run_id="r", source_capture_id="c",
                     detector="model", kind="behaviour", anchor_event_ids=["c1"],
                     structured_facts=[{"type": "event_support",
                                        "quotes": [{"event_id": "c1", "quote": "totally invented span"}]}])
    out = validate_facts(cand, _ctx(evs))
    assert out[0]["validation"] == "failed"


def test_model_proposed_repeated_mutation_fails_validation():
    """A model-proposed 'repetition' fact over two Edits of the same file
    with identical 'ok' acknowledgements must fail Stage G — the same reason
    RepeatedActionNoNewInfo (detectors.py) never emits this claim for a
    mutation: ack text says nothing about what the file became."""
    evs = [
        _event("c1", seq=1, payload={"tool": "Edit", "content": "app.py",
                                     "tool_input": {"file_path": "app.py",
                                                    "old_string": "a", "new_string": "b"}}),
        _event("r1", etype="tool_result", seq=2, payload={"tool": "Edit", "content": "ok"}),
        _event("c2", seq=3, payload={"tool": "Edit", "content": "app.py",
                                     "tool_input": {"file_path": "app.py",
                                                    "old_string": "a", "new_string": "b"}}),
        _event("r2", etype="tool_result", seq=4, payload={"tool": "Edit", "content": "ok"}),
    ]
    cand = Candidate(candidate_id="cand_1", run_id="r", source_capture_id="c",
                     detector="model", kind="behaviour", anchor_event_ids=["c1", "c2"],
                     structured_facts=[{"type": "repetition",
                                        "signature": ["Edit", "app.py"],
                                        "events": ["c1", "c2"]}])
    out = validate_facts(cand, _ctx(evs))
    assert out[0]["validation"] == "failed"


def test_model_proposed_repeated_non_mutation_still_validates():
    """The mutation guard must not over-widen: a repeated NON-mutation call
    (e.g. a Bash status poll) with equivalent captured output still
    validates exactly as before."""
    evs = [
        _event("c1", seq=1, payload={"tool": "Bash", "content": "git status"}),
        _event("r1", etype="tool_result", seq=2, payload={"tool": "Bash", "content": "clean"}),
        _event("c2", seq=3, payload={"tool": "Bash", "content": "git status"}),
        _event("r2", etype="tool_result", seq=4, payload={"tool": "Bash", "content": "clean"}),
    ]
    cand = Candidate(candidate_id="cand_1", run_id="r", source_capture_id="c",
                     detector="model", kind="behaviour", anchor_event_ids=["c1", "c2"],
                     structured_facts=[{"type": "repetition",
                                        "signature": ["Bash", "git status"],
                                        "events": ["c1", "c2"]}])
    out = validate_facts(cand, _ctx(evs))
    assert out[0]["validation"] == "passed"
