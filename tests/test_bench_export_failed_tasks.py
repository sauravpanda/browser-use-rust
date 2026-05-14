import importlib.util
import shlex
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "export_failed_tasks_under_test",
        ROOT / "bench/export_failed_tasks.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExportFailedTasksTests(unittest.TestCase):
    def test_cost_accepts_serialized_usage(self):
        mod = _load_module()

        self.assertEqual(
            mod._cost({"usage": '{"total_cost": 1.25}'}),
            1.25,
        )
        self.assertEqual(mod._cost({"usage": "{not json"}), 0.0)

    def test_task_text_prefers_known_prompt_fields(self):
        mod = _load_module()

        self.assertEqual(
            mod._task_text({"goal": "Find a headline", "input": "fallback"}),
            "Find a headline",
        )

    def test_rerun_command_quotes_task_text(self):
        mod = _load_module()
        task = 'Search for "legal case studies" and list top 3'

        command = mod._rerun_command(task, 100)

        parts = shlex.split(command)
        self.assertEqual(parts[:2], [".venv/bin/python", "bench/run_ours.py"])
        self.assertEqual(parts[2], task)
        self.assertEqual(parts[3], "100")


if __name__ == "__main__":
    unittest.main()
