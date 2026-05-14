import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

import browser_use_rs._extra_tools as extra_tools  # noqa: E402
from browser_use_rs._extra_tools import _SEARCH_CHALLENGE_CACHE, web_search  # noqa: E402
from browser_use_rs.agent import Agent, BrowserStateSummary  # noqa: E402


class BlockedSearchGuardTests(unittest.TestCase):
    def setUp(self):
        _SEARCH_CHALLENGE_CACHE.clear()

    def test_agent_detects_search_and_cloudflare_challenge_states(self):
        google = BrowserStateSummary(
            url="https://www.google.com/sorry/index?continue=https://www.google.com/search",
            title="",
            screenshot=None,
            elements_text="Our systems have detected unusual traffic.",
        )
        cloudflare = BrowserStateSummary(
            url="https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/f/abc",
            title="Just a moment...",
            screenshot=None,
            elements_text="Checking your browser before accessing the site.",
        )
        normal = BrowserStateSummary(
            url="https://example.com/article",
            title="Article about CAPTCHA design",
            screenshot=None,
            elements_text="This article discusses CAPTCHA usability research.",
        )
        search_about_captcha = BrowserStateSummary(
            url="https://www.bing.com/search?q=captcha+usability",
            title="captcha usability - Search",
            screenshot=None,
            elements_text="Results about CAPTCHA usability research.",
        )

        self.assertEqual(Agent._blocked_state_reason(google), "Google CAPTCHA")
        self.assertEqual(
            Agent._blocked_state_reason(cloudflare),
            "Cloudflare challenge",
        )
        self.assertEqual(Agent._blocked_state_reason(normal), "")
        self.assertEqual(Agent._blocked_state_reason(search_about_captcha), "")

    def test_web_search_reports_search_engine_challenge_redirect(self):
        class Session:
            evaluate_calls = 0

            async def navigate(self, url):
                self.navigated = url

            async def current_url(self):
                return (
                    "https://www.google.com/sorry/index"
                    "?continue=https://www.google.com/search%3Fq%3Dwikiwand"
                )

        out = asyncio.run(
            web_search.func(Session(), "wikiwand artificial intelligence", engine="google")
        )

        self.assertIn("bot/CAPTCHA block", out)
        self.assertIn("Do not retry the same search engine repeatedly", out)

    def test_web_search_skips_engine_after_prior_challenge(self):
        class Session:
            def __init__(self):
                self.navigations = 0

            async def navigate(self, url):
                self.navigations += 1

            async def current_url(self):
                return "https://www.google.com/sorry/index"

        session = Session()
        first = asyncio.run(web_search.func(session, "wikiwand", engine="google"))
        second = asyncio.run(web_search.func(session, "another query", engine="google"))

        self.assertIn("opened google results", first)
        self.assertIn("skipped google search", second)
        self.assertEqual(session.navigations, 1)

    def test_web_search_detects_challenge_page_text_without_redirect(self):
        class Session:
            async def navigate(self, url):
                self.navigated = url

            async def current_url(self):
                return "https://www.google.com/search?q=wikiwand"

            async def evaluate(self, expression):
                return "Google Search\nOur systems have detected unusual traffic."

        out = asyncio.run(web_search.func(Session(), "wikiwand", engine="google"))

        self.assertIn("search-engine CAPTCHA", out)
        self.assertIn("bot/CAPTCHA block", out)

    def test_web_search_snippet_urls_are_clipped(self):
        class Session:
            def __init__(self):
                self.evaluate_calls = 0
                self.expressions = []

            async def navigate(self, url):
                self.navigated = url

            async def current_url(self):
                return "https://www.google.com/search?q=example"

            async def evaluate(self, expression):
                self.evaluate_calls += 1
                self.expressions.append(expression)
                if self.evaluate_calls == 1:
                    return "Google Search\nordinary results"
                return (
                    "1. Long result\n"
                    "   https://example.com/path?aaa ...[1900 chars]... zzz\n"
                    "   Snippet text"
                )

        async def fake_sleep(seconds):
            return None

        session = Session()
        with patch.dict(
            "os.environ",
            {"BROWSER_USE_RS_WEB_SEARCH_SNIPPETS": "1"},
            clear=False,
        ), patch.object(extra_tools.asyncio, "sleep", fake_sleep):
            out = asyncio.run(web_search.func(session, "example", engine="google"))

        self.assertIn("Top visible results", out)
        self.assertIn("Long result", out)
        self.assertIn("chars", out)
        self.assertIn("clipMiddle(parsed.href, 320)", session.expressions[-1])
        self.assertLess(len(out), 600)


if __name__ == "__main__":
    unittest.main()
