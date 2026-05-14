import asyncio
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs._extra_tools import make_extra_tools  # noqa: E402
from browser_use_rs.llm.base import ChatInvokeCompletion  # noqa: E402


class CapturingLLM:
    def __init__(self):
        self.messages = None
        self.system = None

    async def ainvoke(self, messages, tools, *, system=None):
        self.messages = messages
        self.system = system
        return ChatInvokeCompletion(text="OK")


class CountingLLM:
    def __init__(self):
        self.calls = 0
        self.messages = []

    async def ainvoke(self, messages, tools, *, system=None):
        self.calls += 1
        self.messages.append((messages, system))
        return ChatInvokeCompletion(text=f"answer-{self.calls}")


class ExtractorRuntimeContextTests(unittest.TestCase):
    def test_extractor_prompt_includes_current_date_context(self):
        llm = CapturingLLM()

        class Agent:
            task = "Find the latest article. website: https://example.com"
            page_extraction_llm = None
            tool_timeout = 1

            class state:
                n_steps = 3

            def _record_usage(self, step, usage):
                self.recorded = (step, usage)

        agent = Agent()
        agent.llm = llm

        class Session:
            async def current_url(self):
                return "https://example.com/news"

            async def evaluate(self, expression):
                return "Headline\nPublished today\nArticle body"

            async def page_text(self, max_chars):
                raise AssertionError("markdown extraction should be enough")

        tools = make_extra_tools(agent)
        extract = next(t for t in tools if t.name == "extract_structured_data")

        out = asyncio.run(extract.func(Session(), query="Find the latest article"))

        self.assertEqual(out, "OK")
        prompt = llm.messages[0].content
        self.assertIn("<runtime_context>", prompt)
        self.assertIn("Current date:", prompt)
        self.assertIn("latest", prompt)
        self.assertIn("This context is not webpage evidence", prompt)
        self.assertIn("default or unrelated results", llm.system)
        self.assertIn("every requested filter or constraint", llm.system)
        self.assertIn("every returned item must visibly match", llm.system)

    def test_extractor_cache_respects_already_collected(self):
        llm = CountingLLM()

        class Agent:
            task = "List all matching items. website: https://example.com"
            page_extraction_llm = None
            tool_timeout = 1

            class state:
                n_steps = 3

            def _record_usage(self, step, usage):
                self.recorded = (step, usage)

        agent = Agent()
        agent.llm = llm

        class Session:
            async def current_url(self):
                return "https://example.com/items"

            async def evaluate(self, expression):
                return "Alpha\nBeta\nGamma"

            async def page_text(self, max_chars):
                raise AssertionError("markdown extraction should be enough")

        tools = make_extra_tools(agent)
        extract = next(t for t in tools if t.name == "extract_structured_data")
        session = Session()

        first = asyncio.run(extract.func(session, query="List items"))
        second = asyncio.run(
            extract.func(
                session,
                query="List items",
                already_collected=first,
            )
        )

        self.assertEqual(first, "answer-1")
        self.assertEqual(second, "answer-2")
        self.assertEqual(llm.calls, 2)
        second_prompt = llm.messages[-1][0][0].content
        self.assertIn("ITEMS ALREADY COLLECTED", second_prompt)
        self.assertIn("answer-1", second_prompt)

    def test_extractor_caps_requested_page_chars(self):
        llm = CapturingLLM()
        captured = {}

        class Agent:
            task = "Extract content. website: https://example.com"
            page_extraction_llm = None
            tool_timeout = 1

            class state:
                n_steps = 3

            def _record_usage(self, step, usage):
                self.recorded = (step, usage)

        agent = Agent()
        agent.llm = llm

        class Session:
            async def current_url(self):
                return "https://example.com/huge"

            async def evaluate(self, expression):
                return "x" * 70_000

            async def page_text(self, max_chars):
                captured["page_text_max_chars"] = max_chars
                return "fallback"

        tools = make_extra_tools(agent)
        extract = next(t for t in tools if t.name == "extract_structured_data")

        out = asyncio.run(
            extract.func(Session(), query="Extract content", max_chars=1_000_000)
        )

        self.assertEqual(out, "OK")
        prompt = llm.messages[0].content
        page = prompt.split("<webpage_content>\n", 1)[1].split(
            "\n</webpage_content>",
            1,
        )[0]
        self.assertEqual(len(page), 60_000)
        self.assertNotIn("page_text_max_chars", captured)


if __name__ == "__main__":
    unittest.main()
