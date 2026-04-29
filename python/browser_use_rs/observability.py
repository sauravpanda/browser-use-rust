"""Compat stubs for `from browser_use.observability import observe, observe_debug`.

browser_use exposes Laminar tracing decorators here. We don't ship Laminar
integration; these are no-op decorators so consumer code that imports them
(judges, eval orchestration) keeps working without changes.

If you want tracing, keep importing the real decorators directly from
`browser_use.observability` — those work as no-ops when no Laminar key is
configured anyway, and they emit spans when `LMNR_PROJECT_API_KEY` is set.
This module exists for the case where browser_use isn't installed at all
(rs-only deployments).
"""

from __future__ import annotations

import functools
import inspect
from typing import Any, Callable, TypeVar

F = TypeVar("F", bound=Callable[..., Any])


def observe(
    name: str | None = None,
    *,
    span_type: str | None = None,
    ignore_input: bool = False,
    ignore_output: bool = False,
    **_compat_kwargs: Any,
) -> Callable[[F], F]:
    """No-op replacement for `lmnr.observe` / `browser_use.observability.observe`.

    Accepts the same kwargs as the real decorator and returns the wrapped
    function unchanged. Preserves async vs sync correctly.
    """

    def decorator(func: F) -> F:
        if inspect.iscoroutinefunction(func):

            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                return await func(*args, **kwargs)

            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(func)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            return func(*args, **kwargs)

        return sync_wrapper  # type: ignore[return-value]

    return decorator


def observe_debug(
    name: str | None = None,
    *,
    ignore_input: bool = False,
    ignore_output: bool = False,
    **_compat_kwargs: Any,
) -> Callable[[F], F]:
    """No-op replacement for `browser_use.observability.observe_debug`."""
    return observe(
        name=name,
        ignore_input=ignore_input,
        ignore_output=ignore_output,
    )


__all__ = ["observe", "observe_debug"]
