import asyncio
import json
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs._extra_tools import (  # noqa: E402
    EXTRA_STATELESS_TOOLS,
    extract_links,
    extract_result_cards,
)
from browser_use_rs.agent import Agent  # noqa: E402


class ResultCardToolTests(unittest.TestCase):
    def test_extract_result_cards_formats_visible_card_data(self):
        class Session:
            def __init__(self):
                self.expression = ""

            async def evaluate(self, expression):
                self.expression = expression
                return json.dumps(
                    {
                        "queryTerms": ["digital", "infrastructure"],
                        "cards": [
                            {
                                "title": "Telecoms Modernisation",
                                "url": "https://example.com/telecoms",
                                "date": "2026-05-13",
                                "termsMatched": ["digital", "infrastructure"],
                                "text": "Policy paper about digital infrastructure.",
                            },
                            {
                                "title": "Unrelated policy",
                                "url": "",
                                "date": "",
                                "termsMatched": [],
                                "text": "Another visible card.",
                            },
                        ],
                    }
                )

        session = Session()
        out = asyncio.run(
            extract_result_cards.func(
                session,
                limit=2,
                query="digital infrastructure policy papers",
            )
        )

        self.assertIn("const lim = 2;", session.expression)
        self.assertIn("query terms: digital, infrastructure", out)
        self.assertIn("1. Telecoms Modernisation — 2026-05-13", out)
        self.assertIn("url: https://example.com/telecoms", out)
        self.assertIn("query terms matched: digital, infrastructure", out)
        self.assertIn("2. Unrelated policy", out)

    def test_extract_result_cards_clips_long_urls(self):
        class Session:
            async def evaluate(self, expression):
                return json.dumps(
                    {
                        "cards": [
                            {
                                "title": "Tracking-heavy result",
                                "url": "https://example.com/result?" + ("a" * 2000),
                                "date": "",
                                "termsMatched": [],
                                "text": "Visible result text.",
                            }
                        ],
                    }
                )

        out = asyncio.run(extract_result_cards.func(Session()))

        self.assertLess(len(out), 520)
        self.assertIn("Tracking-heavy result", out)
        self.assertIn("chars", out)

    def test_extract_result_cards_handles_empty_or_bad_results(self):
        class EmptySession:
            async def evaluate(self, expression):
                return json.dumps({"cards": []})

        class BadSession:
            async def evaluate(self, expression):
                return "{not json"

        self.assertIn(
            "no visible result/list cards",
            asyncio.run(extract_result_cards.func(EmptySession())),
        )
        self.assertIn(
            "unparseable result",
            asyncio.run(extract_result_cards.func(BadSession())),
        )

    def test_extract_result_cards_is_registered_as_stateless_tool(self):
        names = [tool.name for tool in EXTRA_STATELESS_TOOLS]

        self.assertIn("extract_result_cards", names)

    def test_extract_links_clips_long_hrefs(self):
        class Session:
            async def get_links(self):
                return [
                    (
                        "https://example.com/product?" + ("x" * 2000),
                        "Product link",
                    )
                ]

        out = asyncio.run(extract_links.func(Session()))

        self.assertLess(len(out), 380)
        self.assertIn("Product link -> https://example.com/product?", out)
        self.assertIn("chars", out)

    def test_agent_treats_extract_result_cards_as_read_and_extract_tool(self):
        self.assertIn("extract_result_cards", Agent._READ_ONLY_CANONICAL)
        self.assertIn("extract_result_cards", Agent._EXTRACT_TOOLS)


if __name__ == "__main__":
    unittest.main()
