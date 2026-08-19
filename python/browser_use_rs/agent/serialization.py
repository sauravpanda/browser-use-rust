"""Message/content serialization and byte-accounting helpers.

Extracted verbatim from ``agent/__init__.py``. These are pure functions
over the normalized message types in ``llm.base`` — no Agent state.
Used for prompt-section metrics, trace dumps, and page-state message
identification.
"""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from typing import Any

from browser_use_rs.llm.base import (
    AssistantMessage,
    ImagePart,
    Message,
    TextPart,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)


def _json_fallback(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    return repr(obj)


def _short_tool_call_repr(tc: ToolCall, max_chars: int = 140) -> str:
    args = getattr(tc, "args", None)
    try:
        args_s = json.dumps(args or {}, sort_keys=True, default=_json_fallback)
    except Exception:
        args_s = repr(args)
    out = f"{tc.name}({args_s})"
    if len(out) <= max_chars:
        return out
    return out[: max(0, max_chars - 3)] + "..."


def _content_byte_len(content: Any) -> int:
    """UTF-8 byte length of a message's content (str | list[Part]).
    Image parts contribute their base64 payload length — same as what
    flows over the wire to the provider.
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, TextPart):
                total += len(part.text.encode("utf-8"))
            elif isinstance(part, ImagePart):
                total += len(part.data) if part.data else 0
            else:
                total += len(repr(part).encode("utf-8"))
        return total
    return len(str(content).encode("utf-8"))


def _content_text(content: Any) -> str:
    """Text-only view of message content for prompt-section metrics."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, TextPart):
                parts.append(part.text)
        return "\n".join(parts)
    return str(content)


def _message_byte_len(msg: Message) -> int:
    """Approximate wire size of a single message: content + tool_call
    payloads if assistant. Used for per-role attribution in
    _compute_call_metrics.
    """
    if isinstance(msg, AssistantMessage):
        n = _content_byte_len(msg.text or "")
        for tc in msg.tool_calls:
            n += len(tc.name.encode("utf-8")) if tc.name else 0
            args = getattr(tc, "arguments", None) or getattr(tc, "args", None)
            if args is not None:
                if isinstance(args, str):
                    n += len(args.encode("utf-8"))
                else:
                    n += len(json.dumps(args, default=_json_fallback).encode("utf-8"))
        return n
    if isinstance(msg, ToolResultMessage):
        return _content_byte_len(msg.content) + len(
            (msg.name or "").encode("utf-8")
        )
    if isinstance(msg, (UserMessage,)):
        return _content_byte_len(msg.content)
    return _content_byte_len(getattr(msg, "content", ""))


def _message_to_dict(msg: Message) -> dict[str, Any]:
    """Serialize a message for trace dump. Lossy on image bytes —
    images are replaced with a `<image:N bytes>` marker so JSON files
    stay readable."""
    base: dict[str, Any] = {"role": type(msg).__name__}
    if isinstance(msg, ToolResultMessage):
        base["tool_call_id"] = msg.tool_call_id
        base["name"] = msg.name
        base["is_error"] = msg.is_error
        base["content"] = _content_to_dict(msg.content)
    elif isinstance(msg, AssistantMessage):
        base["text"] = msg.text
        base["tool_calls"] = [
            {
                "id": getattr(tc, "id", None),
                "name": tc.name,
                "args": getattr(tc, "arguments", None) or getattr(tc, "args", None),
            }
            for tc in msg.tool_calls
        ]
    else:
        base["content"] = _content_to_dict(getattr(msg, "content", ""))
    return base


def _content_to_dict(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[Any] = []
        for part in content:
            if isinstance(part, TextPart):
                out.append({"type": "text", "text": part.text})
            elif isinstance(part, ImagePart):
                size = len(part.data) if part.data else 0
                out.append({"type": "image", "media_type": part.media_type, "bytes": size})
            else:
                out.append(repr(part))
        return out
    return str(content)


def _utf8_len(text: str) -> int:
    return len(text.encode("utf-8", errors="replace"))


def _utf8_prefix(text: str, max_bytes: int) -> str:
    if max_bytes <= 0:
        return ""
    data = text.encode("utf-8", errors="replace")
    if len(data) <= max_bytes:
        return text
    return data[:max_bytes].decode("utf-8", errors="ignore")


# Tag prefix that identifies auto-injected per-step page-state messages.
# We use it to find and supersede the previous step's snapshot so the
# conversation doesn't accumulate stale DOMs across long runs.
_PAGE_STATE_TAG = "[PAGE_STATE]"
_PAGE_STATE_UNCHANGED_TAG = "[PAGE_STATE_UNCHANGED]"
_PAGE_STATE_SUPERSEDED = (
    f"{_PAGE_STATE_TAG} (superseded — see latest page state below)"
)


def _page_state_text(msg: Message) -> str | None:
    if not isinstance(msg, UserMessage):
        return None
    if isinstance(msg.content, str):
        text = msg.content
    elif isinstance(msg.content, list):
        first = msg.content[0] if msg.content else None
        text = first.text if isinstance(first, TextPart) else ""
    else:
        text = ""
    if text.startswith(_PAGE_STATE_TAG) or text.startswith(_PAGE_STATE_UNCHANGED_TAG):
        return text
    return None


def _is_page_state_message(msg: Message) -> bool:
    return _page_state_text(msg) is not None


def _is_page_state_reuse_marker(msg: Message) -> bool:
    text = _page_state_text(msg)
    return bool(text and text.startswith(_PAGE_STATE_UNCHANGED_TAG))
