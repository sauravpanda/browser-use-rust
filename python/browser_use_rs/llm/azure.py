"""Azure OpenAI provider — OpenAI-compatible chat completions through
Azure's deployment routing. Inherits behavior from ChatOpenAI; only the
client construction differs.
"""

from __future__ import annotations

import os
from typing import Any

from openai import AsyncAzureOpenAI

from browser_use_rs.llm.openai import ChatOpenAI


class ChatAzureOpenAI(ChatOpenAI):
    name = "azure_openai"

    def __init__(
        self,
        model: str,
        *,
        api_key: str | None = None,
        api_version: str = "2024-10-21",
        azure_endpoint: str | None = None,
        azure_deployment: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        timeout: float | None = None,
        client: AsyncAzureOpenAI | None = None,
    ):
        # Don't run ChatOpenAI.__init__ — its client construction would
        # build a non-Azure client. Set fields directly.
        self.model = model
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.timeout = timeout
        if client is not None:
            self.client = client
        else:
            kwargs: dict[str, Any] = {
                "api_key": api_key or os.getenv("AZURE_OPENAI_API_KEY"),
                "api_version": api_version,
                "azure_endpoint": azure_endpoint or os.getenv("AZURE_OPENAI_ENDPOINT"),
            }
            if azure_deployment:
                kwargs["azure_deployment"] = azure_deployment
            if timeout is not None:
                kwargs["timeout"] = timeout
            self.client = AsyncAzureOpenAI(**kwargs)
