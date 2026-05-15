# Rust Browser-Use Eval Learnings

Timestamp: 2026-05-14 16:52:40 PDT / 2026-05-14T23:52:40Z

This note captures the practical learnings from the Rust-backed
`browser-use-rs` optimization pass against the reference eval run:

- Reference run: `kh74n8rcqs8bestere2sjjqag186nb7q`
- Active candidate run at this timestamp: `kh774z293rn9qpnzgbvd7bfctn86p4a1`
- Candidate repo commit: `5821d639f8c4e8763c8692a5af9af4479895eb95`
- Upstream `browser-use` commit under test: `933e28c599ddd74c15a48568f159da95547e40dd`
- Model and judge model: `gemini-3-flash-preview`
- Test case: `WebBench_READ_v5`

Do not write eval secrets into repo files. Use env var names only:
`EVALUATION_TOOL_URL` and `EVALUATION_TOOL_SECRET_KEY`.

## Reference Run

The reference dashboard run exposes only limited top-level config:

- Run ID: `kh74n8rcqs8bestere2sjjqag186nb7q`
- Status: completed
- Total tasks: 198
- Successful tasks: 137
- Success rate: 69.1919%
- Failed tasks: 61
- Average cost: `$0.105864`
- Total cost: `$20.96114`
- Average steps: `20.520202`
- P90 steps: `44`
- Average duration: `88.539551s`
- Action errors: 95
- Access denied count: 31
- Git branch: `main`
- Git commit: `933e28c599ddd74c15a48568f159da95547e40dd`
- Model: `gemini-3-flash-preview`
- Dashboard eval group: empty string
- User message: `bu-rust 0.12.7 + WebBench_READ_v5`

Important: `/api/getRun` does not expose every worker flag. Hidden
flags such as headless mode, max actions per step, judge type, and
thinking level must be verified from GitHub worker logs when possible.

## Correct Candidate Config

The current candidate should be compared against the reference using:

- `runtime=rs`
- `browser_use_rs_ref=5821d639f8c4e8763c8692a5af9af4479895eb95`
- `ref=main` for upstream `browser-use`
- `llm_use_branch=main`
- `model=gemini-3-flash-preview`
- `eval_model=gemini-3-flash-preview`
- `test_case=WebBench_READ_v5`
- `total_tasks=198`
- `parallel_runs=1`
- `max_steps=100`
- `max_actions_per_step=4`
- `judge_repeat_count=1`
- `judge_type=ComprehensiveV1`
- `proxyless=true`
- `flash_mode=true`
- `browser=local`
- `images_per_step=1`
- `use_vision=true`
- `agent_type=Agent`
- `headless=false`
- `thinking_level=minimal`

User correction: max steps should be `100`, not `35`.

The completed headed worker log for run `kh774z293rn9qpnzgbvd7bfctn86p4a1`
verified this command shape:

```text
xvfb-run --auto-servernum --server-args=-screen 0 1920x1080x24 \
  python eval/service.py \
  --model gemini-3-flash-preview \
  --eval-model gemini-3-flash-preview \
  --parallel-runs 1 \
  --max-steps 100 \
  --max-actions-per-step 4 \
  --judge-repeat-count 1 \
  --test-case WebBench_READ_v5 \
  --proxyless \
  --judge-type ComprehensiveV1 \
  --thinking-level minimal \
  --flash-mode \
  --browser local \
  --images-per-step 1 \
  --run-id kh774z293rn9qpnzgbvd7bfctn86p4a1 \
  --agent-type Agent
```

GitHub workflow default caveat: when `eval_group` is passed as an empty
string, the worker command line can still show `--eval-group PRTests`.
Because the dashboard run was pre-created and all workers pass `--run-id`,
the dashboard run record remains `evalGroup=""`, matching the reference.

## Run Inventory

Reference:

- `kh74n8rcqs8bestere2sjjqag186nb7q`: completed reference run.

Smoke:

- `kh7f2cbe7kyfz1p0ex6zmnbqsn86p2cc`: one-task smoke on `ebe0e54`.
  It installed Rust and `browser-use-rs` successfully and completed the
  sample task successfully.

Abandoned or non-final runs:

- `kh702dawnp5e4cvsym3xrb8erx86qng9`: first full run on `ebe0e54`.
  It was accidentally cancelled after the max-step discussion. It did
  use `--max-steps 100`, but it did not include `--thinking-level minimal`.
  Treat it as unusable for final comparison.
- `kh780bhsetvr1zc995g0ap6sx586q1x3`: run on `5821d63` with
  `thinking_level=minimal`, but it accidentally used `--headless`.
  Access denials rose too quickly compared with the reference. Treat it
  as a browser-mode mismatch and do not use it for final comparison.

Final candidate:

- `kh774z293rn9qpnzgbvd7bfctn86p4a1`: headed/xvfb, `max_steps=100`,
  `thinking_level=minimal`, upstream commit `933e28c...`, Rust commit
  `5821d63...`.
- Snapshot at 2026-05-14T23:52:40Z: 174/198 completed, 124 successes,
  30 access denials, 10 action errors, 0 tool call failures.
- Final result at 2026-05-15T00:09Z: completed, 198/198 tasks, 143
  successes, 55 failed tasks, 34 access denials, 13 action errors, 0
  tool call failures.

## Final Eval Result

Final comparison command:

```bash
EVALUATION_TOOL_URL=... \
EVALUATION_TOOL_SECRET_KEY=... \
BROWSER_USE_RS_DISABLE_DOTENV=1 \
python3 bench/compare_eval_run.py \
  kh774z293rn9qpnzgbvd7bfctn86p4a1 \
  --baseline kh74n8rcqs8bestere2sjjqag186nb7q
```

Final fully cost-covered comparison:

| Metric | Reference `kh74...` | Candidate `kh774...` | Delta |
| --- | ---: | ---: | ---: |
| Successful tasks | 137 / 198 | 143 / 198 | +6 |
| Success rate | 69.1919% | 72.2222% | +3.03 pp |
| Failed tasks | 61 | 55 | -6 |
| Average cost | $0.105864 | $0.064083 | -$0.041781 |
| Total cost | $20.96114 | $12.688511 | -$8.272629 |
| Average cost ratio | 1.0000 | 0.6053 | -39.47% |
| Average steps | 20.520202 | 17.227273 | -3.292929 |
| P90 steps | 44 | 39 | -5 |
| Average duration | 88.539551s | 73.062986s | -15.476565s |
| Action errors | 95 | 13 | -82 |
| Access denied count | 31 | 34 | +3 |
| Cost coverage | 1.0 | 1.0 | 0 |

Failure category shift:

| Category | Reference | Candidate |
| --- | ---: | ---: |
| Incorrect Result | 39 | 9 |
| Give Up | 21 | 46 |
| Step Limit | 1 | 0 |

Interpretation:

- Accuracy improved by 6 tasks on the same 198-task benchmark.
- Cost dropped by about 39.5% per task and about $8.27 total.
- Steps and duration both improved.
- Action errors dropped sharply, from 95 to 13.
- Access denials rose slightly, from 31 to 34. Most remaining failures
  are hard access/CAPTCHA/search-fallback cases rather than tool crashes.
- Failure mix shifted from "Incorrect Result" to explicit "Give Up",
  which is usually better behavior for blocked or weakly evidenced tasks.

## Eval Platform Learnings

- Do not pass `developer_id="saurav"` or `developerId="saurav"` to
  `/api/startRun`. The API expects an internal Convex `users` id and
  rejects the literal string. Omitting `developerId` while authenticating
  with Saurav's developer key is the working path.
- Verify the developer key with the platform action before attributing
  runs. The key used in this session verified as `Saurav Panda
  <saurav@browser-use.com>`.
- `/api/getRun` is useful but incomplete. It exposes run-level fields
  like model, commit, task count, progress, success count, action errors,
  and access denials. It does not expose all worker flags.
- `gh run view --log` cannot read logs while a workflow is still running.
  Wait for at least one worker to complete, then inspect its emitted
  `eval/service.py` command.
- Partial `bench/compare_eval_run.py` output must be treated as
  directional only. While a run is active, incomplete tasks make
  `failedTasks` and `successRate` misleading.
- Cost and step metrics become useful earlier than success rate, but only
  trust final cost deltas when `costCoverage` reaches `1.0`.
- Access-denial counts are a strong canary for browser-mode mismatch.
  The accidental headless run accumulated more access denials than the
  reference too early; the headed/xvfb run tracked much closer.
- If a full run is cancelled midway, the dashboard run may remain
  `status="running"` with partial task counts. Do not use that run for
  final comparison.

## Implementation Learnings

The first committed patch was:

- `ebe0e544bbf600bafd0f12b2547188130b0cd6a8`
- Message: `Improve eval reliability and bench tooling`

Key improvements:

- Capped expensive or runaway tools:
  - `sleep` capped at 28 seconds.
  - Scroll bounded to 8 seconds.
  - `page_text` capped at 50k chars.
  - `get_links` capped and middle-ellipsized.
- Made scratchpad preview byte-safe and bounded.
- Added robust argument alias/drop behavior for common model-generated
  extra args.
- Improved tab handling and stale CDP recovery.
- Added bot/search-challenge detection to avoid repeated blocked search
  engine loops.
- Added final-answer guards for unsupported external evidence, wrong
  host, stale relative dates, query mismatch, late pagination, and item
  detail/list confusion.
- Added prompt metrics instrumentation for history/read-state size.
- Added benchmark tooling:
  - `bench/compare_eval_run.py`
  - `bench/summarize_failed_run.py`
  - `bench/export_failed_tasks.py`
  - `bench/rerun_failed_tasks.py`
  - `bench/release_preflight.py`
  - PostHog preflight/sampling helpers.

The second committed patch was:

- `5821d639f8c4e8763c8692a5af9af4479895eb95`
- Message: `Tighten result card extraction`

Reason:

- Early traces showed `extract_result_cards` sometimes returned broad
  page containers such as "320 Results" or filter controls as cards.
- Some cards contained multiple results in one blob, which let the model
  answer from noisy or badly ordered evidence.

Fix:

- Prefer real visible links/headings.
- Reject result-count, filter, sorting, cookie, sponsored, and navigation
  chrome titles.
- Avoid containers that are too broad or contain multiple significant
  result links.
- Grow from a link to the nearest useful small card instead of treating
  whole page/list wrappers as one card.
- Extract simple dates from visible card text when there is no `<time>`.

## Testing And Gates

Local tests passed before the first full release dispatch:

- `cargo test -p bu-cdp -p bu-dom -p bu-browser`
- `python3 -m unittest discover -s tests -q`
- `.venv/bin/python -m unittest discover -s tests -q`
- `python3 -m compileall -q python/browser_use_rs tests bench`
- `git diff --check`
- `python3 bench/release_preflight.py`

After the result-card extractor patch:

- `python3 -m unittest tests.test_result_card_tool -q`
- `python3 -m unittest discover -s tests -q`
- `python3 -m compileall -q python/browser_use_rs tests bench`
- `git diff --check`
- `python3 bench/release_preflight.py`

Known local-only file:

- `bench/monitor_runs.sh` remains untracked and local-only.

## Failure Pattern Learnings

Reference failures were heavily affected by site access:

- Baseline failed tasks: 61
- Common markers: CAPTCHA, Cloudflare, Google `/sorry`, access denied,
  tool timeout, and unsupported search-result fallback finals.

Candidate traces showed several classes:

- Hard access blocks: Qatar Airways, ScienceDirect, Rent.com, Nordstrom,
  NYTimes, StackExchange, LiveJournal, and some travel/product sites.
- Search fallback loops are expensive and often produce unsupported
  answers; better to detect blocks, try one alternate engine or same-site
  endpoint, then finish `success=false`.
- Some "incorrect result" examples are plausible answers rejected by the
  judge because evidence was weak, off-host, or not the first/top result.
- Result/list pages are high leverage because a zero-LLM card extractor
  can reduce cost and reduce bad evidence before the model reasons.

Mechanical issues to keep watching:

- A trace had `sleep()` called without `seconds`. Consider a default
  argument for `sleep(session, seconds=1.0)` if this repeats.
- A trace had `unknown tab target_id: 1`. Existing stale-tab recovery
  catches many cases, but tab target id normalization/recovery may need
  another pass if it repeats.

## Final Comparison Procedure

The final candidate has completed, but this procedure is still the right
way to reproduce or re-check the comparison:

```bash
EVALUATION_TOOL_URL=... \
EVALUATION_TOOL_SECRET_KEY=... \
BROWSER_USE_RS_DISABLE_DOTENV=1 \
python3 bench/compare_eval_run.py \
  kh774z293rn9qpnzgbvd7bfctn86p4a1 \
  --baseline kh74n8rcqs8bestere2sjjqag186nb7q
```

Trust future comparisons only when:

- Candidate `status` is completed.
- Candidate `tasksWithCost` is 198.
- Candidate `costCoverage` is 1.0.
- No GitHub worker failed unexpectedly.

Then inspect failures:

```bash
EVALUATION_TOOL_URL=... \
EVALUATION_TOOL_SECRET_KEY=... \
BROWSER_USE_RS_DISABLE_DOTENV=1 \
python3 bench/summarize_failed_run.py \
  kh774z293rn9qpnzgbvd7bfctn86p4a1 \
  --top 20
```

Compare against the reference on:

- Success rate and failed task count.
- Average cost and total cost.
- Average steps and P90 steps.
- Action errors and access denied count.
- Failure category mix.
- New unclassified incorrect-result examples.

## Open Items

- Inspect whether the remaining high-cost Give Up traces deserve another
  targeted patch. The biggest examples are Newegg Review Bytes, People
  Crime, Rent.com utility estimates, IMDb/movie budget, and a few
  search-result first-item tasks.
- Consider adding guards for final answers that remain on search engines
  or off-host pages but do not currently trigger the unsupported-evidence
  classifier.
- Consider a default argument for `sleep(session, seconds=1.0)` only if
  the missing-argument trace repeats.
- Local failed-task reruns remain blocked by missing local `.env`.
- PostHog dataset experimentation has tooling but still needs the
  relevant local credentials/env configured.

## 2026-05-15T00:53:04Z Update: Stronger Reference Run

A newer reference run changed the target:

- New reference run: `kh7b4qp4610am5s99j7e3bzy0d86rfwn`
- Previous candidate run: `kh774z293rn9qpnzgbvd7bfctn86p4a1`
- Upstream commit: `933e28c599ddd74c15a48568f159da95547e40dd`
- Test case: `WebBench_READ_v5`
- Model: `gemini-3-flash-preview`
- New reference user message: `main + python`

New reference summary:

| Metric | New reference `kh7b4...` |
| --- | ---: |
| Total tasks | 198 |
| Successful tasks | 144 |
| Success rate | 72.7273% |
| Failed tasks | 54 |
| Average cost | $0.035510 |
| Total cost | $6.107656 |
| Cost coverage | 172 / 198 |
| Average steps | 12.261628 |
| P90 steps | 22 |
| Average duration | 64.894574s |
| Action errors | 10 |
| Access denied count | 29 |

Comparison of the previous Rust candidate against this newer reference:

| Metric | New reference `kh7b4...` | Rust candidate `kh774...` | Candidate delta |
| --- | ---: | ---: | ---: |
| Successful tasks | 144 / 198 | 143 / 198 | -1 |
| Success rate | 72.7273% | 72.2222% | -0.51 pp |
| Failed tasks | 54 | 55 | +1 |
| Average cost | $0.035510 | $0.064083 | +$0.028573 |
| Average cost ratio | 1.0000 | 1.8046 | +80.46% |
| Average steps | 12.261628 | 17.227273 | +4.965645 |
| P90 steps | 22 | 39 | +17 |
| Average duration | 64.894574s | 73.062986s | +8.168412s |
| Action errors | 10 | 13 | +3 |
| Access denied count | 29 | 34 | +5 |

Interpretation:

- The prior candidate beats the older `kh74...` reference but does not
  beat the newer `kh7b4...` reference.
- A 20% improvement over the newer reference would mean roughly 173
  successful tasks if interpreted as relative success count, average cost
  at or below about `$0.028408`, average steps at or below about `9.81`,
  and average duration at or below about `51.92s`.
- The newer reference is already much cheaper and shorter than the older
  reference, so matching hidden config is now the first thing to verify
  before drawing conclusions from code behavior.

Worker log learning:

- `/api/getRun` still does not expose all important config.
- GitHub worker logs showed the new reference used `xvfb-run`, not
  headless browser mode.
- The new reference worker command used `--max-steps 100`.
- The new reference worker command used both `--no-thinking` and
  `--thinking-level minimal`.
- The new reference worker command used
  `--eval-model gpt-o4-mini`, not `gemini-3-flash-preview`.
- The new reference worker command used
  `--judge-type ComprehensiveV1`, `--max-actions-per-step 4`,
  `--judge-repeat-count 1`, `--browser local`, `--images-per-step 1`,
  and `--use-vision`.
- The new reference did not install `browser-use-rs`; it was an upstream
  Python run.

Config corrections to preserve:

- The developer attribution should be Saurav, not Alex.
- Do not pass literal `developer_id="saurav"` or
  `developerId="saurav"` to `/api/startRun`; the platform expects an
  internal Convex id.
- Use the developer key identity instead of hard-coding a readable
  developer name in API payloads.
- Keep the run configuration exactly aligned with the reference before
  comparing results.
- Use `max_steps=100`, not `35`.
- Use `thinking_level=minimal`.
- For a fair run against the newer reference, also set the workflow
  thinking flag to false so the worker emits `--no-thinking`.
- Use `eval_model=gpt-o4-mini` when matching the newer reference.

Failure-set learning:

- The old Rust candidate failed 24 tasks that the newer Python reference
  passed.
- The old Rust candidate passed 23 tasks that the newer Python reference
  failed.
- Both runs failed 31 tasks.
- This means the previous patch traded wins and regressions rather than
  strictly dominating the newer reference.

Fast next debugging targets:

- Focus first on candidate regressions where the Rust run failed quickly
  but the newer reference succeeded, because those are likely config,
  guard, or extraction issues rather than hard site blocks.
- High-cost candidate failures such as Rent.com and IMDb point to
  expensive Give Up loops; tightening blocked/search-loop exits could
  reduce cost and time, but must be checked against success regressions.
- Run a small eval with `eval_model=gpt-o4-mini`, `thinking=false`,
  `thinking_level=minimal`, headed/xvfb, and `max_steps=100` before
  paying for another full 198-task release.

## 2026-05-15T00:58:45Z Update: Rust Patch For New Reference

Two concrete code lessons came from comparing failed Rust tasks against
the newer Python reference:

- The Rust candidate sometimes treated prose like
  `Action: web_search(query=...)` as a final answer instead of executing
  the tool. Task `1582` failed after one step this way, while the
  reference recovered through external search/archive evidence.
- The eval worker's `--no-thinking --flash-mode` path is materially
  cheaper in upstream Python because it uses a terse fast prompt. The
  Rust agent previously accepted `use_thinking=False` but still used a
  verbose flash prompt with XML state emission.

Patch added after this finding:

- Plain-text finalization now detects pending tool-call prose and nudges
  the model to call the actual tool instead of committing the text as
  final.
- Added final-answer guard tests for pending tool-call prose.

Local verification:

- `python3 -m unittest tests.test_final_answer_guards -q`
- `python3 -m compileall -q python/browser_use_rs tests bench`
- `python3 -m unittest discover -s tests -q`
- `git diff --check`

## 2026-05-15T01:24:10Z Update: Ten-Task Slice Regression

Run `kh73sq1fsp5959p7xb980v1g9h86rsyr` tested commit `dbd7268` on a
10-task slice with the newer reference-style worker flags:

- `model=gemini-3-flash-preview`
- `eval_model=gpt-o4-mini`
- `max_steps=100`
- `thinking=false`
- `thinking_level=minimal`
- `flash_mode=true`
- headed/xvfb browser

Result:

- 10 tasks completed.
- 6 successful.
- 179 total steps.
- 598.589s total task duration.
- 2 access denials.
- 0 action errors.

The task IDs were:

- `1487`, `2397`, `1480`, `582`, `1426`, `2370`, `432`, `1371`,
  `2656`, `232`.

Important regression finding:

- The earlier Rust candidate `kh774z293rn9qpnzgbvd7bfctn86p4a1`
  succeeded all 10 of these task IDs.
- The shortened flash/no-thinking prompt in `dbd7268` caused early Give
  Up failures on `2397`, `1426`, `2370`, and `432`.
- Therefore the prompt-shortening part of `dbd7268` was not safe enough
  for release evaluation, even though it reduced verbosity.

Corrective action:

- Restore the previous grounded flash prompt.
- Keep the safer pending-tool-call finalization guard, because it
  targets a concrete one-step failure mode without removing the
  grounding/state instructions that helped the older Rust run.
- Do not launch a full 198-task run from `dbd7268`.

## 2026-05-15T01:46:05Z Update: Downloaded Files

Trace comparison for task `432` (EPA Los Angeles air quality) exposed a
real tool mismatch:

- The agent clicked the EPA data download link successfully.
- `list_downloads` returned a completed `ad_aqi_tracker_data.csv` with
  an absolute browser download path.
- `read_file` then returned `(no such file: ...)` because it only
  resolved files inside the agent notes sandbox.

Patch:

- `read_file` can now read completed files returned by
  `session.list_downloads()`.
- It also accepts the suggested download filename, such as
  `ad_aqi_tracker_data.csv`.
- General absolute filesystem reads remain blocked; absolute paths are
  allowed only if they are a completed download from the current browser
  session or live under the session's download directory.

Local verification:

- `python3 -m unittest tests.test_download_file_tools -q`
- `python3 -m unittest discover -s tests -q`
- `python3 -m compileall -q python/browser_use_rs tests bench`
- `git diff --check`

## 2026-05-15T01:50:01Z Update: Current-Head Smoke

Run `kh7d5jvzc69n1jtk8ahc7ewryh86r4hp` verified current head
`1119d3df39397223bc60e2ef23e88180ae009abf` installs and runs on the
eval worker.

Worker command was reference-aligned for the tested knobs:

- headed/xvfb browser
- `model=gemini-3-flash-preview`
- `eval_model=gpt-o4-mini`
- `max_steps=100`
- `max_actions_per_step=4`
- `judge_repeat_count=1`
- `judge_type=ComprehensiveV1`
- `thinking=false`
- `thinking_level=minimal`
- `flash_mode=true`
- `browser=local`
- `images_per_step=1`
- `use_vision=true`

Smoke result:

- 1/1 task successful.
- 9 steps.
- 26.077s task duration.
- 0 action errors.
- 0 access denials.

This smoke proves install/config health only. It does not prove the
20% improvement target against the full `kh7b4...` reference.

## 2026-05-15T02:01:38Z Update: EPA Slot And New Fixes

Run `kh7f4fkdnc4k6tbt1c8h4q8vys86safm` targeted the EPA slot
(`start_index=3`, `end_index=4`) at commit
`af236dd59ca23323bf8c7f5a400910eb8d48c429`.

Worker command was reference-aligned for this targeted run:

- `model=gemini-3-flash-preview`
- `eval_model=gpt-o4-mini`
- `max_steps=100`
- `thinking=false`
- `thinking_level=minimal`
- `flash_mode=true`
- headed/xvfb local browser
- `images_per_step=1`
- `use_vision=true`

Result:

- Task `432` judged successful.
- 48 steps.
- 160.277s task duration.
- $0.168670 total task cost.
- 0 action errors.
- 0 access denials.

Important correction:

- This did **not** prove the completed-download `read_file` fix worked.
- The trace still showed `read_file` failing on the absolute path
  returned by `list_downloads`:
  `/tmp/.../downloads/<download-guid>`.
- The judge passed because the agent inferred the final AQI value from
  chart evidence after the file-read attempt failed.

Root cause:

- Chrome can report downloads as `download_dir/<guid>` while the actual
  file lands under the suggested filename, such as
  `download_dir/ad_aqi_tracker_data.csv`.
- `read_file` previously trusted the reported GUID path and did not map
  it to the suggested filename path.

Patch:

- `read_file` now treats a completed download's reported GUID path,
  GUID basename, and suggested filename as aliases.
- When a completed download is requested, it checks both the reported
  path and `download_dir/<suggested filename>`.
- Added a regression test where `list_downloads` reports a GUID path but
  the actual file exists only as `ad_aqi_tracker_data.csv`.

Additional trace-driven patch:

- Bloomberg task `2397` regressed because Gemini repeatedly tried JS
  consent-button clicks for `"Yes, I Accept"` even though every attempt
  returned `Button not found`.
- Passing traces navigated directly to
  `https://www.bloomberg.com/opinion` and then used extraction behind
  the overlay.
- Added a consent-overlay loop nudge: after repeated failed
  cookie/privacy button attempts, tell the agent to stop clicking
  consent controls and navigate directly to the requested same-site
  section/page.
- Added tests for detecting failed consent-button attempts and
  suggesting `https://www.bloomberg.com/opinion` for an Opinion-section
  Bloomberg task.

Platform note:

- The failed GitHub eval workflows created around
  `2026-05-15T01:55:17Z` were not Rust runs; they used
  `--model custom` on `poweruserCloud_v2` and failed before task
  execution because `custom` is not a supported eval model name.

## 2026-05-15T02:26:45Z Update: Targeted Retest And First-10 Slice

Commit `3cedc6db00fb52c3a8830f44f8ed8ecd55ce09e7` was pushed with:

- completed-download GUID/suggested-filename aliasing in `read_file`
- the consent-overlay loop nudge
- tests for both behaviors

Targeted EPA retest:

- Run: `kh759d7z7hfmksy28xqnjpryc986r917`
- GitHub workflow: `25896256350`
- Task: `432`
- Result: success
- Steps: 44
- Duration: 150.397s
- Cost: $0.140475
- Important trace proof: `read_file` on the absolute reported download
  GUID path succeeded and returned `ad_aqi_tracker_data.csv` content.
- This fixed the earlier `af236dd` behavior where the same absolute
  completed-download path returned `(no such file: ...)`.

Targeted Bloomberg retest:

- Run: `kh7fgpqpj9tpvg5xxeqmcfx2sx86rvrv`
- GitHub workflow: `25896258700`
- Task: `2397`
- Result: success
- Steps: 9
- Duration: 34.904s
- Cost: $0.029256
- Trace behavior: navigated directly to
  `https://www.bloomberg.com/opinion`, then used
  `extract_structured_data` for the three Opinion article/title pairs.
- The consent-loop nudge did not need to fire in this sampled run; this
  retest proves the current prompt+patch combination can return to the
  passing direct-navigation pattern and that the patch did not harm this
  task.

Dashboard anomaly:

- The pre-created one-task runs show an extra stale/null CDC task row in
  `getRunResults`, causing `completedTasks=2` and `progress=200`.
- GitHub logs confirm only the intended task executed in each one-task
  workflow.
- For pre-created targeted runs, use GitHub task logs and rows with
  non-null `steps` when interpreting results.

First-10 slice:

- Run: `kh7fv7y64tyy7tjpwm4ksr9b9586s3cq`
- GitHub workflow: `25896560029`
- Config matched the reference-style flags:
  `gemini-3-flash-preview`, `eval_model=gpt-o4-mini`, `max_steps=100`,
  `max_actions_per_step=4`, `thinking=false`,
  `thinking_level=minimal`, `flash_mode=true`, headed/xvfb,
  `browser=local`, `images_per_step=1`, `use_vision=true`.
- Canceled after 5 executed tasks because the slice was clearly not a
  release candidate.
- Dashboard finalization after cancellation marked 10 completed / 2
  successful, but the unstarted tasks have null steps and should not be
  interpreted as executed failures.

Executed subset before cancellation:

- `232` CDC flu prevention: success, 13 steps, 44.013s.
- `1371` PlayStation Horizon Forbidden West: success, 9 steps, 33.964s.
- `2370` BBC Good Food Paleo Pancakes: failed/Give Up, 14 steps,
  40.990s, persistent consent overlay.
- `432` EPA air quality: failed/Give Up, 26 steps, 93.419s; this run
  drifted to AirNow evidence instead of the AQS CSV path used by the
  targeted retest.
- `2656` Southwest flight deals: failed/Incorrect Result, 42 steps,
  170.996s; produced one-way flight offers when the task asked for
  round-trip deals.

Slice-level conclusion:

- The isolated EPA and Bloomberg fixes are real, but the first-10 slice
  is still not close to the newer Python reference.
- The canceled partial slice had 2 successes across 5 executed tasks,
  104 total steps, 383.382s, and $0.343630 before cancellation.
- Do not launch a full 198-task release from `3cedc6d`.
- The next highest-value work is targeted, not broad: prevent EPA from
  drifting to AirNow when the task asks for AQS, and address Southwest's
  one-way-vs-round-trip interpretation before another slice.

## 2026-05-15T02:30:21Z Update: Source-Mismatch Guards

Trace review of the canceled first-10 slice found two reusable
finalization/source failures:

- Task `432` asks for EPA's Air Quality System/AQS page, but the agent
  drifted to `airnow.gov`, briefly visited `/aqs`, then returned to
  AirNow and finalized an AirNow AQI answer. AirNow is related EPA
  content, but it is not the requested AQS evidence source.
- Task `2656` asks for current round-trip flight deals, but the agent
  finalized one-way offers and rationalized them as round-trip
  equivalents when booked as two one-way segments.

Patch:

- Added an AQS source-mismatch nudge. When an EPA AQS task lands on
  `airnow.gov`, the agent is told that AirNow does not satisfy the task
  and is pointed back to the EPA Daily Air Quality Tracker path.
- Added a final-answer guard so AQS tasks cannot self-report success
  from AirNow text or an AirNow final URL.
- Added a final-answer guard so round-trip tasks cannot self-report
  success with one-way-only deal answers unless they include return
  evidence and a round-trip total.

Local verification:

- `python3 -m unittest tests.test_final_answer_guards -q`
- `python3 -m unittest discover -s tests -q`
- `python3 -m compileall -q python/browser_use_rs tests bench`
- `git diff --check`
- `BROWSER_USE_RS_DISABLE_DOTENV=1 python3 bench/release_preflight.py`

## 2026-05-15T02:51:20Z Update: `faa02b4` Southwest Retest

Commit `faa02b4c371ed67b248ff8769673cb49078ec062` was pushed with the
site technical-error detector and retested on Southwest task `2656`.

Run details:

- Run: `kh766j11m8xdb07pktja3p3x1n86sqnc`
- GitHub workflow: `25897456513`
- Task: `2656`
- Installed Rust ref:
  `faa02b4c371ed67b248ff8769673cb49078ec062`
- Worker config: headed/xvfb, `gemini-3-flash-preview`,
  `eval_model=gpt-o4-mini`, `max_steps=100`,
  `max_actions_per_step=4`, `judge_repeat_count=1`,
  `test_case=WebBench_READ_v5`, `judge_type=ComprehensiveV1`,
  `--no-thinking`, `thinking_level=minimal`, `flash_mode=true`,
  `browser=local`, `images_per_step=1`, `use_vision=true`,
  `agent_type=Agent`.
- Result: judge success
- Steps: 20
- Duration: 64.461s
- Cost: $0.075149
- Action errors: 0
- Access denials: 0

Comparison:

- Previous Rust Southwest retest `kh79pcrec07c66kyg569fkcav586sqtr`:
  failed / Give Up, 71 steps, 329.293s, $0.401476.
- Stronger Python reference `kh7b4qp4610am5s99j7e3bzy0d86rfwn` on the
  same task: failed / Incorrect Result, 52 steps, 242.801s.
- The latest Rust retest is materially better than both on this single
  task, but it is only one sampled run.

Trace learning:

- The technical-error cutoff did not fire in this retest. There was no
  repeated Low Fare Calendar modal loop like the previous Rust trace.
- The agent followed a shorter path through the sale/current-specials
  pages, then used `extract_structured_data` on hidden or below-fold
  deal content.
- It first found two explicit-looking round-trip offers from Albany to
  Atlanta and Baltimore/Washington for June 4-7, 2026, but final-answer
  validation continued the run.
- It then extracted visible one-way starting fares and finalized two
  round-trip totals by doubling one-way starts:
  - Albany to Chicago (Midway), departing June 9, 2026:
    `$153` one-way -> `$306` round-trip.
  - Albany to Nashville, departing June 9, 2026:
    `$160` one-way -> `$320` round-trip.
- The external judge accepted this as satisfying the task, with the
  judgement that the agent identified two deals, calculated round-trip
  prices from listed one-way starts, and provided travel dates.

Guard learning:

- The Rust final-answer guard remained more conservative than the judge:
  `selfReportSuccess=false` because the final answer still mentioned
  one-way-derived evidence.
- For eval scoring, this is acceptable because the external judge marked
  success, but it shows the round-trip guard may be stricter than
  necessary for Southwest-style "one-way starting at" fare pages.
- Do not loosen the guard yet from a single accepted sample; the previous
  reference and Rust failures show this task can also produce fabricated
  unsupported round-trip totals.

Current release signal:

- Targeted evidence now validates improvements on EPA task `432` and
  Southwest task `2656`.
- This still does not justify a full 198-task release by itself. The
  canceled first-10 slice also had the BBC Good Food consent failure and
  only five executed tasks. A fresh small slice on `faa02b4` is the next
  reasonable gate before considering a broad release run.

This is still not a release signal. These guards reduce two observed
wrong-answer patterns, but they need targeted eval before another slice.

## 2026-05-15T02:39:48Z Update: Guard Retests On `ab49888`

Commit `ab49888259fd48694c5849c64690017c319b4a8f` was pushed with:

- an EPA AQS source-mismatch nudge away from `airnow.gov`
- a final-answer guard against AQS answers sourced from AirNow
- a final-answer guard against one-way-only answers for round-trip
  flight tasks
- tests covering the new guard patterns

Reference-aligned config was confirmed from the GitHub worker command:

- headed/xvfb local browser
- `model=gemini-3-flash-preview`
- `eval_model=gpt-o4-mini`
- `max_steps=100`
- `max_actions_per_step=4`
- `judge_repeat_count=1`
- `test_case=WebBench_READ_v5`
- `judge_type=ComprehensiveV1`
- `thinking=false`
- `thinking_level=minimal`
- `flash_mode=true`
- `images_per_step=1`
- `use_vision=true`
- `agent_type=Agent`

Launch mistake:

- Two first retest workflows failed before execution because the
  dispatched `browser_use_rs_ref` had a mistyped SHA:
  `ab498884681da4db46037d06fcf75893e688974e`.
- The correct SHA is
  `ab49888259fd48694c5849c64690017c319b4a8f`.
- Treat those failed launch workflows as configuration noise only; they
  did not execute browser tasks.

EPA AQS retest:

- Run: `kh74smn8nnmvtbpmgh013fyvnh86rq7n`
- GitHub workflow: `25897090670`
- Task: `432`
- Installed Rust ref:
  `ab49888259fd48694c5849c64690017c319b4a8f`
- Result: success
- Steps: 27
- Duration: 125.398s
- Cost: $0.113143
- Action errors: 1
- Access denials: 0

Trace proof:

- The agent initially drifted from EPA search to
  `https://www.airnow.gov/state/?name=california`.
- The new AQS source-mismatch nudge fired at step 13.
- The agent navigated back to the EPA Daily Air Quality Tracker,
  selected combined Ozone/PM2.5, year 2026, and
  Los Angeles-Long Beach-Anaheim, CA.
- It downloaded the tracker CSV, `read_file` succeeded on the completed
  download alias, and the final answer used the AQS tracker data instead
  of AirNow.
- The judge passed the answer: AQI `64` for May 13, 2026, with May 14
  and May 15 values still pending in the downloaded tracker data.

Dashboard anomaly repeated:

- The pre-created one-task EPA run again showed an extra stale/null CDC
  row in `getRunResults`, making the run-level counters report
  `completedTasks=2` and `progress=200` even though the GitHub workflow
  executed only task `432`.
- For these targeted pre-created runs, trust GitHub task logs and result
  rows with non-null step counts over aggregate dashboard counters.

Southwest round-trip retest:

- Run: `kh79pcrec07c66kyg569fkcav586sqtr`
- GitHub workflow: `25897092830`
- Task: `2656`
- Installed Rust ref:
  `ab49888259fd48694c5849c64690017c319b4a8f`
- Result: failed / Give Up
- Steps: 71
- Duration: 329.293s
- Cost: $0.401476
- Action errors: 0
- Access denials: 0

Trace learning:

- The agent found the Southwest "Flight Deals" page and extracted visible
  offers, but the visible offers were one-way starting fares.
- It correctly recognized that the task asks for round-trip offers and
  tried to use the booking widget and Low Fare Calendar to confirm return
  dates and pricing.
- Southwest repeatedly displayed a generic technical-error modal:
  "Sorry, we found some errors... We are unable to process your request."
- After repeated calendar failures, the agent returned to the one-way
  deal list and attempted a final answer with two one-way offers.
- The new final-answer guard prevented this from being treated as a
  supported successful answer: `selfReportSuccess=false`.
- The judge rejected the final answer because it did not provide
  round-trip deals with return dates or date ranges.

Southwest conclusion:

- The guard fixed the previous false-success behavior, but it did not
  solve the task.
- The remaining issue is expensive retry behavior on a repeatedly
  failing Southwest booking/calendar flow.
- A future patch should either find a different Southwest source/path for
  round-trip deal date ranges or give up earlier after repeated
  Southwest technical-error modals. The latter would reduce cost and
  time but would not recover the success.

Current conclusion:

- The EPA source-mismatch guard is validated by a targeted passing trace
  and materially improved that task from the canceled first-10 slice.
- The Southwest round-trip guard is validated only as a correctness
  guard: it blocked the unsupported one-way answer from becoming a
  self-reported success, but the task still failed and was expensive.
- This is still not enough evidence for another full 198-task release.
  Another small slice would be premature until Southwest's repeated
  calendar-error loop is shortened or a successful route is found.

## 2026-05-15T02:44:01Z Update: Southwest Reference Comparison

The stronger Python reference run `kh7b4qp4610am5s99j7e3bzy0d86rfwn`
also failed Southwest task `2656`:

- Result: failed / Incorrect Result
- Steps: 52
- Duration: 242.801s
- Final answer claimed two round-trip deals:
  - LAX to LAS, July 1-3, 2026, total $58.40
  - LAX to SFO, July 1-24, 2026, total $171.80
- Judge rejection: the first claimed round-trip deal was likely
  fabricated or unsupported, the agent had not finished selecting the
  return flight, and the path drifted into manual searches rather than
  a supported Special Offers result.

Comparison against the Rust `ab49888` Southwest retest:

- Rust did worse on cost and time: 71 steps / 329.293s / $0.401476.
- Both runs failed the task.
- The Rust guard improved correctness posture by setting
  `selfReportSuccess=false` for one-way-only evidence, whereas the
  Python reference self-reported success for an unsupported round-trip
  answer that the judge rejected.
- Since the reference also fails this task, this is not currently a
  reference-passed success recovery target. It is a cost/time target.

Patch after this comparison:

- Treat repeated site-level technical-error states as blocked/error
  states in the existing blocked-state detector.
- Added phrases for Southwest-style errors:
  "Sorry, we found some errors" and
  "We are unable to process your request".
- Updated the existing blocked-state nudge text from
  bot/CAPTCHA/challenge to bot/CAPTCHA/error/challenge.
- Added a unit test for a Southwest booking-page technical-error modal.

Expected behavior:

- The first repeated error states should trigger the existing
  `[BOT_BLOCKED]` nudge.
- If the page keeps returning the same technical-error modal, the
  existing force-final path should stop the run earlier with
  `success=false`.
- This should reduce expensive tails on tasks that are blocked by site
  technical errors. It does not by itself recover a successful Southwest
  answer.

Local verification:

- `python3 -m unittest tests.test_final_answer_guards -q`
- `python3 -m unittest discover -s tests -q`
- `python3 -m compileall -q python/browser_use_rs tests bench`
- `git diff --check`
- `BROWSER_USE_RS_DISABLE_DOTENV=1 python3 bench/release_preflight.py`
