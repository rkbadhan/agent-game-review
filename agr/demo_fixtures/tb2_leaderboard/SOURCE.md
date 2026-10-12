# Source: Terminal-Bench 2 leaderboard demo slice

Dataset: [harborframework/terminal-bench-2-leaderboard](https://huggingface.co/datasets/harborframework/terminal-bench-2-leaderboard).
Pinned revision: [`572b2614be2c0cb2527e14f5b1e4026f1072e6c1`](https://huggingface.co/datasets/harborframework/terminal-bench-2-leaderboard/tree/572b2614be2c0cb2527e14f5b1e4026f1072e6c1).
Original files are under `submissions/terminal-bench/2.0/`.
The dataset's pinned metadata declares **Apache-2.0**; a copy is in [LICENSE](LICENSE).
This data retains that licence, independently of AGR's MIT-licensed code.

## What is included

350 published attempts: ten tasks, five attempts per task, seven agent/model configurations.
The committed source records total **23,595,099 bytes (23.6 MB)**, plus the manifest.

- Terminus 2 with GLM-4.7, GLM-5, DeepSeek-V3.2, Kimi-k2.5, Minimax-m2.5 and GPT-5.3-Codex.
- Claude Code with GLM-4.7.

Tasks: filter-js-from-html, mteb-retrieve, polyglot-c-py, fix-git,
nginx-request-logging, crack-7z-hash, cancel-async-tasks, kv-store-grpc,
pypi-server and sparql-university.

## Transformation and reproducibility

`runs/` contains compact AGR ATIF JSON produced by the Harbor adapter (version
recorded in `manifest.json`). The adapter imports trajectories, tool calls and
results, task/verifier metadata, rewards and available test diagnostics. It adds
an explicit `configuration_id` matching the submission folder; it does not call
a model or manufacture missing tool exit codes. Terminal recordings and other
artifacts outside the adapter's inputs are omitted. This is a converted subset,
not an archive of every upstream file.

[manifest.json](manifest.json) records each original input path and SHA-256,
each committed file's SHA-256 and size, its canonical source hash, task,
submission and logical run ID. The maintainer script
[`scripts/bake_tb2_demo.py`](../../../scripts/bake_tb2_demo.py) rebuilds the slice
from a local checkout of the pinned corpus, using the committed manifest to
verify the original inputs:

```bash
python scripts/bake_tb2_demo.py <local-corpus-root>
```

The script checks source hashes before converting. This rebuild step is for
maintainers only. `agr demo` reads the packaged files, verifies their hashes,
and derives reviews locally; it never downloads the corpus.

## Reading the stories

These tasks were selected to illustrate review workflows. They are **not a
representative leaderboard sample**. Terminus 2 GLM-4.7 passes 19/50 attempts;
GLM-5 passes 28/50 on this selection. Those are sample results, not leaderboard
scores or a controlled causal claim about the model upgrade.

All 35 filter-js-from-html attempts fail the same test. Shared failures warrant
investigation; they do not establish that the task caused the failures.
Polyglot-c-py is not universally failing: one DeepSeek-V3.2 attempt passes.
The contrast on cancel-async-tasks and kv-store-grpc applies to Claude Code
versus Terminus 2 **with GLM-4.7**, not to all models.

The polyglot and retail links in [START_HERE.md](START_HERE.md) use the existing
curated demo runs, which retain their separately recorded provenance and baked
reviews. The 350 leaderboard runs receive deterministic analysis; no new paid
AI reviews are generated.
