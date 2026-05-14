import logging
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs.agent import Agent  # noqa: E402
from browser_use_rs.llm.base import BaseChatModel, ChatInvokeCompletion  # noqa: E402
from browser_use_rs.tools import tool  # noqa: E402


class DummyLLM(BaseChatModel):
    name = "dummy"
    model = "dummy"

    async def ainvoke(self, messages, tools, *, system=None):
        return ChatInvokeCompletion(text="done")


@tool
async def controller_tool(session) -> str:
    return "ok"


class Controller:
    def __init__(self):
        self.tools = [controller_tool]


class ListHandler(logging.Handler):
    def __init__(self):
        super().__init__(level=logging.WARNING)
        self.records = []

    def emit(self, record):
        self.records.append(record)


class AgentCompatTests(unittest.TestCase):
    def test_controller_kwarg_is_honored_without_ignored_kwarg_warning(self):
        controller = Controller()
        logger = logging.getLogger("browser_use_rs.agent")
        handler = ListHandler()
        old_level = logger.level
        logger.addHandler(handler)
        logger.setLevel(logging.WARNING)
        try:
            agent = Agent(
                "check controller compat",
                DummyLLM(),
                controller=controller,
                browser_session=object(),
                auto_initial_navigation=False,
            )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        ignored_warnings = [
            record.getMessage()
            for record in handler.records
            if "ignored kwargs" in record.getMessage()
        ]
        self.assertEqual([], ignored_warnings)
        self.assertIs(agent.controller, controller)
        self.assertIn("controller_tool", agent.tools_by_name)


if __name__ == "__main__":
    unittest.main()
