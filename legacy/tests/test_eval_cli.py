"""Legacy ``eval`` command: baseline-vs-model rendering and clean failure exits."""

from __future__ import annotations

import json
import os

from agr.pipeline import analyze
from agr.store import Store


def test_eval_comparison_renders_and_verdicts(tmp_path, capsys):
    # Offline check of `agr eval --provider` rendering + M4 acceptance logic:
    # compared against itself, a reviewer never regresses -> VERDICT PASS.
    import glob

    from legacy import cli
    from legacy import reviewer_eval as rev
    from legacy.gold import load_gold_set

    store = Store(str(tmp_path / "store"))
    fixtures_dir = os.path.join(os.path.dirname(__file__), "..", "..", "archive", "synthetic", "fixtures")
    for p in sorted(glob.glob(os.path.join(fixtures_dir, "*.atif.json"))):
        with open(p, encoding="utf-8") as fh:
            analyze(json.load(fh), store)
    gs = load_gold_set(os.path.join(os.path.dirname(__file__), "..", "gold", "synthetic"))
    result = rev.evaluate_store(store, gs)

    cli._print_eval_comparison(result, result, "openai", "demo-model")
    out = capsys.readouterr().out
    assert "baseline vs model reviewer (openai · demo-model)" in out
    assert "M4 acceptance" in out
    assert "VERDICT: PASS" in out  # identical to itself -> no regression on any gate


def test_eval_provider_missing_sdk_exits_cleanly_not_a_traceback(tmp_path, monkeypatch, capsys):
    """`agr eval --provider` used to only catch ValueError around make_reviewer,
    so the RuntimeError make_reviewer now raises for a missing SDK (P0-3's
    fail-fast check) propagated as an unhandled traceback instead of the
    same clean 'cannot run model reviewer' / exit 3 every other command
    gives for this exact failure."""
    from agr import model_reviewer
    from legacy import cli

    def _boom(*a, **k):
        raise RuntimeError("The OpenAI model reviewer requires the 'openai' SDK.")

    monkeypatch.setattr(model_reviewer, "make_reviewer", _boom)
    here = os.path.dirname(__file__)
    rc = cli.main(["--store", str(tmp_path / "store"), "eval",
                   "--gold", os.path.join(here, "..", "gold", "synthetic"),
                   "--fixtures", os.path.join(here, "..", "..", "archive", "synthetic", "fixtures"),
                   "--provider", "openai"])
    err = capsys.readouterr().err
    assert rc == 3
    assert "cannot run model reviewer" in err and "openai" in err.lower()
