import importlib.util
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "bench_env_file_under_test",
        ROOT / "bench/env_file.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BenchEnvFileTests(unittest.TestCase):
    def test_load_dotenv_sets_missing_values_without_overriding_exports(self):
        env_file = _load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".env"
            path.write_text(
                "\n".join(
                    [
                        "GEMINI_API_KEY=file-key",
                        "export GOOGLE_API_KEY='quoted key'",
                        "EXISTING=from-file",
                    ]
                )
            )
            with patch.dict("os.environ", {"EXISTING": "from-env"}, clear=True):
                env_file.load_dotenv(path=path)

                self.assertEqual(env_file.os.environ["GEMINI_API_KEY"], "file-key")
                self.assertEqual(env_file.os.environ["GOOGLE_API_KEY"], "quoted key")
                self.assertEqual(env_file.os.environ["EXISTING"], "from-env")

    def test_load_dotenv_can_be_disabled(self):
        env_file = _load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".env"
            path.write_text("GEMINI_API_KEY=file-key\n")
            with patch.dict(
                "os.environ",
                {"BROWSER_USE_RS_DISABLE_DOTENV": "1"},
                clear=True,
            ):
                env_file.load_dotenv(path=path)

                self.assertNotIn("GEMINI_API_KEY", env_file.os.environ)

    def test_require_env_loads_dotenv_and_reports_missing_values(self):
        env_file = _load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / ".env"
            path.write_text("EVALUATION_TOOL_URL=https://example.test\n")
            with patch.dict("os.environ", {}, clear=True):
                with patch.object(env_file, "REPO", Path(tmpdir)):
                    self.assertEqual(
                        env_file.require_env("EVALUATION_TOOL_URL"),
                        "https://example.test",
                    )
                    with self.assertRaises(SystemExit) as ctx:
                        env_file.require_env("EVALUATION_TOOL_SECRET_KEY")
                    self.assertEqual(str(ctx.exception), "Set EVALUATION_TOOL_SECRET_KEY")


if __name__ == "__main__":
    unittest.main()
