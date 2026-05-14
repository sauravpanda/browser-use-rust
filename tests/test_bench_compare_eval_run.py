import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "compare_eval_run_under_test",
        ROOT / "bench/compare_eval_run.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CompareEvalRunTests(unittest.TestCase):
    def test_summarize_run_computes_release_metrics(self):
        mod = _load_module()

        summary = mod.summarize_run(
            {
                "runId": "run-a",
                "status": "completed",
                "totalTasks": 3,
                "successfulTasks": 2,
                "model": "gemini",
                "actionErrorCount": 7,
                "accessDeniedCount": 1,
            },
            [
                {"steps": 10, "taskDuration": 2.0, "usage": '{"total_cost": 0.1}'},
                {"steps": 20, "taskDuration": 4.0, "usage": {"total_cost": 0.3}},
                {
                    "steps": 30,
                    "taskDuration": 6.0,
                    "usage": "{bad json",
                    "errorCategory": "Incorrect Result",
                },
            ],
        )

        self.assertEqual(summary["runId"], "run-a")
        self.assertEqual(summary["successRate"], 0.666667)
        self.assertEqual(summary["tasksWithCost"], 2)
        self.assertEqual(summary["costCoverage"], 0.666667)
        self.assertEqual(summary["avgCostUsd"], 0.2)
        self.assertEqual(summary["totalCostUsd"], 0.4)
        self.assertEqual(summary["avgSteps"], 20.0)
        self.assertEqual(summary["p90Steps"], 30.0)
        self.assertEqual(summary["errorCategories"], [("Incorrect Result", 1)])

    def test_compare_summaries_reports_cost_and_success_deltas(self):
        mod = _load_module()

        delta = mod.compare_summaries(
            {
                "successRate": 0.69,
                "avgCostUsd": 0.1,
                "costCoverage": 1.0,
                "avgSteps": 20.0,
                "p90Steps": 50.0,
                "failedTasks": 61,
                "actionErrorCount": 95,
                "accessDeniedCount": 31,
            },
            {
                "successRate": 0.72,
                "avgCostUsd": 0.08,
                "costCoverage": 0.95,
                "avgSteps": 18.5,
                "p90Steps": 40.0,
                "failedTasks": 55,
                "actionErrorCount": 70,
                "accessDeniedCount": 28,
            },
        )

        self.assertEqual(delta["successRateDeltaPp"], 3.0)
        self.assertEqual(delta["avgCostDeltaUsd"], -0.02)
        self.assertEqual(delta["costCoverageDelta"], -0.05)
        self.assertEqual(delta["avgCostRatio"], 0.8)
        self.assertEqual(delta["avgStepsDelta"], -1.5)
        self.assertEqual(delta["p90StepsDelta"], -10.0)
        self.assertEqual(delta["failedTasksDelta"], -6)


if __name__ == "__main__":
    unittest.main()
