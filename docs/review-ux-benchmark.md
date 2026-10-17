# Review UX benchmark

17 October 2026 · Research before the next design iteration

The earlier rail/top-navigation mock-ups are exploratory concepts. The checks performed on them establish that their local interactions work; they do not establish usability, visual quality relative to peers, or a preferred navigation model.

## Method and limits

Reviewed official product documentation and visually inspected published interface examples from Langfuse, LangSmith, Braintrust, Linear and Sentry. These products cover the relevant jobs: finding traces, inspecting execution, processing review queues, and prioritizing debugging work. This is a qualitative pattern review, not a ranking or a timed usability study. Authenticated production apps were not exercised. The Sentry redesign announcement is dated February 2025; it explains design rationale rather than proving the exact current screen layout.

## What the references demonstrate

| Product | Observed or documented pattern | Implication for Agent Game Review — design inference |
|---|---|---|
| Langfuse | Its published tracing-table example keeps the same operation name on many rows while showing different Input and Output content. Current table guidance emphasizes filters and saved views for large datasets. | Keep a repeated workflow name stable. Help users distinguish executions through meaningful content and queryable context. |
| LangSmith | The Runs table supports configurable Input/Output preview paths. Selecting a row opens a side panel. Annotation queues provide a separate focused review flow with instructions, feedback and progress. | Provide both broad scanning and focused reviewing. A user should not need to decode a hash or read every full trace to choose the next run. |
| Braintrust | Logs default to ungrouped trace rows. Named views include Errors and Unreviewed. Grouping is optional. A selected trace opens in a right panel that can expand to fullscreen. | Default to a searchable flat worklist with optional task/session grouping; allow the inspector to expand when evidence needs room. |
| Linear | Triage is a dedicated inbox with explicit processing actions and shortcuts. Its general display options distinguish ordering from grouping; grouping is not universal across views. | Treat review as a workflow with a next item and clear completion semantics. Avoid adding groups just because a dataset contains repeated names. |
| Sentry | Its documented redesign separates workflow actions from issue/event information and uses collapsible sections and drawers for secondary details. Issue detail navigation keeps search context relevant to the selected event. | Lead with the finding and supporting evidence. Keep metadata, activity and infrequent actions progressively accessible. |

Sources supporting the rows above:

- Langfuse: [Tracing best practices and the input/output table example](https://langfuse.com/docs/observability/best-practices), [working with the observations table](https://langfuse.com/faq/all/explore-observations-in-v4), [saved table views](https://langfuse.com/changelog/2025-05-20-save-table-views).
- LangSmith: [configurable previews](https://docs.langchain.com/langsmith/configure-input-output-preview), [trace side panel](https://docs.langchain.com/langsmith/view-traces), [annotation queues](https://docs.langchain.com/langsmith/annotation-queues).
- Braintrust: [logs, default views and optional grouping](https://www.braintrust.dev/docs/observe/view-logs), [right-panel and fullscreen trace viewer](https://www.braintrust.dev/docs/observe/examine-traces).
- Linear: [Triage and keyboard actions](https://linear.app/docs/triage), [display options and grouping limits](https://linear.app/docs/display-options).
- Sentry: [information hierarchy and progressive disclosure](https://sentry.io/changelog/new-issue-details-ui-now-available/), [issue details and contextual event navigation](https://docs.sentry.io/product/issues/issue-details/).

## Decisions changed by the research

1. **Withdraw the rail recommendation as a settled decision.** A rail can recover space, but these references do not establish it as the best navigation for this app. Keep labelled navigation in the browsing workspace and compare an optional compact shell during focused review. Test discoverability as well as row capacity.
2. **Make grouping optional.** Start with a flat list. If all 50 runs are `process_case`, a single collapsible group does not make them distinguishable. Offer task/session grouping when the user wants that relationship. A group of matching names does not imply a shared failure cause.
3. **Stop compressing discovery into a two-line sidebar.** Start the next concept with a broad Runs worklist. Show input/context, outcome, finding, and human-review state as distinct fields, with optional columns for time/session/user/configuration. Use explicit empty values when a field was not captured.
4. **Support quick inspection and focused review as separate modes.** Opening a row shows a resizable inspector. Expanding it enters the focused review workspace, with a compact queue, next/previous navigation, and existing review actions. Returning restores filters, selection and scroll position.
5. **Prioritize the selected run's evidence.** Put the concise finding, relevant input/output and review action first. Keep raw identifiers, step counts, secondary metadata and activity in details. Preserve the distinction between execution concerns and task outcome.
6. **Treat Home as an entry into actual work.** Continue the user's saved review or open a meaningful pending view. Retain one clear primary action for the main task, without treating “one primary button per entire screen” as a universal design law.

## Next concept to review

A Runs workbench with saved views such as All runs, Awaiting review and Failed checks; a content-rich table; and an expandable inspector. “Start review” opens a focused queue over the current selection or filtered result. This is the revised recommendation derived from the benchmark, not a claim that all five products use an identical layout.

Compare this with the earlier mock-up on three tasks using the same data: find a particular input among 50 same-name traces, inspect a recorded failure and return to the same result position, and review the next ten items without losing context. Record completion time, wrong selections, backtracking and how readily users find the navigation. Retain 1/50/1,000-run, missing-metadata, long-name, empty/error, mobile and keyboard cases. Browser assertions can verify behavior; observed use must determine whether the design is easier.
