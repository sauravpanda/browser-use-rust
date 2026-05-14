import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs.agent import Agent, HistoryItem  # noqa: E402
from browser_use_rs.llm.base import (  # noqa: E402
    AssistantMessage,
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
