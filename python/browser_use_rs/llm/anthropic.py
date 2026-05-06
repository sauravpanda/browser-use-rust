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

    @staticmethod
    def _model_supports_adaptive_thinking(model: str) -> bool:
        """Whether Anthropic's `thinking={"type": "adaptive"}` extended-
        thinking config is accepted on this model.

        Conservative allow-list: only enable on Opus 4.x and Sonnet 4.5+.
        Anthropic returns 400 "adaptive thinking is not supported on this
        model" for everything else (Haiku 4.5, Claude 3.x, older Sonnet).
        Caller can always force the kwarg via `thinking={...}` explicitly.
        """
        if not model:
            return False
        m = model.lower()
        # Opus 4.x family — all currently support adaptive
        if "opus-4" in m:
            return True
        # Sonnet 4.5 / 4.6 / 4.7+ — adaptive supported. 4.0 is a grey area;
        # err conservative and only enable on 4.5+.
        if "sonnet-4-5" in m or "sonnet-4-6" in m or "sonnet-4-7" in m:
            return True
        # Everything else (Haiku, Claude 3.x, plain sonnet-4 / opus-3,
        # bedrock variants without versioned suffix) → no adaptive.
        return False

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
        # Adaptive thinking is enabled per-default ONLY for models that
        # support it. Anthropic returns 400
        # "adaptive thinking is not supported on this model" for models
        # outside Opus 4.x and Sonnet 4.5+. Caller can always override
        # by passing `thinking=...` explicitly, including
        # `thinking=False` / `thinking={}` to force-disable.
        if thinking is not None:
            self.thinking = thinking
        elif self._model_supports_adaptive_thinking(model):
            self.thinking = {"type": "adaptive"}
        else:
            self.thinking = None
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
            "tools": tool_defs,
            "messages": anthropic_msgs,
        }
        # Caching: tag content blocks with cache_control=ephemeral.
        # Anthropic supports up to 4 cache breakpoints per request and
        # we use 3 (system prompt + agent_history + last message) to
        # maximize prefix-cache hit rate.
        #
        # Earlier code passed `cache_control` as a TOP-LEVEL kwarg to
        # client.messages.create(); the Anthropic SDK rejects that
        # (TypeError: unexpected keyword argument 'cache_control').
        # cache_control only belongs on individual content blocks.

        def _attach_cc(msg: dict) -> bool:
            """Tag the last cacheable block of `msg` with ephemeral
            cache_control. Returns True if a tag was placed.
            """
            content = msg.get("content")
            if isinstance(content, list) and content:
                for blk in reversed(content):
                    if isinstance(blk, dict) and blk.get("type") in (
                        "text", "tool_result", "image", "document",
                    ):
                        blk["cache_control"] = {"type": "ephemeral"}
                        return True
            elif isinstance(content, str) and content:
                msg["content"] = [
                    {
                        "type": "text",
                        "text": content,
                        "cache_control": {"type": "ephemeral"},
                    }
                ]
                return True
            return False

        # v0.11.20 cache breakpoint #2: agent_history block.
        # _collapse_old_history (agent/__init__.py:~2492) injects/refreshes
        # a single UserMessage at index 1 whose first text block starts
        # with "[AGENT_HISTORY]". That block contains collapsed one-line
        # summaries of prior turns and is the slow-changing tail of the
        # cacheable prefix — most steps either don't touch it, or only
        # append new collapsed summaries. Caching here lets the bulk of
        # history hit cache_read on every step instead of getting
        # re-evaluated as cache_creation when the prompt ages out.
        # Per codex's v0.11.20 recommendation: pure architectural
        # change, no behavior shift. Success criterion is cost/M
        # tokens decreasing while step/success/output stay flat.
        for msg in anthropic_msgs:
            content = msg.get("content")
            first_text = ""
            if isinstance(content, list) and content:
                first = content[0]
                if isinstance(first, dict) and first.get("type") == "text":
                    first_text = first.get("text", "") or ""
            elif isinstance(content, str):
                first_text = content
            if first_text.startswith("[AGENT_HISTORY]"):
                _attach_cc(msg)
                break  # only one AGENT_HISTORY message per call

        # v0.11.20 cache breakpoint #3: last message (existing behavior).
        # Tagging the last block ensures the cache extends through the
        # most recent turn so the next call's prefix can hit cache_read
        # on everything up to the prior step.
        if anthropic_msgs:
            _attach_cc(anthropic_msgs[-1])
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
            # output_config is a newer Anthropic API surface that older
            # versions of the python SDK don't recognise as a kwarg
            # (TypeError: unexpected keyword argument 'output_config').
            # Route through extra_body so the SDK forwards it untouched
            # to the server, which always accepts it for models that
            # support extended thinking effort.
            kwargs.setdefault("extra_body", {})["output_config"] = {
                "effort": self.effort,
            }
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature

        from browser_use_rs.llm.base import with_retry

        async def _call():
            return await self.client.messages.create(**kwargs)

        response = await with_retry(_call, label=f"anthropic({self.model})")

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
