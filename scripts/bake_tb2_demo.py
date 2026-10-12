"""Rebuild the offline 350-run demo from a local copy of the pinned public corpus.

Maintainer-only: agr demo reads the committed records and never runs this fetch/bake step.
"""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from agr.ingest_harbor import convert, HARBOR_ADAPTER_VERSION
from agr.store import source_hash

REPOSITORY = "harborframework/terminal-bench-2-leaderboard"
REVISION = "572b2614be2c0cb2527e14f5b1e4026f1072e6c1"
SUBMISSIONS = ["Terminus2__GLM-4.7", "Terminus2__GLM-5", "Terminus2__DeepSeek-V3.2",
    "Terminus2__Kimi-k2.5", "Terminus2__Minimax-m2.5", "Terminus2__GPT-5.3-Codex", "ClaudeCode__GLM-4.7"]
TASKS = ["filter-js-from-html", "mteb-retrieve", "polyglot-c-py", "fix-git",
    "nginx-request-logging", "crack-7z-hash", "cancel-async-tasks", "kv-store-grpc",
    "pypi-server", "sparql-university"]
SOURCE_FILES = ["agent/trajectory.json", "config.json", "result.json", "verifier/ctrf.json",
                "verifier/reward.txt", "verifier/test-stdout.txt"]


def bake(data, out):
    import tempfile
    import shutil
    if not data.is_dir():
        raise ValueError("Missing local corpus")
    # The committed manifest pins the exact original inputs independently of
    # the caller's local download. Never trust a self-reported local revision.
    reference = Path(__file__).resolve().parents[1] / "agr/demo_fixtures/tb2_leaderboard/manifest.json"
    fetched = json.loads(reference.read_text(encoding="utf-8"))
    if fetched["repository"] != REPOSITORY or fetched["revision"] != REVISION:
        raise ValueError("Local corpus revision differs from the pinned demo source")
    expected = {row["path"].removeprefix("submissions/terminal-bench/2.0/"): row["sha256"]
                for run in fetched["runs"] for row in run["source_files"]}
    entries, outcomes = [], {}
    out.parent.mkdir(parents=True, exist_ok=True)
    # Build beside the destination; validate the full slice before replacing it.
    staging = Path(tempfile.mkdtemp(prefix=".tb2-bake-", dir=out.parent))
    try:
        (staging / "runs").mkdir()
        for submission in SUBMISSIONS:
            outcomes[submission] = Counter()
            for task in TASKS:
                sources = sorted((data / submission).glob("*/" + task + "__*/agent/trajectory.json"))
                if len(sources) != 5:
                    raise ValueError(f"Expected five attempts for {submission}/{task}, found {len(sources)}")
                for trajectory in sources:
                    trial = trajectory.parent.parent
                    for relative in SOURCE_FILES:
                        path = trial / relative
                        key = path.relative_to(data).as_posix()
                        if path.is_file():
                            if hashlib.sha256(path.read_bytes()).hexdigest() != expected.get(key):
                                raise ValueError("Unverified source: " + key)
                        elif key in expected:
                            raise ValueError("Missing source: " + key)
                    doc = convert(str(trial), configuration_id=submission).doc
                    name = doc["run"]["logical_run_id"].replace("/", "__") + ".atif.json"
                    payload = (json.dumps(doc, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
                    (staging / "runs" / name).write_bytes(payload)
                    originals = []
                    for relative in SOURCE_FILES:
                        path = trial / relative
                        if path.is_file():
                            originals.append({"path": "submissions/terminal-bench/2.0/" + path.relative_to(data).as_posix(),
                                "sha256": hashlib.sha256(path.read_bytes()).hexdigest()})
                    entries.append({"file": "runs/" + name, "run_id": doc["run"]["logical_run_id"],
                        "submission": submission, "task": task, "source_hash": source_hash(doc),
                        "sha256": hashlib.sha256(payload).hexdigest(), "bytes": len(payload), "source_files": originals})
                    result = json.loads((trial / "result.json").read_text(encoding="utf-8"))
                    reward = ((result.get("verifier_result") or {}).get("rewards") or {}).get("reward")
                    outcomes[submission]["passed" if reward == 1 else "failed" if reward == 0 else "unknown"] += 1
        assert len(entries) == len({e["run_id"] for e in entries}) == 350
        manifest = {"schema": "tb2-demo-1", "repository": REPOSITORY, "revision": REVISION,
            "license": "Apache-2.0", "adapter_version": HARBOR_ADAPTER_VERSION,
            "submissions": SUBMISSIONS, "tasks": TASKS, "attempts_per_task_configuration": 5,
            "run_count": len(entries), "bytes": sum(e["bytes"] for e in entries),
            "outcomes": {k:dict(v) for k,v in outcomes.items()}, "runs": entries}
        (staging / "manifest.json").write_bytes((json.dumps(manifest, indent=2) + "\n").encode("utf-8"))
        # Preserve hand-maintained attribution and guide files on a rebake.
        if out.exists():
            for name in ("SOURCE.md", "LICENSE", "START_HERE.md"):
                if (out / name).is_file(): shutil.copy2(out / name, staging / name)
            # Never recursively remove a caller-selected directory.
            if {p.name for p in out.iterdir()} - {"runs", "manifest.json", "SOURCE.md", "LICENSE", "START_HERE.md"}:
                raise ValueError("Destination contains unrelated files")
            backup = out.with_name(out.name + ".previous")
            if backup.exists(): raise ValueError("Previous bake backup exists; inspect it before rebaking")
            out.rename(backup)
            staging.rename(out)
            print("Previous slice preserved at " + str(backup))
        else:
            staging.rename(out)
        print(json.dumps({"runs":350,"bytes":manifest["bytes"],"outcomes":manifest["outcomes"]}, indent=2))
    except Exception:
        # Keep failed staging data available for inspection rather than deleting caller data.
        print("Incomplete bake retained at " + str(staging), file=sys.stderr)
        raise

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("data", type=Path)
    parser.add_argument("--out", type=Path, default=Path(__file__).resolve().parents[1] / "agr/demo_fixtures/tb2_leaderboard")
    args = parser.parse_args()
    bake(args.data, args.out)
