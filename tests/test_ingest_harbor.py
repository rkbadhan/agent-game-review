"""Harbor adapter tests (spec §5.3).

The adapter is a pure format mapping: a Harbor (Terminal-Bench 2.0) trial
directory -> ATIF-shaped doc. These tests pin the two mappings that need
judgement — the ATIF-v1.7 turn fan-out and the reward -> verifier synthesis —
so a Harbor format change or an adapter edit surfaces as a failure, not a
silently shifted review.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from agr.ingest_harbor import convert

FIXTURE = Path(__file__).resolve().parent.parent / "archive" / "synthetic" / "fixtures" / "harbor_trial"


def _trial(tmp_path, steps, *, reward=1.0, exception=None, schema="ATIF-v1.7", agent=None):
    """Write a minimal Harbor trial dir (agent/trajectory.json + result.json)."""
    traj = {
        "schema_version": schema,
        "trajectory_id": "traj-x",
        "agent": agent if agent is not None else {"name": "pi-coding-agent", "model_name": "gpt-oss"},
        "steps": steps,
    }
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    (agent_dir / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")
    result = {"reward": reward, "exception": exception}
    (tmp_path / "result.json").write_text(json.dumps(result), encoding="utf-8")
    return str(tmp_path)


def test_fixture_converts_and_maps_turn_fanout():
    res = convert(FIXTURE, task_id="fix-failing-test")
    doc = res.doc
    kinds = [s["kind"] for s in doc["steps"]]
    assert kinds[0] == "task_received"
    # One Harbor agent turn with a tool_call + observation fans out into ordered
    # model_output / tool_call / tool_result steps.
    assert "model_output" in kinds and "tool_call" in kinds and "tool_result" in kinds
    assert kinds[-2:] == ["final_submission", "run_finished"]
    # reasoning_content is preserved as a tagged model_output, not dropped.
    assert any(s.get("content", "").startswith("[thinking]") for s in doc["steps"])
    # run metadata comes from the trajectory's agent block.
    assert doc["run"]["model"] == "gpt-oss"
    assert doc["run"]["agent"].startswith("pi-coding-agent")
    assert doc["source_type"] == "harbor"
    # Bare version stored so the core renders "ATIF-v1.7", not "ATIF-vATIF-v1.7";
    # the verbatim source token is retained in harness_version.
    assert doc["atif_version"] == "1.7"
    assert doc["run"]["harness_version"] == "harbor/ATIF-v1.7"


def test_tool_result_names_its_tool_from_the_call():
    res = convert(FIXTURE, task_id="t")
    # The first tool_result should be named after the bash call it answers,
    # resolved via source_call_id -> function_name.
    results = [s for s in res.doc["steps"] if s["kind"] == "tool_result"]
    assert results and results[0]["tool"] == "bash"
    # The extra.exit_code on the failing pytest is carried onto the result.
    assert results[0]["exit_code"] == 1


def test_reward_one_synthesises_a_passed_verifier(tmp_path):
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    res = convert(_trial(tmp_path, steps, reward=1.0), task_id="t")
    check = res.doc["verifier"]["checks"][0]
    assert check["status"] == "passed"
    assert check["source"] == "native_structured"
    assert res.doc["capture_completeness"] == "complete"
    assert res.doc["capabilities"]["verifier_code"] == "complete"


def test_reward_zero_synthesises_a_failed_verifier(tmp_path):
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    res = convert(_trial(tmp_path, steps, reward=0.0), task_id="t")
    assert res.doc["verifier"]["checks"][0]["status"] == "failed"


def test_partial_reward_is_failed_with_warning(tmp_path):
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    res = convert(_trial(tmp_path, steps, reward=0.5), task_id="t")
    assert res.doc["verifier"]["checks"][0]["status"] == "failed"
    assert any("partial reward" in w for w in res.warnings)


def test_exception_without_reward_is_error(tmp_path):
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    res = convert(_trial(tmp_path, steps, reward=None, exception={"type": "Timeout"}), task_id="t")
    assert res.doc["verifier"]["checks"][0]["status"] == "error"


def test_explicit_verifier_overrides_result_reward(tmp_path):
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    explicit = {"raw_output": "manual", "checks": [{"check_id": "C1", "status": "passed"}]}
    res = convert(_trial(tmp_path, steps, reward=0.0), task_id="t", verifier=explicit)
    # The caller's sidecar wins over the trial's failing reward.
    assert res.doc["verifier"]["checks"][0]["check_id"] == "C1"


def test_missing_result_json_ingests_unverified(tmp_path):
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    agent_dir = tmp_path / "agent"
    agent_dir.mkdir()
    traj = {"schema_version": "ATIF-v1.7", "agent": {"model_name": "gpt-oss"}, "steps": steps}
    (agent_dir / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")
    res = convert(str(tmp_path), task_id="t")  # no result.json written
    assert "verifier" not in res.doc
    assert res.doc["capture_completeness"] == "partial"
    assert any("UNVERIFIED" in w for w in res.warnings)


def test_multimodal_message_flattens_to_text(tmp_path):
    steps = [
        {"step_id": 1, "source": "user",
         "message": [{"type": "text", "text": "look at this"}, {"type": "image"}]},
    ]
    res = convert(_trial(tmp_path, steps), task_id="t")
    task = next(s for s in res.doc["steps"] if s["kind"] == "task_received")
    assert "look at this" in task["content"] and "[image]" in task["content"]


def test_unmapped_source_warns_not_drops_silently(tmp_path):
    steps = [
        {"step_id": 1, "source": "user", "message": "hi"},
        {"step_id": 2, "source": "oracle", "message": "???"},
    ]
    res = convert(_trial(tmp_path, steps), task_id="t")
    assert any("oracle" in w for w in res.warnings)


def test_accepts_direct_trajectory_json_path():
    res = convert(FIXTURE / "agent" / "trajectory.json", task_id="t")
    # result.json is found one level up from agent/, so the verifier still lands.
    assert res.doc["verifier"]["checks"][0]["status"] == "passed"


def test_converted_doc_ingests_end_to_end():
    """The adapter output must satisfy the real ingestion contract (§7.2)."""
    from agr.ingest import _validate

    res = convert(FIXTURE, task_id="fix-failing-test")
    _validate(res.doc)  # raises on any contract violation


def test_registry_lists_and_resolves_harbor():
    from agr.adapter import Adapter, adapter_names, get_adapter

    assert "harbor" in adapter_names()
    harbor = get_adapter("harbor")
    assert harbor.name == "harbor"
    assert isinstance(harbor, Adapter)


def test_converted_doc_stamps_adapter_version():
    """The doc carries the adapter's provenance stamp for ingestion to record."""
    from agr.ingest_harbor import HARBOR_ADAPTER_VERSION

    res = convert(FIXTURE, task_id="t")
    assert res.doc["adapter_version"] == HARBOR_ADAPTER_VERSION


def test_nested_verifier_result_reward_shape(tmp_path):
    """Current Harbor writes the reward nested under verifier_result.rewards;
    both shapes must synthesise the same verifier."""
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    trial_dir = Path(_trial(tmp_path, steps, reward=None))
    (trial_dir / "result.json").write_text(json.dumps({
        "trial_name": "my-task__agent__attempt-1",
        "verifier_result": {"rewards": {"reward": 1.0}, "exception_info": None},
    }), encoding="utf-8")
    res = convert(str(trial_dir), task_id="t")
    assert res.doc["verifier"]["checks"][0]["status"] == "passed"


def test_task_id_prefers_what_harbor_recorded(tmp_path):
    """With no --task-id, the trial's own task_name is used (source-supplied),
    with a warning that the contract should be confirmed."""
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    trial_dir = Path(_trial(tmp_path, steps, reward=1.0))
    result_file = trial_dir / "result.json"
    data = json.loads(result_file.read_text(encoding="utf-8"))
    data["task_name"] = "fix-failing-test"
    result_file.write_text(json.dumps(data), encoding="utf-8")
    res = convert(str(trial_dir))
    assert res.doc["run"]["task_id"] == "fix-failing-test"
    assert any("task_id taken from result.json" in w for w in res.warnings)


def test_flat_layout_does_not_steal_an_unrelated_result(tmp_path):
    """A trajectory.json given directly in a flat layout must not reach upward
    past its own directory and adopt some other trial's result.json."""
    outer = tmp_path / "other_trial"
    outer.mkdir()
    (outer / "result.json").write_text(json.dumps({"reward": 1.0}), encoding="utf-8")
    inner = tmp_path / "my_trial"
    inner.mkdir()
    traj = {"schema_version": "ATIF-v1.7", "agent": {}, "steps":
            [{"step_id": 1, "source": "user", "message": "do it"}]}
    (inner / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")

    res = convert(inner / "trajectory.json", task_id="t")
    # No result beside this trajectory: ingests UNVERIFIED, not a borrowed pass.
    assert "verifier" not in res.doc
    assert any("UNVERIFIED" in w for w in res.warnings)


def test_iter_trials_resolves_job_directories(tmp_path):
    """A harbor run job directory (one subdir per trial) fans out; a single
    trial directory resolves to itself."""
    from agr.ingest_harbor import iter_trials

    def _mk_trial(parent: Path, name: str) -> None:
        d = parent / name
        (d / "agent").mkdir(parents=True)
        (d / "agent" / "trajectory.json").write_text("{}", encoding="utf-8")
        (d / "result.json").write_text("{}", encoding="utf-8")

    job = tmp_path / "job"
    _mk_trial(job, "b_trial")
    _mk_trial(job, "a_trial")
    trials = iter_trials(job)
    assert [t.name for t in trials] == ["a_trial", "b_trial"]

    single = tmp_path / "solo"
    _mk_trial(tmp_path, "solo")
    assert iter_trials(single) == [single]

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError, match="no Harbor trial"):
        iter_trials(empty)


def test_job_root_with_rollup_result_is_not_mistaken_for_a_trial(tmp_path):
    """A real `harbor run` job dir carries a top-level result.json beside the
    trial subdirs; discovery must fan out to the trials, not stop at the root."""
    from agr.ingest_harbor import iter_trials

    job = tmp_path / "job"
    job.mkdir()
    (job / "result.json").write_text("{}", encoding="utf-8")  # roll-up result
    for name in ("t1", "t2"):
        d = job / f"{name}__agent__1"
        (d / "agent").mkdir(parents=True)
        (d / "agent" / "trajectory.json").write_text("{}", encoding="utf-8")
    # An errored trial with a result but no trajectory is not reviewable.
    err = job / "errored__agent__1"
    err.mkdir()
    (err / "result.json").write_text("{}", encoding="utf-8")

    assert [t.name for t in iter_trials(job)] == ["t1__agent__1", "t2__agent__1"]


def test_bom_encoded_trial_files_are_tolerated(tmp_path):
    """Logs written by Windows tooling often carry a UTF-8 BOM; the adapter
    must read them, not reject them as invalid JSON."""
    steps = [{"step_id": 1, "source": "user", "message": "do it"}]
    trial_dir = Path(_trial(tmp_path, steps, reward=None))
    for rel in ("result.json", "agent/trajectory.json"):
        p = trial_dir / rel
        p.write_bytes(b"\xef\xbb\xbf" + p.read_bytes())
    res = convert(str(trial_dir), task_id="t")
    assert res.doc["verifier"]["checks"][0]["status"] == "unknown"


def test_cli_ingest_harbor_batches_a_job_directory(tmp_path):
    """The first-class verb ingests every trial in one command."""
    from agr.cli import main

    def _mk_trial(parent: Path, name: str) -> None:
        d = parent / name
        (d / "agent").mkdir(parents=True)
        traj = {"schema_version": "ATIF-v1.7", "trajectory_id": f"tr-{name}",
                "agent": {"name": "pi-coding-agent", "model_name": "gpt-oss"},
                "steps": [{"step_id": 1, "source": "user", "message": "do it"}]}
        (d / "agent" / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")
        (d / "result.json").write_text(
            json.dumps({"reward": 1.0, "task_name": name}), encoding="utf-8")

    job = tmp_path / "job"
    _mk_trial(job, "alpha")
    _mk_trial(job, "beta")
    store = tmp_path / "store"

    rc = main(["--store", str(store), "ingest-harbor", str(job)])
    assert rc == 0
    from agr.store import Store

    ingested_store = Store(str(store))
    # One capture registered per trial, keyed by the derived logical run id.
    assert ingested_store.read_index("harbor__alpha__tr-alpha")
    assert ingested_store.read_index("harbor__beta__tr-beta")
