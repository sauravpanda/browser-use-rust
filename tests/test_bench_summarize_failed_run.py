import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "summarize_failed_run_under_test",
        ROOT / "bench/summarize_failed_run.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SummarizeFailedRunTests(unittest.TestCase):
    def test_host_helpers_extract_target_and_compare_subdomains(self):
        mod = _load_module()

        self.assertEqual(
            mod._website_host("Find data. website: https://www.example.com/path"),
            "www.example.com",
        )
        self.assertTrue(mod._host_matches("news.example.com", "www.example.com"))
        self.assertFalse(mod._host_matches("other.example.net", "www.example.com"))
        self.assertTrue(mod._is_search_or_fallback_host("www.google.com"))

    def test_summarize_detail_reports_final_host_relation(self):
        mod = _load_module()
        task = {
            "taskId": "t1",
            "task": "Use the search bar and list results. website: https://example.com",
            "usage": {"total_cost": 1.0},
        }
        detail = {
            "completeHistory": [
                {"state": {"url": "https://www.example.com/search"}},
                {"state": {"url": "https://duckduckgo.com/?q=example"}},
            ],
            "finalResultResponse": "Based on DuckDuckGo search result snippets.",
        }

        row = mod._summarize_detail(task, detail)

        self.assertEqual(row["targetHost"], "example.com")
        self.assertEqual(row["visitedHosts"], ["www.example.com", "duckduckgo.com"])
        self.assertEqual(row["finalUrl"], "https://duckduckgo.com/?q=example")
        self.assertEqual(row["finalHost"], "duckduckgo.com")
        self.assertFalse(row["finalHostMatchesTarget"])
        self.assertTrue(row["finalHostIsSearchOrFallback"])
        self.assertTrue(row["searchHostFinal"])
        self.assertTrue(row["wrongHostFinal"])

    def test_summarize_detail_reports_wrong_host_final(self):
        mod = _load_module()
        task = {
            "taskId": "t2",
            "task": (
                "Filter listings and list addresses. "
                "website: https://apartments.com"
            ),
            "usage": {"total_cost": 1.0},
        }
        detail = {
            "completeHistory": [
                {"state": {"url": "https://www.apartments.com/"}},
                {"state": {"url": "https://hotpads.com/los-angeles-ca"}},
            ],
            "finalResultResponse": "Here are the addresses.",
        }

        row = mod._summarize_detail(task, detail)

        self.assertFalse(row["searchHostFinal"])
        self.assertFalse(row["staleRelativeDateFinal"])
        self.assertTrue(row["wrongHostFinal"])
        self.assertTrue(row["unsupportedEvidenceFinal"])

    def test_summarize_detail_reports_stale_relative_date_final(self):
        mod = _load_module()
        task = {
            "taskId": "t3",
            "task": (
                "Locate the latest article about an election. "
                "website: https://example.com"
            ),
            "usage": {"total_cost": 1.0},
        }
        detail = {
            "completeHistory": [
                {"state": {"url": "https://example.com/politics"}},
            ],
            "finalResultResponse": (
                "The latest article was published January 12, 2025 "
                "(3 hours ago)."
            ),
        }

        row = mod._summarize_detail(task, detail)

        self.assertTrue(row["staleRelativeDateFinal"])
        self.assertTrue(row["unsupportedEvidenceFinal"])

    def test_summarize_detail_reports_query_mismatch_final(self):
        mod = _load_module()
        task = {
            "taskId": "t4",
            "task": (
                "Search for articles on nutrition and healthy eating within "
                "the health resources. List the titles of the first three "
                "resources you find. website: https://clevelandclinic.org"
            ),
            "usage": {"total_cost": 1.0},
        }
        detail = {
            "completeHistory": [
                {"state": {"url": "https://clevelandclinic.org/search"}},
            ],
            "finalResultResponse": (
                "The first three resources are:\n"
                "1. Chiropractic Adjustment\n"
                "2. Dietitian\n"
                "3. Cardiac Rehab"
            ),
        }

        row = mod._summarize_detail(task, detail)

        self.assertTrue(row["queryMismatchFinal"])
        self.assertTrue(row["unsupportedEvidenceFinal"])

    def test_summarize_detail_reports_item_detail_list_final(self):
        mod = _load_module()
        task = {
            "taskId": "t5",
            "task": (
                "Use the advanced search to filter movies released in 2022 "
                "and output the first 5 results with their average ratings. "
                "website: https://themoviedb.org"
            ),
            "usage": {"total_cost": 1.0},
        }
        detail = {
            "completeHistory": [
                {"state": {"url": "https://www.themoviedb.org/search"}},
                {
                    "state": {
                        "url": "https://www.themoviedb.org/movie/315162-puss-in-boots-the-last-wish",
                    }
                },
            ],
            "finalResultResponse": "The first 5 movies are listed.",
        }

        row = mod._summarize_detail(task, detail)

        self.assertTrue(row["itemDetailListFinal"])
        self.assertTrue(row["unsupportedEvidenceFinal"])

    def test_unclassified_incorrect_final_helper_requires_self_reported_success(self):
        mod = _load_module()

        self.assertTrue(
            mod._is_unclassified_incorrect_final(
                {
                    "errorCategory": "Incorrect Result",
                    "selfReportSuccess": True,
                    "unsupportedEvidenceFinal": False,
                }
            )
        )
        self.assertFalse(
            mod._is_unclassified_incorrect_final(
                {
                    "errorCategory": "Incorrect Result",
                    "selfReportSuccess": True,
                    "unsupportedEvidenceFinal": True,
                }
            )
        )
        self.assertFalse(
            mod._is_unclassified_incorrect_final(
                {
                    "errorCategory": "Give Up",
                    "selfReportSuccess": False,
                    "unsupportedEvidenceFinal": False,
                }
            )
        )


if __name__ == "__main__":
    unittest.main()
