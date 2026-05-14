import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "posthog_preflight_under_test",
        ROOT / "bench/posthog_preflight.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class PosthogPreflightTests(unittest.TestCase):
    def test_report_is_read_only_and_surfaces_blockers(self):
        preflight = _load_module()

        report = preflight.build_report()

        self.assertIn("blocked", report)
        self.assertIn("browserUseApiKeyMissing", report["blocked"])
        self.assertIn("pythonDependencies", report)
        self.assertIn("pythonDependenciesMissing", report["blocked"])
        self.assertIn("runTasksConfig", report)
        self.assertIn("posthogDatasetConfig", report)
        self.assertIn("importTimeApiKeyValidation", report["runTasksConfig"])
        self.assertIn("pushesToHub", report["posthogDatasetConfig"])
        self.assertIn("topLevelLoadDataset", report["posthogDatasetConfig"])
        self.assertIn("topLevelPushToHub", report["posthogDatasetConfig"])
        self.assertEqual(
            report["posthogDatasetConfig"]["sourceDataset"],
            "browser-use/posthog-tasks-080925-labeled",
        )
        self.assertTrue(report["posthogDatasetConfig"]["topLevelLoadDataset"])
        self.assertTrue(report["posthogDatasetConfig"]["topLevelPushToHub"])
        self.assertIsInstance(report["safeNextSteps"], list)


if __name__ == "__main__":
    unittest.main()
