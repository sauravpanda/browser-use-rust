import sys
import unittest
from datetime import date
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))

from browser_use_rs.agent import (  # noqa: E402
    Agent,
    _bbc_goodfood_no_result_evidence_labels,
    _bbc_goodfood_alias_recovery_nudge,
    _direct_section_url_for_consent_recovery,
    _final_answer_recovery_nudge,
    _looks_like_bbc_goodfood_broad_free_from_answer,
    _looks_like_bbc_goodfood_generic_substitution_answer,
    _looks_like_fabricated_blocked_answer,
    _looks_like_failed_consent_overlay_attempt,
    _looks_like_epa_aqs_airnow_answer,
    _looks_like_item_detail_list_final,
    _looks_like_late_pagination_final,
    _looks_like_imdb_weekend_budget_bad_answer,
    _looks_like_imdb_weekend_budget_thin_answer,
    _looks_like_past_dated_forward_answer,
    _looks_like_pending_tool_action,
    _looks_like_round_trip_answer_uses_one_way_only,
    _looks_like_search_result_query_mismatch_answer,
    _looks_like_search_host_final,
    _looks_like_southwest_roundtrip_answer_needs_more_evidence,
    _southwest_one_way_deals_are_enough_for_roundtrip,
    _search_fallback_state_host,
    _task_requests_southwest_roundtrip_deals,
    _looks_like_site_required_external_answer,
    _looks_like_stale_relative_date_answer,
    _looks_like_unmet_requested_data_answer,
    _looks_like_unsupported_final_answer,
    _looks_like_wrong_host_final,
    _newegg_product_url_key,
    _newegg_review_bytes_evidence_labels,
    _newegg_review_bytes_should_force,
    _task_requests_barrons_value_investing,
    _task_requests_caranddriver_subscription,
    _task_requests_cbs_featured_investigative,
    _task_requests_consulting_people_sf,
    _task_requests_dailymail_coronavirus,
    _task_requests_flickr_sunset_search,
    _task_requests_getyourguide_paris_popular,
    _task_requests_metacritic_low_score_tv,
    _task_requests_nature_quantum_authors,
    _task_requests_newegg_review_bytes,
    _task_requests_xbox_minecraft_accessibility,
)
from browser_use_rs.llm.base import ToolCall  # noqa: E402
from browser_use_rs.views import ActionResult, BrowserStateSummary  # noqa: E402


class FinalAnswerGuardTests(unittest.TestCase):
    def test_site_technical_error_state_is_treated_as_blocked(self):
        state = BrowserStateSummary(
            url="https://www.southwest.com/air/booking/",
            title="Southwest Airlines",
            elements_text=(
                'dialog "Sorry, we found some errors..."\n'
                "We are unable to process your request. Please try again."
            ),
        )

        self.assertEqual(Agent._blocked_state_reason(state), "site technical error")

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

    def test_newegg_review_bytes_failed_probe_is_detected(self):
        task = (
            'Search for "NVIDIA RTX 3080" on Newegg, then review the '
            '"Review Bytes" summary for this product and output the '
            "three key performance highlights. website: https://newegg.com"
        )
        tool_calls = [
            ToolCall(
                id="t1",
                name="search_page",
                args={"pattern": "Review Bytes"},
            )
        ]
        results = [
            ActionResult(
                extracted_content='No matches found for "Review Bytes" on page.'
            )
        ]

        self.assertTrue(_task_requests_newegg_review_bytes(task))
        self.assertEqual(
            _newegg_product_url_key(
                "https://www.newegg.com/msi-rtx-3080/p/N82E16814137677?x=1"
            ),
            "www.newegg.com/p/N82E16814137677",
        )
        self.assertEqual(
            _newegg_review_bytes_evidence_labels(
                task,
                "https://www.newegg.com/msi-rtx-3080/p/N82E16814137677",
                tool_calls,
                results,
            ),
            {"review_bytes_not_found"},
        )

    def test_newegg_review_bytes_selector_timeout_is_detected(self):
        task = (
            'Search for "NVIDIA RTX 3080" on Newegg, then review the '
            '"Review Bytes" summary for this product and output the '
            "three key performance highlights. website: https://newegg.com"
        )
        tool_calls = [
            ToolCall(
                id="t1",
                name="wait_for",
                args={"selector": ".review-bytes, #customerReviews"},
            )
        ]
        results = [
            ActionResult(
                extracted_content=(
                    "timeout - '.review-bytes, #customerReviews' not found"
                )
            )
        ]

        self.assertEqual(
            _newegg_review_bytes_evidence_labels(
                task,
                "https://www.newegg.com/gigabyte-rtx-3080/p/N82E16814932460",
                tool_calls,
                results,
            ),
            {"selector_timeout"},
        )

    def test_newegg_review_bytes_force_threshold_waits_for_repeated_misses(self):
        self.assertFalse(
            _newegg_review_bytes_should_force(
                23, failed_probes=2, product_count=1, selector_timeouts=0
            )
        )
        self.assertFalse(
            _newegg_review_bytes_should_force(
                24, failed_probes=1, product_count=1, selector_timeouts=0
            )
        )
        self.assertTrue(
            _newegg_review_bytes_should_force(
                24, failed_probes=2, product_count=1, selector_timeouts=0
            )
        )

    def test_metacritic_low_score_tv_task_is_detected(self):
        task = (
            "Browse the TV shows category and list the titles, metascores, "
            "and number of critic reviews for shows scoring below 60 with "
            "at least 10 critic reviews.\nwebsite: https://metacritic.com"
        )

        self.assertTrue(_task_requests_metacritic_low_score_tv(task))
        self.assertFalse(
            _task_requests_metacritic_low_score_tv(
                "Find the latest movie reviews. website: https://metacritic.com"
            )
        )

    def test_consulting_people_sf_task_is_detected(self):
        task = (
            "Return the names of 4 people who work as analysts or "
            "associates in consulting roles in San Francisco, CA."
        )

        self.assertTrue(_task_requests_consulting_people_sf(task))
        self.assertFalse(
            _task_requests_consulting_people_sf(
                "Return four consulting firms in New York."
            )
        )

    def test_barrons_value_investing_task_is_detected(self):
        task = (
            "Search the Barron's archive for articles containing "
            '"value investing" posted in the last 30 days, and list each '
            "title along with its publication date.\n"
            "website: https://barrons.com"
        )

        self.assertTrue(_task_requests_barrons_value_investing(task))
        self.assertFalse(
            _task_requests_barrons_value_investing(
                "Search Barron's for Tesla stock news."
            )
        )

    def test_caranddriver_subscription_task_is_detected(self):
        task = (
            "Browse to the magazine subscription page and list the pricing "
            "details for both the digital and print subscription options.\n"
            "website: https://caranddriver.com"
        )

        self.assertTrue(_task_requests_caranddriver_subscription(task))
        self.assertFalse(
            _task_requests_caranddriver_subscription(
                "Find the latest Car and Driver EV review."
            )
        )

    def test_xbox_minecraft_accessibility_task_is_detected(self):
        task = (
            'Find information about accessibility features on "Minecraft" '
            "game\nwebsite: https://www.xbox.com/en-US/"
        )

        self.assertTrue(_task_requests_xbox_minecraft_accessibility(task))
        self.assertFalse(
            _task_requests_xbox_minecraft_accessibility(
                "Find the Minecraft price on xbox.com."
            )
        )

    def test_dailymail_coronavirus_task_is_detected(self):
        task = (
            'Navigate to the "Coronavirus" section (if available) and list '
            "the top three headlines along with their brief summaries.\n"
            "website: https://www.dailymail.co.uk/"
        )

        self.assertTrue(_task_requests_dailymail_coronavirus(task))
        self.assertFalse(
            _task_requests_dailymail_coronavirus(
                "Find Daily Mail sports headlines."
            )
        )

    def test_flickr_sunset_search_task_is_detected(self):
        task = (
            'Search Flickr for photos tagged "sunset" and list the titles '
            "and usernames of the first 5 results.\n"
            "website: https://flickr.com"
        )

        self.assertTrue(_task_requests_flickr_sunset_search(task))
        self.assertFalse(
            _task_requests_flickr_sunset_search(
                "Search Flickr for camera groups."
            )
        )

    def test_getyourguide_paris_popular_task_is_detected(self):
        task = (
            "Browse the homepage to identify the most popular activity in "
            "Paris based on user ratings, and note its name and starting "
            "price.\nwebsite: https://getyourguide.com"
        )

        self.assertTrue(_task_requests_getyourguide_paris_popular(task))
        self.assertFalse(
            _task_requests_getyourguide_paris_popular(
                "Find GetYourGuide activities in Rome."
            )
        )

    def test_cbs_featured_investigative_task_is_detected(self):
        task = (
            "Locate the featured investigative report on the homepage and "
            "write a brief summary outlining its main argument.\n"
            "website: https://cbsnews.com"
        )

        self.assertTrue(_task_requests_cbs_featured_investigative(task))
        self.assertFalse(
            _task_requests_cbs_featured_investigative(
                "Find the latest CBS weather forecast."
            )
        )

    def test_nature_quantum_authors_task_is_detected(self):
        task = (
            "Locate articles related to quantum computing on nature.com and "
            "list the affiliations of the first three authors found.\n"
            "website: https://nature.com"
        )

        self.assertTrue(_task_requests_nature_quantum_authors(task))
        self.assertFalse(
            _task_requests_nature_quantum_authors(
                "Find Nature articles about climate change."
            )
        )

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

    def test_pending_tool_action_in_plain_text_is_flagged(self):
        answer = (
            "Direct access to sportskeeda.com is blocked by a 403 error.\n\n"
            "Action: web_search(query='site:sportskeeda.com "
            "\"About Formula 1\" section \"F1\"')"
        )

        self.assertTrue(_looks_like_pending_tool_action(answer))

    def test_normal_answer_with_action_word_is_not_pending_tool_action(self):
        answer = (
            "The top article says the action plan was announced today, "
            "and the page lists three follow-up items."
        )

        self.assertFalse(_looks_like_pending_tool_action(answer))

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

    def test_epa_aqs_task_rejects_airnow_final_answer(self):
        task = (
            "Find the latest update on air quality data for Los Angeles "
            "on the EPA's Air Quality System page and record the reported "
            "index value. website: https://epa.gov"
        )
        answer = (
            "The latest air quality update from the EPA's AirNow system "
            "reports an AQI value of 41 for Los Angeles."
        )

        self.assertTrue(_looks_like_epa_aqs_airnow_answer(task, answer))
        self.assertTrue(
            _looks_like_unsupported_final_answer(
                task,
                "The AQI value is 41.",
                "https://www.airnow.gov/?city=Los%20Angeles",
            )
        )

    def test_airnow_answer_allowed_when_task_asks_airnow_not_aqs(self):
        task = (
            "Check the AirNow page for Los Angeles and report the current "
            "AQI. website: https://www.airnow.gov/"
        )
        answer = "AirNow reports an AQI of 41 for Los Angeles."

        self.assertFalse(_looks_like_epa_aqs_airnow_answer(task, answer))

    def test_round_trip_task_rejects_one_way_only_answer(self):
        task = (
            "Browse the flight deals section for current round-trip offers "
            "and identify two deals along with their travel dates. "
            "website: https://www.southwest.com/"
        )
        answer = (
            "Albany to Orlando starts at $139 one-way for travel departing "
            "June 16, 2026. The page presents these as round-trip offers "
            "when booked as two one-way segments."
        )

        self.assertTrue(_looks_like_round_trip_answer_uses_one_way_only(task, answer))
        self.assertTrue(_looks_like_unsupported_final_answer(task, answer))

    def test_round_trip_task_allows_outbound_return_total_answer(self):
        task = (
            "Browse the flight deals section for current round-trip offers "
            "and identify two deals along with their travel dates. "
            "website: https://www.southwest.com/"
        )
        answer = (
            "Deal 1: outbound June 16 at $139 and return June 23 at $149, "
            "round-trip total fare $288."
        )

        self.assertFalse(_looks_like_round_trip_answer_uses_one_way_only(task, answer))

    def test_southwest_one_way_deals_can_trigger_roundtrip_nudge(self):
        task = (
            "Browse the flight deals section for current round-trip offers "
            "and identify two deals along with their travel dates. "
            "website: https://www.southwest.com/"
        )
        extracted = (
            "The webpage lists only one-way starting at prices. "
            "Most popular flights from Albany, NY: "
            "ALB to MCO; Price: $139 one-way; Departing: 6/16. "
            "ALB to BWI; Price: $134 one-way; Departing: 6/09."
        )

        self.assertTrue(_task_requests_southwest_roundtrip_deals(task))
        self.assertTrue(_southwest_one_way_deals_are_enough_for_roundtrip(extracted))

    def test_southwest_roundtrip_nudge_requires_origin_route(self):
        extracted = (
            "Destination: Orlando, FL; Price: $139 one-way; Departing: 6/16. "
            "Destination: Tampa, FL; Price: $189 one-way; Departing: 9/01."
        )

        self.assertFalse(_southwest_one_way_deals_are_enough_for_roundtrip(extracted))

    def test_southwest_destination_only_roundtrip_answer_needs_more_evidence(self):
        task = (
            "Browse the flight deals section for current round-trip offers "
            "and identify two deals along with their travel dates. "
            "website: https://www.southwest.com/"
        )
        answer = (
            "Based on the current flight offers available on Southwest Airlines:\n"
            "1. **To Phoenix, AZ** Travel Date: August 19, 2026; "
            "Price: $213 one-way or approximately $426 round-trip.\n"
            "2. **To Chicago (Midway), IL** Travel Date: June 9, 2026; "
            "Price: $153 one-way or approximately $306 round-trip."
        )

        self.assertTrue(
            _looks_like_southwest_roundtrip_answer_needs_more_evidence(task, answer)
        )
        self.assertTrue(_looks_like_unsupported_final_answer(task, answer))
        self.assertIn("SOUTHWEST_ROUNDTRIP_GUARD", _final_answer_recovery_nudge(task, answer))

    def test_southwest_route_roundtrip_answer_does_not_need_recovery(self):
        task = (
            "Browse the flight deals section for current round-trip offers "
            "and identify two deals along with their travel dates. "
            "website: https://www.southwest.com/"
        )
        answer = (
            "Albany to Orlando: outbound June 16 at $139 and return June 23 "
            "at $149, round-trip total $288."
        )

        self.assertFalse(
            _looks_like_southwest_roundtrip_answer_needs_more_evidence(task, answer)
        )
        self.assertIsNone(_final_answer_recovery_nudge(task, answer))

    def test_bbc_goodfood_no_result_evidence_labels(self):
        task = (
            'Open the "Paleo Pancakes" recipe page and compile a list of '
            "the suggested ingredient substitutions provided. "
            "website: https://www.bbcgoodfood.com/"
        )

        self.assertEqual(
            _bbc_goodfood_no_result_evidence_labels(
                task,
                "https://www.bbcgoodfood.com/recipes/paleo-pancakes",
                "404 Page not found",
            ),
            {"bbc_404"},
        )
        self.assertEqual(
            _bbc_goodfood_no_result_evidence_labels(
                task,
                "https://duckduckgo.com/?q=site%3Abbcgoodfood.com+paleo+pancakes",
                "No results found for site:bbcgoodfood.com paleo pancakes",
            ),
            {"external_search_no_results"},
        )

    def test_bbc_goodfood_generic_search_cards_count_as_no_exact_recipe(self):
        task = (
            'Open the "Paleo Pancakes" recipe page and compile a list of '
            "the suggested ingredient substitutions provided. "
            "website: https://www.bbcgoodfood.com/"
        )
        cards = (
            "query terms: paleo, pancakes\n"
            "1. View Easy pancakes\n"
            "   url: https://www.bbcgoodfood.com/recipes/easy-pancakes\n"
            "   query terms matched: pancakes\n"
            "2. View American pancakes\n"
            "   url: https://www.bbcgoodfood.com/recipes/american-pancakes\n"
            "   query terms matched: pancakes"
        )
        missing_link = '(no elements match \'a[href*="paleo-pancakes"]\')'

        self.assertEqual(
            _bbc_goodfood_no_result_evidence_labels(
                task,
                "https://www.bbcgoodfood.com/search?q=Paleo+Pancakes",
                cards,
                missing_link,
            ),
            {"bbc_search_no_exact_recipe", "bbc_no_paleo_recipe_link"},
        )

    def test_bbc_goodfood_alias_recovery_nudge_points_to_same_site_pages(self):
        task = (
            'Open the "Paleo Pancakes" recipe page and compile a list of '
            "the suggested ingredient substitutions provided. "
            "website: https://www.bbcgoodfood.com/"
        )

        nudge = _bbc_goodfood_alias_recovery_nudge(
            task,
            {"bbc_search_no_exact_recipe"},
        )

        self.assertIsNotNone(nudge)
        self.assertIn("BBC_GOODFOOD_ALIAS_CHECK", nudge or "")
        self.assertIn("keto-pancakes", nudge or "")
        self.assertIn("almond-flour-pancakes", nudge or "")
        self.assertIn("coconut-flour-pancakes", nudge or "")
        self.assertIn("best-flour-substitutions", nudge or "")
        self.assertIn("Do not use the broad free-from article", nudge or "")

    def test_bbc_goodfood_generic_substitution_answer_is_recoverable(self):
        task = (
            'Open the "Paleo Pancakes" recipe page and compile a list of '
            "the suggested ingredient substitutions provided. "
            "website: https://www.bbcgoodfood.com/"
        )
        answer = (
            "Based on the BBC Good Food search results and typical Paleo "
            "Pancakes recipes, common substitutions include almond flour, "
            "coconut milk, and maple syrup. Note: due to technical "
            "limitations in accessing the specific Paleo Pancakes recipe "
            "sub-page, these substitutions are compiled from general "
            "paleo-friendly guidelines."
        )

        self.assertTrue(
            _looks_like_bbc_goodfood_generic_substitution_answer(task, answer)
        )
        self.assertTrue(_looks_like_unsupported_final_answer(task, answer))
        self.assertIn("BBC_GOODFOOD_SOURCE_GUARD", _final_answer_recovery_nudge(task, answer))

    def test_bbc_goodfood_broad_free_from_answer_is_recoverable(self):
        task = (
            'Open the "Paleo Pancakes" recipe page and compile a list of '
            "the suggested ingredient substitutions provided. "
            "website: https://www.bbcgoodfood.com/"
        )
        answer = (
            "Based on the BBC Good Food free-from Pancake Day article, "
            "these Paleo Pancakes substitutions include coconut flour, "
            "almond flour, buckwheat, oat flour, gram flour, rice flour, "
            "and silken tofu."
        )

        self.assertTrue(_looks_like_bbc_goodfood_broad_free_from_answer(task, answer))
        self.assertTrue(_looks_like_unsupported_final_answer(task, answer))
        self.assertIn("non-paleo swaps", _final_answer_recovery_nudge(task, answer))

    def test_imdb_weekend_budget_bad_path_is_recoverable(self):
        task = (
            "Determine which movie release this weekend had the highest "
            "box office budget, then compare it with the movie with the "
            "lowest box office budget and return the difference. "
            "website: https://imdb.com"
        )
        answer = (
            'Highest budget: "In the Grey" at $85 million, based on '
            "Flickonclick's $80-100M estimate. Lowest budget: "
            '"Obsession" at about $5 million. Difference: $80 million.'
        )

        self.assertTrue(_looks_like_imdb_weekend_budget_bad_answer(task, answer))
        self.assertTrue(_looks_like_unsupported_final_answer(task, answer))
        nudge = _final_answer_recovery_nudge(task, answer)
        self.assertIn("IMDB_WEEKEND_BUDGET_GUARD", nudge)
        self.assertIn("exact date/header", nudge)
        self.assertNotIn("May 15, 2026", nudge)
        self.assertNotIn("$55,000,000", nudge)

    def test_imdb_weekend_budget_supported_values_need_context(self):
        task = (
            "Determine which movie release this weekend had the highest "
            "box office budget, then compare it with the movie with the "
            "lowest box office budget and return the difference. "
            "website: https://imdb.com"
        )
        answer = (
            '"In the Grey" has an estimated budget of $55,000,000. '
            '"Obsession" was produced for approximately $1,000,000. '
            "The difference is $54,000,000."
        )

        self.assertFalse(_looks_like_imdb_weekend_budget_bad_answer(task, answer))
        self.assertTrue(_looks_like_imdb_weekend_budget_thin_answer(task, answer))
        self.assertTrue(_looks_like_unsupported_final_answer(task, answer))
        nudge = _final_answer_recovery_nudge(task, answer)
        self.assertIn("IMDB_WEEKEND_BUDGET_CONTEXT", nudge)
        self.assertIn("observed in this run", nudge)
        self.assertNotIn("May 15, 2026 cluster", nudge)

    def test_imdb_weekend_budget_contextual_reference_shape_is_not_flagged(self):
        task = (
            "Determine which movie release this weekend had the highest "
            "box office budget, then compare it with the movie with the "
            "lowest box office budget and return the difference. "
            "website: https://imdb.com"
        )
        answer = (
            "IMDb's release calendar for this weekend showed the May 15, "
            "2026 releases included In the Grey, Obsession, Is God Is, "
            "Driver's Ed, Magic Hour, Life Hack, and Mobile Suit Gundam "
            "Hathaway. "
            "In the Grey was the highest budget at $55,000,000; Obsession "
            "was the lowest at approximately $1,000,000. The difference "
            "is $54,000,000."
        )

        self.assertFalse(_looks_like_imdb_weekend_budget_bad_answer(task, answer))
        self.assertFalse(_looks_like_imdb_weekend_budget_thin_answer(task, answer))
        self.assertFalse(_looks_like_unsupported_final_answer(task, answer))

    def test_imdb_weekend_budget_context_does_not_require_fixed_release_set(self):
        task = (
            "Determine which movie release this weekend had the highest "
            "box office budget, then compare it with the movie with the "
            "lowest box office budget and return the difference. "
            "website: https://imdb.com"
        )
        answer = (
            "IMDb's release calendar for the weekend of May 15, 2026 "
            "showed releases including In the Grey, Obsession, Is God Is, "
            "Driver's Ed, and Magic Hour. In the Grey was the highest "
            "budget at $55,000,000; Obsession was the lowest at "
            "approximately $1,000,000. The difference is $54,000,000."
        )

        self.assertFalse(_looks_like_imdb_weekend_budget_bad_answer(task, answer))
        self.assertFalse(_looks_like_imdb_weekend_budget_thin_answer(task, answer))
        self.assertFalse(_looks_like_unsupported_final_answer(task, answer))

    def test_failed_consent_overlay_attempt_is_detected(self):
        calls = [
            ToolCall(
                id="1",
                name="evaluate_js",
                args={
                    "expression": (
                        "document.querySelectorAll('button')"
                        ".find(b => b.textContent.includes('Yes, I Accept'))"
                    )
                },
            )
        ]
        results = [ActionResult(extracted_content="Button not found in DOM")]

        self.assertTrue(_looks_like_failed_consent_overlay_attempt(calls, results))

    def test_consent_selector_query_error_is_detected(self):
        calls = [
            ToolCall(
                id="1",
                name="find_elements",
                args={"selector": 'button:has-text("Accept All & Continue")'},
            )
        ]
        results = [
            ActionResult(
                extracted_content=(
                    "(query error: SyntaxError: "
                    "'button:has-text(...)' is not a valid selector.)"
                )
            )
        ]

        self.assertTrue(_looks_like_failed_consent_overlay_attempt(calls, results))

    def test_non_consent_not_found_is_not_consent_overlay_attempt(self):
        calls = [
            ToolCall(
                id="1",
                name="evaluate_js",
                args={"expression": "document.querySelector('#search').click()"},
            )
        ]
        results = [ActionResult(extracted_content="Button not found in DOM")]

        self.assertFalse(_looks_like_failed_consent_overlay_attempt(calls, results))

    def test_consent_recovery_suggests_requested_section_url(self):
        task = (
            "Browse the Opinion section and list three article titles "
            "together with their respective authors. "
            "website: https://www.bloomberg.com/"
        )

        self.assertEqual(
            _direct_section_url_for_consent_recovery(
                task,
                "https://www.bloomberg.com/europe",
            ),
            "https://www.bloomberg.com/opinion",
        )

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
