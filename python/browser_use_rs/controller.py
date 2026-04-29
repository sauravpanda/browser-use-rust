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

When the caller passes `output_model=PydanticModel`, a `done` tool is
registered whose schema mirrors upstream's `StructuredOutputAction`:
`{success: bool, data: <PydanticModel>}`. The Agent loop recognizes the
done call, JSON-serializes `data` into `extracted_content`, and stops —
matching the eval-consumer contract `final_result()` returns JSON.
"""

from __future__ import annotations

import inspect
import json
from typing import Any, Callable

from browser_use_rs.tools import Tool, tool


# Sentinel name the Agent loop watches for to know a structured-output
# done was called. Kept as a constant so callers (and tests) can reference
# it without hardcoding the string in two places.
DONE_TOOL_NAME = "done"


class Controller:
    """Aggregates browser tools + custom registered actions for an Agent.

    Accepts the same constructor kwargs as `browser_use.Controller` so eval
    consumers can drop in. Unknown upstream kwargs are stashed as attributes
    for read-back compatibility.
    """

    def __init__(
        self,
        *,
        exclude_actions: list[str] | None = None,
        include_default_actions: bool = True,
        # When set, registers a `done` tool with the model's JSON schema.
        # Eval consumers depend on `final_result()` returning JSON of
        # this shape — see _build_done_tool below.
        output_model: Any = None,
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

        if output_model is not None and DONE_TOOL_NAME not in excluded:
            self._tools.append(_build_done_tool(output_model))

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


def _build_done_tool(output_model: Any) -> Tool:
    """Build a `done` tool whose schema is `{success: bool, data: <output_model>}`.

    Mirrors upstream browser_use's `StructuredOutputAction[T]`:
    https://github.com/browser-use/browser-use/blob/main/browser_use/tools/views.py
    The model dumps `data` to JSON and returns it as the tool's text
    payload. The Agent loop recognizes the tool by name and uses that
    payload as the run's `final_result()` (an `ActionResult` with
    `is_done=True, success=<from args>`).
    """
    if not hasattr(output_model, "model_json_schema"):
        raise TypeError(
            "Controller(output_model=...) must be a Pydantic BaseModel "
            f"subclass; got {type(output_model).__name__}"
        )

    data_schema = output_model.model_json_schema()
    # Strip metadata that confuses provider tool-schema validators (Anthropic
    # rejects $defs at the top of an input_schema; OpenAI tolerates but the
    # model wastes tokens on it).
    data_schema.pop("title", None)
    nested_defs = data_schema.pop("$defs", None)

    schema: dict[str, Any] = {
        "type": "object",
        "properties": {
            "success": {
                "type": "boolean",
                "description": (
                    "True if the user's task completed successfully, "
                    "False if you're giving up or the task is impossible."
                ),
                "default": True,
            },
            "data": {
                **data_schema,
                "description": (
                    "The structured answer to the user's task. "
                    "Fields must match the schema exactly."
                ),
            },
        },
        "required": ["data"],
        "additionalProperties": False,
    }
    if nested_defs:
        # Preserve nested model definitions referenced via $ref.
        schema["$defs"] = nested_defs

    description = (
        "Signal task completion with a structured answer. Fill `data` with "
        "fields matching the requested schema and set `success` based on "
        "whether you actually accomplished the task. Calling this tool ENDS "
        "the run — do not call any further tools."
    )

    async def done_func(session: Any, **kwargs: Any) -> str:  # noqa: ARG001
        success = bool(kwargs.get("success", True))
        data = kwargs.get("data") or {}
        # Validate the data against the user's model so malformed outputs
        # surface as a tool error the LLM can correct from, instead of
        # us silently writing garbage JSON to extracted_content.
        try:
            validated = output_model(**data) if isinstance(data, dict) else output_model.model_validate(data)
        except Exception as e:
            raise RuntimeError(
                f"done(data=...) failed schema validation: {type(e).__name__}: {e}. "
                "Fix the fields and call done again."
            ) from e
        payload = validated.model_dump_json()
        # The Agent loop reads __DONE__:<json> as a marker to mark the run
        # complete. Returning the JSON alone would be ambiguous with a
        # regular tool that happens to return JSON-looking text.
        return f"__DONE__:{int(success)}:{payload}"

    return Tool(
        name=DONE_TOOL_NAME,
        description=description,
        input_schema=schema,
        func=done_func,
    )


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
