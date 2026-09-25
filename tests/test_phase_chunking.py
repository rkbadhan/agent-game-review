"""GR-1: long traces are chunked by phase, summaries keep event ids, and a
summary is never the evidence for a published claim.

The chunked path summarises each phase chunk, then sends navigation summaries
plus the COMPLETE event-id index to the final review call. Original text stays
retrievable by id through the expansion round; a chunk that cannot be
summarised stops the review as ``incomplete``/``missing_chunks``.
"""

import json

import pytest

from agr.model_packet import (
    build_chunk_context,
    build_packet,
    build_phase_chunks,
    resolve_expansion,
)
from agr.model_reviewer import _LazyModelReviewer
from agr.reviewer import ReviewBudgetExceededError, ReviewerContext
from agr.schema import CapabilityProfile, DerivedEvent


def _long_ctx(n=120, chars=900):
    events = []
    for i in range(n):
        phase = "execution" if i < n // 2 else "verification"
        events.append(DerivedEvent(
            event_id=f"evt_{i:03d}", run_id="r", source_capture_id="c",
            sequence=i + 1, source_step_ids=[f"s{i}"], event_type="tool_call",
            actor="agent", phase_id=phase,
            payload={"content": "x" * chars, "tool": "Bash"}))
    profile = CapabilityProfile(
        run_id="r", source_capture_id="c",
        capabilities={"messages": "complete", "tool_calls": "complete",
                      "tool_results": "complete", "filesystem": "checkpoint_only"})
    return ReviewerContext(run_id="r", source_capture_id="c", candidates=[],
                           slices=[], checks=[], events=events, recoveries=[],
                           profile=profile, declared_artifacts=[])


class _ChunkFake(_LazyModelReviewer):
    """Dispatches on the call kind; records every outbound payload."""

    provider = "fake"

    def __init__(self, fail_summary=None, unusable_summary=False, **kw):
        super().__init__("fake-model", **kw)
        self.sent = []
        self.summary_calls = 0
        self.fail_summary = fail_summary
        self.unusable_summary = unusable_summary

    def _complete(self, system, user_json):
        self.sent.append((system, user_json))
        data = json.loads(user_json)
        if data.get("call") == "summarise_phase_chunk":
            self.summary_calls += 1
            if self.fail_summary is not None:
                raise self.fail_summary
            if self.unusable_summary:
                return {}
            return {"summary": "phase summary",
                    "event_ids": data["chunk"]["event_ids"][:1]}
        return {"moments": []}


# --- the chunks ---------------------------------------------------------------


def test_phase_chunks_keep_every_event_id_in_order():
    ctx = _long_ctx()
    chunks = build_phase_chunks(ctx)
    all_ids = [e.event_id for e in ctx.events]
    listed = [eid for c in chunks for eid in c["event_ids"]]
    assert listed == all_ids  # complete and ordered — nothing silently dropped
    assert list(dict.fromkeys(c["phase_id"] for c in chunks)) == ["execution", "verification"]
    # Every event appears WITH content in exactly one chunk (finding 1): no id
    # is summarised from its identifier alone.
    content_ids = [e["event_id"] for c in chunks for e in c["events"]]
    assert content_ids == all_ids


def test_a_large_phase_is_split_into_content_bearing_chunks():
    ctx = _long_ctx(n=40, chars=900)
    for e in ctx.events:
        e.phase_id = "execution"  # one phase far larger than the chunk budget
    chunks = build_phase_chunks(ctx)
    assert len(chunks) > 1  # split, not truncated
    assert len({c["chunk_id"] for c in chunks}) == len(chunks)
    assert all(c["events"] for c in chunks)  # no id-only chunk
    assert [eid for c in chunks for eid in c["event_ids"]] == [e.event_id for e in ctx.events]


def test_the_prompt_explains_the_chunked_evidence_mode():
    from agr.model_reviewer import SYSTEM_PROMPT

    assert "evidence_mode" in SYSTEM_PROMPT
    assert "phase_chunks" in SYSTEM_PROMPT
    assert "NEVER evidence" in SYSTEM_PROMPT


def test_chunk_context_excludes_the_timeline():
    ctx = _long_ctx()
    context = build_chunk_context(ctx)
    assert context["run_shape"]["total_events"] == len(ctx.events)
    assert "timeline_digest" not in context
    assert "events" not in context


def test_chunked_packet_carries_summaries_and_no_digest():
    ctx = _long_ctx()
    summaries = build_phase_chunks(ctx)
    for s in summaries:
        s["summary"] = "did work"
    packet, red = build_packet(ctx, chunk_summaries=summaries)
    assert packet["evidence_mode"] == "chunked"
    assert packet["timeline_digest"] == []
    assert packet["phase_chunks"][0]["summary"] == "did work"
    assert red["evidence_mode"] == "chunked"
    assert red["chunked"] is True


# --- the summarise + final-call path -----------------------------------------


def test_a_long_trace_takes_the_chunked_path():
    ctx = _long_ctx()
    rev = _ChunkFake()
    rev.propose(ctx)
    expected_chunks = len(build_phase_chunks(ctx))
    assert rev.summary_calls == expected_chunks
    kinds = [r["kind"] for r in rev.telemetry]
    summarise_calls = [k for k in kinds if k.startswith("summarise:") and not k.endswith("_response")]
    assert len(summarise_calls) == expected_chunks
    assert "chunked_review" in kinds
    # The final outbound call is the chunked packet, with the complete index.
    final = json.loads(rev.sent[-1][1])["packet"]
    assert final["evidence_mode"] == "chunked"
    assert final["timeline_digest"] == []
    assert final["phase_chunks"]
    listed = [eid for c in final["phase_chunks"] for eid in c["event_ids"]]
    assert listed == [e.event_id for e in ctx.events]


def test_a_truncated_event_is_retrievable_in_full_by_id():
    ctx = _long_ctx(n=20, chars=900)
    chunks = build_phase_chunks(ctx)
    first = chunks[0]["events"][0]
    assert len(first["content"]) == 800  # chunk excerpts are bounded...
    original = next(e for e in ctx.events if e.event_id == first["event_id"])
    assert len(original.text()) > 800  # ...so the full text needs expansion
    expansion = resolve_expansion(ctx, [{"event_ids": [first["event_id"]], "reason": "ground it"}])
    assert expansion["rejected"] == []
    assert expansion["evidence"][0]["event_id"] == first["event_id"]
    assert expansion["evidence"][0]["content"] == original.text()


def test_a_provider_failure_while_summarising_is_review_failed():
    # Finding 3: provider/SDK/parsing failures are review_failed, not
    # missing_chunks — only genuinely missing evidence is 'incomplete'.
    rev = _ChunkFake(fail_summary=RuntimeError("provider down"))
    with pytest.raises(RuntimeError):
        rev.propose(_long_ctx())
    assert rev.incomplete is None


def test_an_unusable_summary_stops_as_missing_chunks():
    # A response that PARSED but carries no summary is genuinely missing
    # evidence, so it stops as incomplete/missing_chunks.
    rev = _ChunkFake(unusable_summary=True)
    with pytest.raises(ReviewBudgetExceededError) as excinfo:
        rev.propose(_long_ctx())
    assert excinfo.value.reason == "missing_chunks"
    assert rev.incomplete["reason"] == "missing_chunks"
    assert rev.incomplete["chunk_id"].startswith("phase:")
