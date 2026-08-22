"""Milestone 2 acceptance tests — task contract and atomic checks (spec §20).

Accept when:
  * every structured verifier check maps to a confirmed item or warning;
  * prompt-only and verifier-only requirements are surfaced;
  * provisional reviews are watermarked; and
  * seeded contract/verifier mismatches are detected.
"""

import os
import subprocess
import sys

from agr.contract import build_contract, confirm_contract, derive_observations
from agr.pipeline import analyze
from agr.store import Store

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _analyze(load_fixture, name, tmp_path):
    store = Store(str(tmp_path / "store"))
    return analyze(load_fixture(name), store)


# --- Clean, confirmed contract (chess) ---------------------------------------

def test_chess_contract_is_confirmed_and_unwatermarked(load_fixture, tmp_path):
    a = _analyze(load_fixture, "chess_best_move.atif.json", tmp_path)
    c = a.contract
    assert c.status == "human_confirmed"
    assert c.contract_version == 2
    assert c.supersedes_contract_version == 1
    assert c.warnings == []
    assert a.watermark is None
    assert all(i.human_status == "confirmed" for i in c.items)


def test_every_structured_check_maps_to_a_contract_item(load_fixture, tmp_path):
    a = _analyze(load_fixture, "chess_best_move.atif.json", tmp_path)
    mapped = {cid for item in a.contract.items for cid in item.mapped_checks}
    for check in a.checks:
        assert check.check_id in mapped, f"{check.check_id} maps to no contract item"


def test_contract_observation_tracks_failed_check(load_fixture, tmp_path):
    a = _analyze(load_fixture, "chess_best_move.atif.json", tmp_path)
    by_item = {o.contract_item_id: o for o in a.contract_observations}
    # R3 is covered by C3, which failed -> the requirement is evidenced-violated.
    assert by_item["R3"].status == "evidenced_violated"
    assert by_item["R3"].evidence == ["C3"]
    # A passing requirement is evidenced-satisfied.
    assert by_item["R1"].status == "evidenced_satisfied"


# --- Seeded mismatches (greeting-report) -------------------------------------

def test_mismatch_review_is_watermarked(load_fixture, tmp_path):
    a = _analyze(load_fixture, "contract_mismatch.atif.json", tmp_path)
    assert a.contract.status == "draft"
    assert a.contract.watermarked
    assert a.watermark is not None and "PROVISIONAL" in a.watermark


def test_all_seeded_mismatch_types_are_detected(load_fixture, tmp_path):
    a = _analyze(load_fixture, "contract_mismatch.atif.json", tmp_path)
    kinds = {w.warning_type for w in a.contract.warnings}
    assert kinds == {
        "verifier_only_requirement",
        "prompt_only_unverified_requirement",
        "reference_only_assumption",
        "uncovered_artifact_requirement",
        "contradiction",
    }


def test_verifier_only_requirement_is_surfaced_and_mapped(load_fixture, tmp_path):
    a = _analyze(load_fixture, "contract_mismatch.atif.json", tmp_path)
    # C2 declared no contract item; it must still map to a surfaced item.
    warn = next(w for w in a.contract.warnings if w.warning_type == "verifier_only_requirement")
    assert warn.check_ids == ["C2"]
    synth = a.contract.item(warn.item_ids[0])
    assert synth is not None and synth.source_type == "verifier_enforced"
    assert synth.mapped_checks == ["C2"]
    # Acceptance: every structured check maps to at least one item.
    mapped = {cid for item in a.contract.items for cid in item.mapped_checks}
    assert {c.check_id for c in a.checks} <= mapped


def test_prompt_only_requirement_is_surfaced(load_fixture, tmp_path):
    a = _analyze(load_fixture, "contract_mismatch.atif.json", tmp_path)
    prompt_only = {i for w in a.contract.warnings
                   if w.warning_type == "prompt_only_unverified_requirement"
                   for i in w.item_ids}
    assert "R2" in prompt_only  # "must be polite" — stated but unverified


def test_contradiction_between_items_is_detected(load_fixture, tmp_path):
    a = _analyze(load_fixture, "contract_mismatch.atif.json", tmp_path)
    contra = next(w for w in a.contract.warnings if w.warning_type == "contradiction")
    assert contra.item_ids == ["R1", "R3"]  # /out.txt both required and forbidden


# --- Versioning and confirmation flow (unit-level) ---------------------------

def test_confirm_bumps_version_and_supersedes(load_fixture, tmp_path):
    from agr.checks import extract_checks
    from agr.ingest import ingest

    store = Store(str(tmp_path / "store"))
    doc = load_fixture("contract_mismatch.atif.json")
    rs = ingest(doc, store).run_source
    draft = build_contract(doc, rs, extract_checks(doc, rs))
    assert draft.status == "draft" and draft.contract_version == 1

    decisions = {i.id: "confirmed" for i in draft.items}
    confirmed = confirm_contract(draft, decisions, confirmed_by="r")
    assert confirmed.status == "human_confirmed"
    assert confirmed.contract_version == 2
    assert confirmed.supersedes_contract_version == 1
    assert not confirmed.watermarked
    # Warnings are carried forward, not erased by confirmation.
    assert len(confirmed.warnings) == len(draft.warnings)


def test_partial_confirmation_stays_provisional(load_fixture, tmp_path):
    from agr.checks import extract_checks
    from agr.ingest import ingest

    store = Store(str(tmp_path / "store"))
    doc = load_fixture("contract_mismatch.atif.json")
    rs = ingest(doc, store).run_source
    draft = build_contract(doc, rs, extract_checks(doc, rs))

    # Confirm only one required item.
    confirmed = confirm_contract(draft, {"R1": "confirmed"})
    assert confirmed.status == "provisional"
    assert confirmed.watermarked  # still watermarked


def test_cli_confirm_persists_and_clears_watermark(load_fixture, tmp_path):
    store = Store(str(tmp_path / "store"))
    doc = load_fixture("contract_mismatch.atif.json")
    a = analyze(doc, store)
    rs = a.run_source
    assert a.watermark is not None

    # Simulate the CLI confirm command: persist a confirmation decision.
    store.write_derived(rs.run_id, rs.source_capture_id, "contract_confirmation.json",
                        {"confirm_all": True, "confirmed_by": "reviewer"})
    a2 = analyze(doc, store)  # deterministic recompute reads the persisted decision
    assert a2.contract.status == "human_confirmed"
    assert a2.watermark is None
    assert a2.contract.confirmed_by == "reviewer"


def test_contract_derived_records_persisted(load_fixture, tmp_path):
    store = Store(str(tmp_path / "store"))
    a = analyze(load_fixture("chess_best_move.atif.json"), store)
    rs = a.run_source
    for name in ("contract.json", "contract_history.json", "contract_observations.json"):
        assert store.has_derived(rs.run_id, rs.source_capture_id, name)
    history = store.read_derived(rs.run_id, rs.source_capture_id, "contract_history.json")
    # Draft (superseded) + confirmed version.
    assert [h["status"] for h in history] == ["superseded", "human_confirmed"]


def test_build_contract_is_deterministic(load_fixture, tmp_path):
    from agr.checks import extract_checks
    from agr.ingest import ingest

    store = Store(str(tmp_path / "store"))
    doc = load_fixture("contract_mismatch.atif.json")
    rs = ingest(doc, store).run_source
    first = build_contract(doc, rs, extract_checks(doc, rs)).to_dict()
    second = build_contract(doc, rs, extract_checks(doc, rs)).to_dict()
    assert first == second


# --- CLI robustness (regression: cp1252 console must not crash) --------------

def _run_cli(args, tmp_path, encoding="cp1252"):
    """Run the CLI in a subprocess with a forced stdout encoding.

    ``PYTHONIOENCODING=cp1252`` reproduces the Windows-default encoder on any
    OS, so this guards the UnicodeEncodeError regression on the UTF-8 CI runner
    too — the in-process object-level tests never touch the real encoder.
    """
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = encoding
    return subprocess.run(
        [sys.executable, "-m", "agr", "--store", str(tmp_path / "store"), *args],
        cwd=REPO_ROOT, env=env, capture_output=True,
    )


def test_cli_show_survives_cp1252_console(tmp_path):
    ing = _run_cli(["ingest", "archive/synthetic/fixtures/chess_best_move.atif.json"], tmp_path)
    assert ing.returncode == 0, ing.stderr
    # `show` renders the `→` glyph (U+2192), which cp1252 cannot encode; before
    # the UTF-8 guard this crashed with UnicodeEncodeError partway through.
    show = _run_cli(["show", "chess_best_move__seed42"], tmp_path)
    assert show.returncode == 0, show.stderr.decode("utf-8", "replace")
    assert "→".encode("utf-8") in show.stdout  # arrow survived intact


def test_cli_confirm_survives_cp1252_console(tmp_path):
    assert _run_cli(["ingest", "archive/synthetic/fixtures/contract_mismatch.atif.json"], tmp_path).returncode == 0
    # The watermark carries an em dash and `confirm` prints `·`; exercise both.
    show = _run_cli(["show", "greeting_report__seed7"], tmp_path)
    assert show.returncode == 0, show.stderr.decode("utf-8", "replace")
    conf = _run_cli(["confirm", "greeting_report__seed7", "--all", "--by", "r"], tmp_path)
    assert conf.returncode == 0, conf.stderr.decode("utf-8", "replace")
