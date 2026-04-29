# Benchmark — browser-use-rs vs Python browser-use

Runs the same 10 tasks against both systems with the same model
(`gemini-3-flash-preview`). Each system runs in its own venv via
subprocess.

## Run

```bash
export GEMINI_API_KEY=...
.venv/bin/python bench/bench.py
```

Writes `bench/results.json` and prints a markdown table.

## Caveats

- One sample per task. Real variance exists; pages change (HN top story
  varies hour to hour); rerun for stable numbers.
- Pricing constants in the runners use Gemini 2.5 Flash pricing as a
  proxy for `gemini-3-flash-preview` until Google publishes the official
  rate. Ratios between the two systems are unaffected.
- "completed" only means the agent returned without crashing within
  `max_steps`. Manually inspect answers in `results.json` for accuracy.
