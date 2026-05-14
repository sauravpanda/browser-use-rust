import importlib.util
import os
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "analyze_runs_under_test",
        ROOT / "bench/analyze_runs.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    with patch.dict(
        os.environ,
        {
            "EVALUATION_TOOL_URL": "https://example.test",
            "EVALUATION_TOOL_SECRET_KEY": "secret",
            "BROWSER_USE_RS_DISABLE_DOTENV": "1",
        },
    ):
        spec.loader.exec_module(module)
    return module


class AnalyzeRunsTests(unittest.TestCase):
    def test_prompt_metric_rollup_reads_step_metadata(self):
        mod = _load_module()
        detail = {
            "completeHistory": [
                {
                    "metadata": {
                        "prompt_agent_history_bytes": 100,
                        "prompt_read_state_bytes": 0,
                        "prompt_history_items": 1,
                        "prompt_history_collapsed_items": 0,
                        "prompt_n_messages": 5,
                    }
                },
                {
                    "metadata": {
                        "prompt_agent_history_bytes": 300,
                        "prompt_read_state_bytes": 80,
                        "prompt_history_items": 3,
                        "prompt_history_collapsed_items": 2,
                        "prompt_n_messages": 7,
                    }
                },
            ]
        }

        out = mod.prompt_metric_rollup(detail)

        self.assertEqual(out["prompt_agent_history_bytes_mean"], 200)
        self.assertEqual(out["prompt_agent_history_bytes_max"], 300)
        self.assertEqual(out["prompt_read_state_bytes_mean"], 40)
        self.assertEqual(out["prompt_read_state_bytes_max"], 80)
        self.assertEqual(out["prompt_history_items_mean"], 2)
        self.assertEqual(out["prompt_history_collapsed_items_mean"], 1)
        self.assertEqual(out["prompt_n_messages_mean"], 6)

    def test_summarize_includes_prompt_metric_stats_when_present(self):
        mod = _load_module()
        summary = mod.summarize(
            {
                "label": "run",
                "metadata": {"successfulTasks": 1},
                "per_task": [
                    {
                        "selfReportSuccess": True,
                        "prompt_metrics": {
                            "prompt_agent_history_bytes_mean": 200,
                            "prompt_agent_history_bytes_max": 300,
                            "prompt_read_state_bytes_mean": 40,
                            "prompt_read_state_bytes_max": 80,
                            "prompt_history_items_mean": 2,
                            "prompt_history_collapsed_items_mean": 1,
                        },
                    }
                ],
            }
        )

        self.assertEqual(summary["prompt_agent_history_bytes_mean"]["mean"], 200)
        self.assertEqual(summary["prompt_agent_history_bytes_max"]["mean"], 300)
        self.assertEqual(summary["prompt_read_state_bytes_mean"]["mean"], 40)
        self.assertEqual(summary["prompt_read_state_bytes_max"]["mean"], 80)
        self.assertEqual(summary["prompt_history_items_mean"]["mean"], 2)
        self.assertEqual(summary["prompt_history_collapsed_items_mean"]["mean"], 1)


if __name__ == "__main__":
    unittest.main()
