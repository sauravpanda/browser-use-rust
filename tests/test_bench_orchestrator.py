import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "bench_orchestrator_under_test",
        ROOT / "bench/bench.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class BenchOrchestratorTests(unittest.TestCase):
    def test_run_one_error_stubs_include_success_false(self):
        bench = _load_module()

        row = bench.run_one(
            "ours",
            Path("/definitely/missing/python"),
            Path("bench/run_ours.py"),
            "task",
        )

        self.assertFalse(row["completed"])
        self.assertFalse(row["success"])

    def test_run_one_defaults_missing_success_to_unknown(self):
        bench = _load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            script = Path(tmpdir) / "runner.py"
            script.write_text(
                "import json\n"
                "print(json.dumps({"
                "'system':'ours','task':'task','completed':True,"
                "'answer':'ok','elapsed_s':0,'steps':1,"
                "'in_tokens':1,'out_tokens':1,'cache_read_tokens':0,"
                "'cost_usd':0.0"
                "}))\n",
                encoding="utf-8",
            )

            row = bench.run_one("ours", Path(sys.executable), script, "task")

        self.assertTrue(row["completed"])
        self.assertIsNone(row["success"])
        self.assertIn("wall_s", row)

    def test_fmt_success(self):
        bench = _load_module()

        self.assertEqual(bench.fmt_success(True), "ok")
        self.assertEqual(bench.fmt_success(False), "fail")
        self.assertEqual(bench.fmt_success(None), "?")


if __name__ == "__main__":
    unittest.main()
