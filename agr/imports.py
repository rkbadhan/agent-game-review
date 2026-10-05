"""File discovery and conversion for browser imports; no model or network calls."""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import zipfile

from .adapter import get_adapter
from .ingest import _validate
from .store import Store, source_hash
from . import version

MAX_FILE = 100 * 1024 * 1024
MAX_ARCHIVE = 500 * 1024 * 1024
MAX_EXPANDED = 2 * 1024 * 1024 * 1024
MAX_ENTRIES = 10000
MAX_RUNS = 1000
MAX_SPANS = 100000

CATALOG = [
    {"id": "harbor", "name": "Harbor / Terminal-Bench", "help": "Choose a trial or job folder with trajectory.json and its result/verifier files, or upload a ZIP of that folder.", "formats": ".json, folder, .zip", "methods": ["files", "folder"]},
    {"id": "claude", "name": "Claude Code", "help": "Select saved session JSONL files, stream-json output, or final-result JSON. Saved sessions usually live under ~/.claude/projects; choose that directory explicitly.", "formats": ".jsonl, .json", "methods": ["files", "folder"]},
    {"id": "pi", "name": "pi", "help": "Choose session JSONL files, or select your ~/.pi/agent/sessions directory. Only the selected files are transferred.", "formats": ".jsonl", "methods": ["files", "folder"]},
    {"id": "otel", "name": "OpenTelemetry", "help": "Upload OTLP JSON with resourceSpans. Each trace is imported separately. This is a file importer, not a collector receiver.", "formats": "OTLP .json", "methods": ["files"]},
    {"id": "langfuse", "name": "Langfuse", "help": "Import a trace JSON with observations and scores, or fetch one trace by ID using project credentials.", "formats": "trace .json", "methods": ["files", "trace"]},
    {"id": "langsmith", "name": "LangSmith", "help": "Upload a JSON list of run records, or {runs: [...], feedback: [...]}. Each trace is imported separately. Live API connection is not available.", "formats": "run-tree .json", "methods": ["files"]},
    {"id": "atif", "name": "Custom ATIF", "help": "Upload a document matching AGR's ATIF-shaped contract: atif_version, run, task, and steps. Arbitrary JSON is not supported.", "formats": "ATIF .json", "methods": ["files"]},
]


class ImportProblem(ValueError):
    def __init__(self, code, message, status=422):
        super().__init__(message)
        self.code, self.status = code, status


def safe_relative(name: str) -> Path:
    if not isinstance(name, str) or not name or "\\" in name or ":" in name or "\x00" in name:
        raise ImportProblem("invalid_path", "Select a file with a safe relative filename.")
    parts = name.split("/")
    if any(p in ("", ".", "..") or p.rstrip(" .") != p for p in parts):
        raise ImportProblem("invalid_path", "Absolute and parent-directory paths are not accepted.")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if any(p.split(".")[0].upper() in reserved or any(c in p for c in '<>"|?*') for p in parts):
        raise ImportProblem("invalid_path", "This filename cannot be stored safely.")
    return Path(*PurePosixPath(name).parts)


def unpack(root: Path) -> None:
    archives = list(root.rglob("*.zip"))
    for archive in archives:
        destination = archive.with_suffix(".expanded")
        if destination.exists():
            continue
        try:
            with zipfile.ZipFile(archive) as zf:
                entries = zf.infolist()
                if len(entries) > MAX_ENTRIES or sum(i.file_size for i in entries) > MAX_EXPANDED:
                    raise ImportProblem("import_limit", "The archive is too large. Select a smaller batch.")
                destination.mkdir()
                expanded = 0
                for entry in entries:
                    relative = safe_relative(entry.filename.rstrip("/"))
                    mode = entry.external_attr >> 16
                    if stat.S_ISLNK(mode) or (mode and stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                        raise ImportProblem("invalid_archive", "Archive links and special files are not accepted.")
                    target = destination / relative
                    if entry.is_dir():
                        target.mkdir(parents=True, exist_ok=True)
                        continue
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if target.exists():
                        raise ImportProblem("invalid_archive", "The archive contains duplicate filenames.")
                    with zf.open(entry) as src, target.open("xb") as out:
                        while chunk := src.read(1024 * 1024):
                            expanded += len(chunk)
                            if expanded > MAX_EXPANDED:
                                raise ImportProblem("import_limit", "The archive exceeds the expansion limit.")
                            out.write(chunk)
        except (zipfile.BadZipFile, RuntimeError, OSError):
            shutil.rmtree(destination, ignore_errors=True)
            raise ImportProblem("invalid_archive", "This ZIP cannot be read. Export an unencrypted ZIP and try again.") from None
        except ImportProblem:
            shutil.rmtree(destination, ignore_errors=True)
            raise
    if len(list(root.rglob("*"))) > MAX_ENTRIES or sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) > MAX_EXPANDED:
        raise ImportProblem("import_limit", "Select a smaller batch of files.")


def _read(path):
    if path.stat().st_size > MAX_FILE:
        raise ImportProblem("import_limit", "A capture exceeds the file limit. Select a smaller export.")
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except (UnicodeError, json.JSONDecodeError):
        raise ImportProblem("invalid_json", "This file is not a readable JSON export.") from None


def _otel_groups(data):
    if not isinstance(data, dict) or not isinstance(data.get("resourceSpans"), list):
        raise ImportProblem("unsupported_format", "Expected OTLP JSON containing resourceSpans.")
    groups = {}
    count = 0
    for resource in data["resourceSpans"]:
        for scope in resource.get("scopeSpans", resource.get("instrumentationLibrarySpans", [])):
            for span in scope.get("spans", []):
                key = span.get("traceId")
                if not key:
                    raise ImportProblem("mixed_trace_input", "Every span needs a traceId to group this export safely.")
                count += 1
                if count > MAX_SPANS * MAX_RUNS:
                    raise ImportProblem("import_limit", "Select a smaller span export.")
                target = groups.setdefault(key, {"resourceSpans": []})
                target["resourceSpans"].append({"resource": resource.get("resource", {}), "scopeSpans": [{"scope": scope.get("scope", {}), "spans": [span]}]})
    if not groups:
        raise ImportProblem("unsupported_format", "This export contains no spans.")
    for key, group in groups.items():
        spans = [s for r in group["resourceSpans"] for sc in r["scopeSpans"] for s in sc["spans"]]
        if len(spans) > MAX_SPANS:
            raise ImportProblem("import_limit", "One trace exceeds the span limit.")
        seen = {}
        for span in spans:
            sid = span.get("spanId")
            if not sid or sid in seen:
                raise ImportProblem("mixed_trace_input", "Missing or repeated span IDs prevent reliable trace grouping.")
            seen[sid] = span
        yield key, group, []


def _langsmith_groups(data):
    runs = data if isinstance(data, list) else data.get("runs") if isinstance(data, dict) else None
    feedback = data.get("feedback", []) if isinstance(data, dict) else []
    if not isinstance(runs, list) or not runs:
        raise ImportProblem("unsupported_format", "Expected a list of LangSmith runs or an object with runs and feedback.")
    indexed = {}
    for r in runs:
        if not isinstance(r, dict) or not r.get("id") or r["id"] in indexed:
            raise ImportProblem("mixed_trace_input", "Run records need unique IDs for reliable trace grouping.")
        indexed[r["id"]] = r
    groups = {}
    for r in runs:
        node, visited = r, set()
        while node.get("parent_run_id") in indexed:
            if node["id"] in visited:
                raise ImportProblem("mixed_trace_input", "A cycle in the run tree prevents reliable grouping.")
            visited.add(node["id"])
            parent = indexed[node["parent_run_id"]]
            if node.get("trace_id") and parent.get("trace_id") and node["trace_id"] != parent["trace_id"]:
                raise ImportProblem("mixed_trace_input", "Parent and child records declare different traces.")
            node = parent
        key = node.get("trace_id") or node["id"]
        groups.setdefault(key, []).append(r)
    for key, members in groups.items():
        if len(members) > MAX_SPANS:
            raise ImportProblem("import_limit", "One trace exceeds the run-record limit.")
        ids = {r["id"] for r in members}
        selected, warnings = [], []
        for f in feedback:
            if not isinstance(f, dict):
                continue
            if f.get("run_id") in ids or f.get("trace_id") == key:
                selected.append(f)
            elif not f.get("run_id") and not f.get("trace_id"):
                if len(groups) == 1:
                    selected.append(f)
                else:
                    warnings.append("Unscoped feedback was not assigned to any trace in this multi-trace export.")
        yield key, {"runs": members, "feedback": selected}, warnings


def discover(root: Path, source: str, store: Store, options: dict) -> tuple[list, list]:
    """Return candidate metadata and immutable normalized documents for staging."""
    if source not in {s["id"] for s in CATALOG}:
        raise ImportProblem("unsupported_source", "Choose a supported source.")
    unpack(root)
    files = sorted(p for p in root.rglob("*") if p.is_file() and p.suffix.lower() != ".zip")
    entries, accounted = [], set()
    allowed_options = {k: v for k, v in options.items() if k in {"task_id", "instruction", "configuration_id"} and isinstance(v, str) and v}
    verifier_name = options.get("verifier_file")
    if verifier_name:
        path = root / safe_relative(verifier_name)
        if not path.is_file():
            raise ImportProblem("invalid_verifier", "The selected evaluation-results file is missing.")
        verifier = _read(path)
        if not isinstance(verifier, dict) or not isinstance(verifier.get("checks"), list):
            raise ImportProblem("invalid_verifier", "Evaluation results need a checks array.")
        allowed_options["verifier"] = verifier
        accounted.add(path)

    def add(path, doc=None, warnings=None, error=None, key=None, error_code=None):
        item = {"id": f"item_{len(entries) + 1}", "file": str(path.relative_to(root)).replace("\\", "/"), "source": source, "warnings": warnings or []}
        if key:
            item["source_identity"] = str(key)
        if error:
            item.update(status="invalid", error=error)
            if error_code:
                item["error_code"] = error_code
        else:
            try:
                _validate(doc)
                run_id = doc["run"]["logical_run_id"]
                h = source_hash(doc)
                adapter = doc.get("adapter_version") or version.RAW_ATIF_IMPORT_VERSION
                existing = store.find_capture(run_id, h, adapter)
                prior = store.read_index(run_id)
                if prior and not existing:
                    old = store.read_source(run_id, prior[-1]["capture_id"])
                    if old["run"]["task_id"] != doc["run"]["task_id"] or old.get("source_type") != doc.get("source_type"):
                        raise ImportProblem("identity_collision", "This run ID already belongs to a different task or source.")
                coverage = doc.get("capabilities", {})
                item.update(status="duplicate" if existing else "new_capture" if prior else "new",
                            run_id=run_id, title=doc["run"].get("task_id", run_id), model=doc["run"].get("model"),
                            source_hash=h, adapter_version=adapter, steps=len(doc["steps"]),
                            evaluation_available=bool((doc.get("verifier") or {}).get("checks")), coverage=coverage,
                            captured_at=doc["run"].get("started_at"), limited=doc.get("capture_completeness") != "complete")
                item["doc"] = doc
            except ImportProblem as exc:
                item.update(status="invalid", error=str(exc), error_code=exc.code)
            except (ValueError, TypeError, KeyError):
                item.update(status="invalid", error="This capture does not satisfy the input contract or conflicts with an existing identity.")
        entries.append(item)
        if len(entries) > MAX_RUNS:
            raise ImportProblem("import_limit", "Select at most 1,000 logical runs per import.")

    if source == "harbor":
        from .ingest_harbor import iter_trials_detailed
        def find_trials(folder):
            try:
                candidates = iter_trials_detailed(folder)
            except ValueError:
                return []
            trials = []
            for path, reason in candidates:
                # ZIP and folder wrappers can exceed CLI discovery depth.
                if reason and path.is_dir() and any(path.rglob("trajectory.json")):
                    trials.extend(find_trials(path))
                else:
                    trials.append((path, reason))
            return trials
        trials = find_trials(root)
        if verifier_name and len(trials) != 1:
            raise ImportProblem("invalid_verifier", "Attach evaluation results to one trial at a time; batch sidecars must stay in their trial folders.")
        for path, reason in trials:
            if reason:
                add(path, error="This directory has no supported trajectory.")
                continue
            accounted.update([path] if path.is_file() else path.rglob("*"))
            try:
                result = get_adapter(source).convert(path, **allowed_options)
                add(path, result.doc, result.warnings)
            except ImportProblem as exc:
                if exc.code in ("import_limit", "invalid_verifier"):
                    raise
                add(path, error=str(exc), error_code=exc.code)
            except (ValueError, TypeError, KeyError, OSError):
                add(path, error="The trial cannot be converted. Check its trajectory and result files.")
    else:
        selected_files = [p for p in files if p not in accounted and p.suffix.lower() in (".json", ".jsonl")]
        if verifier_name and len(selected_files) != 1:
            raise ImportProblem("invalid_verifier", "Attach evaluation results to one selected capture at a time.")
        for path in selected_files:
            accounted.add(path)
            try:
                if source in ("otel", "langsmith"):
                    groups = list((_otel_groups if source == "otel" else _langsmith_groups)(_read(path)))
                    if verifier_name and len(groups) != 1:
                        raise ImportProblem("invalid_verifier", "An evaluation sidecar cannot be applied to multiple traces.")
                    for key, data, warnings in groups:
                        if source == "otel":
                            result = get_adapter(source).convert(data, **allowed_options)
                        else:
                            normalized = root.parent / f"group_{hashlib.sha256(str(key).encode()).hexdigest()}.json"
                            normalized.write_text(json.dumps(data), encoding="utf-8")
                            result = get_adapter(source).convert(normalized, **allowed_options)
                        add(path, result.doc, result.warnings + warnings, key=key)
                elif source == "atif":
                    doc = _read(path)
                    if allowed_options:
                        doc = copy.deepcopy(doc)
                        for k in ("task_id", "configuration_id"):
                            if k in allowed_options:
                                doc.setdefault("run", {})[k] = allowed_options[k]
                        if "instruction" in allowed_options:
                            doc.setdefault("task", {})["instruction"] = allowed_options["instruction"]
                        if "verifier" in allowed_options:
                            doc["verifier"] = allowed_options["verifier"]
                    add(path, doc)
                else:
                    result = get_adapter(source).convert(path, **allowed_options)
                    if source == "langfuse":
                        export = _read(path)
                        result.warnings.extend(export.get("import_warnings") or [])
                        if export.get("fetch_limits"):
                            result.doc["capture_completeness"] = "partial"
                            result.doc["capabilities"] = {k: "partial" if v == "complete" else v for k, v in result.doc.get("capabilities", {}).items()}
                    add(path, result.doc, result.warnings)
            except ImportProblem as exc:
                if exc.code in ("import_limit", "invalid_verifier"):
                    raise
                add(path, error=str(exc), error_code=exc.code)
            except (ValueError, TypeError, KeyError, OSError):
                add(path, error="This file cannot be converted for the selected source. Check the accepted export format.")
    if not entries:
        raise ImportProblem("unsupported_format", "No supported captures were found. Choose the correct source or different files.")
    ignored = [{"file": str(p.relative_to(root)).replace("\\", "/"), "reason": "Auxiliary or unsupported file"} for p in files if p not in accounted]
    # Internal staging paths must not become user-facing capture identities.
    for item in entries:
        item["warnings"] = [str(w).replace(str(root.parent), "Selected files") for w in item["warnings"]]
    return entries, ignored


def publish_analysis(doc: dict, store: Store, temporary: Path):
    """Analyze away from live runs, then publish completed files atomically.

    A per-run lock serializes publication. Publishing the index last ensures a
    cancelled/crashed incomplete analysis is never a visible completed capture.
    """
    from .pipeline import analyze
    from .workspace import atomic_json
    run_id = doc["run"]["logical_run_id"]
    adapter = doc.get("adapter_version") or version.RAW_ATIF_IMPORT_VERSION
    existing = store.find_capture(run_id, source_hash(doc), adapter)
    if existing:
        return {"status": "duplicate", "run_id": run_id, "capture_id": existing["capture_id"]}
    prior = store.read_index(run_id)
    if prior:
        old = store.read_source(run_id, prior[-1]["capture_id"])
        if old["run"]["task_id"] != doc["run"]["task_id"] or old.get("source_type") != doc.get("source_type"):
            raise ImportProblem("identity_collision", "This run ID already belongs to a different task or source.")
    isolated = Store(str(temporary))
    if prior:
        target = Path(isolated._run_dir(run_id))
        target.mkdir(parents=True, exist_ok=True)
        atomic_json(target / "index.json", prior)
    analysis = analyze(doc, isolated)
    rs = analysis.run_source
    # CLI registration uses the same per-run lock. Re-read after analysis;
    # copying the scratch index back would lose concurrent CLI captures.
    with store.index_lock(run_id):
        existing = store.find_capture(run_id, source_hash(doc), adapter)
        if existing:
            return {"status": "duplicate", "run_id": run_id, "capture_id": existing["capture_id"]}
        current = store.read_index(run_id)
        if current:
            old = store.read_source(run_id, current[-1]["capture_id"])
            if old["run"]["task_id"] != doc["run"]["task_id"] or old.get("source_type") != doc.get("source_type"):
                raise ImportProblem("identity_collision", "This run ID already belongs to a different task or source.")
        entry = dict(isolated.read_index(run_id)[-1])
        entry["capture_revision"] = max((e["capture_revision"] for e in current), default=0) + 1
        entry["supersedes_source_capture_id"] = current[-1]["capture_id"] if current else None
        rs.capture_revision = entry["capture_revision"]
        rs.supersedes_source_capture_id = entry["supersedes_source_capture_id"]
        isolated.write_derived(run_id, rs.source_capture_id, "run_source.json", rs.to_dict())
        cap_path = Path(store._capture_dir(run_id, rs.source_capture_id))
        cap_path.parent.mkdir(parents=True, exist_ok=True)
        if not cap_path.exists():
            shutil.move(isolated._capture_dir(run_id, rs.source_capture_id), str(cap_path))
        else:
            # A crash may have published complete files before its index write.
            atomic_json(cap_path / "run_source.json", rs.to_dict())
        store._write_index(run_id, current + [entry])
        store._write_seq += 1
        return {"status": "new_capture" if current else "new", "run_id": run_id, "capture_id": rs.source_capture_id}
