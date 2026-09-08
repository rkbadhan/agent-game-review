#!/usr/bin/env python3
"""Build the AGR-01 corpus manifest for the published eval-runs/ corpus.

    python experiments/terminal_bench/build_corpus_manifest.py

Writes ``experiments/terminal_bench/corpus_manifest.json``. Re-run after any
change to ``eval-runs/`` or to the ingestion/adapter logic that resolves
logical run identity — the manifest is a build artifact, not a hand-maintained
record, and a stale one should be regenerated rather than hand-edited.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from agr.corpus_manifest import build_manifest, write_manifest  # noqa: E402

# The commit the pinned review (docs/AGR final task list) was performed
# against. Update only when re-pinning the review to a new baseline commit —
# never to match whatever HEAD happens to be at generation time.
REVIEWED_COMMIT = "dea7af10a0093642362d80abbc7637b2c3c12324"

DEFAULT_SOURCE_ROOT = REPO_ROOT / "eval-runs"
DEFAULT_OUT_PATH = Path(__file__).resolve().parent / "corpus_manifest.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", default=str(DEFAULT_SOURCE_ROOT))
    parser.add_argument("--out", default=str(DEFAULT_OUT_PATH))
    parser.add_argument("--reviewed-commit", default=REVIEWED_COMMIT)
    args = parser.parse_args()

    manifest = build_manifest(args.source_root, reviewed_commit=args.reviewed_commit)
    write_manifest(manifest, args.out)

    c = manifest["counts"]
    print(f"wrote {args.out}")
    print(f"  discovered_trials : {c['discovered_trials']}")
    print(f"  eligible_trials   : {c['eligible_trials']}")
    print(f"  excluded_trials   : {c['excluded_trials']}")
    print(f"  logical_runs      : {c['logical_runs']}")
    print(f"  ingested_captures : {c['ingested_captures']}")
    print(f"  active_captures   : {c['active_captures']}")
    if manifest["exclusions_by_reason"]:
        print("  exclusions:")
        for reason, count in sorted(manifest["exclusions_by_reason"].items()):
            print(f"    {count:3d}  {reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
