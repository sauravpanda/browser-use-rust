"""Provider-agnostic chat model interface.

The Agent loop speaks only `BaseChatModel.ainvoke(messages, tools, system)`
and gets back a `ChatInvokeCompletion`. Every provider (Anthropic, Google,
OpenAI, ...) translates the normalized message list to its native format
and back. Adding a new provider means subclassing BaseChatModel — no
changes to the agent loop.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from browser_use_rs.tools import Tool


@dataclass
class ChatInvokeUsage:
    input: int = 0
    output: int = 0
    cache_read: int = 0
    cache_creation: int = 0
    # Set by Agent at construction so model_dump() can attach cost fields.
    # The eval-platform aggregator at temporaryUpload.ts reads
    # `usage.total_cost` to roll up `totalCost` / `avgPrice`; without these
    # fields it silently treats every task's cost as 0.
    model: str | None = field(default=None, repr=False)

    def __add__(self, other: "ChatInvokeUsage") -> "ChatInvokeUsage":
        # Preserve `model` across additions — propagation matters because
        # the agent loop sums per-step usages into history.usage and
        # consumers call model_dump() on the accumulated total.
        return ChatInvokeUsage(
            input=self.input + other.input,
            output=self.output + other.output,
            cache_read=self.cache_read + other.cache_read,
            cache_creation=self.cache_creation + other.cache_creation,
            model=self.model or other.model,
        )

    def model_dump(self) -> dict[str, float]:
        from browser_use_rs.pricing import cost_for

        # Names mirror browser_use's ChatInvokeUsage so consumer code that
        # reads total_prompt_tokens / total_completion_tokens still works.
        out: dict[str, float] = {
            "input": self.input,
            "output": self.output,
            "cache_read": self.cache_read,
            "cache_creation": self.cache_creation,
            "total_prompt_tokens": self.input,
            "total_completion_tokens": self.output,
            "total_prompt_cached_tokens": self.cache_read,
            "total_tokens": self.input + self.output,
        }
        if self.model:
            out["model"] = self.model
        out.update(cost_for(self.model, self))
        return out


@dataclass
class ToolCall:
    """One tool invocation the model emitted.

    `meta` carries provider-private fields that must be echoed back on the
    next request — e.g. Gemini's `thought_signature`. Other providers
    leave it empty.
    """

    id: str
    name: str
    args: dict[str, Any]
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class TextPart:
    text: str


@dataclass
class ImagePart:
    """A base64-encoded image attached to a message."""

    data: str
    media_type: str = "image/png"


ContentPart = TextPart | ImagePart


@dataclass
class UserMessage:
    """User-side turn. Either a plain string or a list of parts (text + images)."""

    content: str | list[ContentPart]


@dataclass
class SystemMessage:
    """System / instructions message. Most providers route this to a separate field."""

    content: str


@dataclass
class AssistantMessage:
    """Model output turn — text and/or one or more tool_calls.

    Reconstructed from structured fields on each provider call; we don't
    cache provider-native blocks. This keeps providers swappable mid-run.
    """

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


@dataclass
class ToolResultMessage:
    """Result of executing one tool call. `content` may include images;
    providers that don't support image-in-tool-result (Gemini, OpenAI) split
    the image into a follow-up user-message part automatically.
    """

    tool_call_id: str
    name: str
    content: str | list[ContentPart]
    is_error: bool = False


Message = UserMessage | AssistantMessage | ToolResultMessage | SystemMessage


@dataclass
class ChatInvokeCompletion:
    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: ChatInvokeUsage = field(default_factory=ChatInvokeUsage)
    raw: Any = None


class BaseChatModel(ABC):
    """Subclass this to add a provider. `name` is used for diagnostics;
    `model` is the concrete model id sent on the wire."""

    name: str = "base"
    model: str = ""

    @abstractmethod
    async def ainvoke(
        self,
        messages: list[Message],
        tools: list["Tool"],
        *,
        system: str | None = None,
    ) -> ChatInvokeCompletion: ...


# ---------------------------------------------------------------------------
# Transient-error retry helper, shared across providers.
#
# Eval traffic against Gemini routinely hits intermittent 503 UNAVAILABLE
# / "Overloaded" / 429 rate-limit responses, and a single such error
# kills the agent run because eval/service.py treats it as a stage
# failure. Pattern lifted from OpenCode's session/retry.ts: exponential
# backoff with a small cap, retrying ONLY on signals that look transient.
# Honor a `Retry-After` style hint when the SDK exposes one.

_RETRY_PHRASES = (
    "503",
    "unavailable",
    "overloaded",
    "rate limit",
    "too many requests",
    "exhausted",
    "internal error",
    "temporarily",
    "deadline exceeded",
)


def _is_retryable(exc: BaseException) -> bool:
    """Return True if the exception's text contains a known transient
    marker. We pattern-match on stringified exceptions because each
    provider SDK raises its own typed exception, and they're all
    convertible-to-string with the upstream HTTP status / message body."""
    msg = str(exc).lower()
    if any(p in msg for p in _RETRY_PHRASES):
        return True
    # Also catch Python stdlib timeout / connection wobbles.
    return isinstance(
        exc,
        (TimeoutError, ConnectionError, OSError),
    )


async def with_retry(
    factory,
    *,
    max_attempts: int = 5,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    label: str = "llm",
):
    """Run `factory()` up to `max_attempts` times with exponential
    backoff on transient errors. `factory` must be a no-arg callable
    that returns a fresh coroutine each call — a plain coroutine can
    only be awaited once.

    v0.8.7: bumped max_attempts 3→5 and max_delay 8s→30s to match
    upstream browser_use's google/chat.py retry policy. Eval against
    Gemini Flash showed 503/overload bursts that needed more than 3
    attempts to ride out, especially under sustained load.
    """
    import asyncio
    import logging
    import random

    logger = logging.getLogger("browser_use_rs.llm.retry")

    last_exc: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await factory()
        except BaseException as exc:  # noqa: BLE001
            if not _is_retryable(exc) or attempt >= max_attempts:
                raise
            # Exponential backoff with small jitter to avoid synchronized
            # retries when many shards hit the same outage at once.
            delay = min(base_delay * (2 ** (attempt - 1)), max_delay)
            delay += random.uniform(0, 0.25)
            logger.info(
                "%s: transient error on attempt %d/%d (%s: %s); "
                "retrying in %.1fs",
                label, attempt, max_attempts,
                type(exc).__name__, str(exc)[:120],
                delay,
            )
            last_exc = exc
            await asyncio.sleep(delay)
    # Unreachable — the loop either returns or re-raises.
    raise last_exc  # type: ignore[misc]
