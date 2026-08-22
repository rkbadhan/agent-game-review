"""Synthetic two-configuration slice, for the demo store and the tests.

The §4.16 comparison surface needs two *configurations* of the same task slice,
and the shipped fixtures are a single one (one harness, no sweep). Rather than
committing a second set of near-duplicate ATIF files, this builds the second
configuration from the first: same tasks, same match keys, a different harness
version and sweep id, and — for the tasks named in ``improves`` — a verifier
outcome flipped to passing.

That flip is **fabricated data**, and it is the only fabrication in this package.
It exists so a reviewer can see the comparison surface work end to end; it is
never presented as a captured run. Every run this builds carries
``source_type="synthetic_demo"`` so a store containing demo runs is obvious from
the run source, and the fixtures it derives from are themselves synthetic.

Both the ``agr demo-store`` command and the version-comparison tests build their
slices here, so the demo and the tests exercise the same shape of data — a fix to
one is a fix to both.

Pure stdlib — no third-party imports, no model calls.
"""

from __future__ import annotations

import copy
import json
import os
from typing import Iterable, Optional


# stdlib resources reader — no third-party dependency.
# Works both in-tree and from an installed wheel.
try:
    import importlib.resources as _resources
except ImportError:  # Python < 3.9 fallback (unlikely but safe)
    import importlib_resources as _resources  # type: ignore[no-redef]


_PACKAGE = "agr"
_FIXTURES_PKG = "demo_fixtures"

from .pipeline import analyze
from .store import Store

# Five distinct tasks: enough matched tasks for the paired statistics to be
# estimable (see ``versions.MIN_TASKS_FOR_DIRECTION``).
DEMO_TASKS = ("chess_best_move.atif.json", "contract_mismatch.atif.json",
              "ignored_failure.atif.json", "tool_failure_recovery.atif.json",
              "stuck_retry.atif.json")

BASELINE = {"sweep_id": "sweep_141", "configuration_id": "harness_1.8",
            "harness_version": "harbor-1.8"}
CANDIDATE = {"sweep_id": "sweep_142", "configuration_id": "harness_1.9",
             "harness_version": "harbor-1.9"}
# Match keys the fixtures do not declare, added identically to both sides so the
# slice matches exactly on them rather than leaving them unresolved.
SHARED_KEYS = {"environment_image_digest": "sha256:demo-env-1",
               "task_parameters": {"difficulty": "standard"}}


def find_fixtures_dir(custom: str | None = None) -> str | None:
    """Resolve the fixtures directory.

    Order of preference:
    1. Explicit ``custom`` path (CLI --fixtures argument).
    2. Package data (``agr/demo_fixtures/`` in the installed wheel).
    3. Relative to CWD (dev checkout: ``archive/synthetic/fixtures/``).
    4. Relative to this source file.

    Returns ``None`` if none of the locations exist (the caller should fall
    back to reading from package data directly).
    """
    if custom and os.path.isdir(custom):
        return custom
    # Try locating the package-data directory via stdlib resources.
    try:
        ref = _resources.files(_PACKAGE).joinpath(_FIXTURES_PKG)
        if ref.is_dir():
            return str(ref)
    except (Exception, OSError):
        pass
    for candidate in (
        os.path.join(os.getcwd(), "archive", "synthetic", "fixtures"),
        os.path.join(os.path.dirname(__file__), _FIXTURES_PKG),
    ):
        if os.path.isdir(candidate):
            return candidate
    return None  # caller falls back to package data


def _read_fixture(name: str, fixtures_dir: str | None = None) -> dict:
    """Read a fixture JSON file, preferring filesystem then package data."""
    if fixtures_dir:
        path = os.path.join(fixtures_dir, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8") as fh:
                return json.load(fh)
    # Fall back to package data (installed wheel).
    try:
        text = _resources.files(_PACKAGE).joinpath(_FIXTURES_PKG, name).read_text(encoding="utf-8")
        return json.loads(text)
    except (Exception, OSError):
        raise FileNotFoundError(
            f"fixture {name!r} not found — tried {fixtures_dir!r} and package data"
        ) from None


def load_fixture(fixtures_dir: str, name: str) -> dict:
    return _read_fixture(name, fixtures_dir)


def _auto_confirm(doc: dict) -> None:
    """Inject a ``contract_confirmation`` record so the demo run is never
    watermarked. Uses ``_now()`` for the timestamp and ``"demo-user"`` as the
    confirmer identity — neither is a real audit trail; the demo is for visual
    review of the product surface, not for provenance."""
    import time as _time
    task = doc.setdefault("task", {})
    task["contract_confirmation"] = {
        "confirmed_by": "demo-user",
        "confirmed_at": _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime()),
        "confirm_all": True,
    }


def set_outcome(doc: dict, status: str) -> None:
    """Force every verifier check to ``status`` — the fabricated part."""
    for check in doc.get("verifier", {}).get("checks", []):
        check["status"] = status
    doc["verifier"]["raw_output"] = f"synthetic demo: all checks {status}"


def variant(doc: dict, *, suffix: str, run_fields: Optional[dict] = None,
            mutate=None) -> dict:
    """A copy of an ATIF document under a new run id and configuration."""
    d = copy.deepcopy(doc)
    d["run"]["logical_run_id"] += suffix
    d["run"].update(run_fields or {})
    d["source_type"] = "synthetic_demo"
    if mutate:
        mutate(d)
    return d


def build_slice(store: Store, fixtures_dir: str, *,
                tasks: Iterable[str] = DEMO_TASKS,
                baseline_extra: Optional[dict] = None,
                candidate_extra: Optional[dict] = None,
                baseline_mutate=None, candidate_mutate=None,
                repeats: int = 1) -> Store:
    """Ingest a baseline and a candidate run for each task into ``store``.

    ``*_mutate`` receive ``(doc, fixture_name)`` so a caller can shape one side —
    the demo improves most tasks on the candidate; the tests use the same hook to
    seed a regression, hold a task flat, or leave an unmatched run behind.
    ``repeats`` ingests several runs per task per side, which is how the
    repeated-run strata and the task-clustering guard get exercised.
    """
    for name in tasks:
        doc = load_fixture(fixtures_dir, name)
        for side, fields, mutate in (
            ("b", {**BASELINE, **SHARED_KEYS, **(baseline_extra or {})}, baseline_mutate),
            ("c", {**CANDIDATE, **SHARED_KEYS, **(candidate_extra or {})}, candidate_mutate),
        ):
            for i in range(repeats):
                suffix = f"__{side}{i}" if repeats > 1 else f"__{side}"
                analyze(variant(doc, suffix=suffix, run_fields=fields,
                                mutate=mutate and (lambda d, n=name: mutate(d, n))), store)
    return store


def build_demo_store(store: Store, fixtures_dir: str) -> dict:
    """The store `agr demo-store` builds: a matched slice the candidate improves.

    Four of the five tasks pass on the candidate side, one stays as captured, and
    one extra run is left on each side without a counterpart — so the match report
    has real exclusions to show (``missing_counterpart`` and a version mismatch)
    rather than a clean intersection that hides how exclusion works.

    Every run gets its contract auto-confirmed so the demo shows clean,
    unwatermarked reviews from the start.
    """
    def auto_confirm(doc, name):
        _auto_confirm(doc)

    def improve(doc, name):
        _auto_confirm(doc)
        if name != "stuck_retry.atif.json":
            set_outcome(doc, "passed")

    build_slice(store, fixtures_dir, baseline_mutate=auto_confirm,
                candidate_mutate=improve)

    # Unmatched extras, one per side, so the exclusion groups are non-empty.
    # Both are auto-confirmed so they are never watermarked in the demo.
    extra = load_fixture(fixtures_dir, "clean_pass.atif.json")
    _auto_confirm(extra)
    analyze(variant(extra, suffix="__b", run_fields={**BASELINE, **SHARED_KEYS}), store)
    odd = load_fixture(fixtures_dir, "chess_best_move.atif.json")
    _auto_confirm(odd)
    odd = variant(odd, suffix="__c_v4", run_fields={**CANDIDATE, **SHARED_KEYS,
                                                      "task_version": "chess-best-move@4"})
    analyze(odd, store)
    return {"baseline": {"sweep_id": BASELINE["sweep_id"]},
            "candidate": {"sweep_id": CANDIDATE["sweep_id"]},
            "axis": "evaluation_harness"}
