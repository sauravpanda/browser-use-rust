"""Gemini-driven agent loop. Parallel to browser_use_rs.agent.Agent —
same Tool registry, same BrowserSession, but uses Gemini's native
function-calling API instead of Anthropic's tool-use.

Usage:
    from browser_use_rs.agent_gemini import GeminiAgent
    from browser_use_rs._browser_tools import BROWSER_TOOLS

    agent = GeminiAgent(task="...", tools=BROWSER_TOOLS)
    result = await agent.run()

Env:
    GEMINI_API_KEY (or GOOGLE_API_KEY)

Notable differences from the Anthropic Agent:
- Gemini does not support image content inside function_response. The
  screenshot tool attaches the PNG as a separate user-message Part right
  after the FunctionResponse, with a placeholder note in the response.
- No prompt caching by default. Gemini's caching requires explicit
  CachedContent setup (separate API surface, not added here).
- Schema is passed as a plain dict (Gemini's parameters accept JSON
  Schema dicts); `additionalProperties` is stripped because some Gemini
  models reject it.
"""

from __future__ import annotations

import base64
import os
from typing import Any

from google import genai
from google.genai import types as gtypes

from browser_use_rs._native import BrowserSession
from browser_use_rs.agent import _expand_secrets, _redact_secrets
from browser_use_rs.tools import Tool

SYSTEM_PROMPT = """\
You are a browser-use agent. You control a real Chromium browser through
a small set of tools and complete the user's task by calling them.

Strategy:
- Snapshot the page, then act. Re-snapshot after any action that changes
  the page (navigate, click, type, scroll).
- Prefer clicking visible links over navigating to known URLs — that
  verifies the page is in the expected state.
- When the page is unfamiliar or text is ambiguous, take a screenshot.
  The image will appear as the next user message.
- When the task is complete, respond with a final answer in plain text.
  Do NOT call any further tools — your text turn is the answer.
"""


def _clean_schema(schema: Any, parent_key: str | None = None) -> Any:
    """Strip JSON Schema features Gemini doesn't accept and patch empty
    object types (Gemini rejects `{type: "object", properties: {}}`)."""
    if isinstance(schema, dict):
        out: dict[str, Any] = {}
        for k, v in schema.items():
            if k in ("additionalProperties", "default"):
                continue
            # 'title' is metadata at the schema level — strip it. But if
            # the parent is 'properties', 'title' is a property NAME and
            # must be kept.
            if k == "title" and parent_key != "properties":
                continue
            out[k] = _clean_schema(v, parent_key=k)
        # ~10 of our tools are zero-arg (dom_snapshot, screenshot, etc.)
        # and their schemas have empty properties. Gemini errors on those
        # — pad with a no-op placeholder. The agent loop strips it from
        # call args before invoking the tool so the function never sees it.
        if (
            isinstance(out.get("type"), str)
            and out["type"].lower() == "object"
            and isinstance(out.get("properties"), dict)
            and len(out["properties"]) == 0
        ):
            out["properties"] = {"_placeholder": {"type": "string"}}
        return out
    if isinstance(schema, list):
        return [_clean_schema(x, parent_key=parent_key) for x in schema]
    return schema


class GeminiAgent:
    def __init__(
        self,
        task: str,
        tools: list[Tool],
        *,
        model: str = "gemini-2.5-flash",
        max_steps: int = 30,
        max_consecutive_errors: int = 5,
        client: genai.Client | None = None,
        system_prompt: str = SYSTEM_PROMPT,
        sensitive_data: dict[str, str] | None = None,
    ):
        self.task = task
        self.tools_by_name: dict[str, Tool] = {t.name: t for t in tools}
        self.model = model
        self.max_steps = max_steps
        self.max_consecutive_errors = max_consecutive_errors
        self.client = client or genai.Client(
            api_key=os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY"),
        )
        self.system_prompt = system_prompt
        self.sensitive_data: dict[str, str] = sensitive_data or {}
        self.session = BrowserSession()
        self.usage_log: list[dict] = []
        self.error_log: list[tuple[int, str]] = []

    async def run(self) -> str:
        await self.session.start()
        try:
            return await self._loop()
        finally:
            await self.session.stop()

    async def _loop(self) -> str:
        # Translate our @tool registry into a single Gemini Tool with
        # function_declarations for each.
        gemini_tool = gtypes.Tool(
            function_declarations=[
                gtypes.FunctionDeclaration(
                    name=t.name,
                    description=t.description,
                    parameters=_clean_schema(t.input_schema),
                )
                for t in self.tools_by_name.values()
            ]
        )

        contents: list[gtypes.Content] = [
            gtypes.Content(role="user", parts=[gtypes.Part(text=self.task)])
        ]
        consecutive_error_turns = 0

        for step in range(self.max_steps):
            response = await self.client.aio.models.generate_content(
                model=self.model,
                contents=contents,
                config=gtypes.GenerateContentConfig(
                    system_instruction=self.system_prompt,
                    tools=[gemini_tool],
                ),
            )
            self._log_usage(step, response.usage_metadata)

            candidate = response.candidates[0]
            parts = candidate.content.parts or []
            function_calls = [p.function_call for p in parts if p.function_call]
            text_parts = [p.text for p in parts if p.text]

            if not function_calls:
                return "\n".join(t for t in text_parts if t).strip() or (
                    f"step {step}: model returned no tool calls and no text"
                )

            # Append the assistant turn (model output, including the
            # function_call parts we just received).
            contents.append(candidate.content)

            response_parts: list[gtypes.Part] = []
            extra_user_parts: list[gtypes.Part] = []
            error_count = 0

            for fc in function_calls:
                name = fc.name
                args = dict(fc.args or {})
                tool = self.tools_by_name.get(name)
                if tool is None:
                    response_parts.append(
                        gtypes.Part(
                            function_response=gtypes.FunctionResponse(
                                name=name,
                                response={"error": f"unknown tool: {name}"},
                            )
                        )
                    )
                    error_count += 1
                    self.error_log.append((step, f"unknown tool: {name}"))
                    continue

                real_args = _expand_secrets(args, self.sensitive_data)
                # Strip the schema padding we added for Gemini's empty-object
                # restriction — the actual tool function doesn't take it.
                real_args.pop("_placeholder", None)
                try:
                    raw = await tool.func(self.session, **real_args)
                except Exception as e:
                    msg = f"tool error: {type(e).__name__}: {e}"
                    msg = _redact_secrets(msg, self.sensitive_data)
                    response_parts.append(
                        gtypes.Part(
                            function_response=gtypes.FunctionResponse(
                                name=name,
                                response={"error": msg},
                            )
                        )
                    )
                    error_count += 1
                    self.error_log.append((step, msg))
                    continue

                # Image return: Gemini doesn't accept image in
                # function_response, so attach as next user-message Part
                # and tell the model in the response.
                if isinstance(raw, dict) and raw.get("_type") == "image":
                    extra_user_parts.append(
                        gtypes.Part(
                            inline_data=gtypes.Blob(
                                mime_type=raw["media_type"],
                                data=base64.b64decode(raw["data"]),
                            )
                        )
                    )
                    response_parts.append(
                        gtypes.Part(
                            function_response=gtypes.FunctionResponse(
                                name=name,
                                response={
                                    "result": "image attached as next user-message part"
                                },
                            )
                        )
                    )
                    continue

                result_str = raw if isinstance(raw, str) else str(raw)
                result_str = _redact_secrets(result_str, self.sensitive_data)
                response_parts.append(
                    gtypes.Part(
                        function_response=gtypes.FunctionResponse(
                            name=name,
                            response={"result": result_str},
                        )
                    )
                )

            # Single user turn with all function responses, plus any image
            # parts the screenshot tool produced. Gemini handles multi-part
            # user content fine and will see the image inline.
            user_parts = response_parts + extra_user_parts
            contents.append(gtypes.Content(role="user", parts=user_parts))

            if error_count == len(function_calls) and function_calls:
                consecutive_error_turns += 1
                if consecutive_error_turns >= self.max_consecutive_errors:
                    return (
                        f"agent gave up after {consecutive_error_turns} consecutive "
                        f"all-error turns at step {step}"
                    )
            else:
                consecutive_error_turns = 0

        return f"hit max_steps={self.max_steps} without final answer"

    def _log_usage(self, step: int, usage) -> None:
        if usage is None:
            self.usage_log.append({"step": step, "input": 0, "output": 0, "cache_read": 0})
            return
        self.usage_log.append(
            {
                "step": step,
                "input": getattr(usage, "prompt_token_count", 0) or 0,
                "output": getattr(usage, "candidates_token_count", 0) or 0,
                "cache_read": getattr(usage, "cached_content_token_count", 0) or 0,
            }
        )
