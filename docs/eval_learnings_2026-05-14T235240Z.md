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
