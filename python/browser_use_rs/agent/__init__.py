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
import time
from typing import Any, Awaitable, Callable

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

Strategy:
- Read the page snapshot, then act. After clicks/navigates the next turn's
  snapshot reflects the new page; indices are not stable across turns.
- Prefer clicking visible links over navigating to known URLs — that
  verifies the page is in the expected state.
- When the page is unfamiliar or text is ambiguous, take a screenshot.
- When the task is complete, respond with a final answer in plain text. Do
  NOT call any further tools — your text turn is the answer.
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

            # Run all tool calls concurrently — providers that emit parallel
            # calls (Anthropic) deserve the speedup. Tools serialize
            # internally if they touch shared state.
            results_and_msgs = await asyncio.gather(
                *(self._run_tool(tc) for tc in completion.tool_calls)
            )
            tool_results: list[ActionResult] = []
            for action_result, tool_message in results_and_msgs:
                tool_results.append(action_result)
                self._messages.append(tool_message)

            self._append_history(state_summary, output, tool_results, t0, step_n)

            if on_step_end is not None:
                await _maybe_await(on_step_end())

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
        url = ""
        try:
            url = await self.session.current_url()
        except Exception:
            pass

        screenshot_b64: str | None = None
        try:
            png = await self.session.screenshot()
            screenshot_b64 = base64.b64encode(png).decode("ascii")
        except Exception:
            screenshot_b64 = None

        dom_text = ""
        try:
            snap = await self.session.dom_snapshot()
            dom_text = snap.to_llm_string()
        except Exception:
            # No active page yet (very first step before initial_actions
            # navigate, or a frame transition mid-step). Skip — the LLM
            # gets a "(no page state available)" placeholder instead of
            # crashing the run.
            dom_text = ""

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
            raw = await tool.func(self.session, **real_args)
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
