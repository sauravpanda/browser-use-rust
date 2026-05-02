"""Optional Laminar (lmnr.ai) tracing integration.

When `lmnr` is installed AND `LMNR_PROJECT_API_KEY` is set, the `@observe`
decorator emits OpenTelemetry spans to Laminar. When lmnr isn't installed,
the decorator becomes a no-op so consumer code keeps working.

Initialization is the caller's responsibility — eval frameworks like
`evaluations-internal/eval/service.py` already call `Laminar.initialize()`
before invoking the agent, after which our `@observe` decorators emit
spans into that active context. Local users can call `Laminar.initialize()`
themselves to enable tracing.

Mirrors `browser_use.observability` so consumers can drop us in.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable
from functools import wraps
from typing import Any, Literal, TypeVar, cast

logger = logging.getLogger(__name__)
F = TypeVar("F", bound=Callable[..., Any])


def _is_debug_mode() -> bool:
    """Debug mode (used by `observe_debug`) gated on LMNR_LOGGING_LEVEL=debug."""
    return os.getenv("LMNR_LOGGING_LEVEL", "").lower() == "debug"


# Optional lmnr import — silently degrades to no-op if missing.
_LMNR_AVAILABLE = False
_lmnr_observe: Any = None
try:
    from lmnr import observe as _lmnr_observe  # type: ignore[no-redef]

    _LMNR_AVAILABLE = True
    if os.environ.get("BROWSER_USE_RS_VERBOSE_OBSERVABILITY", "false").lower() == "true":
        logger.debug("lmnr available — observability decorators will emit spans")
except (ImportError, TypeError):
    if os.environ.get("BROWSER_USE_RS_VERBOSE_OBSERVABILITY", "false").lower() == "true":
        logger.debug("lmnr not installed — observability decorators will no-op")


def _create_no_op_decorator(**_kwargs: Any) -> Callable[[F], F]:
    """No-op decorator that accepts any kwargs and returns the function unchanged."""
    import asyncio

    def decorator(func: F) -> F:
        if asyncio.iscoroutinefunction(func):

            @wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await func(*args, **kwargs)

            return cast(F, async_wrapper)

        @wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        return cast(F, sync_wrapper)

    return decorator


def observe(
    name: str | None = None,
    ignore_input: bool = False,
    ignore_output: bool = False,
    metadata: dict[str, Any] | None = None,
    span_type: Literal["DEFAULT", "LLM", "TOOL"] = "DEFAULT",
    **kwargs: Any,
) -> Callable[[F], F]:
    """Trace function execution via Laminar when available.

    Falls back to no-op when `lmnr` isn't installed. Same signature as
    `lmnr.observe` and `browser_use.observability.observe`.

    Args:
        name: Span name (defaults to function name).
        ignore_input: Don't capture function args in the span.
        ignore_output: Don't capture return value in the span.
        metadata: Extra attributes attached to the span.
        span_type: One of DEFAULT / LLM / TOOL — Laminar uses this for
            UI grouping and per-span semantics.
    """
    decorator_kwargs: dict[str, Any] = {
        "name": name,
        "ignore_input": ignore_input,
        "ignore_output": ignore_output,
        "metadata": metadata,
        "span_type": span_type,
        # `tags` need to be created on Laminar first; mirroring upstream's defaults.
        "tags": ["observe", "observe_debug"],
        **kwargs,
    }
    if _LMNR_AVAILABLE and _lmnr_observe is not None:
        return cast(Callable[[F], F], _lmnr_observe(**decorator_kwargs))
    return _create_no_op_decorator(**decorator_kwargs)


def observe_debug(
    name: str | None = None,
    ignore_input: bool = False,
    ignore_output: bool = False,
    metadata: dict[str, Any] | None = None,
    span_type: Literal["DEFAULT", "LLM", "TOOL"] = "DEFAULT",
    **kwargs: Any,
) -> Callable[[F], F]:
    """Like `observe` but only fires when LMNR_LOGGING_LEVEL=debug.

    Use for hot inner-loop functions where always-on tracing would be
    too noisy or expensive but you want detail when debugging.
    """
    decorator_kwargs: dict[str, Any] = {
        "name": name,
        "ignore_input": ignore_input,
        "ignore_output": ignore_output,
        "metadata": metadata,
        "span_type": span_type,
        "tags": ["observe_debug"],
        **kwargs,
    }
    if _LMNR_AVAILABLE and _lmnr_observe is not None and _is_debug_mode():
        return cast(Callable[[F], F], _lmnr_observe(**decorator_kwargs))
    return _create_no_op_decorator(**decorator_kwargs)


def is_lmnr_available() -> bool:
    """True iff `lmnr` is importable in the current process."""
    return _LMNR_AVAILABLE


def get_observability_status() -> dict[str, bool]:
    """Snapshot of observability state for diagnostics."""
    return {
        "lmnr_available": _LMNR_AVAILABLE,
        "debug_mode": _is_debug_mode(),
        "observe_active": _LMNR_AVAILABLE,
        "observe_debug_active": _LMNR_AVAILABLE and _is_debug_mode(),
    }


__all__ = ["observe", "observe_debug", "is_lmnr_available", "get_observability_status"]
