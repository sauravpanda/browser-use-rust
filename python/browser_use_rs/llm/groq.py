"""Groq provider — OpenAI-compatible chat completions hitting Groq's
high-throughput LPU endpoint. Routes through the openai SDK with
base_url=https://api.groq.com/openai/v1, since Groq mirrors that surface.
"""

from __future__ import annotations

import os
from typing import Any

from openai import AsyncOpenAI

from browser_use_rs.llm.openai import ChatOpenAI


class ChatGroq(ChatOpenAI):
    name = "groq"

    def __init__(
        self,
        model: str = "llama-3.3-70b-versatile",
        *,
        api_key: str | None = None,
        base_url: str = "https://api.groq.com/openai/v1",
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        client: AsyncOpenAI | None = None,
    ):
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        if client is not None:
            self.client = client
        else:
            kwargs: dict[str, Any] = {
                "api_key": api_key or os.getenv("GROQ_API_KEY"),
                "base_url": base_url,
            }
            if timeout is not None:
                kwargs["timeout"] = timeout
            self.client = AsyncOpenAI(**kwargs)
