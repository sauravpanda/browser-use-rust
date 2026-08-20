"""OpenAI provider for the unified Agent loop.

Uses the chat completions API with native function calling. Like Gemini,
OpenAI doesn't accept image content inside `tool` (tool_result) messages,
so the agent's `screenshot`-style tools surface the image as a separate
user-message part right after the tool result.
"""

from __future__ import annotations

import os
from typing import Any

from openai import AsyncOpenAI

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


def _clean_schema(schema: Any) -> Any:
    """OpenAI's strict mode requires `additionalProperties: false` and
    rejects some draft features. We accept either strict or non-strict;
    leave the schema mostly intact and only normalize required fields."""
    if isinstance(schema, dict):
        out = {k: _clean_schema(v) for k, v in schema.items() if k != "default"}
        return out
    if isinstance(schema, list):
        return [_clean_schema(x) for x in schema]
    return schema


def _user_content(content: str | list) -> str | list[dict]:
    if isinstance(content, str):
        return content
    parts: list[dict] = []
    for p in content:
        if isinstance(p, TextPart):
            parts.append({"type": "text", "text": p.text})
        elif isinstance(p, ImagePart):
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{p.media_type};base64,{p.data}",
                    },
                }
            )
    return parts


def _to_openai_messages(
    messages: list[Message], system: str | None
) -> list[dict]:
    out: list[dict] = []
    if system:
        out.append({"role": "system", "content": system})
    pending_extra_user_parts: list[dict] = []

    for msg in messages:
        if isinstance(msg, SystemMessage):
            out.append({"role": "system", "content": msg.content})
        elif isinstance(msg, UserMessage):
            if pending_extra_user_parts:
                out.append({"role": "user", "content": pending_extra_user_parts})
                pending_extra_user_parts = []
            out.append({"role": "user", "content": _user_content(msg.content)})
        elif isinstance(msg, AssistantMessage):
            entry: dict[str, Any] = {"role": "assistant"}
            if msg.text:
                entry["content"] = msg.text
            if msg.tool_calls:
                import json as _json

                entry["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": _json.dumps(tc.args),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            if "content" not in entry:
                entry["content"] = None
            out.append(entry)
        elif isinstance(msg, ToolResultMessage):
            text_chunks: list[str] = []
            image_parts: list[dict] = []
            if isinstance(msg.content, str):
                text_chunks.append(msg.content)
            else:
                for p in msg.content:
                    if isinstance(p, TextPart):
                        text_chunks.append(p.text)
                    elif isinstance(p, ImagePart):
                        image_parts.append(
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{p.media_type};base64,{p.data}",
                                },
                            }
                        )
            if image_parts and not text_chunks:
                text_chunks.append("image attached as next user-message part")
            tool_text = "\n".join(text_chunks) or "ok"
            out.append(
                {
                    "role": "tool",
                    "tool_call_id": msg.tool_call_id,
                    "content": tool_text,
                }
            )
            if image_parts:
                pending_extra_user_parts.extend(image_parts)

    if pending_extra_user_parts:
        out.append({"role": "user", "content": pending_extra_user_parts})
    return out


def _to_responses_input(messages: list[Message]) -> list[dict]:
    """Map the normalized history onto the Responses API's input items.

    Assistant tool calls become `function_call` items and tool results
    become `function_call_output` items (both keyed by call_id). Images
    inside tool results are surfaced as a follow-up user message, same
    as the chat-completions mapping above.
    """
    import json as _json

    out: list[dict] = []
    pending_image_parts: list[dict] = []

    def flush_images() -> None:
        if pending_image_parts:
            out.append({"role": "user", "content": list(pending_image_parts)})
            pending_image_parts.clear()

    for msg in messages:
        if isinstance(msg, SystemMessage):
            out.append({"role": "system", "content": msg.content})
        elif isinstance(msg, UserMessage):
            flush_images()
            if isinstance(msg.content, str):
                out.append({"role": "user", "content": msg.content})
                continue
            parts: list[dict] = []
            for p in msg.content:
                if isinstance(p, TextPart):
                    parts.append({"type": "input_text", "text": p.text})
                elif isinstance(p, ImagePart):
                    parts.append(
                        {
                            "type": "input_image",
                            "image_url": f"data:{p.media_type};base64,{p.data}",
                        }
                    )
            out.append({"role": "user", "content": parts})
        elif isinstance(msg, AssistantMessage):
            if msg.text:
                out.append(
                    {
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": msg.text}],
                    }
                )
            for tc in msg.tool_calls:
                out.append(
                    {
                        "type": "function_call",
                        "call_id": tc.id,
                        "name": tc.name,
                        "arguments": _json.dumps(tc.args),
                    }
                )
        elif isinstance(msg, ToolResultMessage):
            text_chunks: list[str] = []
            if isinstance(msg.content, str):
                text_chunks.append(msg.content)
            else:
                for p in msg.content:
                    if isinstance(p, TextPart):
                        text_chunks.append(p.text)
                    elif isinstance(p, ImagePart):
                        pending_image_parts.append(
                            {
                                "type": "input_image",
                                "image_url": f"data:{p.media_type};base64,{p.data}",
                            }
                        )
            if pending_image_parts and not text_chunks:
                text_chunks.append("image attached as next user-message part")
            out.append(
                {
                    "type": "function_call_output",
                    "call_id": msg.tool_call_id,
                    "output": "\n".join(text_chunks) or "ok",
                }
            )
    flush_images()
    return out


class ChatOpenAI(BaseChatModel):
    name = "openai"

    def __init__(
        self,
        model: str = "gpt-5",
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        # Reasoning-model knobs (v0.12.17). Upstream browser_use's
        # ChatOpenAI defaults reasoning models to effort='low' with
        # max_completion_tokens=4096 and drops temperature; callers that
        # want parity with it (eval harnesses) must set these explicitly
        # — we default to None so the OpenAI server defaults apply.
        reasoning_effort: str | None = None,
        max_completion_tokens: int | None = None,
        # Route through /v1/responses instead of /v1/chat/completions.
        # Required for models that reject function tools combined with
        # reasoning on chat completions (gpt-5.6-luna: "Function tools
        # with reasoning_effort are not supported ... use /v1/responses").
        use_responses_api: bool = False,
        timeout: float | None = None,
        client: AsyncOpenAI | None = None,
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.reasoning_effort = reasoning_effort
        self.max_completion_tokens = max_completion_tokens
        self.use_responses_api = use_responses_api
        self.timeout = timeout
        if client is not None:
            self.client = client
        else:
            kwargs: dict[str, Any] = {}
            if api_key is not None:
                kwargs["api_key"] = api_key
            elif os.getenv("OPENAI_API_KEY"):
                kwargs["api_key"] = os.getenv("OPENAI_API_KEY")
            if base_url is not None:
                kwargs["base_url"] = base_url
            if timeout is not None:
                kwargs["timeout"] = timeout
            self.client = AsyncOpenAI(**kwargs)

    @staticmethod
    def _chat_text(msg: Any) -> tuple[str | None, str]:
        """Extract (text, source) from a chat-completions message.

        OpenRouter (and DeepSeek-style OpenAI-compatible providers) return
        the model's thinking in a separate reasoning field and leave content
        empty on tool-call turns. Without the fallback every journal line
        renders with blank model_text, which destroys render-mode
        continuity — observed as qwen3.8 repeating one identical navigate
        for 80+ steps. v0.12.29; source marker added in v0.12.30 so the
        agent never commits recovered reasoning as a final answer.
        """
        text = msg.content or None
        if text:
            return text, "content"
        extra = getattr(msg, "model_extra", None) or {}
        reasoning = (
            getattr(msg, "reasoning", None)
            or getattr(msg, "reasoning_content", None)
            or extra.get("reasoning")
            or extra.get("reasoning_content")
        )
        if isinstance(reasoning, str) and reasoning.strip():
            return reasoning.strip()[:2000], "reasoning"
        return None, "content"

    @staticmethod
    def _map_tool_choice(tool_choice: str | dict | None) -> Any:
        """Translate the provider-agnostic tool_choice into the OpenAI
        chat-completions encoding. Unknown values → None (auto)."""
        if tool_choice in (None, "auto"):
            return None
        if tool_choice in ("required", "none"):
            return tool_choice
        if isinstance(tool_choice, dict) and tool_choice.get("name"):
            return {
                "type": "function",
                "function": {"name": tool_choice["name"]},
            }
        return None

    @staticmethod
    def _map_responses_tool_choice(tool_choice: str | dict | None) -> Any:
        """Responses API uses a flat function reference (no nested key)."""
        if tool_choice in (None, "auto"):
            return None
        if tool_choice in ("required", "none"):
            return tool_choice
        if isinstance(tool_choice, dict) and tool_choice.get("name"):
            return {"type": "function", "name": tool_choice["name"]}
        return None

    async def _ainvoke_responses(
        self,
        messages: list[Message],
        tools: list[Tool],
        *,
        system: str | None = None,
        tool_choice: str | dict | None = None,
    ) -> ChatInvokeCompletion:
        import json as _json

        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": _to_responses_input(messages),
            "tools": [
                {
                    "type": "function",
                    "name": t.name,
                    "description": t.description,
                    "parameters": _clean_schema(t.input_schema),
                }
                for t in tools
            ],
            # Stateless: the agent replays the full conversation each call,
            # so nothing is persisted server-side.
            "store": False,
        }
        if system:
            kwargs["instructions"] = system
        if self.reasoning_effort is not None:
            kwargs["reasoning"] = {"effort": self.reasoning_effort}
        elif self.temperature is not None:
            # Reasoning requests reject sampling params; only send
            # temperature when reasoning is not configured.
            kwargs["temperature"] = self.temperature
        cap = self.max_completion_tokens or self.max_tokens
        if cap is not None:
            kwargs["max_output_tokens"] = cap
        mapped_choice = self._map_responses_tool_choice(tool_choice)
        if mapped_choice is not None:
            kwargs["tool_choice"] = mapped_choice

        from browser_use_rs.llm.base import with_retry

        async def _call():
            return await self.client.responses.create(**kwargs)

        response = await with_retry(_call, label=f"openai-responses({self.model})")

        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for item in response.output or []:
            item_type = getattr(item, "type", "")
            if item_type == "function_call":
                try:
                    args = _json.loads(item.arguments or "{}")
                except _json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    ToolCall(id=item.call_id, name=item.name, args=args)
                )
            elif item_type == "message":
                for c in getattr(item, "content", None) or []:
                    if getattr(c, "type", "") == "output_text" and c.text:
                        text_parts.append(c.text)
        usage_obj = getattr(response, "usage", None)
        cached = getattr(
            getattr(usage_obj, "input_tokens_details", None), "cached_tokens", 0
        ) or 0
        usage = ChatInvokeUsage(
            input=getattr(usage_obj, "input_tokens", 0) or 0,
            output=getattr(usage_obj, "output_tokens", 0) or 0,
            cache_read=cached,
        )
        return ChatInvokeCompletion(
            text="\n".join(text_parts) or None,
            tool_calls=tool_calls,
            usage=usage,
            raw=response,
        )

    async def ainvoke(
        self,
        messages: list[Message],
        tools: list[Tool],
        *,
        system: str | None = None,
        tool_choice: str | dict | None = None,
    ) -> ChatInvokeCompletion:
        if self.use_responses_api:
            return await self._ainvoke_responses(
                messages, tools, system=system, tool_choice=tool_choice
            )
        openai_msgs = _to_openai_messages(messages, system)
        tool_defs = [
            {
                "type": "function",
                "function": {
                    "name": t.name,
                    "description": t.description,
                    "parameters": _clean_schema(t.input_schema),
                },
            }
            for t in tools
        ]

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": openai_msgs,
            "tools": tool_defs,
        }
        mapped_choice = self._map_tool_choice(tool_choice)
        if mapped_choice is not None:
            kwargs["tool_choice"] = mapped_choice
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens
        if self.reasoning_effort is not None:
            kwargs["reasoning_effort"] = self.reasoning_effort
        if self.max_completion_tokens is not None:
            kwargs["max_completion_tokens"] = self.max_completion_tokens

        from browser_use_rs.llm.base import with_retry

        async def _call():
            return await self.client.chat.completions.create(**kwargs)

        response = await with_retry(_call, label=f"openai({self.model})")
        choice = response.choices[0]
        msg = choice.message

        text, text_source = self._chat_text(msg)
        tool_calls: list[ToolCall] = []
        if msg.tool_calls:
            import json as _json

            for tc in msg.tool_calls:
                try:
                    args = _json.loads(tc.function.arguments or "{}")
                except _json.JSONDecodeError:
                    args = {}
                tool_calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, args=args)
                )

        usage = ChatInvokeUsage(
            input=getattr(response.usage, "prompt_tokens", 0) or 0,
            output=getattr(response.usage, "completion_tokens", 0) or 0,
            cache_read=getattr(
                getattr(response.usage, "prompt_tokens_details", None),
                "cached_tokens",
                0,
            )
            or 0,
        )
        return ChatInvokeCompletion(
            text=text,
            tool_calls=tool_calls,
            usage=usage,
            raw=response,
            text_source=text_source,
        )
