import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs._extra_tools import (  # noqa: E402
    _count_items_in_answer,
    _done_count_check_message,
    make_extra_tools,
    _parse_required_count,
)
from browser_use_rs.agent import Agent  # noqa: E402
from browser_use_rs.llm.base import (  # noqa: E402
    BaseChatModel,
    ChatInvokeCompletion,
    ToolCall,
)
from browser_use_rs.tools import Tool  # noqa: E402


class PlainTextListLLM(BaseChatModel):
    name = "plain-text-list"
    model = "plain-text-list"

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages, tools, *, system=None):
        self.calls += 1
        if self.calls == 1:
            return ChatInvokeCompletion(text="1. Alpha Article")
        return ChatInvokeCompletion(
            text="1. Alpha Article\n2. Beta Article\n3. Gamma Article"
        )


class DoneToolListLLM(BaseChatModel):
    name = "done-tool-list"
    model = "done-tool-list"

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages, tools, *, system=None):
        self.calls += 1
        return ChatInvokeCompletion(
            tool_calls=[
                ToolCall(
                    id=f"done-{self.calls}",
                    name="done",
                    args={},
                )
            ]
        )


class ExtractThenDoneLLM(BaseChatModel):
    name = "extract-then-done"
    model = "extract-then-done"

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages, tools, *, system=None):
        self.calls += 1
        if self.calls == 1:
            return ChatInvokeCompletion(
                tool_calls=[
                    ToolCall(
                        id="extract-1",
                        name="extract_structured_data",
                        args={},
                    )
                ]
            )
        return ChatInvokeCompletion(
            tool_calls=[
                ToolCall(
                    id=f"done-{self.calls}",
                    name="done",
                    args={},
                )
            ]
        )


class Snapshot:
    elements = []

    def to_llm_string(self):
        return "Page content"


class Session:
    async def start(self):
        return None

    async def current_url(self):
        return "https://example.com/news"

    async def screenshot(self):
        return b""

    async def dom_snapshot(self):
        return Snapshot()


class DoneCountHelperTests(unittest.TestCase):
    def test_parse_required_count_accepts_at_least_phrasing(self):
        self.assertEqual(
            _parse_required_count(
                "Use the site's search function and list the titles of "
                "at least two such articles."
            ),
            2,
        )

    def test_count_items_accepts_inline_numbered_list(self):
        text = (
            "The first three results are: 1. Alpha Article 2. Beta Article "
            "3. Gamma Article"
        )

        self.assertEqual(_count_items_in_answer(text), 3)

    def test_count_items_reports_single_numbered_item(self):
        self.assertEqual(_count_items_in_answer("1. Alpha Article"), 1)

    def test_count_items_ignores_decimals_and_dates(self):
        text = (
            "The value is 1.20% as of May 2, 2026. "
            "No numbered list is present."
        )

        self.assertIsNone(_count_items_in_answer(text))

    def test_done_tool_nudges_one_item_for_small_top_n_task(self):
        class Agent:
            task = "List the top 3 headlines. website: https://example.com"
            llm = None
            page_extraction_llm = None
            _done_count_check_fired = False

        done = next(t for t in make_extra_tools(Agent()) if t.name == "done")

        out = asyncio.run(done.func(object(), text="1. Alpha Article", success=True))

        self.assertIn("[DONE_COUNT_CHECK]", out)
        self.assertIn("3 items", out)

    def test_shared_count_check_message_supports_plain_text_instruction(self):
        message = _done_count_check_message(
            "List the top 3 headlines. website: https://example.com",
            "1. Alpha Article",
            already_fired=False,
            finish_instruction="reply again with an explicit partial note",
        )

        self.assertIsNotNone(message)
        self.assertIn("reply again with an explicit partial note", message)

    def test_shared_count_check_message_fires_once(self):
        self.assertIsNone(
            _done_count_check_message(
                "List the top 3 headlines. website: https://example.com",
                "1. Alpha Article",
                already_fired=True,
            )
        )

    def test_plain_text_final_path_uses_count_check_before_committing(self):
        llm = PlainTextListLLM()
        agent = Agent(
            "List the top 3 headlines. website: https://example.com",
            llm,
            browser_session=Session(),
            max_steps=3,
            self_validate=False,
            auto_initial_navigation=False,
        )

        history = asyncio.run(agent.run())

        self.assertEqual(llm.calls, 2)
        self.assertFalse(history.history[0].result[0].is_done)
        self.assertIn("Gamma Article", history.final_result())
        self.assertTrue(agent._done_count_check_fired)

    def test_done_marker_path_uses_count_check_before_committing(self):
        llm = DoneToolListLLM()
        tool_calls = {"count": 0}

        async def done_tool(session):
            tool_calls["count"] += 1
            if tool_calls["count"] == 1:
                return "__DONE__:1:1. Alpha Article"
            return "__DONE__:1:1. Alpha Article\n2. Beta Article\n3. Gamma Article"

        agent = Agent(
            "List the top 3 headlines. website: https://example.com",
            llm,
            tools=[
                Tool(
                    name="done",
                    description="",
                    input_schema={"type": "object", "properties": {}},
                    func=done_tool,
                )
            ],
            browser_session=Session(),
            max_steps=3,
            self_validate=False,
            auto_initial_navigation=False,
        )

        history = asyncio.run(agent.run())

        self.assertEqual(tool_calls["count"], 2)
        self.assertFalse(history.history[0].result[0].is_done)
        self.assertIn("Gamma Article", history.final_result())
        self.assertTrue(agent._done_count_check_fired)

    def test_done_marker_skips_validation_after_fresh_extract(self):
        llm = ExtractThenDoneLLM()

        async def extract_tool(session):
            return "1. Alpha Article\n2. Beta Article\n3. Gamma Article"

        async def done_tool(session):
            return "__DONE__:1:1. Alpha Article\n2. Beta Article\n3. Gamma Article"

        agent = Agent(
            "List the top 3 headlines. website: https://example.com",
            llm,
            tools=[
                Tool(
                    name="extract_structured_data",
                    description="",
                    input_schema={"type": "object", "properties": {}},
                    func=extract_tool,
                ),
                Tool(
                    name="done",
                    description="",
                    input_schema={"type": "object", "properties": {}},
                    func=done_tool,
                ),
            ],
            browser_session=Session(),
            max_steps=4,
            self_validate=True,
            self_validate_min_steps=1,
            auto_initial_navigation=False,
        )

        history = asyncio.run(agent.run())

        self.assertEqual(llm.calls, 2)
        self.assertTrue(history.is_done())
        self.assertIn("Gamma Article", history.final_result())
        self.assertFalse(agent._validation_step_used)


if __name__ == "__main__":
    unittest.main()
