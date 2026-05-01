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

### Phase 1 — cost + reliability (revised plan)

The plan was reshuffled mid-stream after two diagnostics shifted priorities:
1. **v0.7.3 at 50-step (63.6%) and 100-step (64.2%) judge are within noise** → step-out is loop-driven, not budget-driven. Loop-killers move higher up the queue.
2. **Eval logs show `Stage errors: run_agent: ...` on some tasks** → the agent re-raises unhandled exceptions and the eval framework records those tasks as no-answer fails. Each crash is asymmetrically ~0.5pp judge lost. Crash recovery jumps to top of queue.

Switching to **100-step budget from v0.8.x onward** so step-outs are unambiguously loops, not insufficient time. New baseline = v0.8.1 100-step (~67.1% / 8.6¢ / 18% step-out).

| Version | Single change | Expected |
|---|---|---|
| v0.8.2 | history_window_steps default 6 → 3 | cost ↓$0.02-$0.04, judge ±1pp |
| v0.8.3 | top-level crash recovery in `Agent.run()` — wrap `_loop` in try/except, route crashes through `_force_final_answer` instead of re-raising | judge ↑1-3pp on crash-prone tasks (was 0% → now whatever partial answer survived); cost ±0¢ |
| v0.8.4 | loop signature normalization — drop element `index` from tight-loop sig hash so "click idx 5 → click idx 12 → click idx 23" is detected as a loop | step-out% ↓3-6pp, judge ↑1-2pp, cost ↓$0.005-$0.01 |
| v0.8.5 | hard abort on persistent loop — after 2 LOOP_DETECTED nudges OR stagnation streak ≥6, force `_force_final_answer` | step-out% ↓further, partial credit on remaining loopers |
| v0.8.6 | max_consecutive_errors 5 → 3 | cost ↓$0.005, judge ±0pp |
| v0.8.7 | prompt nudge "batch 2-3 safe actions per turn" | steps ↓ → cost ↓$0.02 |
| v0.8.8 | cap extract_structured_data at 2 calls/task (hard) | cost ↓$0.005 |

### Phase 2 — re-test miscalled regressions in isolation

> Version numbers below were drafted before Phase 1 expanded from 4 to 7 versions. Renumber by +3 (so v0.8.9 → v0.8.12, etc.) once Phase 1 lands. Keeping original numbers here for traceability against the codex item table at the bottom — that table still references the *content* of each change, which hasn't moved.

| Version | Single change | Reason |
|---|---|---|
| v0.8.6 | persistent memory/next_goal state ALONE | v0.8.0 was bundled; clean test on baseline |
| v0.8.7 | `<browser_state>` markers explanation ALONE | v0.7.4 bundled three blocks |
| v0.8.8 | `<task_completion>` checklist ALONE | same |
| v0.8.9 | reasoning XML mandate ALONE (re-test) | v0.7.5 cost win was real, judge re-test on baseline |
| v0.8.10 | 2-pass validation prompt re-introduced ALONE | v0.6.5 was undercalled |

### Phase 3 — codex parity items (every codex complaint as its own version)

Each as a single change on the v0.8.1 baseline, only after Phase 1+2 land. Most won't move judge but close out the production parity backlog. Codex item numbers cross-referenced.

#### Phase 3a — constructor kwargs (codex #1) split by category

| Version | Codex ref | Single change |
|---|---|---|
| v0.8.11 | #1 | Honor `step_timeout` + `llm_timeout` (per-step + per-LLM-call timeouts; eval may pass both). Default to `tool_timeout` value if unset. |
| v0.8.12 | #1 | Honor `vision_detail_level` + `include_attributes` + `sample_images`. Vision detail (`auto`/`low`/`high`) goes to image rendering; include_attributes filters DOM walker output (token saver); sample_images attaches in user message. |
| v0.8.13 | #1 | Honor `browser_profile` + `browser` + `file_system_path` + `save_conversation_path`. Profile/browser map to BrowserSession config; file_system_path overrides our temp sandbox; save_conversation_path triggers a transcript writer (covered more in v0.8.24). |
| v0.8.14 | #1 | Honor `skills` / `skill_ids` / `skill_service` (treat as no-op compatibly with WARN log unless we have a real implementation). Honor `demo_mode`, `enable_planning` (real impl in v0.8.20), `pricing_url`, `calculate_cost`, `generate_gif` (real impl in v0.8.25), `extraction_schema`, `output_model_schema` (real impl in v0.8.16). |

#### Phase 3b — judge-relevant items

| Version | Codex ref | Single change |
|---|---|---|
| v0.8.15 | #6 | `fallback_llm` rate-limit failover. On 429/5xx/Overloaded, retry on the fallback LLM if provided. Already have `with_retry`; this adds cross-LLM failover. |
| v0.8.16 | #11 | Direct `output_model_schema` / `extraction_schema` task-enhancement path. Inject the schema into the task prompt at run() start so the LLM knows the expected output shape from turn 1, not just at done(). Mirrors upstream's task-enhance path. |
| v0.8.17 | #8 | Full upstream FileSystem parity. Replace sandboxed temp dir with a `FileSystem` service: managed files (track creation/edit/read), output-file events, full done-text/display-files behavior, `file_system_path` actually used as root, parity with upstream's `available_file_paths` model (was partial in v0.7.2). |
| v0.8.18 | #3 | Typed `AgentOutput` / `AgentBrain` shape + proper `use_thinking=False` no-op + real flash-mode reduced-output contract. Currently flash_mode is just a prompt swap; upstream actually changes the output schema (skips `thinking` field in flash). Implement parity. |
| v0.8.19 | #4 | Upstream `MessageManager` core: include_attributes handling (DOM filter), screenshot resizing, recent browser events surfacing, sample_images injection, full message compaction settings (max_clickable_elements truncation, max_input_tokens etc), unavailable skills text. Medium refactor; may help judge if eval signals rely on these. |
| v0.8.20 | #2 | Real planner: `PlanItem`, `enable_planning`, `planning_replan_on_stall`, `planning_exploration_limit`, plan list in agent state, plan markers (`[x]/[>]/[ ]/[-]`) shown in agent context. Mirrors upstream's first-class planning. Closes the planning-side judge gap if there is one. |
| v0.8.21 | #7 | Full upstream judge construction in `_judge_and_log`. Pass the richer browser-state context (snapshots, screenshots, full agent history) the way upstream constructs its judge messages. Lower judge-score leverage (eval judge is identical) but parity for offline/inline judge use cases. |

#### Phase 3c — operational + infra parity

| Version | Codex ref | Single change |
|---|---|---|
| v0.8.22 | #5 | Public control API: `pause` / `resume` / `stop` state, external status callbacks, signal handling (SIGINT triggers graceful pause), event-based pause control (`asyncio.Event`-driven instead of poll). |
| v0.8.23 | #9 part 1 | Telemetry events (anonymized) — agent_start, step_completed, run_finished, with versioned schema. No cloud sync yet. |
| v0.8.24 | #9 part 2 | Cloud sync hooks: cloud task/session/step events. Pluggable `cloud_client` kwarg; default no-op. Match upstream's WAL-backed event bus shape. |
| v0.8.25 | #10 | Artifacts: `save_conversation_path` writes a JSONL transcript per run; GIF generation from screenshot history; screenshot service equivalent (saves PNGs to a known dir); recording/HAR hooks. |
| v0.8.26 | #12 | Richer `AgentHistoryList`: structured output deserialization (parse `output_model_schema` payloads), full browser state history per step, file system state snapshot per step, pause/stop state captured, loop detector state, richer action metadata (latency, retries, batched-with-others). |

### Phase 4 — final push to ≥73% if Phases 1-3 don't get us there

If we plateau in Phase 3, options that were deferred earlier. Each as a single change.

| Version | Single change | Reason |
|---|---|---|
| v0.8.27 | Port upstream's full `system_prompt.md` (269 lines) + JSON output paradigm | biggest single lever; closest to upstream behavioral shape; ~1 week effort |
| v0.8.28 | Route extract_structured_data calls to claude-haiku-4-5 by default | cost lever — extract is 5% of cost today but if we double its usage in Phase 1+2 it grows |
| v0.8.29 | Per-model prompt tuning — gemini-3-flash-preview specific language | gemini may need different phrasing than generic upstream prompt |
| v0.8.30 | Cross-origin iframe DOM walking (currently same-origin only) | unblocks tasks where content lives in 3rd-party iframes |
| v0.8.31 | Real shadow-DOM closed root attempt via CDP (not just open roots) | last DOM coverage gap |

### Release rule reminder

**v0.9.0 ships only when judge ≥ 73%.** All work above stays in v0.8.x. If we hit parity at, say, v0.8.18, that's v0.9.0 and Phase 3c+ becomes v0.9.x.

### Codex item → version map (cross-reference)

| Codex # | Description | Version(s) |
|---|---|---|
| 1 | Constructor kwargs honored | v0.8.11, v0.8.12, v0.8.13, v0.8.14 |
| 2 | Real planner | v0.8.20 |
| 3 | Typed AgentOutput / use_thinking / flash-mode contract | v0.8.18 |
| 4 | Full MessageManager | v0.8.19 |
| 5 | Public control API (pause/resume/stop, signals) | v0.8.22 |
| 6 | fallback_llm | v0.8.15 |
| 7 | Full upstream judge construction | v0.8.21 |
| 8 | FileSystem state parity | v0.8.17 |
| 9 | Cloud telemetry / event bus | v0.8.23, v0.8.24 |
| 10 | Artifacts (save_conversation, GIF, recording) | v0.8.25 |
| 11 | Direct output_model_schema task path | v0.8.16 |
| 12 | Richer AgentHistoryList | v0.8.26 |

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
