# legacy

Evaluation tooling that was built for directions the project has since moved away
from. It is kept **runnable**, but it is deliberately outside the core `agr`
package and the default test run, so core work does not have to keep it passing.

## What is here

| Path | What it is |
|---|---|
| `benchmark.py`, `benchmark_whowhen.py`, `benchmark_trail.py` | External attribution benchmark adapters (Who&When, TRAIL) and scorer |
| `gold.py`, `gold/` | Gold-label schema and loader; `gold/synthetic/` (reference labels for `archive/synthetic/fixtures`) and `gold/real/` (empty scaffold) |
| `reviewer_eval.py` | Reviewer-agnostic scoring harness (Precision@3, Recall@3, ...) |
| `evaluation.py` | Human-audit pack and scoped evaluation report |
| `corpus_manifest.py` | Reproducible manifest of the `eval-runs/` Harbor corpus |
| `cli.py` | `python -m legacy {eval,benchmark,audit-pack,publish-evaluation}` |
| `tests/`, `docs/` | Tests and design notes for the above |

## Running it

`legacy` is **not part of the installed package** (the wheel ships `agr` only), so it
only runs from a repository checkout, with the dev environment installed:

```bash
python -m legacy eval                     # score the baseline against the synthetic gold set
python -m legacy benchmark who-and-when --data DATA
pytest legacy                             # the legacy tests (the default `pytest` skips them)
```

Run these from the repository root. From any other directory the module is not
importable; point `PYTHONPATH` at the checkout (an editable install does not help,
it only exposes `agr`):

```bash
PYTHONPATH=/path/to/eval-game python -m legacy eval
```

`eval`'s default gold and fixture directories resolve against the checkout, so they
work from any directory once `legacy` is importable.

## Migrating callers

`agr eval`, `agr benchmark`, `agr audit-pack` and `agr publish-evaluation` still
exist as stubs: they print the pointer to `python -m legacy <command>` and **exit
with code 2**, so any script or notebook that calls them must be switched.
`benchmark`, `audit-pack` and `publish-evaluation` used to work from a
`pip install`; they now need a checkout. `eval` always needed one (the fixtures and
gold labels were never packaged).

## Rules

- **One-way dependency.** `legacy` may import `agr`. `agr` must never import `legacy`.
  This is what keeps core changes free of legacy drag. The one thing the core
  shares with this code, the moment shape and anchor matching used by Compare, lives
  in `agr/moments.py`.
- **Not a required check.** `.github/workflows/legacy.yml` runs these tests nightly and
  when `legacy/` changes, but never blocks a merge. A core change that breaks a
  legacy test is not required to fix it.
- **Revive or delete.** Fix a broken legacy test only if you want that tool back. If
  the nightly run stays red and nobody wants it, delete the directory.
