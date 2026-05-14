import asyncio
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_tools_module():
    spec = importlib.util.spec_from_file_location(
        "tools_under_test",
        ROOT / "python/browser_use_rs/tools.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    old = sys.modules.get("tools_under_test")
    sys.modules["tools_under_test"] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        if old is None:
            sys.modules.pop("tools_under_test", None)
        else:
            sys.modules["tools_under_test"] = old


def _load_extra_tools_module(name):
    tools = _load_tools_module()
    pkg = types.ModuleType("browser_use_rs")

    old_modules = {
        mod_name: sys.modules.get(mod_name)
        for mod_name in (
            "browser_use_rs",
            "browser_use_rs.tools",
            name,
        )
    }
    sys.modules["browser_use_rs"] = pkg
    sys.modules["browser_use_rs.tools"] = tools
    spec = importlib.util.spec_from_file_location(
        name,
        ROOT / "python/browser_use_rs/_extra_tools.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
        return module
    finally:
        for mod_name, old in old_modules.items():
            if old is None:
                sys.modules.pop(mod_name, None)
            else:
                sys.modules[mod_name] = old


class ToolArgumentNormalizationTests(unittest.TestCase):
    def test_unknown_tool_args_are_ignored_without_schema_bloat(self):
        tools = _load_tools_module()

        @tools.tool
        async def demo(session, query: str) -> str:
            """Demo tool.

            Args:
                query: Search query.
            """
            return query

        self.assertEqual(
            asyncio.run(demo.func(object(), query="phones", shadow_dom=True)),
            "phones",
        )
        self.assertNotIn("shadow_dom", demo.input_schema["properties"])

    def test_observed_u_prefixed_argument_alias_is_normalized(self):
        tools = _load_tools_module()

        @tools.tool
        async def extract(session, query: str, output_schema_hint: str = "") -> str:
            """Extract.

            Args:
                query: Query.
                output_schema_hint: Schema hint.
            """
            return f"{query}|{output_schema_hint}"

        self.assertEqual(
            asyncio.run(
                extract.func(
                    object(),
                    query="q",
                    u_output_schema_hint='{"items":[]}',
                    target_id="ignored",
                )
            ),
            'q|{"items":[]}',
        )
        self.assertNotIn("u_output_schema_hint", extract.input_schema["properties"])

        self.assertEqual(
            asyncio.run(
                extract.func(
                    object(),
                    query="q",
                    u_output_schema_hint='{"alias": true}',
                    output_schema_hint='{"canonical": true}',
                )
            ),
            'q|{"canonical": true}',
        )

    def test_evaluate_js_returns_page_javascript_errors_as_tool_output(self):
        extra_tools = _load_extra_tools_module("_extra_tools_under_test")

        class Session:
            async def evaluate(self, expression):
                raise RuntimeError(
                    "unexpected response from evaluate: Uncaught: "
                    "TypeError: Cannot read properties of null (reading 'click')"
                )

        out = asyncio.run(
            extra_tools.evaluate_js.func(
                Session(), "document.querySelector('button').click()"
            )
        )

        self.assertIn("javascript error", out)
        self.assertIn("querySelector only accepts CSS", out)

    def test_evaluate_js_preserves_cdp_stale_errors_for_retry_path(self):
        extra_tools = _load_extra_tools_module("_extra_tools_under_test")

        class Session:
            async def evaluate(self, expression):
                raise RuntimeError("cdp protocol error -32001: Session with given id not found")

        with self.assertRaises(RuntimeError):
            asyncio.run(extra_tools.evaluate_js.func(Session(), "location.href"))


if __name__ == "__main__":
    unittest.main()
