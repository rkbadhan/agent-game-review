# Configure and run AI review

AI review is optional. Imports and ordinary browsing use deterministic analysis
without contacting a model.

## Set up the local app

Install the adapter for your provider, then start the app:

```powershell
python -m pip install '.[api,model-openai]'
python -m agr serve
```

Use `.[api,model-anthropic]` for Anthropic. In an active personal project:

1. Open **Settings → AI review**.
2. Choose the API provider, enter the reviewer model ID, and set its endpoint.
   The OpenAI adapter also supports compatible hosted gateways and local servers.
3. Supply a session-only key, or configure `OPENAI_API_KEY` /
   `ANTHROPIC_API_KEY` in the server environment or local `.env`.
   For a local server that ignores authentication, enter a non-empty placeholder.
4. **Save AI settings** saves non-secret defaults without calling a model.
   The API key stays in server memory and disappears on restart.
5. Optionally choose **Test connection · uses tokens**. It sends a small
   synthetic JSON request, never one of your traces, and checks the expected
   response. This checks connection/JSON support, not review quality.

Settings show the effective model/endpoint, credential source, and environment
overrides. The app and CLI share `~/.agr/config.json` (`AGR_CONFIG` overrides its
location). These are defaults for local reviews across projects.

Environment variables override saved settings. CLI flags override both.
When changing providers, old saved model/endpoint values are cleared.
Blank model/endpoint CLI values reset those fields to the provider default;
**Reset saved AI settings** is available when a corrupt file prevents loading.

Session keys are tied to the chosen provider/model/endpoint configuration.
Changing a destination does not reuse a session key from a different
configuration. **Forget session keys** removes all such keys from this server.
Trace-source credentials are never used as model credentials.

## Review one run or a batch

Open a run and choose **Run AI review**, or choose **Review runs with AI** in
Runs. Select the runs, then **Preview AI review**. The preview names the actual
reviewer and destination, shows the selected count and prior successful reviews
for that configuration, and explains cost targets.

**Start AI review · uses tokens** explicitly sends redacted evidence packets.
A long trace can require summaries, expansion, and revision, so a review can
use several completions. Import and preview never start model work.

Jobs show per-run progress and completed, skipped, failed, or incomplete
results. Refresh restores the open job. Reopen past jobs through **Review
history** on the AI review screen. **Cancel remaining reviews** stops between
runs; an in-flight request can finish and incur cost. **Retry unfinished
reviews** retries failures/incomplete work without repeating completed or
skipped runs. After a server interruption, unfinished jobs require an explicit
retry; they do not resume paid work automatically.

Review selections pin each run's capture. If a capture or configuration changes
after preview, preview again. A queued job also refuses to silently review a
newer capture. Jobs are isolated to their project. Sample/archived projects and
saved evidence snapshots cannot start model work.

Successful snapshots are identified by provider, model, and endpoint.
A different configuration receives a separate snapshot. Existing older reviews
remain readable and matching legacy reviews count toward the skip check. If an
older review has no endpoint provenance, it matches only the official provider
endpoint. A custom endpoint requires its own review. Rerunning the same configuration replaces its current
snapshot while retaining attempt/telemetry history. When baking a model with
multiple saved configurations, pass the full reviewer key to `bake-reviews --model`
to select the intended destination.

## Limits and CLI use

Advanced settings save an estimated cost target per run (default $0.15), an
elapsed-time target per run (default 90 seconds), and a timeout per provider
request (default 600 seconds). Zero disables either target; request timeout must
be positive. SDK transient-error retries retain their provider defaults; retrying
an unfinished job is explicit.

Targets are checked between calls. They are not hard billing or elapsed-time
caps. Cost is a character/token estimate using built-in fallback rates, not
actual provider usage; local servers can have no provider charge despite this
estimate. A budget/evidence stop is recorded as incomplete and the deterministic
baseline remains available.

```powershell
python -m agr config --provider openai --model YOUR_MODEL_ID --base-url ENDPOINT_URL
python -m agr config --cost-budget 0.15 --time-budget 90 --request-timeout 600
python -m agr config
python -m agr config --test
python -m agr --store STORE_PATH review RUN_ID
python -m agr --store STORE_PATH review --all
```

A combined `config ... --test` saves changes only if the test succeeds.
`review --all` skips successful reviews for the selected configuration;
`--force` reruns them. Settings/limits can also come from
`AGR_REVIEW_PROVIDER`, `AGR_REVIEW_MODEL`, `OPENAI_BASE_URL` /
`ANTHROPIC_BASE_URL`, `AGR_REVIEW_COST_BUDGET_USD`,
`AGR_REVIEW_TIME_BUDGET_S`, and `AGR_REVIEW_REQUEST_TIMEOUT_S`.

No model availability, billing, or review-quality claim is inferred from a
successful synthetic connection test.


## Verification

Tests use mocked providers; they make no real model requests or billing checks.

Core/API suite:
`python -m pytest tests --ignore=tests/test_ui.py --ignore=tests/test_ai_review_ui.py`

Browser suite (install the optional browser dependency group first):
`python -m pytest tests/test_ui.py tests/test_ai_review_ui.py`

If using an installed Chromium rather than Playwright's bundled browser, set
`PLAYWRIGHT_CHROMIUM_EXECUTABLE` to its absolute executable path.
