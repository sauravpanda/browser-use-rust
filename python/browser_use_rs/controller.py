"""Controller — aggregates the built-in browser tool set with custom
@registry.action()-registered actions. Mirrors browser_use's pattern:

    controller = Controller()

    class SearchParams(BaseModel):
        query: str

    @controller.registry.action("Search the web", param_model=SearchParams)
    async def search(params: SearchParams) -> ActionResult:
        ...
        return ActionResult(extracted_content=results)

    agent = Agent(task=..., llm=..., controller=controller)

The Agent uses controller.tools as its tool registry. Custom actions can
return either a plain string (legacy `@tool` style) or an ActionResult
(browser_use style) — Controller normalizes both.
"""

from __future__ import annotations

import inspect
from typing import Any, Callable

from browser_use_rs.tools import Tool, tool


class Controller:
    """Aggregates browser tools + custom registered actions for an Agent.

    Accepts the same constructor kwargs as `browser_use.Controller` so eval
    consumers can drop in. Most upstream kwargs (`output_model`,
    `display_files_in_done_text`, ...) are accepted but not enforced —
    they're stashed as attributes for read-back compatibility.
    """

    def __init__(
        self,
        *,
        exclude_actions: list[str] | None = None,
        include_default_actions: bool = True,
        # browser_use's Controller accepts a Pydantic model used to
        # constrain the agent's `done` action output. We don't enforce it
        # yet; stash it so consumer code that reads back
        # `controller.output_model` works.
        output_model: Any = None,
        # Accepted for parity, currently no-op:
        display_files_in_done_text: bool = True,
        **extra_kwargs: Any,
    ):
        excluded = set(exclude_actions or [])
        self._tools: list[Tool] = []
        if include_default_actions:
            from browser_use_rs._browser_tools import BROWSER_TOOLS

            self._tools = [t for t in BROWSER_TOOLS if t.name not in excluded]
        self.registry = _Registry(self)
        self.output_model = output_model
        self.display_files_in_done_text = display_files_in_done_text
        # Stash unknown kwargs as attributes so consumer code can read
        # them back without us breaking on every new upstream field.
        for k, v in extra_kwargs.items():
            if not hasattr(self, k):
                setattr(self, k, v)

    @property
    def tools(self) -> list[Tool]:
        return list(self._tools)

    def add_tool(self, t: Tool) -> None:
        """Append a tool directly without going through the @action decorator."""
        self._tools.append(t)


class _Registry:
    """Decorator factory exposed as `controller.registry.action(...)`."""

    def __init__(self, controller: Controller):
        self._controller = controller

    def action(
        self,
        description: str | None = None,
        *,
        param_model: Any = None,
        name: str | None = None,
        **_compat_kwargs: Any,
    ) -> Callable[[Callable], Callable]:
        """Register a function as a tool. If `param_model` is a Pydantic
        model, its JSON schema becomes the tool's input_schema and the
        action receives an instance constructed from the LLM's args.
        Otherwise the function is treated as a plain `@tool`-style coroutine.
        """

        def decorator(func: Callable) -> Callable:
            t = self._build_tool(func, description, param_model, name)
            self._controller._tools.append(t)
            return func

        return decorator

    def _build_tool(
        self,
        func: Callable,
        description: str | None,
        param_model: Any,
        explicit_name: str | None,
    ) -> Tool:
        tool_name = explicit_name or func.__name__
        tool_desc = description or inspect.getdoc(func) or tool_name

        if param_model is not None and hasattr(param_model, "model_json_schema"):
            schema = param_model.model_json_schema()
            schema.pop("title", None)
            schema.pop("$defs", None)

            sig = inspect.signature(func)
            takes_session = len(sig.parameters) >= 2

            async def wrapper(session, **kwargs: Any) -> Any:
                instance = param_model(**kwargs)
                raw = await func(instance, session) if takes_session else await func(instance)
                return _coerce_action_result(raw)

            return Tool(
                name=tool_name,
                description=tool_desc,
                input_schema=schema,
                func=wrapper,
            )

        # No param_model — fall back to our @tool decorator (which infers
        # schema from type hints) and wrap the return value to handle
        # ActionResult.
        base = tool(func, name=tool_name, description=tool_desc)
        inner = base.func

        async def passthrough_wrapper(session, **kwargs: Any) -> Any:
            raw = await inner(session, **kwargs)
            return _coerce_action_result(raw)

        base.func = passthrough_wrapper
        return base


def _coerce_action_result(raw: Any) -> Any:
    """Convert an action's return value into something the agent loop /
    LLM understands. Strings and image-dicts pass through. ActionResult
    is unwrapped to its extracted_content (or error)."""
    from browser_use_rs.views import ActionResult

    if isinstance(raw, ActionResult):
        if raw.error:
            raise RuntimeError(raw.error)
        return raw.extracted_content or raw.long_term_memory or ""
    return raw
