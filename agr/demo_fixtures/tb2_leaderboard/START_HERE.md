# Start here

After installing AGR, run `agr demo` and open [Home](http://127.0.0.1:8000/?view=home).

The same four links appear on Home. No additional downloads or API keys are needed.

1. **[One failed run: polyglot-c-py](http://127.0.0.1:8000/?run=harbor__terminal-bench%2Fpolyglot-c-py__6c3b4b8b-991&chapter=moments)** — Inspect the successful smoke tests at step 22, the later edit at step 25, and the final Checks output showing C=6.

2. **[A customer ends the chat: retail-29](http://127.0.0.1:8000/?run=harbor__sierra-research%2Ftau3-bench__tau3-retail-29__2f510d52-4453-4982-95f1-b2f0e3829164&chapter=outcome)** — The customer said yes and ended the chat. Inspect the recorded stop and the review's evidence limits before attributing a failure to the agent.

3. **[Patterns: a shared test failure](http://127.0.0.1:8000/?view=fleet&pattern_type=tests&test_group_by=task%2Ctest%2Csignature)** — Find filter-js-from-html: the same test failure appears across all seven configurations. Shared failures are a reason to investigate, not proof of who caused them.

4. **[Compare: GLM-4.7 to GLM-5](http://127.0.0.1:8000/?view=versions&baseline=Terminus2__GLM-4.7&candidate=Terminus2__GLM-5&axis=model)** — On these ten selected tasks, Terminus 2 improves from 19/50 to 28/50 passed attempts (38% to 56%). These are sample results, not full-leaderboard scores.

Now try it on your own runs: `agr ingest-harbor <your-job-folder>`.
