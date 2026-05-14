import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _load_module():
    spec = importlib.util.spec_from_file_location(
        "sample_posthog_tasks_under_test",
        ROOT / "bench/sample_posthog_tasks.py",
    )
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SamplePosthogTasksTests(unittest.TestCase):
    def test_filter_accepts_high_quality_browser_task(self):
        sampler = _load_module()
        row = {
            "task": "Find the latest release notes on example.com",
            "is_browser_task": True,
            "is_vague": False,
            "task_category": "Web",
            "includes_login_info": False,
            "requires_login": False,
            "is_reproducible": True,
            "is_unethical": False,
            "complexity": 3,
            "is_high_quality": True,
            "requires_custom_actions": False,
            "result_present": True,
            "task_language": "en",
        }

        self.assertTrue(sampler._filter_task(row))

    def test_filter_rejects_login_or_vague_tasks(self):
        sampler = _load_module()
        base = {
            "task": "Find something",
            "is_browser_task": True,
            "is_vague": False,
            "task_category": "Web",
            "includes_login_info": False,
            "requires_login": False,
            "is_reproducible": True,
            "is_unethical": False,
            "complexity": 3,
            "is_high_quality": True,
            "requires_custom_actions": False,
            "result_present": True,
            "task_language": "en",
        }

        self.assertFalse(sampler._filter_task({**base, "requires_login": True}))
        self.assertFalse(sampler._filter_task({**base, "is_vague": True}))

    def test_eval_friendly_policy_rejects_long_or_untargeted_tasks(self):
        sampler = _load_module()
        policy = sampler.SamplingPolicy(
            max_task_chars=80,
            require_url=True,
            reject_search_engine_tasks=True,
            reject_non_public_targets=True,
            reject_sensitive_or_transactional_tasks=True,
            reject_high_friction_targets=True,
        )

        self.assertEqual(
            sampler._policy_rejection_reason(
                "search for recent AI developments on google",
                policy,
            ),
            "missing_url_or_domain",
        )
        self.assertEqual(
            sampler._policy_rejection_reason(
                "Find a lunch recipe online. Make sure the recipe is not "
                "already in the visited URLs list "
                "{'https://example.com/old-recipe'}",
                sampler.SamplingPolicy(max_task_chars=500, require_url=True),
            ),
            "missing_url_or_domain",
        )
        self.assertEqual(
            sampler._policy_rejection_reason(
                "Search for recent AI developments on google. "
                "website: https://google.com",
                policy,
            ),
            "search_engine_task",
        )
        self.assertEqual(
            sampler._policy_rejection_reason(
                "Search Google for: site:https://example.com accreditation",
                policy,
            ),
            "search_engine_task",
        )
        self.assertEqual(
            sampler._policy_rejection_reason(
                "Find a profile. Search 'Hui-wei Chen Gastroenterology "
                "Pittsburgh PA site:healthgrades.com' in the address bar. "
                "You will get a Google search page.",
                sampler.SamplingPolicy(
                    max_task_chars=500,
                    require_url=True,
                    reject_search_engine_tasks=True,
                ),
            ),
            "search_engine_task",
        )
        self.assertEqual(
            sampler._policy_rejection_reason(
                "Navigate to google.com. Search for example.com and click the first result.",
                sampler.SamplingPolicy(
                    max_task_chars=500,
                    require_url=True,
                    reject_search_engine_tasks=True,
                ),
            ),
            "search_engine_task",
        )
        self.assertEqual(
            sampler._policy_rejection_reason(
                "Go to https://example.com and " + ("extract data " * 20),
                policy,
            ),
            "over_max_task_chars",
        )
        self.assertEqual(
            sampler._policy_rejection_reason(
                "Navigate to target-url: https://haxors-gallery.acmecorp.shinobi.security/",
                policy,
            ),
            "non_public_target",
        )
        self.assertEqual(
            sampler._policy_rejection_reason(
                "Go to https://www.google.com/maps/place/example and extract reviews.",
                policy,
            ),
            "search_engine_task",
        )
        self.assertEqual(
            sampler._policy_rejection_reason(
                "Visit https://www.facebook.com/example and find the support email.",
                policy,
            ),
            "high_friction_target",
        )
        self.assertEqual(
            sampler._policy_rejection_reason(
                "Go to https://shop.example.com and add products to cart.",
                policy,
            ),
            "sensitive_or_transactional_task",
        )
        self.assertEqual(
            sampler._policy_rejection_reason(
                "Go to https://example.com/login and sign in to view my account.",
                policy,
            ),
            "sensitive_or_transactional_task",
        )
        self.assertEqual(
            sampler._policy_rejection_reason(
                "Go to https://clinic.example.com and book an appointment.",
                policy,
            ),
            "sensitive_or_transactional_task",
        )
        self.assertEqual(
            sampler._policy_rejection_reason(
                "Go to https://example.com/contact and submit the form.",
                policy,
            ),
            "sensitive_or_transactional_task",
        )
        self.assertEqual(
            sampler._policy_rejection_reason(
                "Open https://track.example.com, input tracking number "
                "3235738474 and postcode CV5 9PF, then return parcel status.",
                sampler.SamplingPolicy(
                    max_task_chars=500,
                    require_url=True,
                    reject_sensitive_or_transactional_tasks=True,
                ),
            ),
            "sensitive_or_transactional_task",
        )

    def test_eval_friendly_policy_accepts_concise_targeted_task(self):
        sampler = _load_module()
        policy = sampler.SamplingPolicy(
            max_task_chars=200,
            require_url=True,
            reject_search_engine_tasks=True,
            reject_non_public_targets=True,
            reject_sensitive_or_transactional_tasks=True,
        )

        self.assertIsNone(
            sampler._policy_rejection_reason(
                "Go to https://example.com/news and list the top three headlines.",
                policy,
            )
        )
        self.assertIsNone(
            sampler._policy_rejection_reason(
                "Go to https://example.com/news and avoid URLs already in "
                "the visited URLs list {'https://example.com/old'}",
                sampler.SamplingPolicy(max_task_chars=500, require_url=True),
            )
        )

    def test_make_filter_combines_base_and_policy(self):
        sampler = _load_module()
        row = {
            "task": "Go to https://example.com/news and list top headlines.",
            "is_browser_task": True,
            "is_vague": False,
            "task_category": "Web",
            "includes_login_info": False,
            "requires_login": False,
            "is_reproducible": True,
            "is_unethical": False,
            "complexity": 3,
            "is_high_quality": True,
            "requires_custom_actions": False,
            "result_present": True,
            "task_language": "en",
        }
        keep = sampler._make_filter(
            sampler.SamplingPolicy(max_task_chars=100, require_url=True)
        )

        self.assertTrue(keep(row))
        self.assertFalse(keep({**row, "task": "find headlines"}))

    def test_malformed_url_does_not_abort_target_extraction(self):
        sampler = _load_module()

        self.assertEqual(sampler._target_hosts("Go to http://[bad"), [])


if __name__ == "__main__":
    unittest.main()
