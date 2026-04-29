"""Anthropic Claude provider for the unified Agent loop.

Uses native `tool_use` blocks (not JSON-mode unions) and supports image
content blocks inside `tool_result` — that's where most of the speed/cost
win versus serialized prose comes from. Adaptive thinking and prompt
caching (system + tools breakpoint, plus a top-level breakpoint that
auto-caches the most recent message) are on by default.
"""

from __future__ import annotations

from typing import Any

import anthropic

from browser_use_rs.llm.base import (
    AssistantMessage,
    BaseChatModel,
    ChatInvokeCompletion,
    ChatInvokeUsage,
    ImagePart,
    Message,
    SystemMessage,
    TextPart,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from browser_use_rs.tools import Tool


def _content_blocks(content: str | list) -> list[dict]:
    """Convert our ContentPart list (or plain string) into Anthropic content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    out: list[dict] = []
    for part in content:
        if isinstance(part, TextPart):
            out.append({"type": "text", "text": part.text})
        elif isinstance(part, ImagePart):
            out.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": part.media_type,
                        "data": part.data,
                    },
                }
            )
    return out


def _to_anthropic_messages(messages: list[Message]) -> list[dict]:
    """Map our normalized history into Anthropic's `messages` array. Tool
    results are bundled into a single user turn after each assistant turn,
    matching Anthropic's expected interleaving."""
    out: list[dict] = []
    pending_tool_results: list[dict] = []

    def flush_tool_results():
        if pending_tool_results:
            out.append({"role": "user", "content": list(pending_tool_results)})
            pending_tool_results.clear()

    for msg in messages:
        if isinstance(msg, SystemMessage):
            # System prompts are passed via the `system` kwarg; ignore here.
            continue
        if isinstance(msg, UserMessage):
            flush_tool_results()
            out.append({"role": "user", "content": _content_blocks(msg.content)})
        elif isinstance(msg, AssistantMessage):
            flush_tool_results()
            blocks: list[dict] = []
            if msg.text:
                blocks.append({"type": "text", "text": msg.text})
            for tc in msg.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": tc.args,
                    }
                )
            if blocks:
                out.append({"role": "assistant", "content": blocks})
        elif isinstance(msg, ToolResultMessage):
            block: dict[str, Any] = {
                "type": "tool_result",
                "tool_use_id": msg.tool_call_id,
                "content": _content_blocks(msg.content),
            }
            if msg.is_error:
                block["is_error"] = True
            pending_tool_results.append(block)

    flush_tool_results()
    return out


class ChatAnthropic(BaseChatModel):
    name = "anthropic"

    def __init__(
        self,
        model: str = "claude-opus-4-7",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 16000,
        temperature: float | None = None,
        thinking: dict | None = None,
        effort: str | None = "xhigh",
        timeout: float | None = None,
        client: anthropic.AsyncAnthropic | None = None,
    ):
        self.model = model
        self.max_tokens = max_tokens
        self.temperature = temperature
        # Adaptive thinking is on by default for Opus 4.7 — the agent
        # benefits from elastic reasoning budgets per turn.
        self.thinking = thinking if thinking is not None else {"type": "adaptive"}
        self.effort = effort
        self.timeout = timeout
        if client is not None:
            self.client = client
        else:
            kwargs: dict[str, Any] = {}
            if api_key is not None:
                kwargs["api_key"] = api_key
            if base_url is not None:
                kwargs["base_url"] = base_url
            if timeout is not None:
                kwargs["timeout"] = timeout
            self.client = anthropic.AsyncAnthropic(**kwargs)

    async def ainvoke(
        self,
        messages: list[Message],
        tools: list[Tool],
        *,
        system: str | None = None,
    ) -> ChatInvokeCompletion:
        anthropic_msgs = _to_anthropic_messages(messages)
        tool_defs = [t.to_anthropic() for t in tools]

        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": self.max_tokens,
            # Top-level cache_control auto-caches the most recent cacheable
            # block (the latest message). Combined with the explicit
            # breakpoint on the system prompt, two breakpoints accumulate
            # the cache turn by turn.
            "cache_control": {"type": "ephemeral"},
            "tools": tool_defs,
            "messages": anthropic_msgs,
        }
        if system:
            kwargs["system"] = [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
        if self.thinking:
            kwargs["thinking"] = self.thinking
        if self.effort:
            kwargs["output_config"] = {"effort": self.effort}
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature

        response = await self.client.messages.create(**kwargs)

        text_parts = [b.text for b in response.content if b.type == "text"]
        text = "\n".join(p for p in text_parts if p) or None
        tool_calls = [
            ToolCall(id=b.id, name=b.name, args=dict(b.input or {}))
            for b in response.content
            if b.type == "tool_use"
        ]
        usage = ChatInvokeUsage(
            input=response.usage.input_tokens or 0,
            output=response.usage.output_tokens or 0,
            cache_read=getattr(response.usage, "cache_read_input_tokens", 0) or 0,
            cache_creation=getattr(response.usage, "cache_creation_input_tokens", 0) or 0,
        )
        return ChatInvokeCompletion(
            text=text, tool_calls=tool_calls, usage=usage, raw=response
        )
