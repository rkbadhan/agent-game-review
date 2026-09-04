# Published evaluation corpus — Terminal-Bench 2.0 pilot

This directory is the **immutable source record** for the pilot sweep behind the
[gallery](../experiments/terminal_bench/gallery.md) and the Gate B evaluation
(`../experiments/terminal_bench/gate-b-eval-results.md`). Everything here was
produced by real agent runs on this machine; nothing is synthesized.

## What ran

- **Harness:** Harbor (ATIF-v1.7), Terminal-Bench 2.0 tasks
- **Agent model:** `openai/stealth/ox-alpha` via OpenRouter free tier — an
  anonymous, unpinned preview model. Exact reproduction is therefore weak; we
  say so rather than pretend otherwise.
- **Agent configurations:** mini-swe-agent 2.4.6 and Terminus-2 2.0.0, same
  model throughout
- **Reviewer model** (for AGR's semantic layer, not needed to browse):
  Kimi via OpenRouter

## Layout

One directory per Harbor job; one subdirectory per trial
(`<task>__<trial-id>`):

```
eval-runs/<job>/<task>__<id>/
  trajectory.json    # full ATIF agent trajectory (the source of record)
  result.json        # verifier outcome for the trial
  verifier/          # verifier script output
  agent/, artifacts/, config.json, trial.log, ...
<job>/result.json    # job-level aggregate
```

The `_writeoffs/` directory (aborted/debug jobs) is intentionally not
published; it is accounted as infra write-off in the debug log.

## Browse it with AGR

From the repo root, no model calls or credentials needed for the
deterministic review:

```bash
pip install .[api]
agr ingest-harbor eval-runs/          # ingests every job here (batch mode)
agr runs                              # list the ingested runs
agr serve                             # browse at http://127.0.0.1:8000
```

or ingest a single job:

```bash
agr ingest-harbor eval-runs/batch1r-raman-fitting/
```

The featured runs in the gallery are: `batch1r-raman-fitting` and
`swpA2-raman-fitting-a1` (raman-fitting), `pilot-miniswe` /
`pilot-terminus2` (make-mips-interpreter), `swpB2-nginx-request-logging-a2`
(nginx, incl. the false-positive audit), `swpB-nginx-request-logging-a2`
(nginx crash), `swpC2-polyglot-c-py-a1` (polyglot-c-py).

## Redaction

All trajectories and logs were scanned for credentials before publishing
(API-key patterns, bearer tokens, PEM blocks, `export *_KEY=` assignments);
no real secrets are present. The only `*_API_KEY=` strings are placeholder
text inside documentation pages the agent read during a run.
