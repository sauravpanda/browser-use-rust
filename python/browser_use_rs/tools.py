"""Tool registry — `@tool` decorator that derives an Anthropic tool definition
from a Python function's type hints + Google-style docstring.

Convention: the first parameter is the runtime context (passed by the agent
loop, not exposed to the LLM). All subsequent parameters become tool inputs.
"""

from __future__ import annotations

import inspect
import typing
from dataclasses import dataclass
from typing import Any, Callable

_PRIMITIVE_TYPES: dict[type, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
}


def _schema_for(t: Any) -> dict:
    if t in _PRIMITIVE_TYPES:
        return {"type": _PRIMITIVE_TYPES[t]}
    origin = typing.get_origin(t)
    if origin is list:
        args = typing.get_args(t)
        inner = args[0] if args else str
        return {"type": "array", "items": _schema_for(inner)}
    if origin is dict:
        return {"type": "object"}
    return {"type": "string"}


def _parse_docstring(docstring: str | None) -> tuple[str, dict[str, str]]:
    """Split a Google-style docstring into (description, {param: description})."""
    if not docstring:
        return "", {}
    desc_lines: list[str] = []
    param_docs: dict[str, str] = {}
    in_args = False
    for line in docstring.splitlines():
        stripped = line.strip()
        if stripped in ("Args:", "Arguments:", "Parameters:"):
            in_args = True
            continue
        if in_args:
            if not stripped:
                continue
            if not line.startswith((" ", "\t")):
                in_args = False
                desc_lines.append(line)
                continue
            if ":" in stripped:
                name, _, desc = stripped.partition(":")
                param_docs[name.strip()] = desc.strip()
        else:
            desc_lines.append(line)
    return "\n".join(desc_lines).strip(), param_docs


@dataclass
class Tool:
    name: str
    description: str
    input_schema: dict
    func: Callable

    def to_anthropic(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "input_schema": self.input_schema,
        }


def tool(
    func: Callable | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
) -> Callable:
    """Decorator that turns an async function into a `Tool`.

    The function's first parameter is treated as the runtime context (e.g. a
    BrowserSession) and is NOT exposed to the model. Remaining parameters
    become tool inputs; their JSON Schema types are inferred from type hints,
    and per-parameter descriptions are pulled from a Google-style docstring.
    """

    def decorate(f: Callable) -> Tool:
        sig = inspect.signature(f)
        try:
            hints = typing.get_type_hints(f)
        except Exception:
            hints = {}
        derived_desc, param_docs = _parse_docstring(inspect.getdoc(f))
        params = list(sig.parameters.values())[1:]  # skip ctx
        properties: dict[str, dict] = {}
        required: list[str] = []
        for p in params:
            t = hints.get(p.name, str)
            schema = _schema_for(t)
            if p.name in param_docs:
                schema["description"] = param_docs[p.name]
            properties[p.name] = schema
            if p.default is inspect.Parameter.empty:
                required.append(p.name)
        input_schema = {
            "type": "object",
            "properties": properties,
            "required": required,
            "additionalProperties": False,
        }
        return Tool(
            name=name or f.__name__,
            description=description or derived_desc,
            input_schema=input_schema,
            func=f,
        )

    if func is not None:
        return decorate(func)
    return decorate
