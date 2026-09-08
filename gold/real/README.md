# Real-corpus gold labels (AGR-01)

This directory holds independent human annotation of the **real** published
corpus (`eval-runs/`), one `<run_id>.gold.json` file per logical run,
in the schema defined by `agr/gold.py` (spec §15.2).

## Status: no labels yet

This directory is a scaffold, not a dataset. As of this commit it holds no
`*.gold.json` files — human annotation is a separate, scheduled workstream
(see `experiments/terminal_bench/annotation-protocol.md`), not something a
coding change can produce. `agr.gold.load_gold_set("gold/real")` returns an
empty `GoldSet` until annotation lands; that is the honest state, not a bug.

**Do not treat `archive/synthetic/gold/` as a substitute.** Those are
synthetic reference labels authored for synthetic fixtures, used to pin the
schema and scoring mechanics (see that directory's own README). They
demonstrate the schema; they are not evidence about real-world precision or
recall, and must never be reported as if they were.

## How labels land here

1. Run `python experiments/terminal_bench/build_corpus_manifest.py` to get
   the current list of eligible logical runs and their checksums.
2. Follow `experiments/terminal_bench/annotation-protocol.md` to produce a
   `GoldAnnotation` per run, from source evidence only.
3. Write one `<run_id>.gold.json` file per run (`GoldTrajectory.to_dict()`),
   with `label_batch` set to the batch this annotation pass belongs to.
4. Complete and, where double-labelled, adjudicate a whole batch before
   setting `frozen: true` on its trajectories — see the protocol's Freezing
   section. `agr.gold.GoldSet.unfrozen_run_ids()` lists what is still
   in-progress; `agr.gold.GoldSet.independent_human_gold()` is the frozen,
   human-sourced subset actually usable for scoring (AGR-15).

## Provenance a reader can check

Every file here should be loadable and should validate:

```bash
python -c "
from agr import gold
gs = gold.load_gold_set('gold/real')
print(len(gs.trajectories), 'trajectories,', len(gs.unfrozen_run_ids()), 'not yet frozen')
gs.validate()
"
```

`GoldSet.validate` rejects out-of-vocabulary tags, unknown attribution
levels, and an unknown `label_source` — see `agr/gold.py` and
`agr/taxonomy.py` for the controlled vocabularies.
