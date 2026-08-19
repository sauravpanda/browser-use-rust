import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs.agent import Agent  # noqa: E402
from browser_use_rs.llm.base import ToolCall, ToolResultMessage  # noqa: E402
from browser_use_rs.views import ActionResult, BrowserStateSummary  # noqa: E402


class BatchGuardHandlingTests(unittest.TestCase):
    def _agent_with_runner(self, runner, urls=None):
        class Session:
            def __init__(self):
                self._urls = list(urls or [])

            async def current_url(self):
                if len(self._urls) > 1:
                    return self._urls.pop(0)
                return self._urls[0] if self._urls else "https://example.com/page"

        agent = object.__new__(Agent)
        agent.session = Session()
        agent._READ_ONLY_TOOLS = frozenset({"get_text"})
        agent._INDEXED_TOOLS = frozenset({"click"})
        agent._indices_invalidated = False
        agent._run_tool = runner
        return agent

    def test_same_page_indexed_batch_executes_fully(self):
        # v0.12.19: the indexed-after-mutation guard is retired. A
        # same-page [type_text, click] style batch runs to completion;
        # stale indices are handled downstream by selector retargeting.
        ran = []

        async def runner(tc):
            ran.append(tc.id)
            return (
                ActionResult(extracted_content=f"clicked {tc.args['index']}"),
                ToolResultMessage(
                    tool_call_id=tc.id,
                    name=tc.name,
                    content=f"clicked {tc.args['index']}",
                ),
            )

        agent = self._agent_with_runner(runner)
        calls = [
            ToolCall(id="a", name="click", args={"index": 1}),
            ToolCall(id="b", name="click", args={"index": 2}),
        ]

        results = asyncio.run(agent._run_tools_sequentially(calls))

        self.assertEqual(["a", "b"], ran)
        self.assertEqual("clicked 2", results[1][0].extracted_content)
        self.assertTrue(agent._indices_invalidated)

    def test_navigation_mid_batch_skips_non_read_remainder(self):
        async def runner(tc):
            return (
                ActionResult(extracted_content=f"ran {tc.name}"),
                ToolResultMessage(
                    tool_call_id=tc.id, name=tc.name, content=f"ran {tc.name}"
                ),
            )

        agent = self._agent_with_runner(
            runner,
            urls=["https://example.com/a", "https://example.com/b"],
        )
        calls = [
            ToolCall(id="a", name="click", args={"index": 1}),
            ToolCall(id="b", name="click", args={"index": 2}),
            ToolCall(id="c", name="get_text", args={"selector": "h1"}),
        ]

        results = asyncio.run(agent._run_tools_sequentially(calls))

        # Prior action succeeded → the skip stub is non-error feedback.
        skipped_result, skipped_message = results[1]
        self.assertIsNone(skipped_result.error)
        self.assertIn("skipped: page navigated", skipped_result.extracted_content)
        self.assertLessEqual(len(skipped_result.extracted_content), 180)
        self.assertFalse(skipped_message.is_error)
        # Read-only calls after the navigation still run.
        self.assertEqual("ran get_text", results[2][0].extracted_content)

    def test_tool_timeout_message_is_bounded(self):
        async def slow_tool(session, **kwargs):
            await asyncio.sleep(1)

        tool = type(
            "Tool",
            (),
            {"name": "evaluate_js", "func": staticmethod(slow_tool)},
        )()
        agent = object.__new__(Agent)
        agent.session = object()
        agent.tools_by_name = {"evaluate_js": tool}
        agent.sensitive_data = {}
        agent.tool_timeout = 0.01
        agent._INDEXED_TOOLS = frozenset()
        agent._valid_indices = set()
        agent._indices_invalidated = False
        agent._index_to_selector = {}

        call = ToolCall(
            id="a",
            name="evaluate_js",
            args={"code": "document.body.innerText + " + ("x" * 1000)},
        )

        result, message = asyncio.run(agent._run_tool(call))

        self.assertIn("tool timed out after 0s", result.error)
        self.assertIn("evaluate_js(", result.error)
        self.assertLessEqual(len(result.error), 230)
        self.assertTrue(message.is_error)

    def test_loop_and_budget_nudges_are_bounded(self):
        agent = object.__new__(Agent)
        agent._loop_nudge_cooldown = 0
        agent._recent_action_sigs = []
        agent._recent_urls = []
        agent._recent_tool_names = []
        agent._budget_warning_fired = False
        agent._messages = []

        call = ToolCall(id="a", name="click", args={"index": 1})
        state = BrowserStateSummary(url="https://example.com")

        agent._maybe_inject_loop_nudge(state, [call], step_n=25, max_steps=30)
        budget_msg = agent._messages[-1].content
        self.assertIn("[BUDGET_WARNING]", budget_msg)
        self.assertLessEqual(len(budget_msg), 140)

        agent._messages = []
        agent._loop_nudge_cooldown = 0
        agent._recent_action_sigs = []
        agent._recent_urls = []
        agent._recent_tool_names = []
        agent._budget_warning_fired = True
        for step in range(1, 4):
            agent._maybe_inject_loop_nudge(state, [call], step_n=step, max_steps=30)

        loop_msg = agent._messages[-1].content
        self.assertIn("[LOOP_DETECTED]", loop_msg)
        self.assertLessEqual(len(loop_msg), 160)

    def test_batch_failed_nudge_is_bounded(self):
        agent = object.__new__(Agent)
        agent._messages = []

        calls = [
            ToolCall(id="a", name="click", args={"index": 1}),
            ToolCall(id="b", name="click", args={"index": 2}),
            ToolCall(id="c", name="click", args={"index": 3}),
        ]
        results = [
            ActionResult(error="click failed"),
            ActionResult(error="click failed"),
            ActionResult(extracted_content="ok"),
        ]

        agent._maybe_inject_single_action_hint(calls, results)

        msg = agent._messages[-1].content
        self.assertIn("[BATCH_FAILED]", msg)
        self.assertLessEqual(len(msg), 150)


if __name__ == "__main__":
    unittest.main()
