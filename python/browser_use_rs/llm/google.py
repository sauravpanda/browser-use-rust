"""Google Gemini provider for the unified Agent loop.

Two quirks we work around:
- Gemini rejects `{type:"object", properties:{}}` — zero-arg tool schemas
  get a `_placeholder` string property added; the agent strips it from
  args before calling the tool.
- `function_response` doesn't accept image content. When a tool returns an
  image we send the FunctionResponse with a placeholder note, then attach
  the image as an `inline_data` part on the next user turn.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from google import genai
from google.genai import types as gtypes

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


_MEDIA_RESOLUTION_ALIASES: dict[str, str | None] = {
    "": None,
    "auto": None,
    "default": None,
    "none": None,
    "off": None,
    "unspecified": "MEDIA_RESOLUTION_UNSPECIFIED",
    "low": "MEDIA_RESOLUTION_LOW",
    "medium": "MEDIA_RESOLUTION_MEDIUM",
    "high": "MEDIA_RESOLUTION_HIGH",
    "ultra": "MEDIA_RESOLUTION_ULTRA_HIGH",
    "ultra_high": "MEDIA_RESOLUTION_ULTRA_HIGH",
    "ultrahigh": "MEDIA_RESOLUTION_ULTRA_HIGH",
    "media_resolution_unspecified": "MEDIA_RESOLUTION_UNSPECIFIED",
    "media_resolution_low": "MEDIA_RESOLUTION_LOW",
    "media_resolution_medium": "MEDIA_RESOLUTION_MEDIUM",
    "media_resolution_high": "MEDIA_RESOLUTION_HIGH",
    "media_resolution_ultra_high": "MEDIA_RESOLUTION_ULTRA_HIGH",
}


def _normalize_media_resolution(value: Any) -> Any:
    """Return a google-genai MediaResolution enum/string or None.

    Gemini's default image handling can spend far more media tokens than
    browser automation needs because the DOM snapshot is the primary state.
    We default ChatGoogle to LOW and let callers opt back up with
    media_resolution="medium"/"high" or Agent(vision_detail_level=...).
    """
    if value is None:
        return None
    if not isinstance(value, str):
        return value

    key = value.strip().lower().replace("-", "_")
    enum_name = _MEDIA_RESOLUTION_ALIASES.get(key)
    if enum_name is None:
        return None

    enum_cls = getattr(gtypes, "MediaResolution", None)
    if enum_cls is not None:
        enum_value = getattr(enum_cls, enum_name, None)
        if enum_value is not None:
            return enum_value
    return enum_name


def _modality_key(value: Any) -> str:
    raw = getattr(value, "name", None) or getattr(value, "value", None) or str(value)
    raw = raw.split(".")[-1].lower()
    raw = raw.removeprefix("media_modality_").removeprefix("modality_")
    out = []
    for ch in raw:
        out.append(ch if ch.isalnum() else "_")
    normalized = "".join(out).strip("_")
    return normalized or "unknown"


def _token_details(items: Any) -> dict[str, int]:
    details: dict[str, int] = {}
    for item in items or []:
        if isinstance(item, dict):
            modality = item.get("modality")
            count = item.get("token_count") or item.get("tokenCount") or 0
        else:
            modality = getattr(item, "modality", None)
            count = getattr(item, "token_count", None)
            if count is None:
                count = getattr(item, "tokenCount", 0)
        key = _modality_key(modality)
        details[key] = details.get(key, 0) + int(count or 0)
    return details


def _clean_schema(schema: Any, parent_key: str | None = None) -> Any:
    """Strip JSON Schema fields Gemini rejects and pad empty object types.

    Gemini errors on `additionalProperties` and on `{type:"object", properties:{}}`.
    `title` is metadata at the schema level (strip) but a property NAME
    when the parent is `properties` (keep).
    """
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for k, v in schema.items():
            if k in ("additionalProperties", "default"):
                continue
            if k == "title" and parent_key != "properties":
                continue
            out[k] = _clean_schema(v, parent_key=k)
        if (
            isinstance(out.get("type"), str)
            and out["type"].lower() == "object"
            and isinstance(out.get("properties"), dict)
            and len(out["properties"]) == 0
        ):
            out["properties"] = {"_placeholder": {"type": "string"}}
        return out
    if isinstance(schema, list):
        return [_clean_schema(x, parent_key=parent_key) for x in schema]
    return schema


def _user_parts(content: str | list) -> list[gtypes.Part]:
    if isinstance(content, str):
        return [gtypes.Part(text=content)]
    parts: list[gtypes.Part] = []
    for p in content:
        if isinstance(p, TextPart):
            parts.append(gtypes.Part(text=p.text))
        elif isinstance(p, ImagePart):
            parts.append(
                gtypes.Part(
                    inline_data=gtypes.Blob(
                        mime_type=p.media_type,
                        data=base64.b64decode(p.data),
                    )
                )
            )
    return parts


def _to_contents(messages: list[Message]) -> list[gtypes.Content]:
    """Map our normalized history into Gemini's `contents` array."""
    contents: list[gtypes.Content] = []
    pending_user_parts: list[gtypes.Part] = []

    def flush_user():
        if pending_user_parts:
            contents.append(gtypes.Content(role="user", parts=list(pending_user_parts)))
            pending_user_parts.clear()

    for msg in messages:
        if isinstance(msg, SystemMessage):
            continue
        if isinstance(msg, UserMessage):
            flush_user()
            contents.append(
                gtypes.Content(role="user", parts=_user_parts(msg.content))
            )
        elif isinstance(msg, AssistantMessage):
            flush_user()
            parts: list[gtypes.Part] = []
            if msg.text:
                parts.append(gtypes.Part(text=msg.text))
            for tc in msg.tool_calls:
                fc_kwargs: dict[str, Any] = {"name": tc.name, "args": tc.args}
                # Gemini 3+ rejects re-fed function_call parts that drop
                # the thought_signature. We stash it on read and replay.
                sig = tc.meta.get("thought_signature") if tc.meta else None
                part_kwargs: dict[str, Any] = {
                    "function_call": gtypes.FunctionCall(**fc_kwargs)
                }
                if sig:
                    part_kwargs["thought_signature"] = sig
                parts.append(gtypes.Part(**part_kwargs))
            if parts:
                contents.append(gtypes.Content(role="model", parts=parts))
        elif isinstance(msg, ToolResultMessage):
            text_chunks: list[str] = []
            image_parts: list[gtypes.Part] = []
            if isinstance(msg.content, str):
                text_chunks.append(msg.content)
            else:
                for p in msg.content:
                    if isinstance(p, TextPart):
                        text_chunks.append(p.text)
                    elif isinstance(p, ImagePart):
                        image_parts.append(
                            gtypes.Part(
                                inline_data=gtypes.Blob(
                                    mime_type=p.media_type,
                                    data=base64.b64decode(p.data),
                                )
                            )
                        )
            if image_parts and not text_chunks:
                text_chunks.append("image attached as next user-message part")
            response_value = "\n".join(text_chunks) or "ok"
            response_key = "error" if msg.is_error else "result"
            pending_user_parts.append(
                gtypes.Part(
                    function_response=gtypes.FunctionResponse(
                        name=msg.name,
                        response={response_key: response_value},
                    )
                )
            )
            pending_user_parts.extend(image_parts)

    flush_user()
    return contents


class ChatGoogle(BaseChatModel):
    name = "google"

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        *,
        api_key: str | None = None,
        temperature: float | None = None,
        max_output_tokens: int | None = None,
        thinking_level: str | None = None,
        thinking_budget: int | None = None,
        media_resolution: Any = "low",
        client: genai.Client | None = None,
        **_compat_kwargs: Any,
    ):
        self.model = model
        self.temperature = temperature
        # v0.8.18: mirror upstream browser_use's google/chat.py defaults
        # for gemini-3-flash. Without these the model defaults to
        # different (smaller) thinking_budget and a tight max_output
        # cap that truncates long answers / tool-call sequences. Set
        # only when the caller didn't already configure them, so
        # explicit overrides win.
        is_gemini_3 = "gemini-3" in (model or "").lower()
        if max_output_tokens is None and is_gemini_3:
            max_output_tokens = 8096
        if thinking_budget is None and thinking_level is None and is_gemini_3:
            # `-1` means "model decides" — upstream's default for Gemini 3.
            thinking_budget = -1
        self.max_output_tokens = max_output_tokens
        self.thinking_level = thinking_level
        self.thinking_budget = thinking_budget
        self.media_resolution = _normalize_media_resolution(media_resolution)
        self.client = client or genai.Client(
            api_key=api_key
            or os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY"),
        )

    def set_media_resolution(self, value: Any) -> None:
        self.media_resolution = _normalize_media_resolution(value)

    async def ainvoke(
        self,
        messages: list[Message],
        tools: list[Tool],
        *,
        system: str | None = None,
    ) -> ChatInvokeCompletion:
        gemini_tool = gtypes.Tool(
            function_declarations=[
                gtypes.FunctionDeclaration(
                    name=t.name,
                    description=t.description,
                    parameters=_clean_schema(t.input_schema),
                )
                for t in tools
            ]
        )
        config_kwargs: dict[str, Any] = {"tools": [gemini_tool]}
        if system:
            config_kwargs["system_instruction"] = system
        if self.temperature is not None:
            config_kwargs["temperature"] = self.temperature
        if self.max_output_tokens is not None:
            config_kwargs["max_output_tokens"] = self.max_output_tokens
        if self.media_resolution is not None:
            config_kwargs["media_resolution"] = self.media_resolution
        if self.thinking_level is not None or self.thinking_budget is not None:
            tc_kwargs: dict[str, Any] = {}
            if self.thinking_level is not None:
                # v0.8.22: explicitly convert to ThinkingLevel enum (upper-
                # case) before handing to the SDK. The eval framework
                # passes the level as a lowercase string ("medium"); the
                # SDK's `ThinkingLevel` is a CaseInSensitiveEnum with
                # uppercase canonicals (LOW / MEDIUM / HIGH / MINIMAL).
                # Pydantic *may* coerce "medium" → MEDIUM via the
                # case-insensitive matcher, but the behavior isn't
                # guaranteed across SDK versions, and our previous code
                # was passing the raw string, possibly silently no-op'ing
                # the thinking config. Upstream browser_use's
                # google/chat.py:266 does the same explicit conversion.
                level_val = self.thinking_level
                if isinstance(level_val, str):
                    try:
                        level_val = gtypes.ThinkingLevel(level_val.upper())
                    except ValueError:
                        # Unknown level (typo from caller); fall through
                        # with the raw string and let the SDK reject it.
                        pass
                tc_kwargs["thinking_level"] = level_val
            if self.thinking_budget is not None:
                tc_kwargs["thinking_budget"] = self.thinking_budget
            config_kwargs["thinking_config"] = gtypes.ThinkingConfig(**tc_kwargs)
        fields = getattr(gtypes.GenerateContentConfig, "model_fields", None)
        if (
            isinstance(fields, dict)
            and "media_resolution" in config_kwargs
            and "media_resolution" not in fields
        ):
            config_kwargs.pop("media_resolution", None)
        try:
            config = gtypes.GenerateContentConfig(**config_kwargs)
        except Exception as exc:
            # Older google-genai releases did not expose media_resolution.
            # Keep compatibility instead of failing the whole LLM call.
            if (
                "media_resolution" not in config_kwargs
                or "media_resolution" not in str(exc).lower()
            ):
                raise
            config_kwargs.pop("media_resolution", None)
            config = gtypes.GenerateContentConfig(**config_kwargs)

        contents = _to_contents(messages)
        # Wrap in transient-error retry. Gemini in particular hits
        # 503 UNAVAILABLE / "Overloaded" routinely under eval load,
        # and a single such error was killing whole agent runs prior
        # to v0.4.18. See base.with_retry for the retry policy.
        from browser_use_rs.llm.base import with_retry

        async def _call():
            return await self.client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=config,
            )

        response = await with_retry(_call, label=f"google({self.model})")

        candidate = response.candidates[0] if response.candidates else None
        parts = candidate.content.parts if candidate and candidate.content else []
        parts = parts or []
        text_parts = [p.text for p in parts if p.text]
        text = "\n".join(t for t in text_parts if t).strip() or None
        tool_calls: list[ToolCall] = []
        for p in parts:
            fc = p.function_call
            if not fc:
                continue
            args = dict(fc.args or {})
            args.pop("_placeholder", None)
            meta: dict[str, Any] = {}
            sig = getattr(p, "thought_signature", None)
            if sig:
                meta["thought_signature"] = sig
            tool_calls.append(
                ToolCall(
                    id=getattr(fc, "id", "") or fc.name,
                    name=fc.name,
                    args=args,
                    meta=meta,
                )
            )

        usage_meta = response.usage_metadata
        # v0.8.18: include thoughts_token_count in the output total.
        # Gemini 2.5+ separates "thinking" tokens from candidate tokens
        # in the usage metadata; without this addition, billed-but-
        # uncounted thinking tokens were inflating actual cost vs
        # reported. Mirrors upstream google/chat.py:178.
        candidate_tokens = getattr(usage_meta, "candidates_token_count", 0) or 0
        thoughts_tokens = getattr(usage_meta, "thoughts_token_count", 0) or 0
        usage = ChatInvokeUsage(
            input=getattr(usage_meta, "prompt_token_count", 0) or 0,
            output=candidate_tokens + thoughts_tokens,
            cache_read=getattr(usage_meta, "cached_content_token_count", 0) or 0,
            input_details=_token_details(
                getattr(usage_meta, "prompt_tokens_details", None)
            ),
            output_details=_token_details(
                getattr(usage_meta, "candidates_tokens_details", None)
            ),
        )
        return ChatInvokeCompletion(
            text=text, tool_calls=tool_calls, usage=usage, raw=response
        )
