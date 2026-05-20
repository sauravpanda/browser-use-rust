import sys
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs.agent import (  # noqa: E402
    Agent,
    HistoryItem,
    _cap_dom_for_llm,
    _is_page_state_message,
)
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
        agent.images_per_step = 1
        agent._messages = [UserMessage("task")]
        agent._read_state_for_next_turn = []
        agent._previous_evaluation = ""
        agent._memory = ""
        agent._next_goal = ""
        agent._valid_indices = set()
        agent.system_prompt = "system"
        agent.tools = []
        agent._history = []
        agent.state_cache_max_reuse_steps = 3
        agent._cached_page_state_fp = None
        agent._cached_page_state_step = 0
        agent._state_cache_reuse_count = 0
        agent._last_page_state_reused = False
        agent._last_page_state_had_read_state = False
        agent.state = type("State", (), {"n_steps": 1})()
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

    def test_images_per_step_zero_suppresses_llm_screenshot_attachment(self):
        agent = self._agent_for_state_injection()
        agent.images_per_step = 0

        agent._inject_page_state(
            BrowserStateSummary(
                url="https://example.com",
                screenshot="abc123",
                screenshot_media_type="image/jpeg",
                elements_text="[1]<button>Continue",
            )
        )

        content = agent._messages[-1].content
        self.assertIsInstance(content, str)
        self.assertIn("[1]<button>Continue", content)

    def test_unchanged_page_state_reuses_previous_full_state(self):
        agent = self._agent_for_state_injection()
        agent._valid_indices = {1}
        state = BrowserStateSummary(
            url="https://example.com",
            screenshot="abc123",
            screenshot_media_type="image/jpeg",
            elements_text="[1]<button>Continue",
        )

        agent._inject_page_state(state)
        full_state = agent._messages[-1]
        agent.state.n_steps = 2
        agent._inject_page_state(state)

        self.assertIs(agent._messages[-2], full_state)
        marker = agent._messages[-1]
        self.assertIsInstance(marker.content, str)
        self.assertTrue(marker.content.startswith("[PAGE_STATE_UNCHANGED]"))
        self.assertIn("retained PAGE_STATE from step 1", marker.content)
        self.assertTrue(agent._last_page_state_reused)
        self.assertEqual(agent._state_cache_reuse_count, 1)

        metrics = agent._compute_call_metrics()
        self.assertEqual(metrics["page_state_reuse_markers"], 1)
        self.assertTrue(metrics["state_reused"])
        self.assertEqual(metrics["state_reuse_count"], 1)

    def test_cleaned_dom_fingerprint_reuses_across_screenshot_churn(self):
        agent = self._agent_for_state_injection()
        state1 = BrowserStateSummary(
            url="https://example.com",
            screenshot="abc123",
            screenshot_media_type="image/jpeg",
            elements_text="[1]<button>Continue",
        )
        state2 = BrowserStateSummary(
            url="https://example.com",
            screenshot="def456",
            screenshot_media_type="image/jpeg",
            elements_text="[1]<button>Continue",
        )

        agent._inject_page_state(state1)
        agent.state.n_steps = 2
        agent._inject_page_state(state2)

        self.assertTrue(agent._last_page_state_reused)
        self.assertEqual(agent._state_cache_reuse_count, 1)

    def test_changed_cleaned_dom_replaces_cached_full_state(self):
        agent = self._agent_for_state_injection()
        state1 = BrowserStateSummary(
            url="https://example.com",
            screenshot="abc123",
            screenshot_media_type="image/jpeg",
            elements_text="[1]<button>Continue",
        )
        state2 = BrowserStateSummary(
            url="https://example.com",
            screenshot="abc123",
            screenshot_media_type="image/jpeg",
            elements_text="[1]<button>Different",
        )

        agent._inject_page_state(state1)
        agent.state.n_steps = 2
        agent._inject_page_state(state1)
        agent.state.n_steps = 3
        agent._inject_page_state(state2)

        page_states = [m for m in agent._messages if _is_page_state_message(m)]
        self.assertEqual(len(page_states), 1)
        self.assertFalse(agent._last_page_state_reused)
        self.assertEqual(agent._state_cache_reuse_count, 0)

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

    def test_cap_dom_for_llm_filters_omitted_indices(self):
        dom_text = (
            "URL: https://example.com\n"
            "TITLE: Example\n"
            "VIEWPORT: 1280x720\n"
            "PAGE_INFO: 0.0 pages above, 1.0 pages below - scroll down to reveal more\n"
            "\n"
            "ELEMENTS:\n"
            "[1]<button>Alpha\n"
            f"[2]<a href=\"{'x' * 1000}\">Oversized\n"
            "[3]<button>Gamma\n"
        )
        index_to_selector = {
            1: 'button "Alpha"',
            2: 'a "Oversized"',
            3: 'button "Gamma"',
        }

        capped, shown, metrics = _cap_dom_for_llm(
            dom_text,
            index_to_selector,
            512,
            {"total_bytes": len(dom_text)},
        )

        self.assertLessEqual(len(capped.encode("utf-8")), 512)
        self.assertIn("[DOM_TRUNCATED]", capped)
        self.assertIn("[1]<button>Alpha", capped)
        self.assertNotIn("[2]<a", capped)
        self.assertEqual(set(shown), {1, 3})
        self.assertTrue(metrics["truncated"])
        self.assertEqual(metrics["shown_interactive_count"], 2)
        self.assertEqual(metrics["omitted_interactive_count"], 1)

    def test_cap_dom_for_llm_can_be_disabled(self):
        dom_text = "URL: https://example.com\nELEMENTS:\n[1]<button>Alpha\n"
        index_to_selector = {1: 'button "Alpha"'}

        capped, shown, metrics = _cap_dom_for_llm(
            dom_text,
            index_to_selector,
            0,
            {"total_bytes": len(dom_text)},
        )

        self.assertEqual(capped, dom_text)
        self.assertEqual(shown, index_to_selector)
        self.assertFalse(metrics["truncated"])


if __name__ == "__main__":
    unittest.main()
