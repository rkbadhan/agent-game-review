# Archived synthetic data

Everything in this directory is **synthetic reference data** — authored by hand
to build and test the deterministic core before any real trajectory source
existed. It is parked here, not deleted, because the test suite still depends
on it and the `agr eval` baseline harness still scores against it.

## Contents

- `fixtures/` — six synthetic ATIF runs (chess, recovery, retry,
  ignored-failure, contract-mismatch, clean-pass). Used by the test suite and
  by `agr demo-store` / `agr eval` (via `--fixtures`).
- `gold/` — synthetic gold labels for those fixtures, exercising the §15.2
  annotation schema. **Not expert-adjudicated production data** — see
  `gold/README.md` for the honesty note.
- `agr-store/` — a store snapshot ingested from the synthetic fixtures, kept
  for reference. The live store for real runs is the repo-default `.agr-store`.

## Real data

Real runs (pi sessions ingested via `agr ingest-pi`) live in the repo-default
`.agr-store`. Real gold labels, once annotated, should live in a top-level
`gold/` directory again — the archived synthetic set keeps its qualified name
so the two are never confused.
