import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs.agent import Agent  # noqa: E402
from browser_use_rs.llm.base import ToolCall  # noqa: E402
from browser_use_rs.tools import Tool  # noqa: E402


class CdpRecoveryTests(unittest.TestCase):
    def test_navigation_like_tool_retries_on_fresh_tab_after_stale_target(self):
        class Session:
            def __init__(self):
                self.new_tab_called = False
                self.switches = []
                self.tool_calls = 0

            async def list_tabs(self):
                return [("dead-page", "https://dead.example", "", "page", True)]

            async def switch_tab(self, target_id):
                self.switches.append(target_id)

            async def new_tab(self, url=""):
                self.new_tab_called = True
                return ("fresh-page", "about:blank", "", "page", True)

        async def flaky_navigate(session, url):
            session.tool_calls += 1
            if not session.new_tab_called:
                raise RuntimeError(
                    "cdp: cdp protocol error -32001: Session with given id not found."
                )
            return f"loaded {url}"

        agent = object.__new__(Agent)
        session = Session()
        agent.session = session
        agent.tool_timeout = 1.0
        agent.sensitive_data = {}
        agent._index_to_selector = {}
        agent.tools_by_name = {
            "navigate": Tool(
                name="navigate",
                description="",
                input_schema={},
                func=flaky_navigate,
            )
        }

        result, message = asyncio.run(
            agent._run_tool(
                ToolCall(
                    id="tool-1",
                    name="navigate",
                    args={"url": "https://example.com"},
                )
            )
        )

        self.assertEqual(result.extracted_content, "loaded https://example.com")
        self.assertFalse(message.is_error)
        self.assertEqual(session.switches, ["dead-page"])
        self.assertTrue(session.new_tab_called)
        self.assertEqual(session.tool_calls, 3)


if __name__ == "__main__":
    unittest.main()
