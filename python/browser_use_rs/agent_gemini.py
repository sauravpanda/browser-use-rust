"""Compatibility shim — the per-provider GeminiAgent has been folded into
the unified Agent. Use:

    from browser_use_rs import Agent
    from browser_use_rs.llm import ChatGoogle

    agent = Agent(task=..., llm=ChatGoogle(model="gemini-2.5-flash"))

This module exists so existing imports keep working.
"""

from __future__ import annotations

from typing import Any

from browser_use_rs.agent import Agent
from browser_use_rs.llm.google import ChatGoogle
from browser_use_rs.tools import Tool


class GeminiAgent(Agent):
    """Deprecated thin wrapper. Use `Agent(llm=ChatGoogle(...))` directly."""

    def __init__(
        self,
        task: str,
        tools: list[Tool] | None = None,
        *,
        model: str = "gemini-2.5-flash",
        max_steps: int = 30,
        max_consecutive_errors: int = 5,
        client: Any = None,
        system_prompt: str | None = None,
        sensitive_data: dict[str, str] | None = None,
        **kwargs: Any,
    ):
        llm = ChatGoogle(model=model, client=client)
        super().__init__(
            task=task,
            llm=llm,
            tools=tools,
            max_steps=max_steps,
            max_consecutive_errors=max_consecutive_errors,
            system_prompt=system_prompt,
            sensitive_data=sensitive_data,
            **kwargs,
        )

    async def run(self, max_steps: int | None = None) -> str:  # type: ignore[override]
        history = await super().run(max_steps=max_steps)
        return history.final_result() or ""
