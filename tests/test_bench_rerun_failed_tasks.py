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
        "rerun_failed_tasks_under_test",
        ROOT / "bench/rerun_failed_tasks.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RerunFailedTasksTests(unittest.TestCase):
    def test_filter_tasks_by_id_preserves_requested_order(self):
        rerun = _load_module()
        tasks = [
            {"taskId": "507", "task": "expensive"},
            {"taskId": "2347", "task": "academic"},
            {"taskId": "184", "task": "buzzfeed"},
        ]

        selected = rerun._filter_tasks_by_id(tasks, ["184", "507"])

        self.assertEqual([row["taskId"] for row in selected], ["184", "507"])

    def test_filter_tasks_by_id_reports_missing_ids(self):
        rerun = _load_module()

        with self.assertRaises(SystemExit) as ctx:
            rerun._filter_tasks_by_id([{"taskId": "507"}], ["999"])

        self.assertIn("999", str(ctx.exception))

    def test_summary_includes_historical_deltas(self):
        rerun = _load_module()
        rows = [
            {
                "historicalCost": 1.25,
                "historicalSteps": 50,
                "results": {
                    "ours": {
                        "completed": True,
                        "success": True,
                        "steps": 20,
                        "elapsed_s": 12.5,
                        "cost_usd": 0.5,
                    }
                },
            },
            {
                "historicalCost": 0.75,
                "historicalSteps": 30,
                "results": {
                    "ours": {
                        "completed": False,
                        "success": False,
                        "steps": 10,
                        "elapsed_s": 8,
                        "cost_usd": 0.25,
                    }
                },
            },
        ]

        summary = rerun._summarize(rows)

        self.assertEqual(summary["historical"]["steps"], 80)
        self.assertEqual(summary["historical"]["cost_usd"], 2.0)
        self.assertEqual(summary["ours"]["steps"], 30)
        self.assertEqual(summary["ours"]["step_delta_vs_historical"], -50)
        self.assertEqual(summary["ours"]["cost_usd"], 0.75)
        self.assertEqual(summary["ours"]["cost_delta_vs_historical_usd"], -1.25)
        self.assertEqual(summary["ours"]["cost_ratio_vs_historical"], 0.375)

    def test_review_rows_include_compact_answer_previews(self):
        rerun = _load_module()
        rows = [
            {
                "taskId": "507",
                "errorCategory": "Give Up",
                "historicalCost": 1.25,
                "historicalSteps": 50,
                "task": "Collect the top three articles from the page",
                "results": {
                    "ours": {
                        "completed": True,
                        "success": False,
                        "steps": 9,
                        "elapsed_s": 12.5,
                        "cost_usd": 0.12,
                        "answer": "Line one\nLine two " + ("x" * 400),
                    }
                },
            }
        ]

        review = rerun._review_rows(rows)

        self.assertEqual(review[0]["taskId"], "507")
        self.assertEqual(review[0]["errorCategory"], "Give Up")
        self.assertIn("Collect the top three", review[0]["taskPreview"])
        ours = review[0]["systems"]["ours"]
        self.assertTrue(ours["completed"])
        self.assertFalse(ours["success"])
        self.assertEqual(ours["steps"], 9)
        self.assertEqual(ours["cost_usd"], 0.12)
        self.assertIn("Line one Line two", ours["answerPreview"])
        self.assertLessEqual(len(ours["answerPreview"]), 320)

    def test_planned_commands_shell_quote_task_text(self):
        rerun = _load_module()

        planned = rerun._planned_commands(
            python_bin=".venv/bin/python",
            runners={"ours": "bench/run_ours.py"},
            systems=["ours"],
            task="find Bob's report",
            max_steps=42,
        )

        self.assertIn(".venv/bin/python", planned["ours"])
        self.assertIn("bench/run_ours.py", planned["ours"])
        self.assertIn("'find Bob'\"'\"'s report'", planned["ours"])
        self.assertTrue(planned["ours"].endswith(" 42"))

    def test_main_dry_run_writes_plan_without_model_key_or_subprocess(self):
        rerun = _load_module()
        tasks = [
            {
                "taskId": "507",
                "task": {"prompt": "Collect the top three articles"},
                "steps": 87,
                "errorCategory": "Give Up",
                "usage": {"total_cost": 1.2345},
            }
        ]

        with tempfile.TemporaryDirectory() as tmpdir:
            out_path = Path(tmpdir) / "plan.json"
            argv = [
                "rerun_failed_tasks.py",
                "run123",
                "--top",
                "1",
                "--system",
                "both",
                "--max-steps",
                "55",
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
                patch.object(rerun, "_fetch_failed_tasks", return_value=tasks),
                patch.object(
                    rerun,
                    "_run_one",
                    side_effect=AssertionError("dry-run executed a runner"),
                ),
                redirect_stdout(io.StringIO()),
            ):
                rerun.main()

            payload = json.loads(out_path.read_text())

        self.assertTrue(payload["dryRun"])
        self.assertEqual(payload["runId"], "run123")
        self.assertEqual(payload["systems"], ["ours", "theirs"])
        self.assertEqual(payload["summary"]["plannedRuns"], 2)
        self.assertEqual(payload["summary"]["historical"]["runs"], 1)
        self.assertEqual(payload["summary"]["historical"]["steps"], 87)
        self.assertEqual(payload["summary"]["historical"]["cost_usd"], 1.2345)
        self.assertEqual(payload["review"], [])
        self.assertEqual(payload["tasks"][0]["taskId"], "507")
        self.assertNotIn("results", payload["tasks"][0])
        self.assertIn("ours", payload["tasks"][0]["plannedCommands"])
        self.assertIn("theirs", payload["tasks"][0]["plannedCommands"])
        self.assertIn("Collect the top three articles", payload["tasks"][0]["task"])


if __name__ == "__main__":
    unittest.main()
