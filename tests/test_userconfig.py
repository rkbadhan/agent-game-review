"""Tests for `agr config` (user-level reviewer settings) and `agr review --all`.

The saved-settings file is redirected to tmp_path via AGR_CONFIG so tests never
touch the real user home. Model calls use ScriptedReviewer — no network.
"""

import json
from pathlib import Path

import pytest


@pytest.fixture()
def cfg_path(tmp_path, monkeypatch):
    p = tmp_path / "cfg" / "config.json"
    monkeypatch.setenv("AGR_CONFIG", str(p))
    return p


def _mk_trial(parent: Path, name: str) -> None:
    d = parent / name
    (d / "agent").mkdir(parents=True)
    traj = {"schema_version": "ATIF-v1.7", "trajectory_id": f"tr-{name}",
            "agent": {"name": "pi-coding-agent", "model_name": "gpt-oss"},
            "steps": [{"step_id": 1, "source": "user", "message": "do it"}]}
    (d / "agent" / "trajectory.json").write_text(json.dumps(traj), encoding="utf-8")
    (d / "result.json").write_text(
        json.dumps({"reward": 1.0, "task_name": name}), encoding="utf-8")


# --- userconfig: save / load / clear / resolve --------------------------------

def test_config_roundtrip(cfg_path):
    from agr.userconfig import clear_config, load_config, save_config

    assert load_config() == {}                       # missing file -> empty
    save_config(provider="openai", model="kimi-k3")
    save_config(base_url="https://openrouter.ai/api/v1")   # merge, not replace
    cfg = load_config()
    assert cfg["provider"] == "openai" and cfg["model"] == "kimi-k3"
    assert cfg["base_url"] == "https://openrouter.ai/api/v1"
    assert "updated_at" in cfg
    assert clear_config() and not cfg_path.exists()
    assert clear_config() is False                   # second clear is a no-op


def test_resolve_precedence_flag_beats_env_beats_file(cfg_path, monkeypatch):
    from agr.userconfig import resolve_review_settings, save_config

    # Hermetic: other test modules may have set this in the ambient environment.
    monkeypatch.delenv("AGR_REVIEW_MODEL", raising=False)

    # Nothing anywhere -> provider default, no model.
    s = resolve_review_settings()
    assert s == {"provider": "anthropic", "model": None, "base_url": None}

    # File sets a baseline.
    save_config(provider="openai", model="file-model", base_url="https://file")
    monkeypatch.setenv("AGR_REVIEW_MODEL", "env-model")

    s = resolve_review_settings()
    assert s["provider"] == "openai" and s["model"] == "env-model"  # env beats file
    assert s["base_url"] == "https://file"

    # Flags beat everything.
    s = resolve_review_settings(provider="anthropic", model="flag-model",
                                base_url="https://flag")
    assert s == {"provider": "anthropic", "model": "flag-model", "base_url": "https://flag"}


# --- `agr config` command ------------------------------------------------------

def test_cmd_config_save_show_clear(cfg_path, capsys):
    from agr.cli import main

    rc = main(["config", "--provider", "openai", "--model", "kimi-k3",
               "--base-url", "https://openrouter.ai/api/v1"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "saved reviewer settings" in out and "openai" in out
    assert cfg_path.exists()

    rc = main(["config"])           # show effective
    out = capsys.readouterr().out
    assert "kimi-k3" in out and "openrouter.ai/api/v1" in out

    rc = main(["config", "--clear"])
    assert rc == 0 and not cfg_path.exists()


def test_cmd_config_test_success(cfg_path, capsys, monkeypatch):
    from agr.cli import main
    import agr.model_reviewer as mr

    class _Fake:
        model = "fake-model"

        def _complete(self, system, user_json):
            return {"ok": True}

    monkeypatch.setattr(mr, "make_reviewer", lambda *a, **k: _Fake())
    rc = main(["config", "--provider", "openai", "--model", "fake-model", "--test"])
    out = capsys.readouterr().out
    assert rc == 0 and "OK" in out


def test_cmd_config_test_failure_is_actionable(cfg_path, capsys, monkeypatch):
    from agr.cli import main
    import agr.model_reviewer as mr

    def _boom(*a, **k):
        raise ConnectionError("401 Unauthorized")

    monkeypatch.setattr(mr, "make_reviewer", _boom)
    rc = main(["config", "--provider", "openai", "--model", "nope", "--test"])
    err = capsys.readouterr().err
    assert rc == 4 and "FAILED" in err and "API key" in err


# --- `agr review --all` ---------------------------------------------------------

def _ingest_two_runs(tmp_path):
    from agr.cli import main
    from agr.store import Store

    job = tmp_path / "job"
    _mk_trial(job, "alpha")
    _mk_trial(job, "beta")
    store = tmp_path / "store"
    assert main(["--store", str(store), "ingest-harbor", str(job)]) == 0
    # Adapter-0.6 identity (AGR-02): ids derive from the full task+session
    # identity, so read back whatever the store registered rather than guessing.
    live = Store(str(store))
    return live, sorted(p.name for p in (Path(live.root) / "runs").iterdir())


def test_review_all_enriches_then_skips(cfg_path, capsys, monkeypatch, tmp_path):
    from agr.cli import main
    from agr.model_reviewer import ScriptedReviewer

    store, run_ids = _ingest_two_runs(tmp_path)
    reviewer = ScriptedReviewer({"moments": []}, source="model:scripted-test")
    monkeypatch.setattr("agr.model_reviewer.make_reviewer", lambda *a, **k: reviewer)

    # First pass: both runs enriched.
    rc = main(["--store", str(store.root), "review", "--all"])
    out = capsys.readouterr().out
    assert rc == 0 and "reviewed 2 · skipped 0 · failed 0" in out
    for run_id in run_ids:
        keys = store.list_reviews(run_id, store.latest_capture_id(run_id))
        assert any(k.startswith("model") for k in keys)

    # Second pass: both skipped without --force.
    rc = main(["--store", str(store.root), "review", "--all"])
    out = capsys.readouterr().out
    assert rc == 0 and "reviewed 0 · skipped 2 · failed 0" in out

    # --force re-reviews.
    rc = main(["--store", str(store.root), "review", "--all", "--force"])
    out = capsys.readouterr().out
    assert rc == 0 and "reviewed 2 · skipped 0 · failed 0" in out


def test_review_all_failure_is_isolated(cfg_path, capsys, monkeypatch, tmp_path):
    """One failing run must not stop the batch; all-fail exits non-zero."""
    from agr.cli import main

    store, run_ids = _ingest_two_runs(tmp_path)

    def _boom(*a, **k):
        raise ConnectionError("endpoint down")

    monkeypatch.setattr("agr.model_reviewer.make_reviewer", _boom)
    rc = main(["--store", str(store.root), "review", "--all"])
    err = capsys.readouterr().err
    assert rc == 4 and "endpoint down" in err

    # Deterministic runs are untouched by failed model reviews.
    for run_id in run_ids:
        keys = store.list_reviews(run_id, store.latest_capture_id(run_id))
        assert "deterministic" in keys


def test_review_without_run_id_or_all_hints(cfg_path, capsys, tmp_path):
    from agr.cli import main

    store, _ = _ingest_two_runs(tmp_path)
    rc = main(["--store", str(store.root), "review"])
    assert rc == 1
    assert "--all" in capsys.readouterr().err
