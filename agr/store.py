"""Immutable, content-addressed capture store (spec §5.1, §7.3).

The raw source and its derived records are written under
``<store>/runs/<run_id>/captures/<capture_id>/``. Source bytes are never
rewritten; reprocessing writes new derived files alongside the immutable
source. A per-run index tracks captures and their revisions so a fuller
capture of the same logical run is linked, not duplicated.

This is deliberately a filesystem/JSON store: Postgres, DuckDB, and object
storage from the prescribed stack (spec §17) are deferred until there is
enough logic to justify them.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from typing import Any, Optional
from urllib.parse import quote, unquote


class InvalidRunId(ValueError):
    """A run_id is unsafe to use as a store path component (spec §5.1)."""


# One segment: alnum/underscore/dot/hyphen only, starting and ending on
# alnum-or-underscore so a segment can never be exactly "." or ".." nor carry
# a trailing dot that some filesystems treat specially. No colon (blocks
# Windows drive letters like "C:" and NTFS alternate-data-stream syntax
# "name:stream") and no backslash (blocked separately, below) anywhere.
_RUN_ID_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.\-]*[A-Za-z0-9_]$|^[A-Za-z0-9_]$")


def validate_run_id(run_id: str) -> str:
    """Reject any run_id that could let an import escape the store.

    Legitimate run ids are namespaced with "/" (e.g. a Harbor task name
    embedded as ``harbor__terminal-bench/crack-7z-hash__<uuid>``), so "/" is
    allowed as a directory separator — but every segment it produces must be
    a plain name: never empty, never exactly "." or "..", and drawn only from
    a conservative charset. This blocks absolute paths, "../" traversal, and
    platform-specific escapes (Windows drive letters, UNC/backslash paths,
    NTFS alternate-data-stream names) while preserving normal namespacing.
    """
    if not isinstance(run_id, str) or not run_id:
        raise InvalidRunId(f"invalid run_id {run_id!r}: must be a non-empty string")
    if "\x00" in run_id:
        raise InvalidRunId(f"invalid run_id {run_id!r}: contains a NUL byte")
    if "\\" in run_id:
        raise InvalidRunId(f"invalid run_id {run_id!r}: backslashes are not allowed")
    if run_id.startswith("/") or run_id.startswith("~"):
        raise InvalidRunId(f"invalid run_id {run_id!r}: absolute paths are not allowed")
    for segment in run_id.split("/"):
        if segment in ("", ".", ".."):
            raise InvalidRunId(f"invalid run_id {run_id!r}: empty or traversal segment {segment!r}")
        if not _RUN_ID_SEGMENT_RE.match(segment):
            raise InvalidRunId(f"invalid run_id {run_id!r}: disallowed characters in segment {segment!r}")
    return run_id


def canonical_bytes(doc: Any) -> bytes:
    """Deterministic byte encoding used for hashing and provenance."""
    return json.dumps(doc, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def source_hash(doc: Any) -> str:
    return "sha256:" + hashlib.sha256(canonical_bytes(doc)).hexdigest()


def capture_id_for(hash_str: str) -> str:
    digest = hash_str.split(":", 1)[-1]
    return "capture_" + digest[:12]


class Store:
    def __init__(self, root: str):
        self.root = root
        os.makedirs(self.root, exist_ok=True)

    # --- paths ---------------------------------------------------------------

    def _run_dir(self, run_id: str) -> str:
        validate_run_id(run_id)
        return os.path.join(self.root, "runs", run_id)

    def _capture_dir(self, run_id: str, capture_id: str) -> str:
        return os.path.join(self._run_dir(run_id), "captures", capture_id)

    def _index_path(self, run_id: str) -> str:
        return os.path.join(self._run_dir(run_id), "index.json")

    # --- index ---------------------------------------------------------------

    def read_index(self, run_id: str) -> list[dict]:
        path = self._index_path(run_id)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def _write_index(self, run_id: str, index: list[dict]) -> None:
        os.makedirs(self._run_dir(run_id), exist_ok=True)
        with open(self._index_path(run_id), "w", encoding="utf-8") as fh:
            json.dump(index, fh, indent=2)

    def find_capture(self, run_id: str, hash_str: str, adapter_version: str) -> Optional[dict]:
        for entry in self.read_index(run_id):
            if entry["source_hash"] == hash_str and entry["adapter_version"] == adapter_version:
                return entry
        return None

    def register_capture(
        self,
        run_id: str,
        capture_id: str,
        hash_str: str,
        adapter_version: str,
        completeness: str,
    ) -> dict:
        """Register a new capture, computing its revision and predecessor.

        Returns the index entry (existing one if this exact hash+adapter was
        already ingested — idempotent per spec §7.3).
        """
        index = self.read_index(run_id)
        existing = self.find_capture(run_id, hash_str, adapter_version)
        if existing is not None:
            existing["idempotent"] = True
            return existing

        revision = (max((e["capture_revision"] for e in index), default=0) + 1)
        supersedes = index[-1]["capture_id"] if index else None
        entry = {
            "capture_id": capture_id,
            "source_hash": hash_str,
            "adapter_version": adapter_version,
            "capture_revision": revision,
            "supersedes_source_capture_id": supersedes,
            "capture_completeness": completeness,
            "ingested_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "idempotent": False,
        }
        index.append(entry)
        self._write_index(run_id, index)
        return entry

    # --- record IO -----------------------------------------------------------

    def write_source(self, run_id: str, capture_id: str, doc: Any) -> None:
        """Write the immutable source. Refuses to overwrite existing bytes."""
        cap_dir = self._capture_dir(run_id, capture_id)
        os.makedirs(cap_dir, exist_ok=True)
        path = os.path.join(cap_dir, "source.json")
        if os.path.exists(path):
            return  # immutable: never rewrite
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(canonical_bytes(doc).decode("utf-8"))

    def read_source(self, run_id: str, capture_id: str) -> Any:
        path = os.path.join(self._capture_dir(run_id, capture_id), "source.json")
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def write_derived(self, run_id: str, capture_id: str, name: str, payload: Any) -> None:
        cap_dir = self._capture_dir(run_id, capture_id)
        path = os.path.join(cap_dir, name)
        # Nested names (e.g. ``review_telemetry/att_0001.json``) create their
        # subdirectory; flat names are unchanged.
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    def read_derived(self, run_id: str, capture_id: str, name: str) -> Any:
        with open(os.path.join(self._capture_dir(run_id, capture_id), name), encoding="utf-8") as fh:
            return json.load(fh)

    def has_derived(self, run_id: str, capture_id: str, name: str) -> bool:
        return os.path.exists(os.path.join(self._capture_dir(run_id, capture_id), name))

    def latest_capture_id(self, run_id: str) -> Optional[str]:
        index = self.read_index(run_id)
        return index[-1]["capture_id"] if index else None

    # --- product analytics log (§4.21) ---------------------------------------
    #
    # Append-only JSONL at the store root: these events describe how a reviewer
    # moved through the product, not what any run contained, so they live outside
    # every run directory and are never read as evidence.

    def _events_path(self) -> str:
        return os.path.join(self.root, "analytics", "events.jsonl")

    def append_event(self, entry: Any) -> None:
        path = self._events_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(entry, sort_keys=True) + "\n")

    def read_events(self) -> list[dict]:
        path = self._events_path()
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    # --- saved comparison definitions (§4.16.1) ------------------------------
    #
    # A comparison spans runs, so it is stored at the store root rather than
    # under any one run. The file holds the frozen *definition*; results are
    # recomputed from it, which is what keeps a saved comparison honest when the
    # store gains runs — the definition is the promise, not the numbers.

    def _comparison_path(self, comparison_id: str) -> str:
        return os.path.join(self.root, "comparisons", f"{comparison_id}.json")

    def write_comparison(self, comparison_id: str, payload: Any) -> None:
        path = self._comparison_path(comparison_id)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    def read_comparison(self, comparison_id: str) -> Any:
        with open(self._comparison_path(comparison_id), encoding="utf-8") as fh:
            return json.load(fh)

    def has_comparison(self, comparison_id: str) -> bool:
        return os.path.exists(self._comparison_path(comparison_id))

    def list_comparisons(self) -> list[str]:
        root = os.path.join(self.root, "comparisons")
        if not os.path.isdir(root):
            return []
        return sorted(f[:-5] for f in os.listdir(root) if f.endswith(".json"))

    # --- per-reviewer review snapshots (reviewer-diff view) -----------------
    #
    # A capture can carry more than one review of the same source: the
    # deterministic baseline and one or more model-reviewer passes. Each lives
    # under ``reviews/<encoded_key>.json`` inside the capture dir, addressed by a
    # stable ``reviewer_key`` (``"deterministic"`` or ``"model:<source>"``). The
    # key is URL-encoded for the filename so the colon in ``model:...`` is legal
    # on Windows as well as POSIX. A legacy ``review_moments.json`` written by
    # older code is migrated lazily: ``list_reviews`` reports it as the
    # ``"deterministic"`` slot when no ``reviews/`` dir exists yet.

    def _reviews_dir(self, run_id: str, capture_id: str) -> str:
        return os.path.join(self._capture_dir(run_id, capture_id), "reviews")

    @staticmethod
    def _review_filename(reviewer_key: str) -> str:
        return quote(reviewer_key, safe="") + ".json"

    @staticmethod
    def _review_key(filename: str) -> str:
        return unquote(filename[:-5]) if filename.endswith(".json") else unquote(filename)

    def write_review(self, run_id: str, capture_id: str, reviewer_key: str, payload: Any) -> None:
        """Write one reviewer's snapshot to its own slot (never overwrites another)."""
        d = self._reviews_dir(run_id, capture_id)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, self._review_filename(reviewer_key)), "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)

    def list_reviews(self, run_id: str, capture_id: str) -> list[str]:
        """Reviewer keys present for this capture, with lazy legacy migration.

        If the new ``reviews/`` directory exists, its encoded filenames are
        decoded back to keys. Otherwise, a legacy ``review_moments.json`` (written
        by older pipeline code) is reported as the ``"deterministic"`` slot so
        the read layer can address it without forcing a re-ingest.
        """
        d = self._reviews_dir(run_id, capture_id)
        if os.path.isdir(d):
            return [self._review_key(f) for f in sorted(os.listdir(d))
                    if f.endswith(".json")]
        if self.has_derived(run_id, capture_id, "review_moments.json"):
            return ["deterministic"]
        return []

    def read_review_slot(self, run_id: str, capture_id: str, reviewer_key: str) -> Any:
        """Read a review slot, falling back to legacy ``review_moments.json``.

        When ``reviews/`` does not exist yet but the legacy file does, the
        ``"deterministic"`` key is served from it so older stores work unchanged.
        """
        d = self._reviews_dir(run_id, capture_id)
        path = os.path.join(d, self._review_filename(reviewer_key))
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
        if reviewer_key == "deterministic" and self.has_derived(run_id, capture_id, "review_moments.json"):
            return self.read_derived(run_id, capture_id, "review_moments.json")
        raise KeyError(reviewer_key)
