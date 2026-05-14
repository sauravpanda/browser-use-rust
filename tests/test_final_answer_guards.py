import sys
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs.agent import (  # noqa: E402
    _looks_like_fabricated_blocked_answer,
    _looks_like_item_detail_list_final,
    _looks_like_late_pagination_final,
    _looks_like_past_dated_forward_answer,
    _looks_like_search_result_query_mismatch_answer,
    _looks_like_search_host_final,
    _search_fallback_state_host,
    _looks_like_site_required_external_answer,
    _looks_like_stale_relative_date_answer,
    _looks_like_unmet_requested_data_answer,
    _looks_like_unsupported_final_answer,
    _looks_like_wrong_host_final,
)


class FinalAnswerGuardTests(unittest.TestCase):
    def test_site_required_external_snippet_answer_is_flagged(self):
        task = (
            'Search for "vegan" recipes on Simply Recipes and record the '
            "titles of the first four recipes displayed. website: "
            "https://simplyrecipes.com"
        )
        answer = (
            "The first four vegan recipes displayed for Simply Recipes "
            "(based on search results as the site's direct search is "
            "currently inaccessible) are: Vegan Chicken Nuggets, Vegan "
            "Brownies, Vegan Chili, Vegan Pancakes."
        )

        self.assertTrue(_looks_like_site_required_external_answer(task, answer))

    def test_generic_target_site_search_results_are_not_flagged(self):
        task = (
            "Use the advanced search to filter movies released in 2022 "
            "and output the first 5 results with their average ratings. "
            "website: https://themoviedb.org"
        )
        answer = (
            "The first 5 movies released in 2022 according to the advanced "
            "search results on TMDB are Avatar, The Batman, Puss in Boots, "
            "Top Gun, and Black Panther."
        )

        self.assertFalse(_looks_like_site_required_external_answer(task, answer))

    def test_direct_access_wording_requires_blocked_context(self):
        task = (
            "Use the search bar to locate articles about taxes. "
            "website: https://example.com"
        )
        valid_answer = (
            "Direct access to the site search results showed three "
            "articles: Tax Guide, Filing Basics, and Refund Timeline."
        )
        blocked_answer = (
            "Because direct access to example.com was restricted, I used "
            "secondary listings to infer the first three results."
        )

        self.assertFalse(_looks_like_site_required_external_answer(task, valid_answer))
        self.assertTrue(_looks_like_site_required_external_answer(task, blocked_answer))

    def test_find_task_with_third_party_reference_is_flagged(self):
        task = (
            "Find an article that includes a celebrity interview, then list "
            "the celebrity's name and one key quote. website: https://ew.com/"
        )
        answer = (
            "Celebrity: Olivia Cooke. Key Quote: \"Over a decade's worth "
            "of work reduced to a single word.\" Source: Entertainment "
            "Weekly (Interview referenced in BuzzFeed News article titled "
            "\"Olivia Cooke Said She Was Sad...\")."
        )

        self.assertTrue(_looks_like_site_required_external_answer(task, answer))

    def test_current_task_with_external_traffic_source_is_flagged(self):
        task = (
            "Check the current traffic conditions on I-95 in Weston, MA "
            "and list any traffic incidents. website: https://mapquest.com"
        )
        answer = (
            "Based on current travel information from Mass511 and local "
            "traffic reports for Weston, there are no active incidents."
        )

        self.assertTrue(_looks_like_site_required_external_answer(task, answer))

    def test_site_required_task_finalized_on_search_host_is_flagged(self):
        task = (
            "Use the advanced search filters to find academic papers and "
            "list the top 3 titles. website: https://www.academia.edu/"
        )

        self.assertTrue(
            _looks_like_search_host_final(
                task,
                "https://duckduckgo.com/?q=site%3Aacademia.edu+papers",
            )
        )
        self.assertTrue(
            _looks_like_unsupported_final_answer(
                task,
                "The top 3 papers are listed.",
                "https://duckduckgo.com/?q=site%3Aacademia.edu+papers",
            )
        )

    def test_target_site_search_results_are_not_flagged_by_final_host(self):
        task = (
            "Use the advanced search filters to find academic papers and "
            "list the top 3 titles. website: https://www.academia.edu/"
        )

        self.assertFalse(
            _looks_like_search_host_final(
                task,
                "https://www.academia.edu/search?q=Artificial+Intelligence",
            )
        )

    def test_wrong_host_final_for_site_task_is_flagged(self):
        task = (
            "Filter property listings in Los Angeles with a maximum rent "
            "of $3000 and list the addresses shown on the map view. "
            "website: https://apartments.com"
        )
        answer = (
            "Based on property listings in Los Angeles with a maximum rent "
            "of $3000, the following addresses were identified."
        )

        self.assertTrue(
            _looks_like_wrong_host_final(
                task,
                "https://hotpads.com/los-angeles-ca/apartments-for-rent",
            )
        )
        self.assertTrue(
            _looks_like_unsupported_final_answer(
                task,
                answer,
                "https://hotpads.com/los-angeles-ca/apartments-for-rent",
            )
        )

    def test_same_site_subdomain_final_is_not_wrong_host(self):
        task = (
            'Search for CNN articles mentioning "renewable energy" and '
            "list the first two article titles. website: https://www.cnn.com/"
        )

        self.assertFalse(
            _looks_like_wrong_host_final(
                task,
                "https://edition.cnn.com/search?q=renewable+energy",
            )
        )

    def test_non_website_task_is_not_wrong_host(self):
        self.assertFalse(
            _looks_like_wrong_host_final(
                "Find tomorrow's weather in San Francisco.",
                "https://weather.com/weather/tomorrow/l/San+Francisco",
            )
        )

    def test_search_fallback_state_host_requires_site_required_task(self):
        site_task = (
            "Use the search bar to find papers. "
            "website: https://www.academia.edu/"
        )
        general_task = (
            "Open the homepage and summarize what the organization does. "
            "website: https://www.academia.edu/"
        )

        self.assertEqual(
            _search_fallback_state_host(
                site_task,
                "https://duckduckgo.com/?q=site%3Aacademia.edu+papers",
            ),
            "duckduckgo.com",
        )
        self.assertEqual(
            _search_fallback_state_host(
                general_task,
                "https://duckduckgo.com/?q=site%3Aacademia.edu+papers",
            ),
            "",
        )

    def test_non_site_task_with_external_source_is_not_flagged(self):
        task = "Find the weather in San Francisco tomorrow."
        answer = "Based on search result snippets, it will be mild."

        self.assertFalse(_looks_like_site_required_external_answer(task, answer))

    def test_live_current_task_answered_from_match_report_is_flagged(self):
        task = (
            "Access the live scores page, click on a current NBA match, "
            "and note down the current score and quarter information. "
            "website: https://sportskeeda.com"
        )
        answer = (
            "I located a match report for Lakers vs Heat. Final Score: "
            "Lakers 134, Heat 126. Quarter Information: Not explicitly "
            "broken down by quarter in the report."
        )

        self.assertTrue(_looks_like_unmet_requested_data_answer(task, answer))
        self.assertTrue(_looks_like_unsupported_final_answer(task, answer))

    def test_completed_score_task_is_not_flagged_as_live_miss(self):
        task = (
            "Find the final score of the Lakers vs Heat game. "
            "website: https://sportskeeda.com"
        )
        answer = "The final score was Lakers 134, Heat 126."

        self.assertFalse(_looks_like_unmet_requested_data_answer(task, answer))

    def test_specific_requested_feature_admitted_missing_is_flagged(self):
        task = (
            'Search for "NVIDIA RTX 3080" on Newegg, then review the '
            '"Review Bytes" summary for this product and output the '
            "three key performance highlights. website: https://newegg.com"
        )
        answer = (
            "I was unable to locate the specific Review Bytes summary for "
            "these products, but based on product pages the card is fast."
        )

        self.assertTrue(_looks_like_unmet_requested_data_answer(task, answer))

    def test_explicit_unable_to_complete_final_is_flagged(self):
        task = (
            'Using Temu\'s search bar, search for "wireless earbuds", sort '
            "the results by lowest price, and list the names and prices of "
            "the first five products. website: https://www.temu.com/"
        )
        answer = (
            "I am unable to complete the task because I have been blocked "
            "by bot-detection measures on both the target website and "
            "multiple search engines."
        )

        self.assertTrue(_looks_like_fabricated_blocked_answer(answer))
        self.assertTrue(_looks_like_unsupported_final_answer(task, answer))

    def test_cookie_overlay_failure_final_is_flagged(self):
        task = (
            "Search for 5G on Digital Trends and list the titles of the "
            "first four articles. website: https://www.digitaltrends.com/"
        )
        answer = (
            "I was unable to complete the search on Digital Trends due to "
            "a persistent privacy consent modal that blocked all automated "
            "interactions."
        )

        self.assertTrue(_looks_like_unsupported_final_answer(task, answer))

    def test_valid_negative_site_search_is_not_blocked_fabrication(self):
        answer = (
            "The site search returned no articles matching that exact "
            "phrase, and the visible results were unrelated."
        )

        self.assertFalse(_looks_like_fabricated_blocked_answer(answer))

    def test_forward_looking_task_with_past_schedule_date_is_flagged(self):
        task = (
            "Check the live stream schedule and list the next two sports "
            "events along with their start times and channels. "
            "website: https://cbssports.com"
        )
        answer = (
            'Evidence: "College Bowling" and "7:00 PM" are listed under '
            'the "Wednesday, May 13" schedule on TV Insider.'
        )

        self.assertTrue(
            _looks_like_past_dated_forward_answer(
                task,
                answer,
                today=date(2026, 5, 14),
            )
        )

    def test_latest_task_can_legitimately_answer_with_yesterday(self):
        task = (
            "locate the latest research articles and list titles. "
            "website: https://science.org"
        )
        answer = "The latest article was published on May 13."

        self.assertFalse(
            _looks_like_past_dated_forward_answer(
                task,
                answer,
                today=date(2026, 5, 14),
            )
        )

    def test_latest_task_with_stale_absolute_date_and_relative_label_is_flagged(self):
        task = (
            "Navigate to the Politics section and locate the latest article "
            "about the 2024 Presidential Election. website: https://foxnews.com"
        )
        answer = (
            "Publication Date: January 12, 2025 (3 hours ago). "
            "The article discusses 2028 speculation."
        )

        self.assertTrue(
            _looks_like_stale_relative_date_answer(
                task,
                answer,
                today=date(2026, 5, 14),
            )
        )
        self.assertTrue(_looks_like_unmet_requested_data_answer(task, answer))

    def test_recent_absolute_date_with_relative_label_is_not_flagged(self):
        task = (
            "Find the latest article headline and publication time. "
            "website: https://example.com"
        )
        answer = "Publication Date: May 14, 2026 (3 hours ago)."

        self.assertFalse(
            _looks_like_stale_relative_date_answer(
                task,
                answer,
                today=date(2026, 5, 14),
            )
        )

    def test_most_recent_result_finalized_on_later_page_is_flagged(self):
        task = (
            'Look up "Government Publications" and list the titles of the '
            "three most recent policy papers on digital infrastructure. "
            "website: https://www.gov.uk"
        )
        final_url = (
            "https://www.gov.uk/search/all?keywords=digital+infrastructure"
            "&order=updated-newest&page=2"
        )
        answer = "The three most recent policy papers are listed."

        self.assertTrue(_looks_like_late_pagination_final(task, final_url))
        self.assertTrue(_looks_like_unsupported_final_answer(task, answer, final_url))

    def test_explicit_page_two_task_is_not_late_pagination_flagged(self):
        task = (
            "Open page 2 of the archive and list the first three results. "
            "website: https://example.com"
        )

        self.assertFalse(
            _looks_like_late_pagination_final(
                task,
                "https://example.com/archive?page=2",
            )
        )

    def test_first_result_page_one_offsets_are_not_flagged(self):
        task = (
            "Search for CNN articles mentioning renewable energy and list "
            "the first two article titles. website: https://cnn.com"
        )

        self.assertFalse(
            _looks_like_late_pagination_final(
                task,
                "https://edition.cnn.com/search?q=renewable+energy&from=0&page=1",
            )
        )

    def test_multi_result_task_finalized_on_item_detail_page_is_flagged(self):
        task = (
            "Use the advanced search to filter movies released in 2022 and "
            "output the first 5 results with their average ratings. "
            "website: https://themoviedb.org"
        )
        final_url = "https://www.themoviedb.org/movie/315162-puss-in-boots-the-last-wish"
        answer = (
            "The first 5 movies released in 2022 according to the advanced "
            "search results are listed."
        )

        self.assertTrue(_looks_like_item_detail_list_final(task, final_url))
        self.assertTrue(_looks_like_unsupported_final_answer(task, answer, final_url))

    def test_multi_result_task_on_result_page_is_not_detail_flagged(self):
        task = (
            "Search for CNN articles mentioning renewable energy and list "
            "the first two article titles. website: https://cnn.com"
        )

        self.assertFalse(
            _looks_like_item_detail_list_final(
                task,
                "https://edition.cnn.com/search?q=renewable+energy&from=0&page=1",
            )
        )

    def test_singular_article_task_on_detail_page_is_not_detail_flagged(self):
        task = (
            "Locate the featured investigative report on the homepage and "
            "write a brief summary outlining its main argument. "
            "website: https://cbsnews.com"
        )

        self.assertFalse(
            _looks_like_item_detail_list_final(
                task,
                "https://www.cbsnews.com/news/nancy-guthrie-investigation-dna-100-days/",
            )
        )

    def test_site_search_result_titles_with_no_query_overlap_are_flagged(self):
        task = (
            "Search for articles on nutrition and healthy eating within "
            "the health resources. List the titles of the first three "
            "resources you find. website: https://clevelandclinic.org"
        )
        answer = (
            "The first three resources found for \"nutrition and healthy "
            "eating\" in the Cleveland Clinic Health Library are:\n"
            "1. Chiropractic Adjustment\n"
            "2. Dietitian\n"
            "3. Cardiac Rehab"
        )

        self.assertTrue(_looks_like_search_result_query_mismatch_answer(task, answer))
        self.assertTrue(_looks_like_unsupported_final_answer(task, answer))

    def test_site_search_result_titles_with_query_overlap_are_not_flagged(self):
        task = (
            'Search for "legal case studies" documents, filter for English '
            "and Italiano language, and list the top three documents. "
            "website: https://scribd.com"
        )
        answer = (
            "The top three documents are:\n"
            "1. Legal Case Studies for Students - 3 pages\n"
            "2. Legal Bibliography and Case Studies - 2 pages\n"
            "3. Legal Case Study Interview Guide - 11 pages"
        )

        self.assertFalse(_looks_like_search_result_query_mismatch_answer(task, answer))

    def test_multi_concept_site_search_titles_must_cover_each_concept(self):
        task = (
            'Use the search function to locate articles on "Los Angeles" '
            'and "immigration", then provide the titles of the first '
            "three results. website: https://latimes.com"
        )
        answer = (
            "Based on the search results for Los Angeles immigration:\n"
            "1. California governor candidates spar over housing, "
            "immigration, Trump in high-stakes debate\n"
            "2. Los Angeles Times Media Group and Arizona State University "
            "to Host Inaugural Aerospace and Defense Summit\n"
            "3. How to watch tonight's Los Angeles mayoral debate"
        )

        self.assertTrue(_looks_like_search_result_query_mismatch_answer(task, answer))
        self.assertTrue(_looks_like_unsupported_final_answer(task, answer))

    def test_single_phrase_site_search_partial_overlap_is_not_flagged(self):
        task = (
            'Search for CNN articles mentioning "renewable energy" and '
            "list the first two article titles. website: https://cnn.com"
        )
        answer = (
            "The first two articles are:\n"
            "1. This company says nuclear fusion could finally power the grid\n"
            "2. The Iran war has the world buying more clean energy"
        )

        self.assertFalse(_looks_like_search_result_query_mismatch_answer(task, answer))

    def test_non_list_search_summary_is_not_query_mismatch_flagged(self):
        task = (
            "Search for articles on nutrition and healthy eating and "
            "summarize what the site offers. website: https://example.com"
        )
        answer = (
            "The site offers a broad health library with dietitian-led "
            "guidance and condition-specific wellness advice."
        )

        self.assertFalse(_looks_like_search_result_query_mismatch_answer(task, answer))


if __name__ == "__main__":
    unittest.main()
