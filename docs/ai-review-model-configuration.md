# AI review models: current behavior and configuration recommendation

Investigated 2026-10-09 against `origin/main` at `556d1f7`, on
`codex/ai-review-model-config`. Investigation only: no application behavior
changed and no paid model calls made.

> This records the pre-implementation investigation. The implemented flow is
> documented in [AI review setup and behavior](ai-review.md).

## Recommendation

Add **Settings → AI review**, backed by the same settings resolver as the CLI.
Let users choose a provider, enter a model, supply credentials, optionally test
the connection, and save a default. Offer **Run AI review** on a run and
**Review selected runs** in a project. Saving settings, importing traces, and
ordinary navigation should make no model calls.

The adapters and review pipeline already exist. The missing product layer is
discoverable setup and an explicit way to start a review from the app. Before
exposing that layer, fix provider-switch behavior, effective-setting reporting,
and review identity so the chosen configuration reliably determines what runs.

## How models are used today

1. Import/ingest runs deterministic analysis by default. The captured agent's
   model is run metadata; it does not select the model reviewing that run.
2. `agr review <run_id>` resolves settings, constructs one adapter, and runs the
   existing pipeline with it. `--all` reuses that adapter across runs, resetting
   the per-review budget state for each proposal pass.
3. The reviewer gets a typed, redacted evidence packet containing task
   information, verifier checks, deterministic candidates, and a timeline
   digest. It judges candidates and can discover additional semantic moments.
   It has no tools; captured trace content is treated as evidence.
4. The same configured model summarizes phase chunks for long traces, handles
   bounded evidence expansion, and revises a rejected candidate when requested.
   One review can involve multiple completions. There are no separately
   configurable summary, judge, or revision models.
5. Deterministic gates recompute facts, constrain attribution, and select
   moments. A provider failure or budget stop records a failed/incomplete
   attempt and serves the deterministic fallback.
6. Successful review snapshots, attempts, and telemetry are persisted beside
   the capture. Telemetry includes latency and estimated cost; the completion
   interface does not capture actual provider token usage.

Sources: [CLI review commands](../agr/cli.py),
[`_LazyModelReviewer`](../agr/model_reviewer.py),
[`analyze`](../agr/pipeline.py), and
[`run_reviewer`](../agr/reviewer.py).

### Supported adapters

| Adapter | Request format | Default model in this checkout | Credentials | Endpoint |
| --- | --- | --- | --- | --- |
| `anthropic` | Anthropic messages; `max_tokens=8000` | `claude-opus-4-8` | `ANTHROPIC_API_KEY` via SDK | Explicit/saved URL, otherwise SDK environment/default |
| `openai` | Chat completions with JSON-object response format | `gpt-4o` | `OPENAI_API_KEY` via SDK | Explicit/saved URL, otherwise `OPENAI_BASE_URL` or SDK default |

These are code defaults, not verified statements about current availability or
recommended model quality. A compatible endpoint must support the actual request
format and return usable structured output. There is no model catalog, automatic
model selection, provider fallback, or model-quality benchmark in the setup flow.
Provider SDKs are optional extras and are checked before review.

Sources: [`AnthropicReviewer`, `OpenAIReviewer`, `make_reviewer`](../agr/model_reviewer.py)
and [optional dependencies](../pyproject.toml).

## How users can configure it now

The existing CLI provides a one-time setup path:

```powershell
# Install the chosen adapter from the repository.
python -m pip install '.[model-openai]'

# Set OPENAI_API_KEY in the environment or a local, gitignored .env file.
# Replace YOUR_MODEL_ID with an ID supported by the chosen endpoint.
python -m agr config --provider openai --model YOUR_MODEL_ID

# For a custom compatible endpoint, also provide its URL:
# python -m agr config --provider openai --model YOUR_MODEL_ID --base-url ENDPOINT_URL

python -m agr config                  # inspect settings; no completion call
python -m agr config --test           # explicitly makes one real completion
python -m agr --store STORE_PATH review RUN_ID
python -m agr --store STORE_PATH review --all
```

For Anthropic, use `.[model-anthropic]`, `ANTHROPIC_API_KEY`, and
`--provider anthropic`. When changing an existing provider/endpoint configuration,
`agr config --clear` followed by a complete new configuration avoids retaining
old fields. Clearing saved settings does not clear environment variables.

Settings live in `~/.agr/config.json`, overridable through `AGR_CONFIG`.
They are user-level defaults shared across stores, rather than project settings.
The file contains provider, model, endpoint, and update time. Credentials remain
in the environment or `.env`. The CLI loads dotenv when the optional dependency
is available and supports `--env-file` before the subcommand. OS environment
variables win over dotenv values.

### Actual resolution order

| Setting | Current precedence |
| --- | --- |
| Provider | CLI flag → saved provider → `anthropic` |
| Model | CLI flag → `AGR_REVIEW_MODEL` → saved model → adapter default |
| OpenAI endpoint | CLI flag → saved endpoint → `OPENAI_BASE_URL` → SDK default |
| Anthropic endpoint | CLI flag → saved endpoint → SDK environment/default |
| Estimated cost target | Review CLI flag → `AGR_REVIEW_COST_BUDGET_USD` → `$0.15` per review |
| Elapsed-time target | Review CLI flag → `AGR_REVIEW_TIME_BUDGET_S` → `90s` per review |

There is no `AGR_REVIEW_PROVIDER` setting. Budgets cannot be saved through
`agr config`; they are constructor/CLI/environment settings. Zero disables the
respective target. Budgets are checked before subsequent rounds, so an in-flight
completion can finish over the target. The elapsed-time target is not a provider
request timeout.

Prices use a small hardcoded model-prefix table and fallback rates for unknown
models. A local model's estimate is not evidence of an actual provider charge.
The current cost calculation derives tokens from user-payload/output character
counts; it omits system-prompt tokens and does not measure provider billing.

Sources: [`resolve_review_settings`, `save_config`](../agr/userconfig.py),
[`build_parser`, `_load_env`, `cmd_config`](../agr/cli.py),
[budget and cost calculation](../agr/model_reviewer.py), and
[environment example](../.env.example).

## Confirmed gaps

| Finding | Evidence and impact | Recommended change |
| --- | --- | --- |
| App setup and execution are absent | Settings edits reviewer/project names only. Workspace routes do not provide AI configuration or review jobs. The run header directs users to a CLI command in a tooltip. | Add an AI review settings card and explicit review actions with status. |
| Provider switches retain incompatible fields | Saving only `provider=anthropic` after OpenAI preserves the OpenAI model and endpoint. One-off provider overrides inherit saved fields too. The README's provider-switch example can hit this. | Treat provider/model/endpoint as a configuration unit. Clear or explicitly reconfirm incompatible dependent fields. |
| Effective endpoint reporting is incomplete | With only `OPENAI_BASE_URL`, `agr config` prints `base_url: (provider default)` while the adapter uses the environment URL. A saved URL beats the environment despite generic displayed `flag > env > file` precedence. | Resolve/display the actual model, endpoint, and each value's source through one shared resolver. |
| Connection testing is weaker than its UX implies | `--test` sends a string that is not valid JSON and accepts any non-raising completion response. It proves a completion path responded, rather than review-schema compatibility or review quality. | Send a small valid synthetic payload, validate the expected structured output, and label the test's scope and token spend. |
| Failed tests still save settings | `config --provider ... --model ... --test` writes the file before testing. Failure leaves the new configuration active. | Separate Save and Test; show test state. A combined test-and-save action should replace active settings only on success. |
| Changing models does not make `--all` review them | `_already_enriched` skips a capture if any model review exists, regardless of the requested model. `--force` is needed for a different model. | Distinguish reviewed with this configuration from reviewed with another configuration. |
| Review identity omits provider/endpoint | Keys are `model:<model_id>`. The same ID on two endpoints/providers uses the same snapshot key; later successful reviews can replace that slot. | Include a stable non-secret configuration identity in slots and telemetry, preserving compatibility with existing records. |
| Corrupt config silently selects defaults | Invalid JSON becomes an empty config, unexpectedly reverting provider/model. | Show a recoverable error and the file location before paid work. |
| Budgets are hard to discover | They are outside saved config and the Settings UI. Cost is approximate and time targets do not interrupt requests. | Show advanced per-review targets with honest labels; expose provider timeout separately if implemented. |

Additional sources: [`renderWorkspaceSettings`](../agr/static/js/workspace.js),
[workspace API](../agr/workspace_api.py),
[`Workspace.update_settings`](../agr/workspace.py), and
[run header/AI hint](../agr/static/js/render.js).

## Proposed user flow

1. **Settings → AI review:** choose Anthropic, OpenAI, or a custom
   OpenAI-compatible endpoint. Explain that OpenAI-compatible describes the API
   format and includes models hosted elsewhere.
2. **Model:** enter its ID and show the concrete effective value. Start with free
   text to support custom/local endpoints. Model discovery can come later;
   do not promise that every model supports structured reviewer output.
3. **Credentials:** show whether the server has the required environment key.
   Optionally accept a session-only key using the existing local workspace
   boundary. Do not return keys to the browser or save them in plain JSON.
   Keep model credentials separate from trace-source credentials.
4. **Endpoint:** place it under Advanced for hosted defaults and show it directly
   for custom/local providers. Show the effective destination and any override.
5. **Save / Test connection:** Save makes no model call. Test uses synthetic data
   and an explicit small paid completion. Distinguish missing SDK, credentials,
   authorization, endpoint/model, and response-format errors. Show Saved,
   Untested, Verified, or Test failed with test time/configuration identity.
6. **Run AI review:** show the reviewer model separately from the captured agent
   model. Explain that a redacted packet goes to the chosen endpoint, that one
   review may involve multiple calls, and that budget values are targets.
   Starting a review is a separate explicit action after import.
7. **Batch review:** use the active project's store and selected runs. Show
   counts already reviewed with this configuration and the rerun behavior.
   Keep durable queued/running/completed/failed/incomplete status and preserve
   the deterministic fallback and prior reviews.

For first delivery, use one default configuration shared by CLI and local app.
Label its scope “Default for local reviews.” Named profiles/project overrides
can follow once switching works reliably. Avoid a second UI settings file with
conflicting CLI behavior.

This follows the onboarding spec's explicit model-enrichment action after
deterministic import, configured provider/model display, and honest cost
information: [onboarding spec](engineer-onboarding-and-connectors-spec.md).

## Follow-up implementation order

1. Shared effective resolution, dependent-field handling, validation, reset
   controls, and value origins; regression coverage for the confirmed cases.
2. Local settings/test APIs and Settings card, reusing existing
   host/session/origin/read-only protections and session-only secret handling.
3. Configuration-aware review identity, targeted skip behavior, and non-secret
   provenance for every attempt.
4. Project-scoped review jobs, progress, and retries around the existing
   pipeline; verify active projects, sample/read-only mode, snapshots, failures,
   and refresh during a job.

Keep one model selection for all review stages in the first delivery.
Role-specific summary/judge models should follow quality/cost evaluation that
justifies the added configuration.

## Verification and limits

Read current CLI, resolver, adapters, pipeline, app Settings, workspace API,
README, environment example, and existing configuration/budget tests. Ran
isolated stdlib Python probes using temporary `AGR_CONFIG` files, cleared
environment state, mocked dotenv loading, and fake completion/failure paths.

The probes confirmed retained provider-switch settings, saved-endpoint
precedence, environment-endpoint display mismatch, colliding reviewer keys,
persistence after failed testing, invalid synthetic test JSON, acceptance of an
unrelated completion response, inability to save budgets through `config`, and
silent fallback for malformed config. Runtime adapter/budget defaults were
also inspected. No user config or credentials were read or changed by the
probes, and no provider requests were made.

The bundled Python lacks `pytest`; existing suites were read but not executed.
Live endpoint compatibility, model availability, review quality, and actual
pricing were not validated.

