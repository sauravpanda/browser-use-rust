"""Agent loop: drives `claude-opus-4-7` against a tool set with native tool
calling, adaptive thinking, prompt-cached system + tool definitions, and
parallel concurrent tool execution.

Manual loop (not the SDK tool runner) so we can return image content blocks
from tools like `screenshot` — the runner currently only surfaces strings.
"""

from __future__ import annotations

import asyncio
from typing import Any

import anthropic

from browser_use_rs._native import BrowserSession
from browser_use_rs.tools import Tool

SYSTEM_PROMPT = """\
You are a browser-use agent. You control a real Chromium browser through a small
set of tools and complete the user's task by calling them.

Tools:
- dom_snapshot(): returns a numbered list of interactive elements on the
  current page. Always snapshot before clicking or typing — element indices
  are NOT stable across page changes.
- navigate(url): go to a URL.
- click(index): click element [N] from the most recent dom_snapshot.
- type_text(index, text): type text into element [N]. The input is focused
  first (no need to click separately). Inputs are not auto-submitted.
- scroll(dy): scroll the page by dy CSS pixels (positive = down).
- screenshot(): capture a PNG of the current viewport. You will see the image.

Strategy:
- Snapshot the page, then act. Re-snapshot after any action that changes the
  page (navigate, click, type, scroll).
- Prefer clicking visible links over navigating to known URLs — that verifies
  the page is in the expected state.
- When the page is unfamiliar or text is ambiguous, take a screenshot and
  read the rendered page.
- When the task is complete, respond with a final answer in plain text. Do
  NOT call any further tools — your text turn is the answer.
"""


class Agent:
    def __init__(
        self,
        task: str,
        tools: list[Tool],
        *,
        model: str = "claude-opus-4-7",
        effort: str = "xhigh",
        max_steps: int = 30,
        max_tokens: int = 16000,
        client: anthropic.AsyncAnthropic | None = None,
        system_prompt: str = SYSTEM_PROMPT,
    ):
        self.task = task
        self.tools_by_name: dict[str, Tool] = {t.name: t for t in tools}
        self.model = model
        self.effort = effort
        self.max_steps = max_steps
        self.max_tokens = max_tokens
        self.client = client or anthropic.AsyncAnthropic()
        self.system_prompt = system_prompt
        self.session = BrowserSession()
        self.usage_log: list[dict] = []

    async def run(self) -> str:
        await self.session.start()
        try:
            return await self._loop()
        finally:
            await self.session.stop()

    async def _loop(self) -> str:
        messages: list[dict] = [{"role": "user", "content": self.task}]
        tool_defs = [t.to_anthropic() for t in self.tools_by_name.values()]
        system = [
            {
                "type": "text",
                "text": self.system_prompt,
                "cache_control": {"type": "ephemeral"},
            }
        ]

        for step in range(self.max_steps):
            response = await self.client.messages.create(
                model=self.model,
                max_tokens=self.max_tokens,
                # Top-level auto-caches the last cacheable block (the most
                # recent message), so the conversation prefix grows the cache
                # incrementally turn by turn. Combined with the manual
                # breakpoint on `system`, that's 2 breakpoints: tools+system,
                # and tools+system+messages-so-far.
                cache_control={"type": "ephemeral"},
                system=system,
                tools=tool_defs,
                thinking={"type": "adaptive"},
                output_config={"effort": self.effort},
                messages=messages,
            )
            self._log_usage(step, response.usage)

            tool_uses = [b for b in response.content if b.type == "tool_use"]
            if response.stop_reason == "end_turn" or not tool_uses:
                return _extract_text(response.content)

            messages.append({"role": "assistant", "content": response.content})

            # Run all tool_use blocks concurrently. The model emits multiple
            # tool calls per turn deliberately (parallel tool calling) — we
            # honor that with asyncio.gather. Tool implementations are
            # responsible for any locking they need against shared state.
            tool_results = await asyncio.gather(
                *(self._run_tool(tu) for tu in tool_uses)
            )
            messages.append({"role": "user", "content": list(tool_results)})

        return f"hit max_steps={self.max_steps} without final answer"

    async def _run_tool(self, tu) -> dict:
        tool = self.tools_by_name.get(tu.name)
        if tool is None:
            return {
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": f"unknown tool: {tu.name}",
                "is_error": True,
            }
        try:
            raw = await tool.func(self.session, **tu.input)
            return {
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": _format_result(raw),
            }
        except Exception as e:
            return {
                "type": "tool_result",
                "tool_use_id": tu.id,
                "content": f"tool error: {type(e).__name__}: {e}",
                "is_error": True,
            }

    def _log_usage(self, step: int, usage) -> None:
        self.usage_log.append(
            {
                "step": step,
                "input": usage.input_tokens,
                "output": usage.output_tokens,
                "cache_read": getattr(usage, "cache_read_input_tokens", 0),
                "cache_creation": getattr(usage, "cache_creation_input_tokens", 0),
            }
        )


def _extract_text(content) -> str:
    parts = [b.text for b in content if b.type == "text"]
    return "\n".join(p for p in parts if p)


def _format_result(result: Any):
    """Map a tool's return value to Anthropic tool_result content."""
    if isinstance(result, dict) and result.get("_type") == "image":
        return [
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": result["media_type"],
                    "data": result["data"],
                },
            }
        ]
    if isinstance(result, str):
        return result
    return str(result)
