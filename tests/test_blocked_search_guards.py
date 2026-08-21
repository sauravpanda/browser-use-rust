import asyncio
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

import browser_use_rs._extra_tools as extra_tools  # noqa: E402
from browser_use_rs._extra_tools import _SEARCH_CHALLENGE_CACHE, web_search  # noqa: E402
from browser_use_rs.agent import (  # noqa: E402
    Agent,
    BLOCKED_SITE_POLICY,
    BLOCKED_STATE_FORCE_COUNT,
    BLOCKED_STATE_FORCE_MIN_STEP,
    BLOCKED_STATE_WINDOW,
    DEFAULT_SYSTEM_PROMPT,
    FLASH_SYSTEM_PROMPT,
    SEARCH_FALLBACK_FORCE_COUNT,
    SEARCH_FALLBACK_FORCE_MIN_STEP,
    SEARCH_FALLBACK_WINDOW,
    BrowserStateSummary,
)


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
        self.assertIn("consumes the search fallback budget", out)
        self.assertIn("Do not retry the same search engine", out)

    def test_blocked_site_policy_is_shared_and_budgeted(self):
        self.assertIn("at most three recovery moves", BLOCKED_SITE_POLICY)
        self.assertIn(
            "public, non-live facts",
            BLOCKED_SITE_POLICY,
        )
        self.assertIn("live/current", BLOCKED_SITE_POLICY)
        self.assertEqual(
            DEFAULT_SYSTEM_PROMPT.count("at most three recovery moves"),
            1,
        )
        self.assertEqual(
            FLASH_SYSTEM_PROMPT.count("at most three recovery moves"),
            1,
        )

    def test_blocked_loop_thresholds_match_softened_eval_budget(self):
        self.assertEqual(BLOCKED_STATE_WINDOW, 8)
        self.assertEqual(BLOCKED_STATE_FORCE_COUNT, 5)
        self.assertEqual(BLOCKED_STATE_FORCE_MIN_STEP, 15)
        self.assertEqual(SEARCH_FALLBACK_WINDOW, 8)
        self.assertEqual(SEARCH_FALLBACK_FORCE_COUNT, 6)
        self.assertEqual(SEARCH_FALLBACK_FORCE_MIN_STEP, 20)

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


class _ProfileLLM:
    """Bare-attribute stub for policy-profile resolution tests."""

    def __init__(self, model, reasoning_effort=None):
        self.model = model
        self.reasoning_effort = reasoning_effort

    async def ainvoke(self, messages, tools, *, system=None, tool_choice=None):
        raise AssertionError("profile tests never invoke the model")


class SearchClampsFlagTests(unittest.TestCase):
    @staticmethod
    def _agent(llm, **kwargs):
        from browser_use_rs.agent import Agent

        kwargs.setdefault("browser_session", object())
        kwargs.setdefault("auto_initial_navigation", False)
        return Agent("look up a fact", llm, **kwargs)

    def test_search_clamps_defaults_to_auto(self):
        import inspect

        from browser_use_rs.agent import Agent

        sig = inspect.signature(Agent.__init__)
        self.assertIn("search_clamps", sig.parameters)
        self.assertIsNone(sig.parameters["search_clamps"].default)

    def test_auto_profile_guards_flash_class_models(self):
        from browser_use_rs.agent.prompts import RESEARCH_POLICY_OVERRIDE

        agent = self._agent(_ProfileLLM("gemini-3-flash-preview"))
        self.assertTrue(agent.search_clamps)
        self.assertNotIn(RESEARCH_POLICY_OVERRIDE, agent.system_prompt)

    def test_auto_profile_unclamps_strong_reasoning_models(self):
        from browser_use_rs.agent.prompts import RESEARCH_POLICY_OVERRIDE

        agent = self._agent(_ProfileLLM("gpt-5.6-luna", reasoning_effort="xhigh"))
        self.assertFalse(agent.search_clamps)
        self.assertIn(RESEARCH_POLICY_OVERRIDE, agent.system_prompt)
        # Low effort on the same model keeps the guards — only
        # high/xhigh were taken through the gate.
        low = self._agent(_ProfileLLM("gpt-5.6-luna", reasoning_effort="low"))
        self.assertTrue(low.search_clamps)

    def test_explicit_flag_beats_auto_rule(self):
        from browser_use_rs.agent.prompts import RESEARCH_POLICY_OVERRIDE

        forced_off = self._agent(
            _ProfileLLM("gemini-3-flash-preview"), search_clamps=False
        )
        self.assertFalse(forced_off.search_clamps)
        self.assertIn(RESEARCH_POLICY_OVERRIDE, forced_off.system_prompt)
        forced_on = self._agent(
            _ProfileLLM("gpt-5.6-luna", reasoning_effort="xhigh"), search_clamps=True
        )
        self.assertTrue(forced_on.search_clamps)
        self.assertNotIn(RESEARCH_POLICY_OVERRIDE, forced_on.system_prompt)

    def test_probe_style_extend_message_is_not_duplicated(self):
        from browser_use_rs.agent.prompts import RESEARCH_POLICY_OVERRIDE

        agent = self._agent(
            _ProfileLLM("gpt-5.6-luna", reasoning_effort="xhigh"),
            extend_system_message=RESEARCH_POLICY_OVERRIDE,
        )
        self.assertEqual(agent.system_prompt.count(RESEARCH_POLICY_OVERRIDE), 1)

    def test_all_three_clamp_gates_check_the_flag(self):
        # The BOT_BLOCKED force, SEARCH_FALLBACK nudge, and SEARCH_FALLBACK
        # force must each be conditioned on self.search_clamps so that
        # search_clamps=False actually lifts every clamp it promises to.
        import inspect

        import browser_use_rs.agent as agent_mod

        src = inspect.getsource(agent_mod)
        self.assertGreaterEqual(src.count("self.search_clamps"), 3)


if __name__ == "__main__":
    unittest.main()


class AnswerContractProfileTests(unittest.TestCase):
    @staticmethod
    def _agent(llm, **kwargs):
        from browser_use_rs.agent import Agent

        kwargs.setdefault("browser_session", object())
        kwargs.setdefault("auto_initial_navigation", False)
        return Agent("look up a fact", llm, **kwargs)

    def test_auto_on_for_qwen_off_for_gemini(self):
        from browser_use_rs.agent.prompts import ANSWER_CONTRACT_OVERRIDE

        q = self._agent(_ProfileLLM("qwen/qwen3.8-27b"))
        self.assertTrue(q.answer_contract)
        self.assertIn(ANSWER_CONTRACT_OVERRIDE, q.system_prompt)
        g = self._agent(_ProfileLLM("gemini-3-flash-preview"))
        self.assertFalse(g.answer_contract)
        self.assertNotIn(ANSWER_CONTRACT_OVERRIDE, g.system_prompt)
        forced = self._agent(
            _ProfileLLM("gemini-3-flash-preview"), answer_contract=True
        )
        self.assertTrue(forced.answer_contract)

    def test_phantom_tools_dispatchable_but_not_advertised(self):
        q = self._agent(_ProfileLLM("qwen/qwen3.8-27b"))
        self.assertIn("memory", q.tools_by_name)
        self.assertIn("evaluation_previous_goal", q.tools_by_name)
        advertised = {t.name for t in q.tools}
        self.assertNotIn("memory", advertised)
        self.assertNotIn("evaluation_previous_goal", advertised)
        g = self._agent(_ProfileLLM("gemini-3-flash-preview"))
        self.assertNotIn("memory", g.tools_by_name)

    def test_think_blocks_stripped_from_answers(self):
        from browser_use_rs.agent import Agent

        s = Agent._strip_state_tags_for_answer
        self.assertEqual(
            "The answer is 42.",
            s("<think>let me reason about this</think>The answer is 42."),
        )
        self.assertEqual("The answer is 42.", s("(Thinking) The answer is 42."))
        self.assertEqual("The answer is 42.", s("Thinking: The answer is 42."))
        self.assertEqual("plain already", s("plain already"))
