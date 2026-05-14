import asyncio
import importlib.util
import io
import json
import os
import subprocess
import sys
import types
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_runner_module(path: str):
    spec = importlib.util.spec_from_file_location(
        f"{Path(path).stem}_under_test",
        ROOT / path,
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BenchRunnerTests(unittest.TestCase):
    def _run_without_model_key(self, runner: str) -> dict:
        env = os.environ.copy()
        env.pop("GEMINI_API_KEY", None)
        env.pop("GOOGLE_API_KEY", None)
        env["BROWSER_USE_RS_DISABLE_DOTENV"] = "1"
        proc = subprocess.run(
            [sys.executable, str(ROOT / runner), "smoke task", "1"],
            cwd=str(ROOT),
            env=env,
            text=True,
            capture_output=True,
            timeout=10,
            check=False,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        return json.loads(proc.stdout)

    def test_run_ours_missing_key_preflight_is_clean_json(self):
        payload = self._run_without_model_key("bench/run_ours.py")

        self.assertEqual(payload["system"], "ours")
        self.assertFalse(payload["completed"])
        self.assertFalse(payload["success"])
        self.assertIn("missing GEMINI_API_KEY or GOOGLE_API_KEY", payload["answer"])

    def test_run_theirs_missing_key_preflight_is_clean_json(self):
        payload = self._run_without_model_key("bench/run_theirs.py")

        self.assertEqual(payload["system"], "theirs")
        self.assertFalse(payload["completed"])
        self.assertFalse(payload["success"])
        self.assertIn("missing GEMINI_API_KEY or GOOGLE_API_KEY", payload["answer"])

    def test_run_ours_constructor_failure_is_clean_json(self):
        runner = _load_runner_module("bench/run_ours.py")

        class FailingAgent:
            def __init__(self, *args, **kwargs):
                raise RuntimeError("agent constructor failed")

        class ChatGoogle:
            def __init__(self, *args, **kwargs):
                pass

        fake_pkg = types.ModuleType("browser_use_rs")
        fake_pkg.Agent = FailingAgent
        fake_llm = types.ModuleType("browser_use_rs.llm")
        fake_llm.ChatGoogle = ChatGoogle
        stdout = io.StringIO()
        with (
            patch.object(sys, "argv", ["run_ours.py", "smoke task", "1"]),
            patch.dict(
                os.environ,
                {
                    "GEMINI_API_KEY": "test-key",
                    "BROWSER_USE_RS_DISABLE_DOTENV": "1",
                },
                clear=True,
            ),
            patch.dict(
                sys.modules,
                {
                    "browser_use_rs": fake_pkg,
                    "browser_use_rs.llm": fake_llm,
                },
            ),
            redirect_stdout(stdout),
        ):
            asyncio.run(runner.main())

        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["system"], "ours")
        self.assertFalse(payload["completed"])
        self.assertFalse(payload["success"])
        self.assertIn("agent constructor failed", payload["answer"])
        self.assertEqual(payload["steps"], 0)


if __name__ == "__main__":
    unittest.main()
