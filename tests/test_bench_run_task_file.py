import importlib.util
import io
import json
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "run_task_file_under_test",
        ROOT / "bench/run_task_file.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RunTaskFileTests(unittest.TestCase):
    def test_load_tasks_accepts_posthog_sample_shape(self):
        runner = _load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            task_file = Path(tmpdir) / "sample.json"
            task_file.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "task_id": "p1",
                                "task": "Go to https://example.com",
                                "source": "Posthog",
                            },
                            {
                                "task_id": "p2",
                                "task": "Second task",
                            },
                        ]
                    }
                )
            )

            tasks = runner.load_tasks(task_file, limit=1)

        self.assertEqual(tasks, [
            {
                "taskId": "p1",
                "task": "Go to https://example.com",
                "source": "Posthog",
            }
        ])

    def test_load_tasks_accepts_json_list_and_prompt_key(self):
        runner = _load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            task_file = Path(tmpdir) / "tasks.json"
            task_file.write_text(json.dumps([{"id": 7, "prompt": "Prompt task"}]))

            tasks = runner.load_tasks(task_file)

        self.assertEqual(tasks, [{"taskId": "7", "task": "Prompt task"}])

    def test_main_dry_run_writes_plan_without_model_key_or_subprocess(self):
        runner = _load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            task_file = Path(tmpdir) / "posthog.json"
            out_path = Path(tmpdir) / "plan.json"
            task_file.write_text(
                json.dumps(
                    {
                        "tasks": [
                            {
                                "task_id": "1",
                                "task": "Read https://example.com/news",
                                "source": "Posthog",
                            }
                        ]
                    }
                )
            )
            argv = [
                "run_task_file.py",
                str(task_file),
                "--system",
                "both",
                "--max-steps",
                "33",
                "--python",
                "python3",
                "--dry-run",
                "--out",
                str(out_path),
            ]
            with (
                patch.object(sys, "argv", argv),
                patch.dict(
                    "os.environ",
                    {"BROWSER_USE_RS_DISABLE_DOTENV": "1"},
                    clear=True,
                ),
                patch.object(
                    runner.rerun,
                    "_run_one",
                    side_effect=AssertionError("dry-run executed a runner"),
                ),
                redirect_stdout(io.StringIO()),
            ):
                runner.main()

            payload = json.loads(out_path.read_text())

        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["systems"], ["ours", "theirs"])
        self.assertEqual(payload["summary"], {"tasks": 1, "plannedRuns": 2})
        self.assertEqual(payload["review"], [])
        self.assertEqual(payload["tasks"][0]["taskId"], "1")
        self.assertNotIn("results", payload["tasks"][0])
        self.assertIn("ours", payload["tasks"][0]["plannedCommands"])
        self.assertIn("theirs", payload["tasks"][0]["plannedCommands"])

    def test_main_requires_model_key_when_not_dry_run(self):
        runner = _load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            task_file = Path(tmpdir) / "tasks.json"
            task_file.write_text(json.dumps([{"task": "Task"}]))
            argv = ["run_task_file.py", str(task_file)]

            with (
                patch.object(sys, "argv", argv),
                patch.dict(
                    "os.environ",
                    {"BROWSER_USE_RS_DISABLE_DOTENV": "1"},
                    clear=True,
                ),
                self.assertRaises(SystemExit) as ctx,
            ):
                runner.main()

        self.assertIn("GEMINI_API_KEY", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
