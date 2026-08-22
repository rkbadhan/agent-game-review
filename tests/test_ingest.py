"""Milestone 1 acceptance: trustworthy source and capture linkage (spec §20)."""

import copy
import json
import os

import pytest

from agr.ingest import IngestError, ingest
from agr.store import Store, canonical_bytes, source_hash

FIXTURE = os.path.join(os.path.dirname(__file__), "..", "archive", "synthetic", "fixtures", "chess_best_move.atif.json")


def load_fixture():
    with open(FIXTURE, encoding="utf-8") as fh:
        return json.load(fh)


def test_ingest_creates_capture_and_hash_roundtrips(tmp_path):
    store = Store(str(tmp_path / "store"))
    doc = load_fixture()

    result = ingest(doc, store)
    rs = result.run_source

    assert result.idempotent is False
    assert rs.run_id == "chess_best_move__seed42"
    assert rs.source_schema == "ATIF-v1.7"
    assert rs.capture_revision == 1
    assert rs.supersedes_source_capture_id is None

    # Imported source bytes match the recorded hash (spec §20 Milestone 1).
    stored = store.read_source(rs.run_id, rs.source_capture_id)
    assert source_hash(stored) == rs.source_hash
    assert canonical_bytes(stored) == canonical_bytes(doc)


def test_reingest_same_source_is_idempotent(tmp_path):
    store = Store(str(tmp_path / "store"))
    doc = load_fixture()

    first = ingest(doc, store)
    second = ingest(doc, store)

    assert first.idempotent is False
    assert second.idempotent is True
    assert first.run_source.source_capture_id == second.run_source.source_capture_id
    assert len(store.read_index(first.run_source.run_id)) == 1


def test_fuller_capture_links_as_new_revision(tmp_path):
    store = Store(str(tmp_path / "store"))
    doc = load_fixture()
    first = ingest(doc, store)

    # A fuller capture of the SAME logical run: filesystem now fully captured.
    fuller = copy.deepcopy(doc)
    fuller["capabilities"]["filesystem"] = "complete"
    fuller["capture_completeness"] = "corrected"
    second = ingest(fuller, store)

    assert second.idempotent is False
    assert second.run_source.run_id == first.run_source.run_id  # same logical run
    assert second.run_source.source_capture_id != first.run_source.source_capture_id
    assert second.run_source.capture_revision == 2
    assert second.run_source.supersedes_source_capture_id == first.run_source.source_capture_id

    # Earlier capture remains immutable and readable.
    old = store.read_source(first.run_source.run_id, first.run_source.source_capture_id)
    assert old["capabilities"]["filesystem"] == "checkpoint_only"
    assert len(store.read_index(first.run_source.run_id)) == 2


def test_capability_profile_defaults_missing_to_unavailable(tmp_path):
    store = Store(str(tmp_path / "store"))
    doc = load_fixture()
    del doc["capabilities"]  # adapter must default, never assume presence

    result = ingest(doc, store)
    caps = result.capabilities.capabilities
    assert caps["messages"] == "unavailable"
    assert caps["verifier_code"] == "unavailable"


def test_validation_rejects_malformed_sources(tmp_path):
    store = Store(str(tmp_path / "store"))

    with pytest.raises(IngestError):
        ingest({"atif_version": "1.7"}, store)  # no run

    dup = load_fixture()
    dup["steps"].append(dict(dup["steps"][0]))  # duplicate step_id
    with pytest.raises(IngestError):
        ingest(dup, store)


def test_validation_rejects_unknown_step_kind(tmp_path):
    """An out-of-vocabulary step kind fails at the contract boundary with a
    clear message, not deeper in event derivation (spec §7.2)."""
    store = Store(str(tmp_path / "store"))

    bad = load_fixture()
    bad["steps"][0] = dict(bad["steps"][0], kind="totally_made_up")
    with pytest.raises(IngestError, match="unknown kind"):
        ingest(bad, store)

    missing = load_fixture()
    missing["steps"][0] = {k: v for k, v in missing["steps"][0].items() if k != "kind"}
    with pytest.raises(IngestError, match="missing kind"):
        ingest(missing, store)


def test_adapter_provenance_is_per_source_not_a_global_default(tmp_path):
    """The doc's own adapter_version stamp wins; a raw ATIF document with no
    adapter in the path records the honest import default — never some other
    harness's name."""
    from agr.version import RAW_ATIF_IMPORT_VERSION

    store = Store(str(tmp_path / "store"))
    doc = load_fixture()  # fixture: no adapter_version stamp (raw ATIF path)
    rs = ingest(doc, store).run_source
    assert rs.adapter_version == RAW_ATIF_IMPORT_VERSION

    stamped = load_fixture()
    stamped["adapter_version"] = "harbor-adapter-9.9"
    rs2 = ingest(stamped, Store(str(tmp_path / "store2"))).run_source
    assert rs2.adapter_version == "harbor-adapter-9.9"

    explicit = load_fixture()
    rs3 = ingest(explicit, Store(str(tmp_path / "store3")),
                 adapter_version="explicit-1.0").run_source
    assert rs3.adapter_version == "explicit-1.0"


def test_undeclared_source_type_is_unknown_never_assumed_harbor(tmp_path):
    store = Store(str(tmp_path / "store"))
    doc = load_fixture()
    del doc["source_type"]
    assert ingest(doc, store).run_source.source_type == "unknown"


def test_malformed_steps_and_capabilities_fail_as_ingest_errors(tmp_path):
    """Non-dict steps[] entries and non-object capabilities fail at the
    contract boundary as IngestError, not as an AttributeError deeper in."""
    store = Store(str(tmp_path / "store"))
    bad_steps = load_fixture()
    bad_steps["steps"][2] = None
    with pytest.raises(IngestError, match="must be a JSON object"):
        ingest(bad_steps, store)

    bad_caps = load_fixture()
    bad_caps["capabilities"] = ["messages", "complete"]
    with pytest.raises(IngestError, match="capabilities must be"):
        ingest(bad_caps, store)
