"""Offline review probes for main d62efb8; synthetic data only."""
import copy
import json
import sys
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path.cwd()))

from agr import read, workflow
from agr.benchmark import build_run_manifest, run_benchmark
from agr.benchmark_whowhen import WHO_WHEN_ADAPTER
from agr.evaluation import create_audit_pack
from agr.model_packet import build_packet
from agr.ingest import ingest
from agr.model_reviewer import ScriptedReviewer
from agr.pipeline import analyze
from agr.redaction import redact, redact_value
from agr.reviewer import ReviewerContext
from agr.schema import DerivedEvent
from agr.store import Store

root = Path(tempfile.mkdtemp(prefix="agr-main-review-probes-"))
out = {}

# Keys used here are deliberately fabricated and cannot authenticate anywhere.
fake_keys = {name: prefix + "a" * 48 for name, prefix in (
    ("project_key", "sk-proj-"), ("router_key", "sk-or-v1-"),
    ("service_key", "sk-svcacct-"))}
out["redaction"] = {}
for name, fake in fake_keys.items():
    ev = DerivedEvent("evt_001", "probe", "capture_1", 1, ["s1"],
                      "model_output", "assistant", payload={"content": f"API_KEY={fake}"})
    ctx = ReviewerContext("probe", "capture_1", [], [], [], [ev], task_instruction=fake)
    packet, metadata = build_packet(ctx)
    final, _ = redact_value(packet)
    out["redaction"][name] = {"direct_survives": fake in redact(fake).text,
                              "provider_packet_survives": fake in json.dumps(final)}

docs = []
for name in ("clean_pass.atif.json", "ignored_failure.atif.json"):
    docs.append(json.loads((Path("archive/synthetic/fixtures") / name).read_text(encoding="utf-8")))

# Sample real-shaped verifier documents with per-check status and no top-level status.
audit_store = Store(str(root / "audit-store"))
for doc in docs:
    analyze(doc, audit_store, reviewer=ScriptedReviewer({"moments": []}))
pack = create_audit_pack(audit_store, str(root / "audit-pack"), sample_size=2)
out["audit_strata"] = {"actual_outcomes": [row["outcome"]["status"] for row in read.list_runs(audit_store)],
                        "recorded_strata": pack["strata"],
                        "source_verifier_keys": [list(doc.get("verifier", {})) for doc in docs]}

# Synchronize two genuine editors after each has read the same workflow version.
race_store = Store(str(root / "race-store"))
analysis = analyze(docs[0], race_store)
rid = analysis.run_source.run_id
barrier = threading.Barrier(2)
original_read = workflow.read_workflow
original_write = race_store.write_derived
write_lock = threading.Lock()
def synchronized_read(store, run_id):
    value = original_read(store, run_id)
    barrier.wait(timeout=10)
    return value
def serialized_file_write(*args):
    # Avoid conflating the optimistic-lock failure with concurrent file truncation.
    with write_lock:
        return original_write(*args)
with patch.object(workflow, "read_workflow", synchronized_read), patch.object(race_store, "write_derived", serialized_file_write):
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(workflow.set_workflow, race_store, rid,
                                   actor=actor, base_version=0, note=actor)
                   for actor in ("editor-A", "editor-B")]
        results = [future.result() for future in futures]
final_workflow = original_read(race_store, rid)
out["workflow_race"] = {"accepted_versions": [row["workflow_version"] for row in results],
                         "persisted_version": final_workflow["workflow_version"],
                         "persisted_revision_count": len(final_workflow["revisions"]),
                         "persisted_note": final_workflow["note"]}

# A deterministic benchmark rerun must score the deterministic slot in a reused store.
data = "tests/fixtures/whowhen/Algorithm-Generated"
case = WHO_WHEN_ADAPTER.load_cases(data)[0][0]
benchmark_store = Store(str(root / "benchmark-store"))
analyze(case.doc, benchmark_store, reviewer=ScriptedReviewer({"moments": []}))
manifest = build_run_manifest(WHO_WHEN_ADAPTER, data, limit=None, provider=None, model=None)
result = run_benchmark(WHO_WHEN_ADAPTER, data, benchmark_store, run_manifest=manifest)
out["benchmark_reused_store"] = {
    "declared_reviewer": manifest["reviewer"],
    "actually_served_key": read.get_review(benchmark_store, case.run_id)["served_reviewer_key"],
    "scored_status": result.scores[0].review_status,
    "explicit_deterministic_status": read.get_review(benchmark_store, case.run_id, reviewer_key="deterministic")["review_status"],
    "explicit_deterministic_moments": len(read.get_review(benchmark_store, case.run_id, reviewer_key="deterministic")["moments"]),
}

# An ordinary command containing 'check' must not establish artifact verification.
verification_doc = json.loads(Path("archive/synthetic/fixtures/chess_best_move.atif.json").read_text(encoding="utf-8"))
verification_doc["run"]["logical_run_id"] = "verification-keyword-probe"
verification_doc["steps"].insert(7, {"step_id": "s7a", "kind": "tool_call", "actor": "main_agent",
                                     "tool": "shell", "content": "git checkout -b feature"})
verification_analysis = analyze(verification_doc, Store(str(root / "verification-store")))
out["verification_keyword"] = next(row.to_dict() for row in verification_analysis.signature
                                    if row.ability == "Verify before submission")

# Supersession is computed by the pipeline, but the signature still reads raw status.
superseded_doc = copy.deepcopy(docs[0])
superseded_doc["run"]["logical_run_id"] = "signature-supersession-probe"
superseded_doc["verifier"] = {"checks": [
    {"check_id": "C-old", "name": "same test suite", "status": "failed",
     "source": "output_interpretation", "timing": "during_run", "scope": "pytest -q", "sequence": 1},
    {"check_id": "C-new", "name": "same test suite", "status": "passed",
     "source": "output_interpretation", "timing": "during_run", "scope": "pytest -q", "sequence": 2},
]}
superseded_analysis = analyze(superseded_doc, Store(str(root / "superseded-store")))
out["signature_supersession"] = {
    "checks": [{"id": c.check_id, "effective_status": c.effective_status} for c in superseded_analysis.checks],
    "signature_rows": [row.to_dict() for row in superseded_analysis.signature if row.ability == "same test suite"],
}

# The public adapter override creates two registrations sharing one capture path.
lineage_store = Store(str(root / "lineage-store"))
first = ingest(docs[0], lineage_store, adapter_version="adapter-v1")
second = ingest(docs[0], lineage_store, adapter_version="adapter-v2")
out["capture_lineage"] = {
    "first_capture_id": first.run_source.source_capture_id,
    "second_capture_id": second.run_source.source_capture_id,
    "second_supersedes": second.run_source.supersedes_source_capture_id,
    "index_revisions": [row["capture_revision"] for row in lineage_store.read_index(first.run_source.run_id)],
    "first_capture_stored_adapter": lineage_store.read_derived(first.run_source.run_id,
        first.run_source.source_capture_id, "run_source.json")["adapter_version"],
}

Path("review-probe-results.json").write_text(json.dumps(out, indent=2), encoding="utf-8")
print(json.dumps(out, indent=2))
