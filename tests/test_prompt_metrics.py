import sys
import time
import unittest
import asyncio
import base64
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs.agent import Agent, HistoryItem  # noqa: E402
from browser_use_rs.llm.base import (  # noqa: E402
    AssistantMessage,
    ImagePart,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from browser_use_rs.views import (  # noqa: E402
    ActionResult,
    AgentHistoryList,
    AgentOutput,
    BrowserStateSummary,
)


class PromptMetricsTests(unittest.TestCase):
    def _agent_for_state_injection(self, *, use_vision: bool = True):
        agent = Agent.__new__(Agent)
        agent.use_vision = use_vision
        agent._messages = [UserMessage("task")]
        agent._read_state_for_next_turn = []
        agent._previous_evaluation = ""
        agent._memory = ""
        agent._next_goal = ""
        agent._valid_indices = set()
        return agent

    def test_inject_page_state_uses_screenshot_media_type(self):
        agent = self._agent_for_state_injection()

        agent._inject_page_state(
            BrowserStateSummary(
                url="https://example.com",
                screenshot="abc123",
                screenshot_media_type="image/jpeg",
                elements_text="[1]<button>Continue",
            )
        )

        content = agent._messages[-1].content
        self.assertIsInstance(content, list)
        image = next(part for part in content if isinstance(part, ImagePart))
        self.assertEqual(image.media_type, "image/jpeg")

    def test_inject_page_state_defaults_screenshot_media_type_to_png(self):
        agent = self._agent_for_state_injection()

        agent._inject_page_state(
            BrowserStateSummary(
                url="https://example.com",
                screenshot="abc123",
                elements_text="[1]<button>Continue",
            )
        )

        content = agent._messages[-1].content
        self.assertIsInstance(content, list)
        image = next(part for part in content if isinstance(part, ImagePart))
        self.assertEqual(image.media_type, "image/png")

    def test_capture_state_prefers_scaled_jpeg_for_vision(self):
        class FakeSnapshot:
            elements = []

            def to_llm_string(self):
                return ""

        class FakeSession:
            def __init__(self):
                self.scaled_args = None

            async def current_url(self):
                return "https://example.com"

            async def screenshot_jpeg_scaled(self, quality, scale):
                self.scaled_args = (quality, scale)
                return b"scaled-jpeg"

            async def screenshot_jpeg(self, quality):
                raise AssertionError("unscaled JPEG should not be used")

            async def screenshot(self):
                raise AssertionError("PNG screenshot should not be used")

            async def dom_snapshot(self):
                return FakeSnapshot()

        session = FakeSession()
        agent = Agent.__new__(Agent)
        agent.use_vision = True
        agent.session = session
        agent._prev_selectors = set()
        agent.state = type("State", (), {"n_steps": 1})()

        state = asyncio.run(agent._capture_state())

        self.assertEqual(session.scaled_args, (60, 0.5))
        self.assertEqual(
            state.screenshot,
            base64.b64encode(b"scaled-jpeg").decode("ascii"),
        )
        self.assertEqual(state.screenshot_media_type, "image/jpeg")

    def test_compute_call_metrics_splits_history_and_read_state_bytes(self):
        agent = Agent.__new__(Agent)
        agent.system_prompt = "system prompt"
        agent.tools = []
        tool_call = ToolCall(id="tc1", name="click", args={"index": 3})
        agent._history = [
            HistoryItem(step_number=1, tool_calls=[tool_call], collapsed=True),
            HistoryItem(step_number=2, tool_calls=[tool_call], collapsed=False),
        ]
        agent._messages = [
            UserMessage("task"),
            UserMessage(
                "[AGENT_HISTORY] Earlier turns:\n"
                "<step 1> navigate to page -> ok\n"
                "<step 2> click result -> ok"
            ),
            UserMessage(
                "<read_state>\n"
                "<result tool=\"page_text\" file=\"results/page.txt\">\n"
                "Important page evidence.\n"
                "</result>\n"
                "</read_state>\n\n"
                "<browser_state>\nURL: https://example.com\n</browser_state>"
            ),
            AssistantMessage(text=None, tool_calls=[tool_call]),
            ToolResultMessage(tool_call_id="tc1", name="click", content="Clicked"),
        ]

        metrics = agent._compute_call_metrics()

        self.assertGreater(metrics["agent_history_bytes"], 0)
        self.assertEqual(metrics["agent_history_lines"], 2)
        self.assertGreater(metrics["read_state_bytes"], 0)
        self.assertEqual(metrics["read_state_entries"], 1)
        self.assertEqual(metrics["history_items"], 2)
        self.assertEqual(metrics["history_collapsed_items"], 1)

    def test_append_history_surfaces_prompt_history_metrics(self):
        agent = Agent.__new__(Agent)
        agent.usage_log = [
            {
                "step": 1,
                "input": 100,
                "output": 10,
                "cache_read": 50,
                "agent_history_bytes": 123,
                "agent_history_lines": 4,
                "read_state_bytes": 456,
                "read_state_entries": 2,
                "history_items": 5,
                "history_collapsed_items": 3,
            }
        ]
        agent.state = type("State", (), {"history": AgentHistoryList()})()
        agent._previous_evaluation = ""
        agent._memory = ""
        agent._next_goal = ""
        agent._history = []

        agent._append_history(
            BrowserStateSummary(url="https://example.com"),
            AgentOutput(text="done"),
            [ActionResult(extracted_content="done", is_done=True, success=True)],
            time.monotonic(),
            1,
        )

        metadata = agent.state.history.history[0].metadata
        self.assertEqual(metadata.prompt_agent_history_bytes, 123)
        self.assertEqual(metadata.prompt_agent_history_lines, 4)
        self.assertEqual(metadata.prompt_read_state_bytes, 456)
        self.assertEqual(metadata.prompt_read_state_entries, 2)
        self.assertEqual(metadata.prompt_history_items, 5)
        self.assertEqual(metadata.prompt_history_collapsed_items, 3)


if __name__ == "__main__":
    unittest.main()
