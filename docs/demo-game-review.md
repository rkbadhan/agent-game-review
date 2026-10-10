# Demo review handoff

> The pass rate says your agent lost. The game review shows how it lost.

The implementation is limited to the four demo fixes. The source trajectories and
individual verifier reports were matched by their captured messages, outputs, and
commands, including the legacy exporter’s 2,000-character command cutoff. All 15
Terminal-Bench demo runs now carry their original individual test results and
bounded verifier logs. Updated source hashes and baked review validations are
recomputed; the model reviewer is not called.

## The two stories

- **polyglot — agent regression.** Step 22 records matching C and Python outputs
  for the tested values through F(93). Step 25 rewrites the C implementation. The
  final verifier reports F(42) as `267914296` in Python and `6` in C. The negative
  card anchors the rewrite at 25; the positive card anchors the earlier matching
  outputs at 22. This establishes the recorded self-check, not correctness for
  every possible input. The full rewrite command is restored from the original
  trajectory; its old export cutoff is not treated as an agent mistake.
- **retail-29 — action window closed.** Step 26 requests confirmation. Step 27
  approves the two exchanges and includes `###STOP###`. No later agent turn is
  recorded. The Overview and Runs table explain this termination without
  inventing an agent omission or claiming all earlier actions were correct.

## Other demo fixes

- **nginx:** 7 of 8 tests passed; `test_log_file_format` failed. Header, sidebar,
  Overview, and Runs use the individual test tally. The aggregate task reward is
  retained as authoritative evidence and is not counted as an extra test.
- **mips:** the initialization banner is described as progress, not a verified
  working VM. The final verifier failed all three VM/frame tests.
- The evidence panel shows exact supporting excerpts and links the quoted source
  events. The strongest-behaviour teaser uses the specific finding headline.
- Human-curated cards show **Curated review · 2026-10-10** in the header,
  Overview, and Key moments. The correction reason is visible on Overview.
  Historical model provenance stays in the metadata, including through bake/rebuild.
- All five failed customer-ended confirmation runs lead with their conversation
  ending, including retail-35 and retail-76 with earlier strengths. Earlier
  findings remain accessible in Key moments.
- The header and Runs table display **GLM-5.3 Flash** for the captured
  `stealth/ox-alpha` model, including its `openai/` and `openrouter/` routing
  prefixes. The original ID remains in source data, API responses, and tooltips.

## Review and preview

The changes are on `codex/demo-game-review`, rebased onto `main` after the
protocol-validation changes merged. No merge or publication is performed.
The working preview uses a fresh, ignored `.agr-demo/game-review` store and runs
read-only at port 8765.

- [Polyglot](http://127.0.0.1:8765/?run=harbor__terminal-bench%2Fpolyglot-c-py__6c3b4b8b-991)
- [Retail-29](http://127.0.0.1:8765/?run=harbor__sierra-research%2Ftau3-bench__tau3-retail-29__2f510d52-4453-4982-95f1-b2f0e3829164)
- [Nginx](http://127.0.0.1:8765/?run=harbor__terminal-bench%2Fnginx-request-logging__4e941d41-a66)

Validation includes offline demo rebuild/replay, the harness-protocol gates,
outcome/read-model tests, Harbor ingestion tests, and real Chromium interactions
for the cited steps, termination headline, supporting excerpts, and test tallies.
QA screenshots are in `.agr-demo/game-review/screens/`.

After review, merge and publish. Record the 40-second MP4 from that published
product, then derive the GIF and stills. Recording is not substituted with a
mockup or performed before publication.
