"""Offline, packaged leaderboard slice used by agr demo."""
from __future__ import annotations
import hashlib
import importlib.resources as resources
import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from . import version
from .store import Store, source_hash
from .storage_io import atomic_json


def dataset_dir() -> Path:
    return Path(str(resources.files("agr").joinpath("demo_fixtures", "tb2_leaderboard")))


def start_here() -> dict:
    polyglot = "harbor__terminal-bench/polyglot-c-py__6c3b4b8b-991"
    retail = "harbor__sierra-research/tau3-bench__tau3-retail-29__2f510d52-4453-4982-95f1-b2f0e3829164"
    def link(**params):
        return "/?" + urlencode(params)
    return {"title": "Start here", "description": "Four stops through real recorded runs. No downloads, credentials, or model calls.",
        "stories": [
            {"title": "One failed run: polyglot-c-py", "description": "Inspect the successful smoke tests at step 22, the later edit at step 25, and the final Checks output showing C=6.",
             "href": link(run=polyglot, chapter="moments"), "run_id": polyglot},
            {"title": "A customer ends the chat: retail-29", "description": "The customer said yes and ended the chat. Inspect the recorded stop and the review's evidence limits before attributing a failure to the agent.",
             "href": link(run=retail, chapter="outcome"), "run_id": retail},
            {"title": "Patterns: a shared test failure", "description": "Find filter-js-from-html: the same test failure appears across all seven configurations. Shared failures are a reason to investigate, not proof of who caused them.",
             "href": link(view="fleet", pattern_type="tests", test_group_by="task,test,signature")},
            {"title": "Compare: GLM-4.7 to GLM-5", "description": "On these ten selected tasks, Terminus 2 improves from 19/50 to 28/50 passed attempts (38% to 56%). These are sample results, not full-leaderboard scores.",
             "href": link(view="versions", baseline="Terminus2__GLM-4.7", candidate="Terminus2__GLM-5", axis="model")}],
        "own_runs_command": "agr ingest-harbor <your-job-folder>"}


def build(store: Store, directory: Path | None = None) -> dict:
    """Validate and ingest committed source records, without any network access."""
    from .demo import _ingest_with_confirmed_contract
    directory = directory or dataset_dir()
    manifest_bytes = (directory / "manifest.json").read_bytes()
    manifest = json.loads(manifest_bytes)
    entries = manifest["runs"]
    if manifest.get("schema") != "tb2-demo-1" or len(entries) != 350:
        raise ValueError("Invalid bundled leaderboard manifest")
    if len({e["run_id"] for e in entries}) != len(entries):
        raise ValueError("Duplicate bundled run identity")
    # Validate every payload before writing any run into the destination store.
    documents = []
    for entry in entries:
        path = (directory / entry["file"]).resolve()
        if not path.is_relative_to(directory.resolve()):
            raise ValueError("Bundled run escapes dataset directory")
        payload = path.read_bytes()
        doc = json.loads(payload)
        if (hashlib.sha256(payload).hexdigest() != entry["sha256"]
                or source_hash(doc) != entry["source_hash"]
                or doc["run"]["logical_run_id"] != entry["run_id"]):
            raise ValueError("Bundled source checksum mismatch: " + entry["file"])
        documents.append((entry, doc))
    stamp = {key:value for key,value in vars(version).items() if key.endswith("_VERSION")}
    stamp["manifest_sha256"] = hashlib.sha256(manifest_bytes).hexdigest()
    marker = Path(store.root) / "leaderboard-demo.json"
    try:
        previous = json.loads(marker.read_text(encoding="utf-8"))
    except (FileNotFoundError, ValueError):
        previous = {}
    cached = previous.get("stamp") == stamp
    def install(item):
        entry, doc = item
        capture = store.latest_capture_id(entry["run_id"])
        required = ("contract.json", "run_source.json", "events.json", "checks.json",
                    "outcome.json", "signature.json", "recoveries.json", "review_moments.json")
        if cached and capture and all(store.has_derived(entry["run_id"], capture, name) for name in required):
            source = store.read_derived(entry["run_id"], capture, "run_source.json")
            if source.get("source_hash") == entry["source_hash"]:
                return
        _ingest_with_confirmed_contract(doc, store)
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(install, documents))
    result = {"run_count":len(entries), "repository":manifest["repository"],
              "revision":manifest["revision"], "stamp":stamp, "bytes":manifest["bytes"]}
    atomic_json(marker, result)
    atomic_json(Path(store.root) / "start-here.json", start_here())
    return result
