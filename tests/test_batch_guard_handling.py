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
    def _agent_with_runner(self, runner):
        class Session:
            async def current_url(self):
                return "https://example.com/page"

        agent = object.__new__(Agent)
        agent.session = Session()
        agent._READ_ONLY_TOOLS = frozenset()
        agent._INDEXED_TOOLS = frozenset({"click"})
        agent._indices_invalidated = False
        agent._run_tool = runner
        return agent

    def test_indexed_call_without_identity_is_skipped_non_error(self):
        async def runner(tc):
            return (
                ActionResult(extracted_content=f"clicked {tc.args['index']}"),
                ToolResultMessage(
                    tool_call_id=tc.id, name=tc.name, content=f"clicked {tc.args['index']}"
                ),
            )

        agent = self._agent_with_runner(runner)
        calls = [
            ToolCall(id="a", name="click", args={"index": 1}),
            ToolCall(id="b", name="click", args={"index": 2}),
        ]
        results = asyncio.run(agent._run_tools_sequentially(calls))
        skipped_result, skipped_message = results[1]
        self.assertIsNone(skipped_result.error)
        self.assertIn("skipped:", skipped_result.extracted_content)
        self.assertLessEqual(len(skipped_result.extracted_content), 180)
        self.assertFalse(skipped_message.is_error)

    def test_guard_skip_after_failed_mutation_stays_error_feedback(self):
        async def runner(tc):
            return (
                ActionResult(error="click failed"),
                ToolResultMessage(
                    tool_call_id=tc.id, name=tc.name, content="click failed", is_error=True
                ),
            )

        agent = self._agent_with_runner(runner)
        calls = [
            ToolCall(id="a", name="click", args={"index": 1}),
            ToolCall(id="b", name="click", args={"index": 2}),
        ]
        results = asyncio.run(agent._run_tools_sequentially(calls))
        skipped_result, skipped_message = results[1]
        self.assertIn("skipped:", skipped_result.error)
        self.assertTrue(skipped_message.is_error)

    def _snapshot_session(self, after_elements):
        class El:
            def __init__(self, index, selector):
                self.index, self.selector = index, selector

        class Snap:
            def __init__(self, els):
                self.elements = [El(i, s) for i, s in els]

        class Session:
            def __init__(self):
                self.snapshots = 0

            async def current_url(self):
                return "https://example.com/page"

            async def dom_snapshot(self):
                self.snapshots += 1
                return Snap(after_elements)

        return Session()

    def test_type_then_click_retargets_shifted_index(self):
        # v0.12.38: plan [type_text(1), click(2)]; typing opens a
        # suggestion panel that shifts every index by one. The click must
        # be re-located by selector to [3] instead of being skipped.
        seen = []

        async def runner(tc):
            seen.append((tc.name, tc.args["index"]))
            return (
                ActionResult(extracted_content=f"{tc.name} ok"),
                ToolResultMessage(tool_call_id=tc.id, name=tc.name, content="ok"),
            )

        agent = self._agent_with_runner(runner)
        agent._INDEXED_TOOLS = frozenset({"click", "type_text"})
        agent._index_to_selector = {1: 'input "Search"', 2: 'button "Go"'}
        agent._valid_indices = {1, 2}
        agent.session = self._snapshot_session(
            [(1, 'div "Suggestions"'), (2, 'input "Search"'), (3, 'button "Go"')]
        )
        calls = [
            ToolCall(id="a", name="type_text", args={"index": 1, "text": "q"}),
            ToolCall(id="b", name="click", args={"index": 2}),
        ]
        results = asyncio.run(agent._run_tools_sequentially(calls))
        self.assertEqual([("type_text", 1), ("click", 3)], seen)
        self.assertEqual(1, agent.session.snapshots)
        self.assertIn("re-located: [2] is now [3]", results[1][0].extracted_content)
        self.assertEqual(1, agent._batch_retargets)

    def test_ambiguous_target_after_mutation_is_skipped(self):
        seen = []

        async def runner(tc):
            seen.append((tc.name, tc.args["index"]))
            return (
                ActionResult(extracted_content="ok"),
                ToolResultMessage(tool_call_id=tc.id, name=tc.name, content="ok"),
            )

        agent = self._agent_with_runner(runner)
        agent._INDEXED_TOOLS = frozenset({"click"})
        agent._index_to_selector = {1: 'button "Add"', 2: 'a "Details"'}
        agent._valid_indices = {1, 2}
        # After the click the page re-rendered with THREE "Details" links
        # (count changed) — no safe way to pick one.
        agent.session = self._snapshot_session(
            [(1, 'button "Add"'), (2, 'a "Details"'), (3, 'a "Details"'), (4, 'a "Details"')]
        )
        calls = [
            ToolCall(id="a", name="click", args={"index": 1}),
            ToolCall(id="b", name="click", args={"index": 2}),
        ]
        results = asyncio.run(agent._run_tools_sequentially(calls))
        self.assertEqual([("click", 1)], seen)
        self.assertIn("ambiguous", results[1][0].extracted_content)
        self.assertEqual(1, agent._batch_skips)

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
