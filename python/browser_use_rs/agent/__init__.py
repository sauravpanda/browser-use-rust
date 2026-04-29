"""Unified, provider-agnostic Agent loop.

Drives any `BaseChatModel` against a browser tool set with native tool
calling, parallel tool execution, optional vision, sensitive-data
redaction, and resumable state. Returns an `AgentHistoryList` shaped to
match `browser_use.Agent` so eval/cloud consumers can drop it in.

Manual loop (not the SDK runner) so we can return image content blocks
from tools like `screenshot` — providers that don't allow image-in-tool-result
(Gemini, OpenAI) handle it inside their `ainvoke` mapping.
"""

from __future__ import annotations

import asyncio
import base64
import inspect
import logging
import time
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

from browser_use_rs._native import BrowserSession
from browser_use_rs.llm.base import (
    AssistantMessage,
    BaseChatModel,
    ChatInvokeUsage,
    ContentPart,
    ImagePart,
    Message,
    TextPart,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from browser_use_rs.tools import Tool
from browser_use_rs.views import (
    ActionResult,
    AgentHistory,
    AgentHistoryList,
    AgentOutput,
    AgentState,
    BrowserStateSummary,
    StepMetadata,
)

DEFAULT_SYSTEM_PROMPT = """\
You are a browser-use agent. You control a real Chromium browser through a
small set of tools and complete the user's task by calling them.

You receive a fresh page snapshot (URL + numbered interactive elements) at
the start of every turn — do NOT call a snapshot tool yourself. Reference
elements by their `[N]` index from the most recent snapshot.

Multi-action turns: emit MULTIPLE tool calls in a single turn when the
next steps don't depend on each other's output (e.g. `[scroll(800),
get_text("h1.title")]`, or a sequence of scrolls to reveal a list).
Calls execute sequentially in the order you provide. The batch STOPS
automatically if any action navigates to a new URL — subsequent calls
are skipped because their `[N]` indices were valid only for the page
you saw at the start of the turn. This means you can plan 2-4 actions
ahead and have them run without spending an extra LLM turn each.

Strategy:
- Read the page snapshot, then act. After clicks/navigates the next turn's
  snapshot reflects the new page; indices are not stable across turns.
- Prefer clicking visible links over navigating to known URLs — that
  verifies the page is in the expected state.
- When the page is unfamiliar or text is ambiguous, take a screenshot.
- Extract content with `get_text` / `page_text` / `get_links` rather than
  relying solely on the snapshot — long pages render only above-the-fold
  elements in the snapshot.
- When the task is complete, respond with a final answer in plain text. Do
  NOT call any further tools — your text turn is the answer.

Overlays: many sites open with a cookie consent / age gate / region
selector / newsletter / "log in to continue" / "this site uses cookies"
modal that covers the actual content. If the page snapshot is dominated
by such an overlay, your FIRST action must be to dismiss it before
extracting anything. Look for buttons matching: Accept, Agree, Continue,
OK, Got it, I agree, Allow all, Allow, Dismiss, Close, Skip, Maybe
later, No thanks, X (close icons). Do NOT conclude "task impossible" on
your first turn — the real content is almost always one click away.

Stamina: keep going until the task is fully solved. Do NOT end your
turn with plain text describing what you would do — ACTUALLY make the
tool call. A turn with no tool calls is treated as your FINAL ANSWER;
only end your turn that way when you've genuinely completed the task
or hit a hard blocker. When unsure, prefer one more concrete action
(scroll, read, click) over giving up.

When calling tools: never invent values for required arguments. If the
snapshot doesn't show what you need (no [N] for the element, no text
to read), scroll, navigate, or extract first to get real values.
"""

# Tag prefix that identifies auto-injected per-step page-state messages.
# We use it to find and supersede the previous step's snapshot so the
# conversation doesn't accumulate stale DOMs across long runs.
_PAGE_STATE_TAG = "[PAGE_STATE]"
_PAGE_STATE_SUPERSEDED = (
    f"{_PAGE_STATE_TAG} (superseded — see latest page state below)"
)


StepStartCallback = Callable[
    [BrowserStateSummary, AgentOutput, int], Any
]
StepEndCallback = Callable[[], Any]
DoneCallback = Callable[[AgentHistoryList], Any]
ShouldStopCallback = Callable[[], Awaitable[bool]]


class Agent:
    def __init__(
        self,
        task: str,
        llm: BaseChatModel,
        *,
        tools: list[Tool] | None = None,
        browser_session: BrowserSession | None = None,
        max_steps: int = 30,
        max_consecutive_errors: int = 5,
        # Per-tool-call timeout in seconds. Caps how long a single CDP
        # operation (click / scroll / navigate / dom_snapshot) can hang
        # before we surface it as a tool error to the LLM. Without this,
        # one stuck call can consume the eval framework's whole stage
        # timeout and kill the run with a bare TimeoutError.
        tool_timeout: float = 30.0,
        use_vision: bool = True,
        sensitive_data: dict[str, str] | None = None,
        system_prompt: str | None = None,
        extend_system_message: str | None = None,
        override_system_message: str | None = None,
        initial_actions: list[dict] | None = None,
        register_new_step_callback: StepStartCallback | None = None,
        register_done_callback: DoneCallback | None = None,
        register_should_stop_callback: ShouldStopCallback | None = None,
        injected_agent_state: AgentState | None = None,
        source: str = "browser_use_rs",
        # Accepted for browser_use API compat. Swallowed silently for now;
        # consumer code keeps working but these don't change behavior yet.
        **_compat_kwargs: Any,
    ):
        self.task = task
        self.llm = llm
        # Inline-judge inputs. eval/service.py:712 calls
        # `agent._judge_and_log()` after run() for ComprehensiveV1 grading
        # and reads `agent.history.is_judged()` / `.judgement()` after.
        # We pluck these from the compat kwargs so the captured llm and
        # ground truth are available to the inline judge.
        self.judge_llm: BaseChatModel | None = _compat_kwargs.pop("judge_llm", None)
        self.ground_truth: str | None = _compat_kwargs.pop("ground_truth", None)
        # Tool source: explicit tools= wins, then controller=, then defaults.
        controller = _compat_kwargs.pop("controller", None)
        if tools is None and controller is not None:
            tools = controller.tools
        if tools is None:
            from browser_use_rs._browser_tools import BROWSER_TOOLS

            tools = BROWSER_TOOLS
        self.controller = controller
        self.tools = tools
        self.tools_by_name: dict[str, Tool] = {t.name: t for t in tools}
        self._owns_session = browser_session is None
        self.session = browser_session or BrowserSession()
        self.max_steps = max_steps
        self.max_consecutive_errors = max_consecutive_errors
        self.tool_timeout = tool_timeout
        self.use_vision = use_vision
        self.sensitive_data: dict[str, str] = sensitive_data or {}
        if override_system_message is not None:
            self.system_prompt = override_system_message
        else:
            self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT
            if extend_system_message:
                self.system_prompt = (
                    self.system_prompt.rstrip() + "\n\n" + extend_system_message
                )
            # Switch the completion contract when the controller declared a
            # structured output: the LLM must call done(...) with fields
            # matching the schema instead of emitting plain text.
            if (
                controller is not None
                and getattr(controller, "output_model", None) is not None
                and any(t.name == "done" for t in tools)
            ):
                self.system_prompt = self.system_prompt.rstrip() + (
                    "\n\nIMPORTANT: To finish this task you MUST call the "
                    "`done` tool with `data` filled to match the requested "
                    "schema. Do NOT answer in plain text — a plain-text turn "
                    "will be treated as 'still working' and waste a step."
                )
        self.initial_actions = initial_actions or []
        self.register_new_step_callback = register_new_step_callback
        self.register_done_callback = register_done_callback
        self.register_should_stop_callback = register_should_stop_callback
        self.source = source

        # Resumable state. Conversation history is built on the fly each run.
        self.state: AgentState = injected_agent_state or AgentState()
        # Tag the accumulator with the model so usage.model_dump() can
        # compute per-cost fields (eval aggregator reads `total_cost`).
        if self.state.history.usage.model is None:
            self.state.history.usage.model = getattr(llm, "model", None)
        # Compat: agents that tracked usage_log on the old Anthropic Agent
        # can keep reading it. Each entry: {step, input, output, cache_read}.
        self.usage_log: list[dict[str, Any]] = []
        self.error_log: list[tuple[int, str]] = []

        # Conversation messages live across run() calls so add_new_task()
        # can append a continuation without losing browser/page context.
        self._messages: list[Message] = []
        self._consecutive_error_turns = 0
        # Loop-detection state: rolling windows of recent activity
        # + cooldown so a single nudge doesn't fire on consecutive steps.
        # See _maybe_inject_loop_nudge for the heuristics.
        self._recent_action_sigs: list[str] = []
        self._loop_nudge_cooldown = 0
        self._recent_urls: list[str] = []
        # Tool names emitted per turn (tuple per turn). Drives the
        # "no extract" nudge: many turns with zero read-tool calls
        # signals the agent is exploring without reading content.
        self._recent_tool_names: list[tuple[str, ...]] = []
        # One-shot budget warning at step == max_steps - 5.
        self._budget_warning_fired = False
        # Compat: `agent.message_manager.last_input_messages` mirrors
        # browser_use's API. Consumer code (evaluations-internal) reads
        # this for diagnostics; we just expose the live message buffer.
        self.message_manager = _MessageManagerView(self)

    @property
    def last_input_messages(self) -> list[Message]:
        """The current conversation history fed to the LLM each step."""
        return list(self._messages)

    @property
    def history(self) -> AgentHistoryList:
        """Alias for `self.state.history`. Eval consumers read this
        directly after `agent.run()` returns: `agent.history.history[i]`,
        `agent.history.final_result()`, etc."""
        return self.state.history

    async def run(
        self,
        max_steps: int | None = None,
        on_step_start: StepStartCallback | None = None,
        on_step_end: StepEndCallback | None = None,
    ) -> AgentHistoryList:
        max_steps = max_steps if max_steps is not None else self.max_steps
        on_step_start = on_step_start or self.register_new_step_callback
        on_step_end = on_step_end

        # Always try to start the session — browser_use's Agent does the
        # same. If the caller already started it, the call errors and we
        # ignore: the cdp_url-attached cloud case typically passes an
        # already-started session, but local examples pass an unstarted one.
        if not self._messages:
            try:
                await self.session.start()
            except Exception:
                pass

        # First-run setup: seed the conversation with the task and any
        # initial_actions the caller scripted (typically a navigate).
        if not self._messages:
            self._messages.append(UserMessage(content=self.task))
            await self._run_initial_actions()

        try:
            await self._loop(max_steps, on_step_start, on_step_end)
        finally:
            # Only the Agent-owned session gets stopped — caller-owned
            # sessions (cloud-attached, multi-agent) outlive the Agent.
            if self._owns_session:
                try:
                    await self.session.stop()
                except Exception:
                    pass

        if self.register_done_callback:
            await _maybe_await(self.register_done_callback(self.state.history))
        return self.state.history

    async def add_new_task(self, new_task: str) -> None:
        """Append a follow-up user instruction. Next run() picks it up."""
        self._messages.append(UserMessage(content=new_task))

    async def _judge_and_log(self) -> dict[str, Any] | None:
        """Run an inline LLM-based judge and store the verdict on history.

        Mirrors `browser_use.Agent._judge_and_log()` so eval consumers
        can grade ComprehensiveV1-style runs. After this call,
        `self.history.is_judged()` returns True and `self.history.judgement()`
        returns the verdict dict (`reasoning`, `verdict`, `score`,
        `impossible_task`, `reached_captcha`).

        Lower fidelity than browser_use's full ComprehensiveV1 judge — we
        feed the trajectory + final answer + ground truth text to
        `self.judge_llm` and parse a JSON verdict. No screenshot input.
        For per-step screenshot judging, use the eval suite's deferred
        judge types (simplev1 / onlinemind2web / webjudge) instead.

        Returns the verdict dict (also stored), or None if judge_llm
        wasn't supplied at construction.
        """
        if self.judge_llm is None:
            return None

        # Build a compact trajectory string for the judge.
        steps: list[str] = []
        final_answer: str | None = None
        for i, h in enumerate(self.state.history.history):
            actions = ", ".join(
                f"{tc.name}({tc.args})" for tc in h.output.tool_calls
            ) or "(no tool calls)"
            steps.append(f"step {i + 1}: url={h.state.url} | {actions}")
            for r in h.result:
                if r.is_done and r.extracted_content:
                    final_answer = r.extracted_content

        prompt = (
            f"You are evaluating whether an autonomous browser agent successfully "
            f"completed a task.\n\n"
            f"TASK:\n{self.task}\n\n"
            f"GROUND TRUTH (reference answer, may be empty):\n{self.ground_truth or '(none)'}\n\n"
            f"AGENT TRAJECTORY ({len(steps)} steps):\n"
            + "\n".join(steps)
            + f"\n\nAGENT FINAL ANSWER:\n{final_answer or '(no done action)'}\n\n"
            f"Decide if the agent completed the task. Respond ONLY with JSON of "
            f'the form: {{"verdict": true|false, "reasoning": "...", '
            f'"score": 0-100, "impossible_task": true|false, '
            f'"reached_captcha": true|false}}. No prose outside JSON.'
        )

        from browser_use_rs.llm.base import UserMessage as _UM

        completion = await self.judge_llm.ainvoke(
            [_UM(content=prompt)],
            tools=[],
            system="You are a strict, concise judge. Output JSON only.",
        )
        verdict = _parse_judgement(completion.text or "")
        self.state.history._set_judgement(verdict)
        return verdict

    # ------------------------------------------------------------------
    # internal

    async def _run_initial_actions(self) -> None:
        """Execute scripted initial_actions before handing control to the LLM.
        Format matches browser_use: [{"navigate": {"url": "..."}}, ...]."""
        for action in self.initial_actions:
            for name, args in action.items():
                tool = self.tools_by_name.get(name)
                if tool is None:
                    continue
                try:
                    await tool.func(self.session, **(args or {}))
                except Exception:
                    pass

    async def _loop(
        self,
        max_steps: int,
        on_step_start: StepStartCallback | None,
        on_step_end: StepEndCallback | None,
    ) -> None:
        for _ in range(max_steps):
            if await self._should_stop():
                return
            self.state.n_steps += 1
            step_n = self.state.n_steps

            t0 = time.monotonic()
            state_summary = await self._capture_state()
            self._inject_page_state(state_summary)

            completion = await self.llm.ainvoke(
                self._messages,
                self.tools,
                system=self.system_prompt,
            )
            self._record_usage(step_n, completion.usage)

            output = AgentOutput(
                text=completion.text,
                tool_calls=list(completion.tool_calls),
            )

            if on_step_start is not None:
                await _maybe_await(on_step_start(state_summary, output, step_n))

            # No tool calls = the model is answering. Synthesize a done
            # ActionResult so consumers can read final_result() / is_done().
            if not completion.tool_calls:
                done_text = completion.text or ""
                results = [
                    ActionResult(
                        extracted_content=done_text,
                        is_done=True,
                        success=bool(done_text),
                    )
                ]
                self._append_history(state_summary, output, results, t0, step_n)
                if on_step_end is not None:
                    await _maybe_await(on_step_end())
                return

            # Echo the assistant turn so the next request includes it.
            self._messages.append(
                AssistantMessage(text=completion.text, tool_calls=output.tool_calls)
            )

            # Run tool calls SEQUENTIALLY in the order the LLM emitted
            # them, with a page-change guard between each. Mirrors
            # upstream browser_use's `multi_act` semantics: the LLM may
            # plan a batch like `[scroll(800), get_text("h1")]` and we
            # execute them one after the other. If any action causes the
            # URL to change, we abort the rest of the batch — subsequent
            # tool calls were addressed using `[N]` indices from the OLD
            # page snapshot and would land on the wrong elements.
            #
            # This replaces the prior asyncio.gather; parallelism was a
            # mirage because consecutive actions almost always depend on
            # each other's effects (click → snapshot → click), and the
            # gather order was undefined which made the second click in
            # a 2-call turn unsafe. Sequential is both faster (no
            # round-trip per action) and safer.
            # Visibility for eval audits: how big is each batch? Stable
            # 1's mean multi_act isn't helping; consistent 3-4's mean the
            # LLM is using the budget.
            batch_size = len(completion.tool_calls)
            if batch_size > 1:
                logger.info(
                    "agent: step %d batch=%d tools=%s",
                    step_n,
                    batch_size,
                    [tc.name for tc in completion.tool_calls],
                )

            results_and_msgs = await self._run_tools_sequentially(
                completion.tool_calls
            )
            tool_results: list[ActionResult] = []
            done_result: ActionResult | None = None
            for action_result, tool_message in results_and_msgs:
                tool_results.append(action_result)
                self._messages.append(tool_message)
                # The structured-output `done` tool encodes its payload as
                # `__DONE__:<0|1>:<json>` in extracted_content (see
                # controller._build_done_tool). Parse it back into a final
                # ActionResult so eval consumers see is_done=True and
                # final_result() returns the JSON.
                if (
                    action_result.extracted_content
                    and isinstance(action_result.extracted_content, str)
                    and action_result.extracted_content.startswith("__DONE__:")
                ):
                    try:
                        _, success_flag, payload = action_result.extracted_content.split(":", 2)
                        done_result = ActionResult(
                            extracted_content=payload,
                            is_done=True,
                            success=bool(int(success_flag)),
                        )
                    except (ValueError, IndexError):
                        # Marker malformed — fall back to treating the raw
                        # text as the final answer rather than crashing.
                        done_result = ActionResult(
                            extracted_content=action_result.extracted_content,
                            is_done=True,
                            success=True,
                        )

            if done_result is not None:
                # Replace the parsed-out tool result with the done flag set
                # so history.is_done() / final_result() see it directly,
                # then exit the loop. Other tool results from the same turn
                # are kept (they ran and may have side effects).
                tool_results = [
                    done_result if r.extracted_content
                    and isinstance(r.extracted_content, str)
                    and r.extracted_content.startswith("__DONE__:")
                    else r
                    for r in tool_results
                ]

            self._append_history(state_summary, output, tool_results, t0, step_n)

            if on_step_end is not None:
                await _maybe_await(on_step_end())

            if done_result is not None:
                return

            self._maybe_inject_loop_nudge(
                state_summary, completion.tool_calls, step_n, max_steps
            )

            # Single-action fallback: if a multi-action turn produced a
            # majority of errors AND no extract, the agent is batching
            # dead-end actions. Hint to back off to one action per turn
            # so each result can inform the next, instead of compounding
            # failures. The hint is a one-shot UserMessage; we don't
            # mechanically enforce single-action — that would silently
            # break legitimate batches the LLM recovers with.
            self._maybe_inject_single_action_hint(
                completion.tool_calls, tool_results
            )

            # All-error streak guard. Model can self-correct from one bad
            # turn; multiple in a row means it's stuck.
            if all(r.error for r in tool_results):
                self._consecutive_error_turns += 1
                for r in tool_results:
                    self.error_log.append((step_n, r.error or ""))
                if self._consecutive_error_turns >= self.max_consecutive_errors:
                    self.state.history.history[-1].result.append(
                        ActionResult(
                            error=(
                                f"agent gave up after "
                                f"{self._consecutive_error_turns} all-error turns"
                            ),
                            is_done=True,
                            success=False,
                        )
                    )
                    return
            else:
                self._consecutive_error_turns = 0

        # max_steps exhausted without explicit done.
        if self.state.history.history:
            self.state.history.history[-1].result.append(
                ActionResult(
                    error=f"hit max_steps={max_steps} without final answer",
                    is_done=True,
                    success=False,
                )
            )

    # Tool name groups used by the loop-detection heuristics.
    _EXTRACT_TOOLS: frozenset[str] = frozenset({
        "get_text",
        "page_text",
        "get_links",
        "list_tabs",
        "list_downloads",
        "get_cookies",
        "save_pdf",
        "done",
    })

    def _maybe_inject_loop_nudge(
        self,
        state: BrowserStateSummary,
        tool_calls: list[ToolCall],
        step_n: int,
        max_steps: int,
    ) -> None:
        """Detect three classes of stall and inject a one-shot nudge.

        1. **Tight loop** — same canonical (name+args) signature emitted
           3+ times in the last 6 turns. The URL guard from earlier
           versions was dropped (v0.4.13) because the dominant eval
           failure was search-bouncing where the URL changes every
           turn but the same `[type, click, click]` cycle repeats. We
           now match OpenCode's doom-loop semantics: identical args
           three times wins regardless of URL.
        2. **No extract** — fewer than 25% of recent turns called a
           read tool (`get_text`, `page_text`, `get_links`, `done`)
           AND the agent has used >1/3 of its step budget. Catches
           agents that nav/click/scroll endlessly with only token
           extracts. Was "zero extracts" — too lenient.
        3. **Budget warning** — at `step == max_steps - 5`, fire a
           one-shot "wrap up now" reminder so the agent commits to a
           best-effort answer instead of silently maxing out.

        Each nudge engages a 3-step cooldown so we don't spam, but the
        budget warning is a separate one-shot independent of cooldown.
        Each nudge is also logged at INFO level so eval consumers can
        audit nudge trigger rates from the eval workflow logs.
        """
        WINDOW = 6
        REPEAT_THRESHOLD = 3
        COOLDOWN_STEPS = 3
        BUDGET_WARNING_REMAINING = 5
        # Fire no-extract when read-tool turns / window <= this ratio.
        # 1/4 of 6 = 1.5 → fires when ≤1 read turn in 6.
        NO_EXTRACT_RATIO = 0.25

        if self._loop_nudge_cooldown > 0:
            self._loop_nudge_cooldown -= 1

        # Bookkeeping: build a stable signature for the *set* of tool
        # calls in this turn (name+args, JSON-sorted), and track the
        # tool names emitted per turn for the no-extract heuristic.
        try:
            import json as _json

            sig_payload = sorted(
                (tc.name, _json.dumps(tc.args, sort_keys=True, default=str))
                for tc in tool_calls
            )
            sig = _json.dumps(sig_payload)
        except Exception:
            sig = "|".join(sorted(tc.name for tc in tool_calls))

        self._recent_action_sigs.append(sig)
        self._recent_action_sigs = self._recent_action_sigs[-WINDOW:]
        self._recent_urls.append(state.url or "")
        self._recent_urls = self._recent_urls[-WINDOW:]
        self._recent_tool_names.append(tuple(tc.name for tc in tool_calls))
        self._recent_tool_names = self._recent_tool_names[-WINDOW:]

        # ---- 3. Budget warning (independent of cooldown, fires once) ----
        # max_steps == 0 means "no cap" — skip the warning in that case.
        if (
            not self._budget_warning_fired
            and max_steps > 0
            and step_n >= max_steps - BUDGET_WARNING_REMAINING
            and step_n < max_steps
        ):
            remaining = max_steps - step_n
            nudge = (
                f"[BUDGET_WARNING] You have ~{remaining} turn(s) left "
                f"before the run ends. Stop exploring — read whatever "
                f"content is on the current page and finish with the "
                f"best answer you have. A partial answer is better than "
                f"no answer."
            )
            self._messages.append(UserMessage(content=nudge))
            logger.info(
                "agent: BUDGET_WARNING fired at step %d/%d (remaining=%d)",
                step_n, max_steps, remaining,
            )
            self._budget_warning_fired = True
            # Don't return — a budget warning and a loop nudge can coexist.

        if self._loop_nudge_cooldown > 0:
            return
        if len(self._recent_action_sigs) < REPEAT_THRESHOLD:
            return

        # ---- 1. Tight-loop check (no URL guard since v0.4.13) ----
        repeat_count = self._recent_action_sigs.count(sig)
        if repeat_count >= REPEAT_THRESHOLD:
            nudge = (
                f"[LOOP_DETECTED] You have called the EXACT same action "
                f"sequence {repeat_count} times in your last {WINDOW} "
                f"turns. Repeating it again will not work. Try a "
                f"materially different approach: pick different elements, "
                f"reformulate the search query, scroll to a different "
                f"region, switch tabs, or finish with `done` if you have "
                f"enough information. Do NOT issue the same tool calls "
                f"again."
            )
            self._messages.append(UserMessage(content=nudge))
            logger.info(
                "agent: LOOP_DETECTED fired at step %d (sig repeats %d/%d)",
                step_n, repeat_count, WINDOW,
            )
            self._loop_nudge_cooldown = COOLDOWN_STEPS
            return

        # ---- 2. No-extract check (≤25% of last 6 turns called a read) ----
        # Wait until the agent has used >1/3 of the budget — no point
        # nudging on early exploration where reading isn't the goal yet.
        budget_used = step_n / max_steps if max_steps > 0 else 0
        if (
            len(self._recent_tool_names) >= WINDOW
            and budget_used > 1 / 3
        ):
            extract_turns = sum(
                1
                for names in self._recent_tool_names
                if any(n in self._EXTRACT_TOOLS for n in names)
            )
            if extract_turns / len(self._recent_tool_names) <= NO_EXTRACT_RATIO:
                domains = {
                    self._domain_of(u) for u in self._recent_urls if u
                }
                nudge = (
                    f"[STUCK_NO_EXTRACT] In your last "
                    f"{len(self._recent_tool_names)} turns you have only "
                    f"called a content-read tool {extract_turns} time(s) "
                    f"(target: every other turn). You've been navigating/"
                    f"clicking across {len(domains)} domain(s) without "
                    f"reading much. The data you need is on the page "
                    f"already — call page_text or get_text NOW to read "
                    f"it, or call done with the best answer you have. "
                    f"More navigation is unlikely to help."
                )
                self._messages.append(UserMessage(content=nudge))
                logger.info(
                    "agent: STUCK_NO_EXTRACT fired at step %d "
                    "(extracts=%d/%d, domains=%d)",
                    step_n, extract_turns, len(self._recent_tool_names),
                    len(domains),
                )
                self._loop_nudge_cooldown = COOLDOWN_STEPS

    def _maybe_inject_single_action_hint(
        self,
        tool_calls: list[ToolCall],
        results: list[ActionResult],
    ) -> None:
        """If a multi-action turn went badly (>50% errors AND no extract),
        hint the LLM to drop back to one action per turn next time.

        Rationale from v0.4.12 eval data: when the LLM batches 3-4
        actions and the first one errors (e.g. clicked a stale index
        because the page changed), the rest of the batch usually errors
        too because they share the same assumption about page state.
        Having the LLM pause for the next turn's snapshot before acting
        again breaks the cascade.
        """
        # Single-action turns can't be the problem — they're already
        # what the hint would recommend.
        if len(tool_calls) <= 1:
            return

        error_count = sum(1 for r in results if r.error)
        had_extract = any(
            tc.name in self._EXTRACT_TOOLS for tc in tool_calls
        )
        if had_extract or error_count <= len(tool_calls) // 2:
            return

        nudge = (
            f"[BATCH_FAILED] {error_count} of {len(tool_calls)} actions "
            f"in your last turn errored. Multi-action turns work when "
            f"the actions are independent or pre-planned; when one fails "
            f"the rest usually fail too because they assumed the page "
            f"state would advance. For the NEXT turn, emit ONE tool "
            f"call only so its result can inform the next decision. "
            f"Resume batching only after you confirm the page is in the "
            f"expected state."
        )
        self._messages.append(UserMessage(content=nudge))
        logger.info(
            "agent: BATCH_FAILED hint fired (errors=%d/%d, batch=%s)",
            error_count, len(tool_calls), [tc.name for tc in tool_calls],
        )

    @staticmethod
    def _domain_of(url: str) -> str:
        """Crude eTLD+1 extractor — `https://www.foo.bar.com/x` → `bar.com`.

        Doesn't bother with the public-suffix list because the loop
        nudge only needs a stable bucket per site, not RFC-correctness.
        """
        if not url:
            return ""
        try:
            from urllib.parse import urlparse

            host = (urlparse(url).hostname or "").lower()
            if not host:
                return ""
            parts = host.split(".")
            if len(parts) >= 2:
                return ".".join(parts[-2:])
            return host
        except Exception:
            return url

    async def _capture_state(self) -> BrowserStateSummary:
        """Pre-step snapshot stored in history for the judge / callbacks.

        Always captures a screenshot AND a DOM snapshot regardless of
        `use_vision` — matching browser_use, where `use_vision` controls
        whether the LLM *sees* the image, not whether it lands in history.
        Eval consumers iterate `history.history[i].state.get_screenshot()`
        to feed judges, and most run with `use_vision=False`, so gating
        capture on it produced empty judge inputs.

        The DOM text lands on `summary.elements_text` so
        `_inject_page_state` can prepend it to the next LLM call without
        a second snapshot round trip.
        """
        # All three CDP roundtrips run in parallel — they don't depend on
        # each other. Sequentially they were ~150-300ms of fixed overhead
        # per step (current_url + screenshot + dom_snapshot); together
        # they finish in roughly the slowest single call's time. On a
        # 35-turn run that's 30+ saved seconds per task.
        async def _safe_url() -> str:
            try:
                return await self.session.current_url()
            except Exception:
                return ""

        async def _safe_screenshot() -> str | None:
            try:
                png = await self.session.screenshot()
                return base64.b64encode(png).decode("ascii")
            except Exception:
                return None

        async def _safe_dom() -> str:
            try:
                snap = await self.session.dom_snapshot()
                return snap.to_llm_string()
            except Exception:
                # No active page yet (very first step before
                # initial_actions navigate, or a frame transition
                # mid-step). Skip — the LLM gets a "(no page state
                # available)" placeholder instead of crashing the run.
                return ""

        url, screenshot_b64, dom_text = await asyncio.gather(
            _safe_url(), _safe_screenshot(), _safe_dom()
        )

        return BrowserStateSummary(
            url=url,
            title="",
            screenshot=screenshot_b64,
            elements_text=dom_text,
        )

    def _inject_page_state(self, state: BrowserStateSummary) -> None:
        """Append a UserMessage with the current page state so the LLM sees
        the DOM without spending a turn on `dom_snapshot`. Older auto-injected
        snapshots are replaced with a one-line "superseded" placeholder so the
        conversation doesn't accumulate stale DOMs across long runs.

        When `use_vision=True` and we captured a screenshot, attach it as an
        ImagePart alongside the DOM text — same one-message shape upstream
        browser_use uses for state messages.
        """
        # Supersede any prior page-state messages. Their indices are stale
        # the moment a new snapshot lands; keeping them just inflates tokens.
        for msg in self._messages:
            if not isinstance(msg, UserMessage):
                continue
            if isinstance(msg.content, str) and msg.content.startswith(_PAGE_STATE_TAG):
                msg.content = _PAGE_STATE_SUPERSEDED
            elif isinstance(msg.content, list):
                # Mixed text+image state message from a prior vision-on step.
                first = msg.content[0] if msg.content else None
                if (
                    isinstance(first, TextPart)
                    and first.text.startswith(_PAGE_STATE_TAG)
                ):
                    msg.content = _PAGE_STATE_SUPERSEDED

        dom_text = state.elements_text or ""
        if not dom_text:
            body = (
                f"{_PAGE_STATE_TAG}\nURL: {state.url}\n"
                "(no DOM snapshot available — page not ready)"
            )
        else:
            body = f"{_PAGE_STATE_TAG}\n{dom_text}"

        if self.use_vision and state.screenshot:
            self._messages.append(
                UserMessage(
                    content=[
                        TextPart(text=body),
                        ImagePart(data=state.screenshot, media_type="image/png"),
                    ]
                )
            )
        else:
            self._messages.append(UserMessage(content=body))

    async def _run_tools_sequentially(
        self, tool_calls: list[ToolCall]
    ) -> list[tuple[ActionResult, ToolResultMessage]]:
        """Execute tool calls in order with a page-change guard.

        Mirrors upstream `multi_act`: after each call, we check whether
        the active URL changed. If it did and there are more calls
        queued, we mark each remaining call as skipped (with a clear
        error explaining why) instead of running it against indices
        that were valid for the previous page. The skipped tool
        messages MUST still be emitted because the LLM provider expects
        a result for every tool call it issued — omitting them produces
        a "no result for tool_use_id" error on the next turn.
        """
        if not tool_calls:
            return []

        async def _current_url() -> str:
            try:
                return await self.session.current_url()
            except Exception:
                return ""

        # Tools that read state without changing the page — safe to keep
        # running even after a navigation in the same batch since they
        # don't depend on the pre-batch DOM indices.
        READ_ONLY_TOOLS = {
            "screenshot",
            "page_text",
            "get_text",
            "get_links",
            "list_tabs",
            "list_downloads",
            "get_cookies",
            "save_pdf",
            "done",
        }

        start_url = await _current_url()
        results: list[tuple[ActionResult, ToolResultMessage]] = []

        for i, tc in enumerate(tool_calls):
            pair = await self._run_tool(tc)
            results.append(pair)

            if i + 1 >= len(tool_calls):
                break

            cur_url = await _current_url()
            if cur_url == start_url:
                continue

            # Page changed. Skip remaining calls UNLESS they're read-only
            # (those don't rely on stale `[N]` indices).
            for skipped in tool_calls[i + 1 :]:
                if skipped.name in READ_ONLY_TOOLS:
                    pair = await self._run_tool(skipped)
                    results.append(pair)
                else:
                    err = (
                        f"skipped: page navigated mid-batch "
                        f"({start_url!r} → {cur_url!r}). The `[N]` indices "
                        f"in your tool calls were from the old page; "
                        f"re-plan on the next turn using the fresh "
                        f"snapshot."
                    )
                    results.append(
                        (
                            ActionResult(error=err),
                            ToolResultMessage(
                                tool_call_id=skipped.id,
                                name=skipped.name,
                                content=err,
                                is_error=True,
                            ),
                        )
                    )
            break

        return results

    async def _run_tool(
        self, tc: ToolCall
    ) -> tuple[ActionResult, ToolResultMessage]:
        tool = self.tools_by_name.get(tc.name)
        if tool is None:
            err = f"unknown tool: {tc.name}"
            return (
                ActionResult(error=err),
                ToolResultMessage(
                    tool_call_id=tc.id, name=tc.name, content=err, is_error=True
                ),
            )

        real_args = _expand_secrets(tc.args, self.sensitive_data)
        try:
            # Per-tool timeout. Without it, a single hung CDP op (e.g.
            # `scroll` against a page stuck on a network request) can
            # consume the eval framework's entire stage budget and kill
            # the run with a bare TimeoutError. Returning the timeout as
            # an ActionResult error lets the LLM react and try a
            # different tool / element on the next turn.
            raw = await asyncio.wait_for(
                tool.func(self.session, **real_args),
                timeout=self.tool_timeout,
            )
        except asyncio.TimeoutError:
            err = (
                f"tool timed out after {self.tool_timeout:.0f}s "
                f"(call: {tc.name}({tc.args})). The page may be stuck "
                f"loading or the element unresponsive. Try a different "
                f"approach next turn — wait_for_navigation, switch tabs, "
                f"or pick a different element."
            )
            logger.info(
                "agent: tool timeout %s(%s) after %.0fs",
                tc.name, tc.args, self.tool_timeout,
            )
            return (
                ActionResult(error=err),
                ToolResultMessage(
                    tool_call_id=tc.id, name=tc.name, content=err, is_error=True
                ),
            )
        except Exception as e:
            err = f"tool error: {type(e).__name__}: {e}"
            err = _redact_secrets(err, self.sensitive_data)
            return (
                ActionResult(error=err),
                ToolResultMessage(
                    tool_call_id=tc.id, name=tc.name, content=err, is_error=True
                ),
            )

        # Map return value into ContentParts the LLM layer understands.
        content_parts, summary_text = _format_tool_return(raw, self.sensitive_data)
        return (
            ActionResult(extracted_content=summary_text),
            ToolResultMessage(
                tool_call_id=tc.id, name=tc.name, content=content_parts
            ),
        )

    def _append_history(
        self,
        state: BrowserStateSummary,
        output: AgentOutput,
        results: list[ActionResult],
        t_start: float,
        step_n: int,
    ) -> None:
        last = self.usage_log[-1] if self.usage_log else None
        metadata = StepMetadata(
            step_number=step_n,
            input_tokens=(last["input"] if last else 0),
            output_tokens=(last["output"] if last else 0),
            cache_read_tokens=(last["cache_read"] if last else 0),
            step_start_time=t_start,
            step_end_time=time.monotonic(),
        )
        self.state.history.history.append(
            AgentHistory(state=state, output=output, result=results, metadata=metadata)
        )

    def _record_usage(self, step_n: int, usage) -> None:
        self.usage_log.append(
            {
                "step": step_n,
                "input": usage.input,
                "output": usage.output,
                "cache_read": usage.cache_read,
                "cache_creation": usage.cache_creation,
            }
        )
        self.state.history.usage = self.state.history.usage + usage

    async def _should_stop(self) -> bool:
        if self.register_should_stop_callback is None:
            return False
        try:
            return bool(await self.register_should_stop_callback())
        except Exception:
            return False


# ---------------------------------------------------------------------------
# helpers


class _MessageManagerView:
    """Thin pass-through so `agent.message_manager.last_input_messages`
    works the way browser_use consumers expect."""

    def __init__(self, agent: "Agent") -> None:
        self._agent = agent

    @property
    def last_input_messages(self) -> list[Message]:
        return list(self._agent._messages)


def _parse_judgement(text: str) -> dict[str, Any]:
    """Best-effort JSON extraction from a judge LLM response.

    Models sometimes wrap JSON in ```json fences or trailing prose; pull
    the first balanced object. Falls back to a not-judged-shape verdict
    on parse failure so downstream code doesn't crash.
    """
    import json as _json
    import re as _re

    fence = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=_re.S)
    if fence:
        candidate = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        candidate = text[start : end + 1] if start != -1 and end > start else "{}"
    try:
        parsed = _json.loads(candidate)
        if isinstance(parsed, dict):
            return {
                "verdict": bool(parsed.get("verdict", False)),
                "reasoning": str(parsed.get("reasoning", "")),
                "score": float(parsed.get("score", 0.0)),
                "impossible_task": bool(parsed.get("impossible_task", False)),
                "reached_captcha": bool(parsed.get("reached_captcha", False)),
            }
    except (ValueError, TypeError):
        pass
    return {
        "verdict": False,
        "reasoning": f"judge response could not be parsed: {text[:200]}",
        "score": 0.0,
        "impossible_task": False,
        "reached_captcha": False,
    }


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _format_tool_return(
    raw: Any, secrets: dict[str, str]
) -> tuple[list[ContentPart], str]:
    """Return (content_parts_for_llm, summary_text_for_history).

    Image returns are surfaced as ImagePart for providers that accept
    image-in-tool-result (Anthropic) and split into a follow-up user
    message by Gemini/OpenAI providers internally.
    """
    if isinstance(raw, dict) and raw.get("_type") == "image":
        return (
            [
                ImagePart(
                    data=raw["data"], media_type=raw.get("media_type", "image/png")
                )
            ],
            "[screenshot captured]",
        )
    text = raw if isinstance(raw, str) else str(raw)
    text = _redact_secrets(text, secrets)
    return ([TextPart(text=text)], text)


def _expand_secrets(value: Any, secrets: dict[str, str]) -> Any:
    """Replace placeholder substrings in tool args with real secret values
    BEFORE the tool runs. The model never sees the real value."""
    if not secrets:
        return value
    if isinstance(value, str):
        for placeholder, real in secrets.items():
            if placeholder in value:
                value = value.replace(placeholder, real)
        return value
    if isinstance(value, list):
        return [_expand_secrets(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: _expand_secrets(v, secrets) for k, v in value.items()}
    return value


def _redact_secrets(content: Any, secrets: dict[str, str]) -> Any:
    """Inverse: replace real values with placeholders before sending to the LLM."""
    if not secrets:
        return content
    if isinstance(content, str):
        for placeholder, real in secrets.items():
            if real and real in content:
                content = content.replace(real, placeholder)
        return content
    if isinstance(content, list):
        return [_redact_secrets(c, secrets) for c in content]
    if isinstance(content, dict):
        return {k: _redact_secrets(v, secrets) for k, v in content.items()}
    return content
