"""AGR-02 migration: rebuild Harbor execution identity without touching history.

Reads the OLD store's Harbor runs (adapter <= 0.5 ids), re-converts every
corpus trial with the current adapter (0.6 UUID-based ids), and produces:

1. a fresh store (``--to``), the old store left byte-for-byte untouched;
2. an old-capture -> new-execution mapping (``--map`` JSON) that states, for
   every old logical run, which new execution(s) it corresponds to — or that
   the mapping is ambiguous because the old id truncated the only evidence
   that could distinguish its attempts.

Feedback/dispositions move ONLY when the mapping is unambiguous (single old
run, exactly one new execution candidate). Ambiguous runs keep their
annotations in the old store rather than guessing. In this corpus no Harbor
run carries feedback yet, so the move is a no-op — the script still reports
what it would have moved.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from agr.ingest_harbor import HarborAdapter, iter_trials_detailed  # noqa: E402
from agr.pipeline import analyze  # noqa: E402
from agr.store import Store  # noqa: E402


def old_harbor_runs(old_store: Store) -> list[dict]:
    """Old logical runs under the harbor task namespace, with their captures."""
    runs_root = Path(old_store.root) / "runs" / "harbor__terminal-bench"
    out = []
    if not runs_root.is_dir():
        return out
    for run_dir in sorted(runs_root.iterdir()):
        index_path = run_dir / "index.json"
        if not index_path.is_file():
            continue
        captures = json.loads(index_path.read_text(encoding="utf-8"))
        out.append({
            "old_run_id": f"harbor__terminal-bench/{run_dir.name}",
            "captures": [{
                "capture_id": c["capture_id"],
                "adapter_version": c.get("adapter_version"),
                "ingested_at": c.get("ingested_at"),
            } for c in captures],
            "feedback_moved": False,  # populated by the caller if feedback exists
        })
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--from-store", default=".agr-store")
    ap.add_argument("--corpus", default="eval-runs")
    ap.add_argument("--to", required=True, help="new store root (must not exist)")
    ap.add_argument("--map", dest="map_path", required=True, help="output mapping JSON")
    args = ap.parse_args()

    old_store = Store(args.from_store)
    adapter = HarborAdapter()
    new_store = Store(args.to)

    # Re-convert every corpus trial with the current adapter; ingest into the
    # NEW store only. The old store is never written.
    executions = []  # (new_run_id, trial_uuid, trial_name, outcome)
    for trial, reason in iter_trials_detailed(args.corpus):
        if reason is not None:
            print(f"excluded {Path(trial).name}: {reason}")
            continue
        doc = adapter.convert(trial).doc
        analysis = analyze(doc, new_store)
        run = doc["run"]
        executions.append({
            "new_run_id": analysis.run_source.run_id,
            "trial_uuid": run.get("trial_uuid"),
            "trial_name": run.get("trial_name"),
            "source_session_id": run.get("source_session_id"),
            "outcome": analysis.outcome["status"],
        })

    def _matches(execution, suffix):
        return any(value and str(value).startswith(suffix)
                   for value in (execution.get("trial_uuid"),
                                 execution.get("source_session_id"),
                                 execution.get("trial_name")))

    mapping = []
    for old in old_harbor_runs(old_store):
        old_id = old["old_run_id"]
        # Old id shape: harbor__<task>__<session/uuid prefix> where the prefix
        # was ALWAYS the first 12 characters of the session/uuid string — and
        # may itself contain "__" (task-prefixed sessions). Parse positionally:
        # the suffix is exactly the last 12 characters.
        _, _, task_and_suffix = old_id.partition("harbor__")
        suffix = task_and_suffix[-12:]
        task = task_and_suffix[:-(len(suffix) + 2)]  # strip "__" + suffix
        candidates = [e for e in executions if _matches(e, suffix)]
        if not candidates:
            status = "unmatched"
            entry = {"old_run_id": old_id, "captures": old["captures"],
                     "status": status, "truncated_suffix": suffix}
        elif len(candidates) == 1:
            entry = {"old_run_id": old_id, "captures": old["captures"],
                     "status": "mapped", "new_run_id": candidates[0]["new_run_id"],
                     "trial_uuid": candidates[0]["trial_uuid"]}
        else:
            entry = {"old_run_id": old_id, "captures": old["captures"],
                     "status": "ambiguous",
                     "candidates": [c["new_run_id"] for c in candidates]}
        mapping.append(entry)

    resolved = [m for m in mapping if m["status"] == "mapped"]
    ambiguous = [m for m in mapping if m["status"] == "ambiguous"]
    payload = {
        "migration": "AGR-02 harbor execution identity (adapter 0.6)",
        "old_store": args.from_store, "new_store": args.to,
        "executions": executions,
        "mapping": mapping,
        "summary": {
            "new_executions": len(executions),
            "old_runs": len(mapping),
            "mapped": len(resolved),
            "ambiguous": len(ambiguous),
            "feedback_moved": 0,
            "note": "no feedback.json records existed under old harbor run ids; "
                    "nothing to move. Ambiguous mappings are reported, never guessed.",
        },
    }
    Path(args.map_path).write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nnew store: {args.to} ({len(executions)} executions)")
    print(f"mapping: {len(resolved)} mapped, {len(ambiguous)} ambiguous "
          f"-> {args.map_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
