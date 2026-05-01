# browser-use-rs eval methodology + parity backlog

Living doc tracking how we evaluate vs upstream `browser_use`, what we've
shipped in the v0.5.x → v0.8.x arc, what regressed, and what's still on
the parity backlog. Updated as runs land.

## Current shipping state (2026-05-01)

- **Latest stable**: `v0.8.1` (clean v0.7.3 baseline; expected 64% judge / $0.131 cost).
- **Upstream**: 73% judge / $0.067 cost on WebBench_READ_v5 with gemini-3-flash-preview.
- **Gap**: −9pp judge, ~2× cost.

## How we measure

- **Eval**: WebBench_READ_v5 (198 tasks) on the dashboard at https://browser-use.tools.
- **Judge**: ComprehensiveV1 in `evaluations-internal/eval/judges/comprehensive_judge.py`. Both runtimes hit the SAME judge code path — judge reads the saved Convex trace post-completion.
- **Model**: gemini-3-flash-preview for both agent + judge.
- **Concurrency**: 25 parallel.
- **Cost per task**: average price field on the dashboard run.

The judge is identical for both runtimes — confirmed by reading the workflow (`evaluations-internal/.github/workflows/eval.yaml:242` installs both `browser_use` and `browser_use_rs`; the judge imports `browser_use.llm` only for its own LLM, not the agent's).

## Trajectory

| Version | Judge | Cost | Notes |
|---|---|---|---|
| v0.4.14 | 52% | $0.035 | starting point of current arc |
| **v0.4.15** | **59%** | $0.067 | early peak — `self_validate=True` |
| v0.4.16 | 53% | $0.041 | scratchpad + skip-validate-short |
| v0.4.17 | 54% | $0.069 | batch-skip mutation guard |
| v0.4.19 | 45% | $0.038 | `self_validate=False` flipped (regression) |
| v0.5.0 | 47% | $0.043 | history collapse + selectors |
| v0.5.1 | 53% | $0.053 | less aggressive collapse |
| v0.5.2 | 52% | $0.064 | stale-element fix |
| v0.5.3 | 55% | $0.062 | self-validate revert + prompt |
| v0.5.4 | 47% | $0.089 | prev screenshot + page-stats hint (regression — reverted) |
| v0.5.6 | 53% | $0.063 | selector retargeting only |
| v0.5.8 | 52% | $0.060 | narrowed static text |
| v0.6.0 | 56% | $0.084 | extract_structured_data + 13 tools |
| v0.6.1 | 55% | $0.092 | reverted static text (regression — restored) |
| v0.6.2 | 57% | $0.091 | parity fixes (max_actions, tool aliases) |
| v0.6.4 | 58% | $0.091 | DOM enrichment + restored static text |
| v0.6.5 | 60% | $0.099 | 2-pass validation + 14 more aliases |
| v0.7.0 | 62% | $0.121 | shadow DOM + tree hierarchy + extract upgrades |
| v0.7.1 | 63% | $0.130 | flash prompt + markdown extract + 4 more |
| v0.7.2 | broken | — | import error (`_alias` shadowed) |
| **v0.7.3** | **64%** | $0.131 | **current peak** — codex parity fixes |
| v0.7.4 | 60% | $0.135 | added long browser_state + browser_rules + task_completion (regression) |
| v0.7.5 | 54% | $0.076 | added forced reasoning XML mandate (regression but cheap) |
| v0.8.0 | 52% | $0.086 | persistent memory state on top of v0.7.5 (regression) |
| **v0.8.1** | TBD | TBD | clean v0.7.3 baseline (revert v0.7.4/5/v0.8.0 prompt) |

## Things I called regressions that may have been miscalled

The bundling habit hid signals. These deserve isolated re-testing:

| Version | What I called it | What it actually showed | Worth re-testing? |
|---|---|---|---|
| v0.6.5 | "no improvement, just costly" | Actually +2pp judge (58→60). I conflated cost rise with no judge gain. | Yes — the 2-pass validation alone may be a net positive. |
| v0.7.5 | "−10pp regression" | True for judge; cost crashed 42% to $0.076. **Cost-per-judge-point: 71 vs v0.7.3's 49** — best efficiency in the arc. | Yes — could be the right shipping config for cost-sensitive uses. |
| v0.7.4 components | "all bad as a bundle" | Never tested individually. The marker explanation alone might help. | Yes — split into 3 isolated tests. |
| v0.5.4 components | "−9pp" | Two changes bundled. Neither tested alone. | Maybe one component alone helps. |
| v0.5.7 (static text) | "−2pp" | Then v0.6.0 inverted: static text + extract = +3pp synergy. Component wasn't bad — wrong context. | Pattern lesson: components depend on context. |

## Methodology going forward (one change per version)

After three consecutive bundled regressions (v0.7.4, v0.7.5, v0.8.0), shifting to **one change per version** so we know which knob moves which metric. Each release commit message will note:

```
delta vs v0.8.1: <one sentence>
expected impact: judge ?pp, cost ?$, steps ?
```

## Release rule

**No v0.9.0 until judge parity with upstream (≈73%).** Everything stays in v0.8.x. v0.9.0 is the parity-hit signal — both for us and for downstream consumers. Until then every iteration is single-change on the v0.8.1 baseline.

## v0.8.x planned roadmap

Each isolated on the v0.8.1 baseline. Order chosen to do cheap-and-safe cost wins first, then re-test the components we may have miscalled, then graduate to bigger structural items only after we've isolated all small wins.

### Phase 1 — cost optimization (low risk, may cut $0.04-0.06)

| Version | Single change | Expected |
|---|---|---|
| v0.8.2 | history_window_steps default 6 → 3 | cost ↓$0.02-$0.04, judge ±1pp |
| v0.8.3 | max_consecutive_errors 5 → 3 | cost ↓$0.005, judge ±0pp |
| v0.8.4 | prompt nudge "batch 2-3 safe actions per turn" | steps ↓ → cost ↓$0.02 |
| v0.8.5 | cap extract_structured_data at 2 calls/task (hard) | cost ↓$0.005 |

### Phase 2 — re-test miscalled regressions in isolation

| Version | Single change | Reason |
|---|---|---|
| v0.8.6 | persistent memory/next_goal state ALONE | v0.8.0 was bundled; clean test on baseline |
| v0.8.7 | `<browser_state>` markers explanation ALONE | v0.7.4 bundled three blocks |
| v0.8.8 | `<task_completion>` checklist ALONE | same |
| v0.8.9 | reasoning XML mandate ALONE (re-test) | v0.7.5 cost win was real, judge re-test on baseline |
| v0.8.10 | 2-pass validation prompt re-introduced ALONE | v0.6.5 was undercalled |

### Phase 3 — codex parity items, judge-leverage ranked

Each as a single change, only after Phase 1+2 land. Most won't move judge but build production parity.

| Version | Codex item | Why this order |
|---|---|---|
| v0.8.11 | constructor kwargs honored (`step_timeout`, `llm_timeout`, `vision_detail_level`) | small, may matter if eval passes them |
| v0.8.12 | `include_attributes` kwarg honored — filters which DOM attrs we emit | could trim snapshot tokens |
| v0.8.13 | `fallback_llm` rate-limit failover | helps tasks failing on transient model errors |
| v0.8.14 | `available_file_paths` parity (real upstream FileSystem state) | already partial; close out |
| v0.8.15 | `output_model_schema` direct task-enhancement path | structured output without controller dance |
| v0.8.16 | typed `AgentOutput`/`AgentBrain` shape | parity for downstream code reading our trace |
| v0.8.17 | upstream MessageManager core (recent_events, sample_images, screenshot resize) | medium refactor, may help judge if eval relies on these signals |
| v0.8.18 | real planner (`PlanItem`, `enable_planning`, `planning_replan_on_stall`) | bigger lift; may close planning-side judge gap |
| v0.8.19 | full upstream judge construction in `_judge_and_log` | parity but eval judge is identical so low judge-score impact |
| v0.8.20+ | lifecycle API (pause/resume/stop), telemetry/event bus, artifacts (save_conversation_path, GIF), full AgentHistoryList | infra parity, post-judge-parity hardening |

### Phase 4 — final push to ≥73% if Phases 1-3 don't get us there

If we plateau in Phase 3, options that were deferred earlier:

- **Real upstream prompt template port + JSON output paradigm** (~1 week). Biggest lift but closest to upstream's behavioral shape.
- **Cheaper extraction LLM** (route extract_structured_data to claude-haiku/gemini-2-flash). Cost lever, may free budget for more aggressive agent loops.
- **Per-model prompt tuning** — gemini-3-flash-preview may need different language than the generic upstream prompt.

Re-tests above are independently informative regardless of result.

## Codex's outstanding parity items (12)

Triaged by judge-leverage and effort. Most are infrastructure that won't directly move judge but matter for production parity.

### Likely judge-relevant (medium effort)

| # | Codex item | Effort | Plan |
|---|---|---|---|
| 1 | Constructor kwargs honored (`step_timeout`, `llm_timeout`, `vision_detail_level`, `include_attributes`) | small | v0.8.x — one kwarg per version, log if eval passes them |
| 6 | `fallback_llm` for rate-limit failover | medium | v0.8.x — helps tasks that fail on transient model errors |

### Infrastructure (low judge impact, multi-day)

| # | Codex item | Effort |
|---|---|---|
| 2 | Real planner: `PlanItem`, `enable_planning`, `planning_replan_on_stall`, `planning_exploration_limit`, plan in agent state | multi-day |
| 3 | Typed `AgentOutput`/`AgentBrain`, no-thinking variant, real flash-mode reduced-output contract | multi-day |
| 4 | Full upstream `MessageManager`: include_attributes handling, screenshot resizing, recent_events, sample_images, message compaction settings, unavailable skills text, max_clickable_elements truncation | multi-day |
| 5 | Public control API: pause/resume/stop state, external status callbacks, signal handling, event-based pause control | medium |
| 7 | Full upstream judge construction (richer browser-state context in judge messages) | medium — but eval judge is already identical, so low judge-score impact |
| 8 | Upstream `FileSystem` state in `AgentState`, `file_system_path`, managed files, output-file events, full done-text/display-files behavior | medium |
| 9 | Cloud telemetry / event bus / cloud sync hooks | multi-day |
| 10 | Artifacts: `save_conversation_path`, conversation transcript, GIF generation, screenshot service, recording hooks | medium |
| 11 | Direct `output_model_schema` / `extraction_schema` task-enhancement path (vs. our controller-based done) | medium |
| 12 | Richer `AgentHistoryList`: structured output deserialization, full browser state history, file system state, pause/stop state, loop detector state, action metadata | medium |

### Skipped from "fix everything" because none directly closes the 9pp judge gap

The arc has shown: judge gains come from agent-loop changes (extract tool, selector retargeting, stale-element handling) — not constructor parity, not lifecycle parity, not artifact parity. So the parity backlog is real but won't move us toward 73%.

## Where the 9pp gap likely lives (informed guesses)

- **Model ceiling**: gemini-3-flash-preview may just cap around ~65% on this benchmark for our agent shape. Upstream's gain may partly be JSON-output paradigm pulling more out of the model.
- **Real prompt template port**: upstream's `system_prompt.md` is 269 lines tuned for the JSON-output paradigm. Our experiments porting parts of it bundled regressed 3 times in a row. A clean room port + JSON output mode is ~1 week.
- **DOM tree depth**: our snapshot is interactive-only + indented; upstream's includes more text nodes and a deeper hierarchy. v0.5.7 showed naive static text was bad; v0.6.0 showed it works with extract tools. There may be more nuance to tune.

## Cost analysis (per-task)

For v0.7.3 ($0.131/task):

| Driver | Calls/task | $/call (est) | Subtotal | % |
|---|---|---|---|---|
| **Agent main LLM round trips** | ~20 (medSteps) | $0.005-0.007 | **~$0.10-0.14** | **80%** |
| extract_structured_data | 1.95 | $0.003 | $0.006 | 5% |
| web_search (page load) | 2.67 | $0.001 | $0.003 | 2% |
| evaluate_js | 1.42 | $0.0005 | $0.001 | <1% |

Cost lever = "fewer turns per task". Upstream averages 12 steps; we average 17-20.

## Lessons (don't repeat)

1. **Bundling hides signals**. v0.5.4, v0.7.4, v0.8.0 all stacked 2-3 changes; couldn't attribute which broke things. → one change per version.
2. **More prompt content ≠ better**. Tested 5 times; regressed every time. Static text + extract tools was a counter-example, but only because the addition was load-bearing for a specific tool path.
3. **Component-in-isolation may differ from component-in-bundle**. v0.5.7 (static text alone) regressed; v0.6.0 (static text + extract) gained. Same change, different context.
4. **Trace numbers update post-eval**. Judge re-runs and partial completions can shift judge by 1-3pp after a run "completes". Don't lock in a verdict on the first read.
5. **Self-report ≠ judge**. We had a 30-40pp gap between agent self-report and judge throughout. Self-report is unreliable as an eval signal.
6. **Cost trajectory matters**. We doubled cost ($0.063 → $0.131) over the arc for +11pp judge. Need to attack cost separately.
7. **Don't forget to verify imports before tagging**. v0.7.2 broke at import time; v0.7.3 was a one-line fix. Ship a smoke test in CI eventually.

## Versions worth re-testing in isolation (priority order)

These are the highest-information re-tests after the v0.8.1 baseline lands:

1. **v0.8.6** = v0.8.1 + persistent memory state alone. v0.8.0 stacked it on v0.7.5 bloat; clean baseline test could reveal whether memory state is actually beneficial.
2. **v0.8.10** = v0.8.1 + 2-pass validation prompt alone. v0.6.5 was undercalled (+2pp); fresh test on clean baseline confirms.
3. **v0.8.7** = v0.8.1 + `<browser_state>` markers explanation only. v0.7.4 bundled three blocks; isolating the marker explanation tells us whether ANY of those blocks are net-positive.
4. **v0.8.9** = v0.8.1 + reasoning XML mandate alone. v0.7.5 was costly-but-cheap; if the cost benefit is real and judge holds, it's a candidate for shipping.

## Open questions

- Does upstream's flash_anthropic prompt help our model? (Never tested with that prompt specifically.)
- What happens if we route extract_structured_data calls to a CHEAPER model (claude-haiku, gemini-2-flash)? page_extraction_llm support was shipped in v0.7.0 but eval doesn't pass one.
- Would porting upstream's full JSON output mode + thinking-block paradigm close the remaining 9pp gap, or is it a model-ceiling problem?
