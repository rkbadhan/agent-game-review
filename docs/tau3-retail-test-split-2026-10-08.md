# τ³ retail test split — 2026-10-08

The held-out τ³ retail **test** split (40 tasks), same agent, model and harness
as the train run. This is the split the plan reserved "for testing a fix later";
run here as a held-out evaluation of the frozen agent. Aggregate report only —
raw trials stay local (see "Data").

## Setup

| | |
| --- | --- |
| Benchmark | τ³ retail, `sierra-research/tau2-bench` v1.0.0 — **test** split (40 tasks) |
| Runner | Harbor `v0.24.0` + tau3-bench adapter |
| Runtime | tau2-bench v1.0.1 |
| Agent / user / NL judge | `deepseek-v4p1-flash` (Fireworks, OpenAI-compatible) |
| Attempts | 40 tasks × 5 = 200 |
| Fix applied | none — this is the frozen agent |

## Results

| | |
| --- | --- |
| Trials | 200 / 200 |
| Exceptions | 0 |
| Passed | 176 (0.88) |
| Runtime | 1h 12m (concurrency 10) |
| Tokens | 12,687,986 in · 744,925 out · 11,280,172 cached |

Pass distribution (tasks by x of 5):

| 0/5 | 1/5 | 2/5 | 3/5 | 4/5 | 5/5 |
| --- | --- | --- | --- | --- | --- |
| 1 | 1 | 0 | 4 | 7 | 27 |

**12 of 40 tasks vary** (0 < passes < 5); 27 always pass; 1 always fails.

## Failure shape (24 failures)

| measure | value |
| --- | --- |
| database mismatch | 22 |
| unmet NL assertion | 0 |
| `agent_error` (the agent's own error, counted as a failure) | 2 |
| failed action checks | 36 (**23 omitted / 13 mismatch**) |
| failures that are ALL omitted writes | 11 |
| tasks with an omitted required write | 10 |

Most-failed actions: `return_delivered_order_items` (8), `cancel_pending_order`
(7), `modify_pending_order_items` (6), `calculate` (6),
`exchange_delivered_order_items` (5).

## Train vs test

| | train (74×5) | test (40×5) |
| --- | --- | --- |
| trials | 370 | 200 |
| pass rate | 0.84 | 0.88 |
| tasks with variation | 30 / 74 | 12 / 40 |
| failures | 60 | 24 |
| db mismatch | 49 | 22 |
| unmet NL assertion | 10 | 0 |
| all-omitted-write failures | 40 | 11 |
| tasks with an omitted write | 23 | 10 |

The same shape appears on held-out tasks — omitted required writes, mostly
returns/cancellations — but weaker, and with no NL-assertion failures at all
(every test-split failure was a database mismatch or an `agent_error`).

## Reading

- The frozen agent scores **0.88** on the held-out split, consistent with the
  0.84 train figure for the same unchanged agent.
- The omitted-write behaviour **generalises** to unseen tasks (10 of 40), so it
  is a real agent tendency, not a train-set artifact.
- But, as on the train split, it is entangled with the stochastic user, and the
  plan's one-clean-case caveat still holds. This run does **not** test a fix;
  the earlier candidate fix was negative and is not applied here.

## Data

Raw trials at `jobs/test/2026-10-07__23-49-21/` (local). Regenerate with
`configs/test.yaml` + the run-folder scripts. The aggregate numbers above are
the committed record.
