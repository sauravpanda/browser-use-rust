"""Pluggable LLM providers for the unified Agent loop.

`BaseChatModel` is the abstract interface. Adding a provider means
subclassing it. Providers are import-on-demand so callers don't need
unused SDKs installed.
"""

from browser_use_rs.llm.base import (
    AssistantMessage,
    BaseChatModel,
    ChatInvokeCompletion,
    ChatInvokeUsage,
    ContentPart,
    ImagePart,
    Message,
    SystemMessage,
    TextPart,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)


def __getattr__(name: str):
    """Lazy provider imports — `from browser_use_rs.llm import ChatGoogle`
    only loads `google-genai` if you actually ask for it."""
    if name == "ChatAnthropic":
        from browser_use_rs.llm.anthropic import ChatAnthropic

        return ChatAnthropic
    if name == "ChatGoogle":
        from browser_use_rs.llm.google import ChatGoogle

        return ChatGoogle
    if name == "ChatOpenAI":
        from browser_use_rs.llm.openai import ChatOpenAI

        return ChatOpenAI
    if name == "ChatAzureOpenAI":
        from browser_use_rs.llm.azure import ChatAzureOpenAI

        return ChatAzureOpenAI
    if name == "ChatGroq":
        from browser_use_rs.llm.groq import ChatGroq

        return ChatGroq
    raise AttributeError(name)


__all__ = [
    "AssistantMessage",
    "BaseChatModel",
    "ChatAnthropic",
    "ChatAzureOpenAI",
    "ChatGoogle",
    "ChatGroq",
    "ChatInvokeCompletion",
    "ChatInvokeUsage",
    "ChatOpenAI",
    "ContentPart",
    "ImagePart",
    "Message",
    "SystemMessage",
    "TextPart",
    "ToolCall",
    "ToolResultMessage",
    "UserMessage",
]
