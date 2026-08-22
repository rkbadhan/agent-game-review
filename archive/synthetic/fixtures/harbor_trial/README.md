# Synthetic Harbor trial fixture

A hand-authored, **synthetic** Harbor (Terminal-Bench 2.0) trial directory,
shaped like the real `harbor run` output tree:

```
harbor_trial/
  result.json          synthetic TrialResult (reward = 1.0 pass)
  agent/
    trajectory.json    synthetic ATIF-v1.7 trajectory
```

It exists so the `harbor` adapter (`agr/ingest_harbor.py`) and its tests have a
realistic source without needing a live Terminal-Bench run. It is **not** a real
run — the model output and reward are invented for testing the format mapping
only, exactly like the other fixtures under `archive/synthetic/`.

Ingest it the same way you would a real trial:

```bash
python3 -m agr ingest-from --adapter harbor \
    archive/synthetic/fixtures/harbor_trial --task-id fix-failing-test
```
