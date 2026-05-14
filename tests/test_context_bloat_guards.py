import asyncio
import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_scratchpad_module():
    spec = importlib.util.spec_from_file_location(
        "scratchpad_under_test",
        ROOT / "python/browser_use_rs/_scratchpad.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_browser_tools_module():
    pkg = types.ModuleType("browser_use_rs")
    tools = types.ModuleType("browser_use_rs.tools")

    def tool(fn):
        fn.name = fn.__name__
        fn.description = fn.__doc__ or ""
        fn.input_schema = {}
        return fn

    tools.tool = tool
    extra = types.ModuleType("browser_use_rs._extra_tools")
    extra.EXTRA_STATELESS_TOOLS = []

    old_modules = {
        name: sys.modules.get(name)
        for name in (
            "browser_use_rs",
            "browser_use_rs.tools",
            "browser_use_rs._extra_tools",
        )
    }
    sys.modules["browser_use_rs"] = pkg
    sys.modules["browser_use_rs.tools"] = tools
    sys.modules["browser_use_rs._extra_tools"] = extra
    try:
        spec = importlib.util.spec_from_file_location(
            "browser_tools_under_test",
            ROOT / "python/browser_use_rs/_browser_tools.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old in old_modules.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


def _load_extra_tools_module():
    pkg = types.ModuleType("browser_use_rs")
    tools = types.ModuleType("browser_use_rs.tools")

    def tool(fn):
        fn.name = fn.__name__
        fn.description = fn.__doc__ or ""
        fn.input_schema = {}
        return fn

    tools.tool = tool

    old_modules = {
        name: sys.modules.get(name)
        for name in (
            "browser_use_rs",
            "browser_use_rs.tools",
        )
    }
    sys.modules["browser_use_rs"] = pkg
    sys.modules["browser_use_rs.tools"] = tools
    try:
        spec = importlib.util.spec_from_file_location(
            "extra_tools_under_test",
            ROOT / "python/browser_use_rs/_extra_tools.py",
        )
        assert spec and spec.loader
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    finally:
        for name, old in old_modules.items():
            if old is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = old


class ContextBloatGuardTests(unittest.TestCase):
    def test_scratchpad_preview_is_byte_bounded_for_long_single_line_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("BROWSER_USE_RS_SCRATCHPAD")
            os.environ["BROWSER_USE_RS_SCRATCHPAD"] = tmp
            try:
                scratchpad = _load_scratchpad_module()
            finally:
                if old is None:
                    os.environ.pop("BROWSER_USE_RS_SCRATCHPAD", None)
                else:
                    os.environ["BROWSER_USE_RS_SCRATCHPAD"] = old

            long_url = "https://cm.g.doubleclick.net/partnerpixels?x=" + ("a" * 110_000)
            content = "\n".join(
                f"  [iframe:{i:02d}] {long_url} — {long_url}" for i in range(74)
            )

            spilled = scratchpad.maybe_spill(
                content,
                agent_id="test-agent",
                step=15,
                tool_name="list_tabs",
                max_bytes=32 * 1024,
                max_lines=1000,
            )

            self.assertIsNotNone(spilled)
            self.assertGreater(spilled.full_bytes, 16_000_000)
            self.assertLessEqual(
                len(spilled.preview.encode("utf-8", errors="replace")),
                32 * 1024,
            )
            self.assertIn("[SCRATCHPAD_PREVIEW_TRIMMED]", spilled.preview)
            self.assertTrue(Path(spilled.path).exists())

    def test_list_tabs_filters_noisy_iframes_and_preserves_real_targets(self):
        browser_tools = _load_browser_tools_module()
        long_ad = "https://cm.g.doubleclick.net/partnerpixels?x=" + ("a" * 10_000)
        real_iframe = "https://checkout.example.com/widget/session?id=" + ("b" * 1000)
        consent_iframe = "https://cdn.privacy-mgmt.com/index.html?message_id=abc"

        class Session:
            async def list_tabs(self):
                return [
                    ("page1", "https://site.example/page", "Main page", "page", True),
                    ("blank", None, None, "iframe", False),
                    ("consent1", consent_iframe, "Privacy Choices", "iframe", False),
                    ("ad1", long_ad, long_ad, "iframe", False),
                    (
                        "cf1",
                        "https://challenges.cloudflare.com/cdn-cgi/challenge-platform/h/b/turnstile/f/abc",
                        "turnstile",
                        "iframe",
                        False,
                    ),
                    ("frame1", real_iframe, "Checkout Widget", "iframe", False),
                ]

        out = asyncio.run(browser_tools.list_tabs(Session()))

        self.assertIn("[page:page1]", out)
        self.assertIn("[iframe:blank]", out)
        self.assertIn("[iframe:consent1]", out)
        self.assertIn("[iframe:frame1]", out)
        self.assertNotIn("ad1", out)
        self.assertNotIn("cf1", out)
        self.assertIn("omitted 2 ad/challenge iframe target(s)", out)
        self.assertLess(len(out), 1100)

    def test_click_waits_for_new_tab_to_leave_about_blank(self):
        browser_tools = _load_browser_tools_module()

        class Session:
            def __init__(self):
                self.current_url_calls = 0

            async def list_tabs(self):
                if self.current_url_calls == 0:
                    return [("old", "https://site.example", "", "page", True)]
                return [
                    ("old", "https://site.example", "", "page", False),
                    ("new", "https://target.example/article", "", "page", True),
                ]

            async def click_index(self, index):
                self.current_url_calls = 1

            async def switch_tab(self, target_id):
                self.switched = target_id

            async def current_url(self):
                self.current_url_calls += 1
                if self.current_url_calls < 3:
                    return "about:blank"
                return "https://target.example/article"

        out = asyncio.run(browser_tools.click(Session(), 7))

        self.assertIn("target_id=new", out)
        self.assertIn("https://target.example/article", out)
        self.assertNotIn("still about:blank", out)

    def test_tab_tools_accept_displayed_type_prefixed_target_ids(self):
        browser_tools = _load_browser_tools_module()

        class Session:
            def __init__(self):
                self.switched = None
                self.closed = None

            async def switch_tab(self, target_id):
                self.switched = target_id

            async def close_tab(self, target_id):
                self.closed = target_id

        session = Session()

        out = asyncio.run(browser_tools.switch_tab(session, "iframe:ABC123"))
        self.assertEqual(session.switched, "ABC123")
        self.assertEqual(out, "switched to tab ABC123")

        out = asyncio.run(browser_tools.close_tab(session, "[page:DEF456]"))
        self.assertEqual(session.closed, "DEF456")
        self.assertEqual(out, "closed tab DEF456")

    def test_sleep_caps_below_default_tool_timeout(self):
        browser_tools = _load_browser_tools_module()
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        old_sleep = browser_tools.asyncio.sleep
        browser_tools.asyncio.sleep = fake_sleep
        try:
            out = asyncio.run(browser_tools.sleep(object(), 30))
        finally:
            browser_tools.asyncio.sleep = old_sleep

        self.assertEqual(sleeps, [28.0])
        self.assertEqual(out, "slept 28.0s (requested 30s)")

    def test_scroll_times_out_before_outer_tool_timeout(self):
        browser_tools = _load_browser_tools_module()

        class Session:
            async def evaluate(self, expression):
                return "800"

            async def scroll(self, delta):
                await asyncio.sleep(10)

        old_timeout = browser_tools.MAX_SCROLL_SECONDS
        browser_tools.MAX_SCROLL_SECONDS = 0.01
        try:
            out = asyncio.run(
                browser_tools.scroll(Session(), direction="down", pages=1)
            )
        finally:
            browser_tools.MAX_SCROLL_SECONDS = old_timeout

        self.assertIn("scroll timed out after 0.01s", out)
        self.assertIn("page may be stuck loading or blocked", out)

    def test_scroll_default_moves_one_viewport_down(self):
        browser_tools = _load_browser_tools_module()

        class Session:
            def __init__(self):
                self.deltas = []

            async def evaluate(self, expression):
                return "720"

            async def scroll(self, delta):
                self.deltas.append(delta)

        session = Session()
        out = asyncio.run(browser_tools.scroll(session))

        self.assertEqual(session.deltas, [720])
        self.assertEqual(out, "scrolled down 1 pages (720px)")

    def test_scroll_direction_without_pages_moves_one_viewport(self):
        browser_tools = _load_browser_tools_module()

        class Session:
            def __init__(self):
                self.deltas = []

            async def evaluate(self, expression):
                return "640"

            async def scroll(self, delta):
                self.deltas.append(delta)

        session = Session()
        out = asyncio.run(browser_tools.scroll(session, direction="up"))

        self.assertEqual(session.deltas, [-640])
        self.assertEqual(out, "scrolled up 1 pages (-640px)")

    def test_page_text_caps_large_requests(self):
        browser_tools = _load_browser_tools_module()

        class Session:
            def __init__(self):
                self.max_chars = None

            async def page_text(self, max_chars):
                self.max_chars = max_chars
                return "body text"

        session = Session()
        out = asyncio.run(browser_tools.page_text(session, max_chars=1_000_000))

        self.assertEqual(session.max_chars, browser_tools.MAX_PAGE_TEXT_CHARS)
        self.assertIn("page_text capped to 50000 chars", out)
        self.assertIn("body text", out)

    def test_get_links_truncates_dense_pages(self):
        browser_tools = _load_browser_tools_module()

        class Session:
            async def get_links(self):
                return [
                    (f"https://example.com/{i}", f"Link {i}")
                    for i in range(browser_tools.MAX_GET_LINKS + 3)
                ]

        out = asyncio.run(browser_tools.get_links(Session()))

        self.assertIn("Link 0 -> https://example.com/0", out)
        self.assertIn("Link 99 -> https://example.com/99", out)
        self.assertNotIn("Link 100 -> https://example.com/100", out)
        self.assertIn("3 more links truncated", out)

    def test_get_links_clips_pathological_link_lines(self):
        browser_tools = _load_browser_tools_module()

        class Session:
            async def get_links(self):
                return [
                    (
                        "https://example.com/path?" + ("a" * 2000),
                        "Long\nTracking Link " + ("b" * 500),
                    )
                ]

        out = asyncio.run(browser_tools.get_links(Session()))

        self.assertLess(len(out), 520)
        self.assertIn("[", out)
        self.assertIn("chars", out)
        self.assertNotIn("\nTracking", out)

    def test_extract_structured_data_refuses_about_blank_without_llm_call(self):
        extra_tools = _load_extra_tools_module()

        class Llm:
            async def ainvoke(self, *args, **kwargs):
                raise AssertionError("extractor LLM should not be called")

        class Agent:
            _file_sandbox = None
            llm = Llm()

        class Session:
            evaluated = False

            async def current_url(self):
                return "about:blank"

            async def evaluate(self, expression):
                self.evaluated = True
                raise AssertionError("page JS should not be evaluated")

        tools = extra_tools.make_extra_tools(Agent())
        extract = next(t for t in tools if t.name == "extract_structured_data")
        session = Session()

        out = asyncio.run(extract(session, "extract any page data"))

        self.assertIn("current page is about:blank", out)
        self.assertFalse(session.evaluated)


if __name__ == "__main__":
    unittest.main()
