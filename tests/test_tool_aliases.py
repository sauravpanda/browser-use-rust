import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs._browser_tools import (  # noqa: E402
    ALIAS_TO_CANONICAL,
    BROWSER_TOOLS,
)


class ToolAliasTests(unittest.TestCase):
    def test_alias_descriptions_are_compact_and_keep_schema(self):
        tools = {tool.name: tool for tool in BROWSER_TOOLS}

        for alias, canonical in ALIAS_TO_CANONICAL.items():
            if alias == canonical or alias not in tools or canonical not in tools:
                continue
            with self.subTest(alias=alias):
                alias_tool = tools[alias]
                canonical_tool = tools[canonical]
                self.assertLessEqual(len(alias_tool.description), 90)
                self.assertIn(canonical, alias_tool.description)
                self.assertEqual(
                    alias_tool.input_schema,
                    canonical_tool.input_schema,
                )
                self.assertIs(alias_tool.func, canonical_tool.func)


if __name__ == "__main__":
    unittest.main()
