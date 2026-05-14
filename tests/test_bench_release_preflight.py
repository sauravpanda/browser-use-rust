import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "release_preflight_under_test",
        ROOT / "bench/release_preflight.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ReleasePreflightTests(unittest.TestCase):
    def test_release_candidate_paths_exclude_local_artifacts(self):
        mod = _load_module()

        paths = mod._release_candidate_paths(
            [
                (" M", "python/browser_use_rs/agent/__init__.py"),
                ("??", "bench/compare_eval_run.py"),
                ("??", "tests/test_bench_compare_eval_run.py"),
                ("??", "bench/monitor_runs.sh"),
                ("??", "bench/.trace_cache/all_summaries.json"),
                ("??", "bench/results.baseline.json"),
            ]
        )

        self.assertIn("python/browser_use_rs/agent/__init__.py", paths)
        self.assertIn("bench/compare_eval_run.py", paths)
        self.assertIn("tests/test_bench_compare_eval_run.py", paths)
        self.assertNotIn("bench/monitor_runs.sh", paths)
        self.assertNotIn("bench/.trace_cache/all_summaries.json", paths)
        self.assertNotIn("bench/results.baseline.json", paths)

    def test_secret_pattern_rejects_real_assignment_but_allows_placeholder(self):
        mod = _load_module()

        real = "EVALUATION_TOOL_SECRET_KEY=" + "abcdefghijklmnopqrstuvwxyz123456"
        placeholder = "EVALUATION_TOOL_SECRET_KEY=..."

        self.assertTrue(any(p.search(real) for p in mod.SECRET_PATTERNS))
        self.assertFalse(any(p.search(placeholder) for p in mod.SECRET_PATTERNS))


if __name__ == "__main__":
    unittest.main()
