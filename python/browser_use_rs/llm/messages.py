"""Compatibility import path for `from browser_use.llm.messages import ...`.

browser_use ships these names under `browser_use.llm.messages`. We
re-export from `browser_use_rs.llm.base` so consumer code that imports
from this dotted path keeps working.

Aliases provided for the names browser_use uses but we don't have under
the same name:
- `BaseMessage`        → our `Message` union (UserMessage|AssistantMessage|...)
- `ContentPartTextParam`  → `TextPart`
- `ContentPartImageParam` → `ImagePart`
- `ChatInvokeCompletion`/`ChatInvokeUsage` re-exported from base
"""

from browser_use_rs.llm.base import (
    AssistantMessage,
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

# browser_use names → our names
BaseMessage = Message
ContentPartTextParam = TextPart
ContentPartImageParam = ImagePart


class ImageURL:
    """Compatibility shim for `browser_use.llm.messages.ImageURL`. Our
    ImagePart already carries base64 data + media_type; consumers passing
    URLs should just construct an ImagePart directly. This stub exists so
    `from browser_use.llm.messages import ImageURL` doesn't fail on import.
    """

    def __init__(self, url: str, detail: str | None = None):
        self.url = url
        self.detail = detail


__all__ = [
    "AssistantMessage",
    "BaseMessage",
    "ChatInvokeCompletion",
    "ChatInvokeUsage",
    "ContentPartImageParam",
    "ContentPartTextParam",
    "ImagePart",
    "ImageURL",
    "Message",
    "SystemMessage",
    "TextPart",
    "ToolCall",
    "ToolResultMessage",
    "UserMessage",
]
