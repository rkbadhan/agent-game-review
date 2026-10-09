# τ³ retail run — 2026-10-07

An end-to-end run of AGR over the τ³ retail benchmark (τ²-bench / tau2-bench,
MIT). 74 train tasks × 5 attempts, one agent and model, plus controls. This is
the aggregate report; the raw trials stay local (see "Data" below).

## Setup

| | |
| --- | --- |
| Benchmark | τ³ retail, `sierra-research/tau2-bench` v1.0.0 (split: 74 train / 40 test) |
| Runner | Harbor `v0.24.0`, tau3-bench adapter |
| Runtime | tau2-bench v1.0.1 in the task container |
| Agent + user + NL judge | `deepseek-v4p1-flash` (Fireworks, OpenAI-compatible) |
| Attempts | 74 tasks × 5 = 370 |

## Full run

| | |
| --- | --- |
| Trials | 370 / 370 |
| Exceptions | 0 |
| Passed | 310 (0.84) |
| Runtime | 2h 13m (concurrency 10) |
| Tokens | 23.4M in · 1.45M out · 21.0M cached |

Pass distribution (tasks by x of 5):

| 0/5 | 1/5 | 2/5 | 3/5 | 4/5 | 5/5 |
| --- | --- | --- | --- | --- | --- |
| 2 | 3 | 2 | 7 | 18 | 42 |

30 of 74 tasks vary (0 < passes < 5); 42 always pass; 2 always fail.

## Failure shape (60 failures)

- 49 database mismatch · 10 unmet NL assertion · 4 `agent_error`. The groups
  overlap: a failure can fall in more than one, so they sum to 63, not 60.
  `agent_error` is the agent's own error and counts as a failure (adapter
  0.13 treats only the verifier-side runtime-log statuses as operational).
- 88 failed action checks: **64 omitted / 24 mismatch**
- 40 of 60 failures are all-omitted-writes, across 23 of 74 tasks
- most-failed actions: `exchange_delivered_order_items` (37),
  `return_delivered_order_items` (15), `cancel_pending_order` (11)

So the recurring shape — the agent omits a required *write* — holds at scale.

## Controls

- **Same seed** (20 attempts): the user simulator is **not** seed- or
  temperature-controllable (five same-seed attempts produced five different
  openings), so the plan's "match user turns" control is not implementable as
  written.
- **Fixed user** (a recorded user script replayed via `_ScriptedUser`, 20
  attempts): 18/20 pass. The failures are largely **simulated-user-driven**;
  the one task that reproduced the omission with a held-constant user is partly
  a **task artifact** (it requires a no-op `modify_user_address` the agent
  correctly skips).
- **Candidate fix** (pre-commit state check, task 110): baseline 2/5 → fix 1/5.
  **Negative / no signal** on n=5.

## Blind comparison (prepared, not judged)

A bare model given only raw traces + the verifier diff reaches the **same**
diagnosis AGR does ("omitted the required … call"), and sometimes disagrees
("no single common cause"). 10-task blind package assembled; it has not been
judged.

## Conclusion

- AGR ingests τ³ runs end to end with the full verifier breakdown and explains
  failures (divergence + breakdown) — proven.
- The recurring omitted-write behaviour is real, but it is entangled with a
  stochastic user simulator, and the one clean agent case is a task artifact.
  It is a hypothesis, not a proven single-cause fixable mechanism.
- Early (non-blind) signal: AGR is **not obviously better** than a plain model
  on the per-task diagnosis.

## Data

Raw trials, verifier breakdowns, the AGR store and the blind package stay
local — they are large (~170 MB) and contain benchmark task content. Regenerate
with the scripts in the run folder; the aggregate numbers above are the
committed record.
