"""Langfuse adapter tests (docs/span-adapters.md).

Same toy "config-fixer" agent run as the OTel fixture (tests/fixtures/otel),
re-expressed as a Langfuse trace, plus the two things new at this layer: an
observation whose ``level`` looks clean (``DEFAULT``) but whose ``output``
encodes a JSON error shape (must be decoded via the reused Harbor decoder),
and trace-level ``scores`` (one clean boolean pass, one ambiguous numeric
value with no universal pass/fail meaning).

The fixture is the REAL flat ``TraceWithFullDetails`` shape (trace fields at
the top level, ``observations``/``scores`` nested inside as siblings of
``id`` — no ``"trace"`` wrapper). That is what
``GET /api/public/traces/{id}`` and :func:`agr.langfuse_api.fetch_trace`
actually return; the adapter's original wrapped-export shape
(``{"trace": {...}, "observations": [...], "scores": [...]}``) is still
accepted too (see ``test_wrapped_export_shape_still_accepted`` below) but is
no longer the primary shape under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from agr.adapter import get_adapter
from agr.ingest_langfuse import LANGFUSE_ADAPTER, LANGFUSE_ADAPTER_VERSION, convert

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "langfuse" / "langfuse_toy_run.json"


def test_registered_after_otel():
    from agr.adapter import adapter_names
    names = adapter_names()
    assert names.index("langfuse") > names.index("otel")
    adapter = get_adapter("langfuse")
    assert adapter is LANGFUSE_ADAPTER
    assert adapter.version == LANGFUSE_ADAPTER_VERSION


def test_maps_kinds_and_order():
    res = convert(FIXTURE, task_id="t")
    doc = res.doc
    kinds = [s["kind"] for s in doc["steps"]]
    assert kinds[0] == "task_received"
    assert "model_output" in kinds and "tool_call" in kinds and "tool_result" in kinds
    assert "final_submission" not in kinds
    assert doc["source_type"] == "langfuse"


def test_trace_input_output_become_instruction_and_terminal_content():
    res = convert(FIXTURE, task_id="t")
    doc = res.doc
    assert "fix the port field" in doc["task"]["instruction"]
    terminal = doc["steps"][-1]
    assert terminal["kind"] == "run_completed"
    assert "reviewer subagent confirmed" in terminal["content"]


def test_trace_with_no_level_field_warns_unknown_and_still_completes():
    """The fixture's trace carries no top-level 'level' -- span_tree's own
    honest-unknown handling applies, not a guessed success."""
    res = convert(FIXTURE, task_id="t")
    assert any("status was not explicitly set" in w for w in res.warnings)


def test_default_level_with_json_error_output_is_decoded_as_error():
    """The core gotcha this adapter exists for: validate_config's first
    attempt shows level=DEFAULT (looks clean) but its output decodes to
    {"returncode": 1, ...} via the reused Harbor decoder -- must be 'error',
    not 'ok'."""
    res = convert(FIXTURE, task_id="t")
    validate_steps = [s for s in res.doc["steps"] if s.get("tool") == "validate_config"]
    first_result = [s for s in validate_steps if s["kind"] == "tool_result"][0]
    assert first_result["status"] == "error"
    assert first_result["exit_code"] == 1


def test_flaky_tool_retry_recognised():
    res = convert(FIXTURE, task_id="t")
    validate_steps = [s for s in res.doc["steps"] if s.get("tool") == "validate_config"]
    kinds = [s["kind"] for s in validate_steps]
    assert kinds.count("tool_call") == 1
    assert kinds.count("tool_result") == 1
    assert kinds.count("retry") == 1
    assert res.meta["retry_count"] == 1


def test_default_level_plain_text_output_is_ok():
    """A DEFAULT-level observation whose output is ordinary text (no decodable
    JSON-error shape) is a genuine ok, not downgraded."""
    res = convert(FIXTURE, task_id="t")
    read_result = [s for s in res.doc["steps"]
                   if s["kind"] == "tool_result" and s.get("tool") == "read_config"][0]
    assert read_result["status"] == "ok"


def test_subagent_grouping_reviewer():
    res = convert(FIXTURE, task_id="t")
    steps = res.doc["steps"]
    diff_call = [s for s in steps if s.get("tool") == "diff_check"]
    assert diff_call and diff_call[0]["actor"] == "subagent:reviewer"
    main_calls = [s for s in steps if s["kind"] == "tool_call" and s["actor"] == "main_agent"]
    assert {c["tool"] for c in main_calls} == {"read_config", "validate_config", "write_config"}
    assert "reviewer" in res.meta["subagent_names"]


def test_evaluator_observation_becomes_generic_observation_step():
    res = convert(FIXTURE, task_id="t")
    obs_steps = [s for s in res.doc["steps"]
                 if s["kind"] == "environment_observation" and "policy" in s.get("content", "")]
    assert obs_steps


def test_usage_carried_as_cost():
    res = convert(FIXTURE, task_id="t")
    model_steps = [s for s in res.doc["steps"] if s["kind"] == "model_output"]
    with_cost = [s for s in model_steps if "cost" in s]
    assert with_cost
    assert with_cost[0]["cost"]["input_tokens"] > 0


def test_session_id_from_trace_session_id():
    res = convert(FIXTURE, task_id="t")
    assert res.doc["run"]["source_session_id"] == "conv-toy-001"


# --- scores -> verifier.checks ------------------------------------------------

def test_scores_synthesize_verifier_when_no_explicit_one_given():
    res = convert(FIXTURE, task_id="t")
    assert "verifier" in res.doc
    checks = res.doc["verifier"]["checks"]
    assert len(checks) == 2
    assert all(c["source"] == "instrumented_assertion" for c in checks)


def test_boolean_score_maps_to_passed():
    res = convert(FIXTURE, task_id="t")
    checks = {c["name"]: c for c in res.doc["verifier"]["checks"]}
    assert checks["correctness"]["status"] == "passed"


def test_ambiguous_numeric_score_maps_to_unknown_with_warning():
    """0.8 on an arbitrary evaluator-defined numeric scale has no universal
    pass/fail meaning -- must never be guessed into a pass or fail."""
    res = convert(FIXTURE, task_id="t")
    checks = {c["name"]: c for c in res.doc["verifier"]["checks"]}
    assert checks["helpfulness"]["status"] == "unknown"
    assert any("no clear pass/fail mapping" in w for w in res.warnings)


def test_explicit_verifier_overrides_scores():
    verifier = {"raw_output": "explicit", "checks": [
        {"check_id": "c1", "status": "passed", "source": "native_structured"}]}
    res = convert(FIXTURE, task_id="t", verifier=verifier)
    assert res.doc["verifier"] == verifier


def test_no_scores_and_no_verifier_ingests_unverified(tmp_path):
    import json
    export = json.loads(FIXTURE.read_text())
    export["scores"] = []
    p = tmp_path / "no_scores.json"
    p.write_text(json.dumps(export), encoding="utf-8")
    res = convert(p, task_id="t")
    assert "verifier" not in res.doc
    assert any("no verifier evidence" in w for w in res.warnings)


# --- content-catch and structural edge cases -----------------------------------

def test_uncaptured_content_declares_partial_or_unavailable(tmp_path):
    import json
    export = json.loads(FIXTURE.read_text())
    for obs in export["observations"]:
        obs.pop("input", None)
        obs.pop("output", None)
    export["scores"] = []
    p = tmp_path / "no_content.json"
    p.write_text(json.dumps(export), encoding="utf-8")
    res = convert(p, task_id="t")
    caps = res.doc["capabilities"]
    assert caps["tool_calls"] == "unavailable"
    assert caps["tool_results"] == "unavailable"


def test_rejects_non_langfuse_export(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text('{"not_a_trace": true}', encoding="utf-8")
    with pytest.raises(ValueError, match="not a Langfuse trace export"):
        convert(bad)


def test_task_id_defaulted_from_trace_id():
    res = convert(FIXTURE)
    assert res.doc["run"]["task_id"].startswith("langfuse-trace-")
    assert any("task_id derived from the trace id" in w for w in res.warnings)


def test_adapter_object_matches_convert_function():
    a = LANGFUSE_ADAPTER.convert(FIXTURE, task_id="t")
    b = convert(FIXTURE, task_id="t")
    assert a.doc["run"]["task_id"] == b.doc["run"]["task_id"]
    assert len(a.doc["steps"]) == len(b.doc["steps"])


# --- Fix 1: flat TraceWithFullDetails (the real live-API shape) ----------------

def test_flat_trace_with_full_details_shape_ingests_with_observations():
    """Guards Fix 1: a raw ``GET /api/public/traces/{id}`` response is FLAT
    -- the trace's own fields sit at the top level and ``observations``/
    ``scores`` are nested as siblings of ``id``, with no ``"trace"`` wrapper
    key at all. Before the fix, the loader looked for a top-level ``"trace"``
    key this shape never has, so a real API response silently parsed with an
    EMPTY observation list. This dict is built independently of the shared
    fixture (which is itself already flat) specifically to pin that a flat
    shape produces real, non-empty observations -- not zero."""
    flat_trace = {
        "id": "trace-flat-1",
        "name": "flat-check",
        "timestamp": "2026-09-01T10:00:00.000Z",
        "input": "do the thing",
        "output": "did the thing",
        "observations": [
            {
                "id": "obs-1",
                "type": "TOOL",
                "parentObservationId": None,
                "name": "do_thing",
                "startTime": "2026-09-01T10:00:01.000Z",
                "level": "DEFAULT",
                "output": "done",
            },
        ],
        "scores": [],
    }
    res = convert(flat_trace, task_id="t")
    assert res.meta["observation_count"] == 1
    tool_steps = [s for s in res.doc["steps"] if s.get("tool") == "do_thing"]
    assert tool_steps, "flat-shape observation must not be dropped (Fix 1 regression)"


def test_convert_accepts_a_dict_source_not_only_a_path():
    """convert() must accept an already-parsed dict directly (Fix 4/5): a
    live-fetched trace (agr.langfuse_api.fetch_trace's return value) is
    converted with no intermediate file round-trip."""
    import json as _json
    trace_dict = _json.loads(FIXTURE.read_text())
    assert isinstance(trace_dict, dict)
    res = convert(trace_dict, task_id="t")
    assert res.doc["source_type"] == "langfuse"
    assert res.meta["observation_count"] > 0


def test_wrapped_export_shape_still_accepted():
    """Back-compat: the original blob-storage/UI export shape
    (``{"trace": {...}, "observations": [...], "scores": [...]}``) must keep
    working -- some Langfuse exports genuinely come in this wrapped form."""
    wrapped = {
        "trace": {
            "id": "trace-wrapped-1",
            "name": "wrapped-check",
            "timestamp": "2026-09-01T10:00:00.000Z",
            "input": "do the thing",
            "output": "did the thing",
        },
        "observations": [
            {
                "id": "obs-1",
                "type": "TOOL",
                "parentObservationId": None,
                "name": "do_thing",
                "startTime": "2026-09-01T10:00:01.000Z",
                "level": "DEFAULT",
                "output": "done",
            },
        ],
        "scores": [],
    }
    res = convert(wrapped, task_id="t")
    assert res.meta["observation_count"] == 1
    tool_steps = [s for s in res.doc["steps"] if s.get("tool") == "do_thing"]
    assert tool_steps


# --- Fix 2: CATEGORICAL score label lives in stringValue, not value -----------

def test_categorical_score_label_is_read_from_string_value():
    """Guards Fix 2: per Langfuse's ScoreV1 schema, a CATEGORICAL score's
    ``value`` is a NUMERIC category-index mapping -- the human label
    ("pass"/"fail") is in ``stringValue``. Reading ``value`` for the label
    (the bug) means these scores always fell through to 'unknown'."""
    export = {
        "id": "trace-cat-1",
        "name": "categorical-check",
        "timestamp": "2026-09-01T10:00:00.000Z",
        "observations": [],
        "scores": [
            {"id": "s1", "name": "quality", "dataType": "CATEGORICAL",
             "value": 1, "stringValue": "pass"},
            {"id": "s2", "name": "safety", "dataType": "CATEGORICAL",
             "value": 0, "stringValue": "fail"},
        ],
    }
    res = convert(export, task_id="t")
    checks = {c["name"]: c for c in res.doc["verifier"]["checks"]}
    assert checks["quality"]["status"] == "passed"
    assert checks["safety"]["status"] == "failed"


def test_boolean_score_numeric_value_and_string_value_fallback():
    """BOOLEAN scores: ``value`` (0/1) is the primary encoding and is
    preferred; ``stringValue`` ("True"/"False") is accepted as a fallback
    when ``value`` itself is not usable."""
    export = {
        "id": "trace-bool-1",
        "name": "boolean-check",
        "timestamp": "2026-09-01T10:00:00.000Z",
        "observations": [],
        "scores": [
            {"id": "s1", "name": "numeric_true", "dataType": "BOOLEAN", "value": 1},
            {"id": "s2", "name": "numeric_false", "dataType": "BOOLEAN", "value": 0},
            {"id": "s3", "name": "string_only_true", "dataType": "BOOLEAN",
             "value": None, "stringValue": "True"},
            {"id": "s4", "name": "string_only_false", "dataType": "BOOLEAN",
             "value": None, "stringValue": "False"},
        ],
    }
    res = convert(export, task_id="t")
    checks = {c["name"]: c for c in res.doc["verifier"]["checks"]}
    assert checks["numeric_true"]["status"] == "passed"
    assert checks["numeric_false"]["status"] == "failed"
    assert checks["string_only_true"]["status"] == "passed"
    assert checks["string_only_false"]["status"] == "failed"


def test_categorical_score_outside_closed_vocab_is_unknown():
    """A categorical label outside the small closed vocabulary is 'unknown',
    never guessed -- the narrow vocab is unchanged by Fix 2, only which field
    it's matched against."""
    export = {
        "id": "trace-cat-2",
        "name": "categorical-unknown",
        "timestamp": "2026-09-01T10:00:00.000Z",
        "observations": [],
        "scores": [
            {"id": "s1", "name": "vibe", "dataType": "CATEGORICAL",
             "value": 2, "stringValue": "excellent"},
        ],
    }
    res = convert(export, task_id="t")
    checks = {c["name"]: c for c in res.doc["verifier"]["checks"]}
    assert checks["vibe"]["status"] == "unknown"
