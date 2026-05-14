# Benchmark — browser-use-rs vs Python browser-use

Runs the same 10 tasks against both systems with the same model
(`gemini-3-flash-preview`). Each system runs in its own venv via
subprocess.

## Run

```bash
export GEMINI_API_KEY=...
.venv/bin/python bench/bench.py
```

Bench scripts also load missing values from a repo-root `.env` file.
Existing shell exports take precedence, and `.env` is git-ignored.

Writes `bench/results.json` and prints a markdown table.

## Rerun dashboard failures

To pull failed tasks from an evaluation dashboard run and print local
rerun commands sorted by cost:

```bash
export EVALUATION_TOOL_URL=...
export EVALUATION_TOOL_SECRET_KEY=...
python3 bench/export_failed_tasks.py <run_id> --top 20 --max-steps 100
```

The printed `bench/run_ours.py` commands still need `GEMINI_API_KEY` or
`GOOGLE_API_KEY` in the environment.

To aggregate all failed traces into tool/error/final-answer pattern
counts, including whether each task finalized on the requested target
host, on a search/fallback host, on an unrelated wrong host, or with
stale relative-date evidence. It also counts search/list finals whose
visible result titles do not overlap the requested query terms:

```bash
python3 bench/summarize_failed_run.py <run_id> --top 20
```

After running a release eval, compare it against the current baseline
run (`kh74n8rcqs8bestere2sjjqag186nb7q`) with:

```bash
python3 bench/compare_eval_run.py <new_run_id>
```

The comparison prints success-rate, cost, step, failure-count, and
action/access-error deltas as JSON. Use `--baseline <run_id>` to compare
against a different baseline.

Before committing a release candidate, run:

```bash
python3 bench/release_preflight.py
```

This read-only check reports secret-like values, unexpected untracked
files, missing helper/test files, and local-only files that should not
be staged.

To actually rerun the highest-cost failed tasks locally and write a
JSON result artifact:

```bash
export GEMINI_API_KEY=...  # or GOOGLE_API_KEY
.venv/bin/python bench/rerun_failed_tasks.py <run_id> --top 10 --system ours
```

Real rerun artifacts include a top-level `review` list with compact
task and answer previews plus per-system success, step, time, and cost
fields for quick manual accuracy review.

Use `--dry-run` to fetch the same failed tasks and write a planned rerun
artifact without requiring a model key or launching agents:

```bash
python3 bench/rerun_failed_tasks.py <run_id> --top 10 --system both --dry-run
```

Use repeated `--task-id` options to rerun specific dashboard failures:

```bash
.venv/bin/python bench/rerun_failed_tasks.py <run_id> \
  --task-id 507 --task-id 2347 --system ours
```

Use `--system both` to compare this package against `../browser-use`
when the upstream package is installed in the selected Python
environment.

## PostHog rollout preflight

Before running the neighboring `../custom-llm` PostHog rollout scripts,
run a read-only preflight:

```bash
python3 bench/posthog_preflight.py --json
```

This checks whether required credentials are present and surfaces risky
script defaults such as high rollout concurrency or dataset
`push_to_hub` calls without starting Browser Use API tasks. It also
reports import-time side effects and missing local Python dependencies
needed by the rollout scripts.

To inspect a local PostHog task sample without pushing anything:

```bash
.venv/bin/python -m pip install -e ".[posthog]"
.venv/bin/python bench/sample_posthog_tasks.py \
  --profile eval-friendly --limit 50 --out bench/posthog_sample_tasks.json
```

This writes only the local JSON path you provide. The
`--profile eval-friendly` option rejects very long prompts, generic
search-engine workflows, known non-public/test targets, high-friction
public hosts such as social or Google surfaces, and sensitive
payment/PII/cart/parcel-tracking flows before sampling a cheaper smoke
set. Use `--profile mirror` to keep the neighboring rollout script's
broader filter.

To turn that sample into local `ours` / `theirs` runner commands without
spending model tokens:

```bash
.venv/bin/python bench/run_task_file.py bench/posthog_sample_tasks.json \
  --system both --dry-run
```

Remove `--dry-run` after setting `GEMINI_API_KEY` or `GOOGLE_API_KEY` to
execute the sampled tasks and write a JSON cost/step artifact. Real
task-file runs also include the compact top-level `review` list.

## Caveats

- One sample per task. Real variance exists; pages change (HN top story
  varies hour to hour); rerun for stable numbers.
- Pricing constants in the runners use Gemini 2.5 Flash pricing as a
  proxy for `gemini-3-flash-preview` until Google publishes the official
  rate. Ratios between the two systems are unaffected.
- "completed" only means the agent returned without crashing within
  `max_steps`. Manually inspect answers in `results.json` for accuracy.
