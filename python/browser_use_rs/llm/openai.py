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
        timeout: float | None = None,
        client: AsyncOpenAI | None = None,
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
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

    async def ainvoke(
        self,
        messages: list[Message],
        tools: list[Tool],
        *,
        system: str | None = None,
    ) -> ChatInvokeCompletion:
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
        if self.temperature is not None:
            kwargs["temperature"] = self.temperature
        if self.max_tokens is not None:
            kwargs["max_tokens"] = self.max_tokens

        response = await self.client.chat.completions.create(**kwargs)
        choice = response.choices[0]
        msg = choice.message

        text = msg.content or None
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
            text=text, tool_calls=tool_calls, usage=usage, raw=response
        )
