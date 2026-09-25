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
import shutil
import tempfile
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
    """Inject a DEMO confirmation so the synthetic run is not watermarked.

    ``demo_override`` selects the ``demo_confirmed`` contract status, so the run
    is never reported as human-confirmed; ``confirmed_by`` names the demo, not a
    person. The synthetic slice is fabricated data (``source_type=synthetic_demo``)
    for visual review of the product surface, not provenance.
    """
    task = doc.setdefault("task", {})
    task["contract_confirmation"] = {
        "confirmed_by": "demo",
        "confirmed_at": _now_iso(),
        "confirm_all": True,
        "demo_override": True,
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


# --- GR-4: pre-computed model reviews over real published runs ---------------
#
# The real demo dataset lives beside the synthetic fixtures: ``runs/`` holds the
# real trajectory source documents, ``reviews/`` the pre-computed model reviews
# (moments + reviewer model + date). ``agr demo`` installs those reviews into a
# freshly-built store, so the demo shows real model reviews with NO provider
# credential and no model call. ``agr bake-reviews`` regenerates the dataset from
# a working store after a maintainer runs the reviewer over the corpus.
_REAL_PKG = "real_demo"
_DEFAULT_REVIEW_MODEL = "accounts/fireworks/models/kimi-k3"


def find_real_demo_dir(custom: Optional[str] = None) -> Optional[str]:
    """Resolve the directory holding ``runs/`` and ``reviews/`` for the real demo.

    Preference: explicit ``custom`` path, then package data, then the source tree.
    Returns ``None`` when no real dataset is present (caller falls back to the
    synthetic demo).
    """
    if custom and os.path.isdir(custom):
        return custom
    try:
        ref = _resources.files(_PACKAGE).joinpath(_FIXTURES_PKG, _REAL_PKG)
        if ref.is_dir():
            return str(ref)
    except (Exception, OSError):
        pass
    candidate = os.path.join(os.path.dirname(__file__), _FIXTURES_PKG, _REAL_PKG)
    return candidate if os.path.isdir(candidate) else None


def _safe_name(run_id: str) -> str:
    """A filesystem-safe stem for a logical run id (``/`` is not legal on Windows)."""
    return run_id.replace("/", "__").replace("\\", "__")


def _iso_mtime(path: str) -> str:
    """UTC timestamp of a file's last write, or now when it is missing."""
    import time as _time
    try:
        ts = os.path.getmtime(path)
    except OSError:
        ts = _time.time()
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime(ts))


def _read_review_meta(store: Store, run_id: str, capture_id: str) -> dict:
    return (store.read_derived(run_id, capture_id, "review_meta.json")
            if store.has_derived(run_id, capture_id, "review_meta.json") else {})


def _now_iso() -> str:
    import time as _time
    return _time.strftime("%Y-%m-%dT%H:%M:%SZ", _time.gmtime())


def _ingest_with_confirmed_contract(doc: dict, store: Store):
    """Ingest a REAL source UNCHANGED, then clear the watermark via a derived record.

    The source of record is never edited: injecting a confirmation into it (the
    previous behavior) changed its hash and capture identity and presented a
    confirmation that never happened on the run. Instead the confirmation is
    written as its own derived record (the same path ``agr confirm`` uses) and the
    run is re-analyzed so the served contract is confirmed. The real source hash
    and capture id are preserved, which is what lets a baked review be verified
    against its exact source.
    """
    analysis = analyze(doc, store)
    run_id = analysis.run_source.run_id
    capture_id = analysis.run_source.source_capture_id
    if not store.has_derived(run_id, capture_id, "contract_confirmation.json"):
        store.write_derived(run_id, capture_id, "contract_confirmation.json", {
            "confirm_all": True, "confirmed_by": "demo",
            "confirmed_at": _now_iso(), "demo_override": True})
    return analyze(doc, store)


def _load_json_dir(directory: str) -> list[dict]:
    out = []
    for name in sorted(os.listdir(directory)) if os.path.isdir(directory) else []:
        if name.endswith(".json"):
            with open(os.path.join(directory, name), encoding="utf-8") as fh:
                out.append(json.load(fh))
    return out


def build_real_demo_store(store: Store, real_dir: Optional[str] = None, *,
                          include_comparison: bool = True) -> dict:
    """GR-4: build the demo from real runs + their pre-computed model reviews.

    Ingests each committed real trajectory, installs the corresponding baked
    model-review snapshot into the run's latest capture (re-keyed to that capture
    so the slot is self-consistent), records the reviewer model and date in
    ``review_meta.json``, and — unless disabled — adds the synthetic comparison
    slice so the Compare surface still has matched data. No model is called.
    """
    from . import read

    real_dir = real_dir or find_real_demo_dir()
    if real_dir is None:
        raise FileNotFoundError("no real demo dataset found (expected runs/ and reviews/)")
    runs_dir = os.path.join(real_dir, "runs")
    reviews_dir = os.path.join(real_dir, "reviews")

    store_runs: dict[str, str] = {}
    for doc in _load_json_dir(runs_dir):
        analysis = _ingest_with_confirmed_contract(doc, store)
        store_runs[analysis.run_source.run_id] = analysis.run_source.source_capture_id

    # Clear every model slot this dataset owns before installing, so a review
    # that is no longer in the dataset (or is rejected below) cannot remain
    # served from a previous build of a reused store. The deterministic baseline
    # is left untouched.
    for run_id, capture_id in store_runs.items():
        for key in store.list_reviews(run_id, capture_id):
            if key.startswith("model:"):
                store.delete_review(run_id, capture_id, key)
        if store.has_derived(run_id, capture_id, "review_meta.json"):
            store.write_derived(run_id, capture_id, "review_meta.json", {})

    installed: list[str] = []
    stale: list[str] = []
    for review in _load_json_dir(reviews_dir):
        run_id = review.get("run_id")
        capture_id = store_runs.get(run_id)
        key = review.get("reviewer_key")
        if capture_id is None or not key:
            continue  # a review whose run is not in this dataset is skipped, not guessed
        # A baked review is installed, not re-validated, so it is only trustworthy
        # against the EXACT source it was computed from. BOTH hashes must be
        # present and equal: the source hash covers the whole trajectory text
        # (not just event positions), so an unverifiable or changed run is
        # rejected rather than served with stale quotes and validated facts.
        source = store.read_derived(run_id, capture_id, "run_source.json") \
            if store.has_derived(run_id, capture_id, "run_source.json") else {}
        baked_hash = review.get("source_hash")
        source_hash = source.get("source_hash")
        if not baked_hash or not source_hash or baked_hash != source_hash:
            stale.append(run_id)
            continue
        moments = review.get("moments") or []
        for moment in moments:
            moment["run_id"] = run_id
            moment["source_capture_id"] = capture_id
        store.write_review(run_id, capture_id, key, moments)
        meta = _read_review_meta(store, run_id, capture_id)
        meta[key] = {"model": review.get("model"), "reviewed_at": review.get("reviewed_at"),
                     "origin": "precomputed"}
        store.write_derived(run_id, capture_id, "review_meta.json", meta)
        installed.append(run_id)

    if include_comparison:
        fixtures_dir = find_fixtures_dir()
        if fixtures_dir:
            build_demo_store(store, fixtures_dir)

    return {"landing_run": _choose_landing_run(store, installed),
            "real_runs": len(store_runs), "model_reviews": len(installed),
            "stale_reviews": len(stale),
            "axis": "evaluation_harness"}


def _choose_landing_run(store: Store, run_ids: Iterable[str]) -> Optional[str]:
    """The run ``agr demo`` opens on: a real FAILED run whose model review found
    moments, then any other non-passing run with moments, then any reviewed run.

    Deterministic (sorted) so the demo lands on the same run every build.
    """
    from . import read

    ranked: list[tuple[int, str]] = []
    reviewed = sorted(set(run_ids))
    for run_id in reviewed:
        try:
            review = read.get_review(store, run_id)
        except Exception:  # noqa: BLE001 — a run that cannot be read is not a landing candidate
            continue
        if not review.get("moments"):
            continue
        status = (review.get("outcome") or {}).get("status")
        ranked.append((0 if status == "FAILED" else 1, run_id))
    if ranked:
        ranked.sort()
        return ranked[0][1]
    return reviewed[0] if reviewed else None


def _healthy_model_key(store: Store, run_id: str, capture_id: str,
                       model: str) -> Optional[str]:
    """The model-review key to bake, only when its LATEST attempt succeeded.

    A failed or incomplete retry deliberately leaves the reviewer's older slot on
    disk (so a stale snapshot is never served as the retry's result). Baking must
    not resurrect that older slot: when the store records attempts/errors, the
    chosen reviewer must have a latest ``ok`` attempt and no active error.
    """
    key = next((k for k in store.list_reviews(run_id, capture_id)
                if k.startswith("model:") and model in k), None)
    if key is None:
        return None
    if store.has_derived(run_id, capture_id, "review_errors.json"):
        errors = store.read_derived(run_id, capture_id, "review_errors.json") or []
        if any(e.get("reviewer_key") == key for e in errors if isinstance(e, dict)):
            return None
    if store.has_derived(run_id, capture_id, "review_attempts.json"):
        attempts = store.read_derived(run_id, capture_id, "review_attempts.json") or []
        latest = next((a for a in reversed(attempts)
                       if isinstance(a, dict) and a.get("reviewer_key") == key), None)
        if latest is None or latest.get("outcome") != "ok":
            return None
    return key


def bake_reviews(store: Store, out_dir: str, *, model: Optional[str] = None) -> dict:
    """Export real runs + their pre-computed model reviews into a demo dataset.

    A maintainer runs the model reviewer over the corpus
    (``agr review --all --provider … --model …``), then runs
    ``agr --store <store> bake-reviews --out <dir>`` to write
    ``out_dir/runs/*.atif.json`` (the source of record) and
    ``out_dir/reviews/*.json`` (moments + reviewer model + date). Committing the
    result lets ``agr demo`` reproduce the reviews with no provider credential.

    The export is written to a staging directory and swapped in only on success,
    so a re-bake can never leave runs or reviews from a previous corpus behind.
    """
    from . import read

    model = model or _DEFAULT_REVIEW_MODEL
    parent = os.path.dirname(os.path.abspath(out_dir)) or "."
    os.makedirs(parent, exist_ok=True)
    staging = tempfile.mkdtemp(prefix=".bake-reviews-", dir=parent)
    written = 0
    try:
        runs_dir = os.path.join(staging, "runs")
        reviews_dir = os.path.join(staging, "reviews")
        os.makedirs(runs_dir, exist_ok=True)
        os.makedirs(reviews_dir, exist_ok=True)
        for row in read.list_runs(store):
            run_id, capture_id = row["run_id"], row["capture_id"]
            key = _healthy_model_key(store, run_id, capture_id, model)
            if key is None:
                continue
            moments = store.read_review_slot(run_id, capture_id, key)
            reviewer_model = key.split("model:", 1)[1] if key.startswith("model:") else key
            stem = _safe_name(run_id)
            with open(os.path.join(runs_dir, stem + ".atif.json"), "w", encoding="utf-8") as fh:
                json.dump(store.read_source(run_id, capture_id), fh, indent=1)
            source = store.read_derived(run_id, capture_id, "run_source.json") \
                if store.has_derived(run_id, capture_id, "run_source.json") else {}
            review = {
                "run_id": run_id,
                "task_id": source.get("task_id"),
                "reviewer_key": key,
                "model": reviewer_model,
                "reviewed_at": _iso_mtime(store.review_slot_path(run_id, capture_id, key)),
                "source_hash": source.get("source_hash"),
                "moment_count": len(moments),
                "selected_count": sum(1 for m in moments if m.get("selected")),
                "moments": moments,
            }
            with open(os.path.join(reviews_dir, stem + ".json"), "w", encoding="utf-8") as fh:
                json.dump(review, fh, indent=1)
            written += 1
        if written == 0:
            # Nothing eligible: keep the existing dataset intact rather than
            # replacing it with an empty one. The CLI reports failure.
            shutil.rmtree(staging, ignore_errors=True)
            return {"runs": 0, "out_dir": out_dir, "model": model}
        # Replace only once the export is ready, and keep the old dataset until
        # the swap succeeds: vacate it to a sibling backup, move staging in, and
        # restore the backup if the move fails.
        backup = staging + ".old"
        moved_existing = False
        if os.path.exists(out_dir):
            os.replace(out_dir, backup)
            moved_existing = True
        try:
            os.replace(staging, out_dir)
        except Exception:
            if moved_existing and os.path.exists(backup):
                os.replace(backup, out_dir)
            raise
        if moved_existing:
            shutil.rmtree(backup, ignore_errors=True)
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return {"runs": written, "out_dir": out_dir, "model": model}
