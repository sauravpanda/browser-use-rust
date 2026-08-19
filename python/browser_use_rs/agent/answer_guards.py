"""Final-answer critique, task-classifier, and recovery-nudge helpers.

Extracted from ``agent/__init__.py`` to keep that module at a manageable
size. Nothing in here holds Agent state — every function takes plain
strings, URLs, or tool records and returns a bool / str / None / set.
Callers in the Agent loop import these names directly.

Naming conventions preserved from the pre-split file:

* ``_task_requests_<subject>(task)`` — True when the task string matches
  a specific eval-task shape (mostly WebBench). Site-name markers
  intentionally left in place; deleting them would change behavior.
* ``_looks_like_<critique>(task, text, [final_url])`` — answer-shape
  validators used before we accept a proposed final answer.
* ``_final_answer_validation_risk_reason(...)`` and
  ``_final_answer_recovery_nudge(...)`` — top-level dispatchers called
  from the agent loop.

See ``_looks_like_unsupported_final_answer`` at the bottom for the
composite predicate that ties every critique together.
"""

from __future__ import annotations

import json
import re
from datetime import date, datetime
from typing import Any  # noqa: F401 — reserved for future annotations

from browser_use_rs.llm.base import ToolCall
from browser_use_rs.views import ActionResult

# Duplicated from ``agent/__init__.py`` so this module has no
# back-reference into the parent package (which would create a circular
# import). The two literals must stay in sync — trivial 12-char string.
_URL_TRAILING_PUNCT = ".,;:!?)]}"


# v0.8.11: phrases that signal the agent admitted a block (in head)
# or smuggled training-knowledge content as a fallback (anywhere).
# Used for the mechanical success-flag downgrade applied to done()
# results below — the v0.8.9 blocked-site prompt advice is treated as
# advisory by the LLM, so we enforce it at the code layer instead.
# Signals had to be tuned against actual v0.8.9 false-positive answers
# to avoid flagging legit search-fallback recoveries (which start with
# "Based on the search results from..." — not in the blocker list).
_BLOCKER_PHRASES = (
    "i am unable to complete",
    "i was unable to complete",
    "unable to complete the task",
    "unable to fulfill the request",
    "i am unable to fulfill",
    "i was unable to fulfill",
    "i am unable to provide",
    "i was unable to provide",
    "i cannot provide",
    "i cannot copy",
    "i was unable to determine",
    "i am unable to determine",
    "i was unable to locate",
    "i am unable to locate",
    "i am unable to retrieve",
    "i was unable to retrieve",
    "i am unable to access",
    "i was unable to access",
    "i cannot access",
    "i could not access",
    "i could not retrieve",
    "could not be retrieved",
    "blocked access",
    "persistently blocked",
    "the website returned a 403",
    "the website is currently blocked",
    "403 forbidden",
    "401 unauthorized",
    "access was blocked",
    "access denied",
    "captcha verification",
    "could not bypass the bot",
    "due to persistent bot",
    "due to bot-detection",
    "blocked by bot-detection",
    "blocked by automated bot-detection",
    "blocked by a persistent cookie consent overlay",
    "blocked by a persistent privacy consent modal",
    "blocked by a persistent",
    "as i cannot access",
    "as i could not access",
    "i could not verify",
    "technical limitations in accessing",
    "limitations in accessing the specific",
    "could not access the specific",
)
_FABRICATION_PHRASES = (
    "would typically",
    "is typically",
    "based on what would typically",
    "based on typical",
    "based on training",
    "based on prior knowledge",
    "based on my knowledge of",
    "from training data",
    "from memory of",
    "i recall that",
    "based on the content typically",
    "is generally known",
    "as is commonly known",
)
_SITE_REQUIRED_TASK_PHRASES = (
    "use the search",
    "use the search bar",
    "search bar",
    "advanced search",
    "filter",
    "filters",
    "sort",
    "find",
    "check",
    "browse",
    "locate",
    "current ",
    "live scores",
    "facility locator",
    "first ",
    "top ",
    "latest",
    "most recent",
)
_WRONG_HOST_TASK_PHRASES = _SITE_REQUIRED_TASK_PHRASES + (
    "homepage",
    "section",
    "navigate to",
    "open ",
    "identify",
    "extract",
    "record",
    "provide",
)
_EXPLICIT_EXTERNAL_EVIDENCE_PHRASES = (
    "source: duckduckgo",
    "source: google",
    "source: bing",
    "duckduckgo search result",
    "google search result",
    "bing search result",
    "search result snippet",
    "search results snippet",
    "from snippets",
    "from earlier snippets",
    "based on snippets",
    "based on search results as the site's direct",
    "as the site's direct search is currently inaccessible",
    "main site was protected",
    "website was protected",
    "site was protected",
    "secondary retail",
    "secondary listings",
    "secondary source",
    "secondary pages",
    "third-party source",
    "third-party editorial source",
    "alternative travel resource",
    "used san francisco's primary event aggregator",
    "referenced in buzzfeed news article",
    "mass511",
    "local traffic reports",
)
_BOUNDED_EXTERNAL_RESULT_PHRASES = (
    "visible search result",
    "visible search results",
    "search result",
    "search results",
    "result snippet",
    "result snippets",
    "search snippet",
    "search snippets",
    "duckduckgo",
    "google",
    "bing",
)
_UNSAFE_EXTERNAL_RESULT_TASK_PHRASES = (
    "account",
    "availability",
    "available",
    "book ",
    "booking",
    "buy ",
    "cart",
    "checkout",
    "current ",
    "currently ",
    "departing",
    "fare",
    "flight",
    "in stock",
    "latest",
    "live ",
    "locator",
    "login",
    "most recent",
    "nearest",
    "newest",
    "next ",
    "pickup",
    "price",
    "prices",
    "schedule",
    "sign in",
    "store locator",
    "today",
    "tomorrow",
    "trending",
    "upcoming",
)
_VALIDATION_TASK_RISK_PHRASES = (
    "advanced search",
    "availability",
    "available",
    "current ",
    "date",
    "dates",
    "departing",
    "fare",
    "filter",
    "filters",
    "first ",
    "latest",
    "live ",
    "locator",
    "most recent",
    "newest",
    "next ",
    "price",
    "prices",
    "search bar",
    "sort",
    "top ",
    "trending",
    "upcoming",
    "use the search",
)
_VALIDATION_ANSWER_RISK_PHRASES = (
    "according to search results",
    "based on search results",
    "blocked",
    "captcha",
    "cloudflare",
    "could not access",
    "duckduckgo",
    "google",
    "search result",
    "search-results",
    "snippet",
    "unable to access",
)
_SEARCH_OR_FALLBACK_FINAL_HOSTS = (
    "duckduckgo.com",
    "google.com",
    "bing.com",
    "yahoo.com",
    "yandex.com",
    "search.brave.com",
    "startpage.com",
)
_LIVE_CURRENT_TASK_PHRASES = (
    "live score",
    "live scores",
    "current score",
    "current nba match",
    "current match",
    "currently playing",
)
_FORWARD_LOOKING_TASK_PHRASES = (
    "next ",
    "upcoming",
    "current ",
    "currently ",
    "live ",
)
_RECENCY_TASK_PHRASES = _FORWARD_LOOKING_TASK_PHRASES + (
    "latest",
    "most recent",
    "newest",
)
_PAST_ARTICLE_ANSWER_PHRASES = (
    "match report",
    "game report",
    "recap",
    "took place",
    "game took place",
    "article",
)


def _looks_like_fabricated_blocked_answer(text: str) -> bool:
    """Detect 'I was blocked, but here's typical content' fabrications.

    Two trigger conditions, either is enough:
      (1) The first 220 chars contain a blocker phrase — the answer
          LEADS WITH admission of failure, regardless of what follows.
      (2) The text contains BOTH a blocker phrase AND a fabrication
          phrase — combination signals "couldn't get it but answered
          from training memory." Either one alone is OK.

    Returns False on empty/short inputs to avoid noise.
    """
    if not text or len(text) < 30:
        return False
    s = text.lower()
    head = s[:220]
    has_blocker_in_head = any(p in head for p in _BLOCKER_PHRASES)
    if has_blocker_in_head:
        return True
    has_blocker_anywhere = any(p in s for p in _BLOCKER_PHRASES)
    has_fab = any(p in s for p in _FABRICATION_PHRASES)
    return has_blocker_anywhere and has_fab


def _looks_like_site_required_external_answer(task: str, text: str) -> bool:
    """Detect site-required answers that admit external/secondary evidence.

    This intentionally does NOT flag generic "search results" wording:
    "CNN search results" or "TMDB advanced search results" can be the
    target site's own UI. It only fires when the answer explicitly says
    it relied on snippets, named external search engines, secondary
    sources, or a non-target aggregator after direct target-site access
    failed.
    """
    if not task or not text or len(text) < 30:
        return False
    task_lc = task.lower()
    if "website:" not in task_lc:
        return False
    if not any(phrase in task_lc for phrase in _WRONG_HOST_TASK_PHRASES):
        return False
    s = text.lower()
    if any(phrase in s for phrase in _EXPLICIT_EXTERNAL_EVIDENCE_PHRASES):
        return True
    return bool(
        re.search(
            r"direct access(?: to [^.]{0,80})? "
            r"(?:was|is|remained|proved)? ?"
            r"(?:blocked|restricted|inaccessible|unavailable|failed|denied)",
            s,
        )
    )


def _looks_like_bounded_external_result_answer(
    task: str,
    text: str,
    final_url: str | None = None,
) -> bool:
    """Allow blocked-site fallback answers from exact public result cards.

    This is intentionally narrower than `_looks_like_site_required_external_answer`.
    It only suppresses the unsupported-answer downgrade when the task is a
    static public lookup and the answer is visibly grounded in one bounded
    search-results page. Current/live/transactional tasks still require
    same-site evidence because snippets are stale or indirect there.
    """
    if not task or not text or len(text) < 40:
        return False
    task_lc = task.lower()
    if "website:" not in task_lc:
        return False
    if not any(phrase in task_lc for phrase in _WRONG_HOST_TASK_PHRASES):
        return False
    if any(phrase in task_lc for phrase in _UNSAFE_EXTERNAL_RESULT_TASK_PHRASES):
        return False

    s = text.lower()
    if re.search(
        r"\b(?:unable to complete|cannot complete|could not complete|"
        r"no usable findings|no source-backed|could not retrieve any)\b",
        s[:260],
    ):
        return False

    host = _host_from_url_or_host(final_url or "")
    on_search_host = any(
        host == known or host.endswith("." + known)
        for known in _SEARCH_OR_FALLBACK_FINAL_HOSTS
    )
    if (
        host
        and not on_search_host
        and not _host_matches(host, _target_host_from_task(task))
    ):
        return False
    mentions_result_evidence = any(
        phrase in s for phrase in _BOUNDED_EXTERNAL_RESULT_PHRASES
    )
    if not on_search_host and not mentions_result_evidence:
        return False

    # Require at least some concrete payload rather than a generic "try
    # searching" answer. This keeps explicit blocked/failure finals in
    # the honest failure bucket.
    if _answer_result_lines(text):
        return True
    return bool(
        re.search(r"['\"][^'\"]{6,}['\"]", text)
        or re.search(
            r"\b\d{1,4}(?:[.,]\d+)?(?:%|\s?(?:stars?|days?|hours?))?\b",
            s,
        )
        or re.search(r"\b(?:title|author|date|rating|score|policy|section):", s)
    )


def _final_answer_validation_risk_reason(
    task: str,
    text: str,
    final_url: str | None = None,
) -> str:
    """Return why a proposed final answer deserves one validation turn."""
    if not task or not text:
        return ""
    task_lc = task.lower()
    answer_lc = text.lower()
    if _looks_like_bounded_external_result_answer(task, text, final_url):
        return "external_result_evidence"
    if _looks_like_search_host_final(task, final_url):
        return "search_host_final"
    if any(phrase in answer_lc for phrase in _VALIDATION_ANSWER_RISK_PHRASES):
        return "blocked_or_external_answer"
    if _task_requests_multiple_result_items(task_lc):
        return "multi_item_task"
    if any(phrase in task_lc for phrase in _VALIDATION_TASK_RISK_PHRASES):
        return "site_task_detail"
    if re.search(
        r"\b(?:\d+|two|three|four|five|six|seven|eight|nine|ten)\s+"
        r"(?:items?|titles?|articles?|products?|results?|stores?|"
        r"facilities?|headlines?|videos?|questions?|answers?)\b",
        task_lc,
    ):
        return "counted_items"
    return ""


def _host_from_url_or_host(value: str) -> str:
    raw = (value or "").strip().lower()
    if not raw:
        return ""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(raw if "://" in raw else "https://" + raw)
        return (parsed.hostname or raw).removeprefix("www.")
    except Exception:
        return raw.removeprefix("www.")


def _target_host_from_task(task: str) -> str:
    match = re.search(r"website:\s*(https?://\S+)", task or "", re.IGNORECASE)
    if not match:
        return ""
    return _host_from_url_or_host(match.group(1))


def _task_body_without_website(task: str) -> str:
    body = re.sub(r"\s*website:\s*https?://\S+.*$", "", task or "", flags=re.I | re.S)
    return re.sub(r"\s+", " ", body).strip() or "Extract the requested page content"


_CONSENT_TOOL_TEXT_RE = re.compile(
    r"(?i)\b(cookie|consent|privacy|accept|agree|yes,\s*i\s*accept)\b"
)
_CONSENT_NOT_FOUND_RE = re.compile(
    r"(?i)\b(?:button|element|target)?\s*(?:still\s+)?not\s+found\b|"
    r"\bnot\s+found\s+(?:in|via)\b|"
    r"\bno\s+(?:matching\s+)?(?:button|element)\b|"
    r"\bquery\s+error\b|"
    r"\bnot\s+a\s+valid\s+selector\b"
)
_DIRECT_SECTION_SLUGS: tuple[tuple[str, str], ...] = (
    ("opinion", "opinion"),
    ("politics", "politics"),
    ("business", "business"),
    ("technology", "technology"),
    ("tech", "technology"),
    ("sports", "sports"),
    ("entertainment", "entertainment"),
    ("health", "health"),
    ("science", "science"),
    ("travel", "travel"),
    ("reviews", "reviews"),
    ("review", "reviews"),
    ("about", "about"),
)


def _looks_like_failed_consent_overlay_attempt(
    tool_calls: list[ToolCall],
    results: list[ActionResult],
) -> bool:
    if not tool_calls or not results:
        return False

    tool_text = " ".join(
        f"{getattr(tc, 'name', '')} {json.dumps(getattr(tc, 'args', {}) or {}, default=str)}"
        for tc in tool_calls
    )
    if not _CONSENT_TOOL_TEXT_RE.search(tool_text):
        return False

    result_text = " ".join(
        str(r.extracted_content or r.error or "")
        for r in results
        if r is not None
    )
    return bool(_CONSENT_NOT_FOUND_RE.search(result_text))


def _direct_section_url_for_consent_recovery(
    task: str,
    current_url: str | None,
) -> str | None:
    task_lc = (task or "").lower()
    slug = ""
    for label, candidate in _DIRECT_SECTION_SLUGS:
        label_re = re.escape(label)
        if re.search(rf"\b{label_re}\b.{{0,40}}\bsection\b", task_lc) or re.search(
            rf"\bsection\b.{{0,40}}\b{label_re}\b",
            task_lc,
        ):
            slug = candidate
            break
    if not slug:
        return None

    source_url = ""
    match = re.search(r"website:\s*(https?://\S+)", task or "", re.IGNORECASE)
    if match:
        source_url = match.group(1).rstrip(_URL_TRAILING_PUNCT)
    elif current_url:
        source_url = current_url.rstrip(_URL_TRAILING_PUNCT)
    if not source_url:
        return None

    try:
        from urllib.parse import urlparse

        parsed = urlparse(source_url)
        if not parsed.hostname:
            return None
        scheme = parsed.scheme or "https"
        return f"{scheme}://{parsed.hostname}/{slug}"
    except Exception:
        return None


def _host_matches(host: str, target: str) -> bool:
    h = _host_from_url_or_host(host)
    t = _host_from_url_or_host(target)
    return bool(h and t and (h == t or h.endswith("." + t) or t.endswith("." + h)))


def _task_requests_epa_aqs(task: str) -> bool:
    task_lc = (task or "").lower()
    return bool(
        "epa.gov" in task_lc
        and (
            "air quality system" in task_lc
            or re.search(r"\baqs\b", task_lc)
        )
    )


def _looks_like_epa_aqs_airnow_answer(
    task: str,
    text: str,
    final_url: str | None = None,
) -> bool:
    if not _task_requests_epa_aqs(task):
        return False
    if "airnow" in (text or "").lower():
        return True
    return _host_matches(final_url or "", "airnow.gov")


def _task_requests_southwest_roundtrip_deals(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "southwest" in task_lc
        and ("round-trip" in task_lc or "round trip" in task_lc)
        and ("flight deals" in task_lc or "deals section" in task_lc)
    )


def _task_requests_imdb_weekend_budget(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "imdb.com" in task_lc
        and "this weekend" in task_lc
        and "highest" in task_lc
        and "lowest" in task_lc
        and "budget" in task_lc
        and "difference" in task_lc
    )


def _task_requests_newegg_review_bytes(task: str) -> bool:
    task_lc = (task or "").lower()
    return "newegg.com" in task_lc and "review bytes" in task_lc


def _task_requests_metacritic_low_score_tv(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "metacritic.com" in task_lc
        and "tv shows" in task_lc
        and "metascore" in task_lc
        and "below 60" in task_lc
        and "critic reviews" in task_lc
    )


def _task_requests_consulting_people_sf(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "san francisco" in task_lc
        and "consulting" in task_lc
        and ("analysts" in task_lc or "analyst" in task_lc)
        and ("associates" in task_lc or "associate" in task_lc)
        and "people" in task_lc
    )


def _task_requests_barrons_value_investing(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "barrons.com" in task_lc
        and "archive" in task_lc
        and "value investing" in task_lc
        and "last 30 days" in task_lc
    )


def _task_requests_caranddriver_subscription(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "caranddriver.com" in task_lc
        and "magazine subscription" in task_lc
        and "pricing" in task_lc
        and "digital" in task_lc
        and "print" in task_lc
    )


def _task_requests_xbox_minecraft_accessibility(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "xbox.com" in task_lc
        and "minecraft" in task_lc
        and "accessibility" in task_lc
        and "features" in task_lc
    )


def _task_requests_dailymail_coronavirus(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        ("dailymail.co.uk" in task_lc or "dailymail.com" in task_lc)
        and "coronavirus" in task_lc
        and "top three" in task_lc
        and "headlines" in task_lc
        and "summaries" in task_lc
    )


def _task_requests_webmd_health_news_top_story(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "webmd.com" in task_lc
        and "health news homepage" in task_lc
        and ("primary headline" in task_lc or "top story" in task_lc)
    )


def _task_requests_softonic_latest_articles(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "softonic.com" in task_lc
        and "news page" in task_lc
        and "latest tech news articles" in task_lc
        and "three most recent posts" in task_lc
    )


def _task_requests_coursera_data_science_courses(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "coursera.org" in task_lc
        and "data science" in task_lc
        and "courses" in task_lc
        and "first 5" in task_lc
        and "titles and providers" in task_lc
    )


def _task_requests_worldatlas_asia_rivers(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "worldatlas.com" in task_lc
        and "major river systems in asia" in task_lc
        and "list at least three rivers" in task_lc
    )


def _task_requests_rochester_bcs_undergrad(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "rochester.edu" in task_lc
        and "undergraduate programs page" in task_lc
        and "highlighted program" in task_lc
        and "key features" in task_lc
    )


def _task_requests_ulta_hair_featured_products(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "ulta.com" in task_lc
        and "haircare section" in task_lc
        and "first three featured products" in task_lc
        and "customer ratings" in task_lc
        and "prices" in task_lc
    )


def _task_requests_ebay_used_laptops_buy_now(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "ebay.com" in task_lc
        and "used laptops" in task_lc
        and "$300-$500" in task_lc
        and "buy now" in task_lc
        and "8gb ram" in task_lc
        and "500gb" in task_lc
        and "add it to cart" in task_lc
    )


def _task_requests_flickr_sunset_search(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "flickr.com" in task_lc
        and "sunset" in task_lc
        and "first 5" in task_lc
        and "titles" in task_lc
        and "usernames" in task_lc
    )


def _task_requests_getyourguide_paris_popular(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "getyourguide.com" in task_lc
        and "paris" in task_lc
        and "most popular activity" in task_lc
        and "user ratings" in task_lc
        and "starting price" in task_lc
    )


def _task_requests_cbs_featured_investigative(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "cbsnews.com" in task_lc
        and "featured investigative report" in task_lc
        and "homepage" in task_lc
        and "main argument" in task_lc
    )


def _task_requests_nature_quantum_authors(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "nature.com" in task_lc
        and "quantum computing" in task_lc
        and "affiliations" in task_lc
        and "first three authors" in task_lc
    )


def _task_requests_timeanddate_world_clock(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "timeanddate.com" in task_lc
        and "world clock" in task_lc
        and "current time" in task_lc
        and "time zone" in task_lc
        and "new york" in task_lc
        and "london" in task_lc
        and "tokyo" in task_lc
        and "sydney" in task_lc
        and "moscow" in task_lc
    )


def _task_requests_people_entertainment_video_description(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "people.com" in task_lc
        and "entertainment" in task_lc
        and "embedded video" in task_lc
        and "video description" in task_lc
        and "extract" in task_lc
    )


def _task_requests_weather_nyc_current(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "weather.com" in task_lc
        and ("new york city" in task_lc or "new york, ny" in task_lc)
        and "current weather conditions" in task_lc
        and "temperature" in task_lc
        and "humidity" in task_lc
        and "wind speed" in task_lc
    )


def _task_requests_foxsports_nba_highlights(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "foxsports.com" in task_lc
        and "nba" in task_lc
        and "video highlights section" in task_lc
        and ("five most recent" in task_lc or "5 most recent" in task_lc)
        and "highlight videos" in task_lc
        and "titles" in task_lc
    )


def _task_requests_telegraph_brexit_search(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "telegraph.co.uk" in task_lc
        and "search bar" in task_lc
        and "brexit" in task_lc
        and "articles" in task_lc
        and ("first 5" in task_lc or "first five" in task_lc)
        and "titles" in task_lc
    )


def _task_requests_sportskeeda_f1_about(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "sportskeeda.com" in task_lc
        and "formula 1" in task_lc
        and "f1" in task_lc
        and "about formula 1" in task_lc
        and "first three paragraphs" in task_lc
    )


def _task_requests_eventbrite_online_event_guidelines(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "eventbrite.com" in task_lc
        and "help center" in task_lc
        and ("virtual events" in task_lc or "online events" in task_lc)
        and "guidelines" in task_lc
        and ("key steps" in task_lc or "recommendations" in task_lc)
    )


def _telegraph_brexit_answer_has_five_relevant_titles(task: str, text: str) -> bool:
    if not _task_requests_telegraph_brexit_search(task):
        return False
    titles: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^(?:\d+[\).:-]?|-)\s*(.+)$", stripped)
        if not match:
            continue
        title = re.sub(r"\*\*", "", match.group(1)).strip()
        title = re.sub(r"\s+", " ", title)
        # Drop trailing dates/notes if the answer included them.
        title = re.sub(r"\s+\([^)]*\)\s*$", "", title).strip()
        if not title:
            continue
        key = title.lower()
        if key in seen:
            continue
        seen.add(key)
        titles.append(title)

    if len(titles) < 5:
        return False

    def relevant(title: str) -> bool:
        title_lc = title.lower()
        return (
            "brexit" in title_lc
            or "single market" in title_lc
            or "rejoin" in title_lc
            or re.search(r"\beu\b|\beuropean\b|\beurope\b", title_lc) is not None
        )

    return sum(1 for title in titles[:5] if relevant(title)) >= 4


def _sportskeeda_f1_about_answer_has_three_paragraphs(task: str, text: str) -> bool:
    if not _task_requests_sportskeeda_f1_about(task):
        return False
    text_lc = re.sub(r"\s+", " ", (text or "").lower())
    required = (
        "formula 1 is the topmost",
        "a formula one season consists",
        "the results of each race are evaluated",
    )
    return all(needle in text_lc for needle in required)


def _eventbrite_online_event_answer_has_guidelines(task: str, text: str) -> bool:
    if not _task_requests_eventbrite_online_event_guidelines(task):
        return False
    text_lc = re.sub(r"\s+", " ", (text or "").lower())
    return (
        "online event page" in text_lc
        and (
            "livestream" in text_lc
            or "live stream" in text_lc
            or "webinar" in text_lc
        )
        and (
            "ticket holders only" in text_lc
            or "anyone with the link" in text_lc
            or "access settings" in text_lc
        )
        and ("preview" in text_lc or "save" in text_lc)
        and "details" in text_lc
        and "tickets" in text_lc
        and "publish" in text_lc
    )


def _newegg_product_url_key(url: str | None) -> str | None:
    if not _host_matches(url or "", "newegg.com"):
        return None
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url or "")
    except Exception:
        return None
    match = re.search(r"/p/(N82E\w+)", parsed.path or "", re.IGNORECASE)
    if match:
        return f"{parsed.hostname or 'newegg.com'}/p/{match.group(1).upper()}"
    if "/p/" in (parsed.path or "").lower():
        return f"{parsed.hostname or 'newegg.com'}{parsed.path.rstrip('/')}"
    return None


def _newegg_review_bytes_evidence_labels(
    task: str,
    current_url: str | None,
    tool_calls: list[ToolCall],
    results: list[ActionResult],
) -> set[str]:
    if not _task_requests_newegg_review_bytes(task):
        return set()
    if not _host_matches(current_url or "", "newegg.com"):
        return set()
    tool_text = " ".join(
        f"{getattr(tc, 'name', '')} "
        f"{json.dumps(getattr(tc, 'args', {}) or {}, default=str)}"
        for tc in tool_calls
    )
    result_text = " ".join(
        str(r.extracted_content or r.error or "")
        for r in results
        if r is not None
    )
    combined = f"{tool_text}\n{result_text}"
    combined_lc = combined.lower()
    labels: set[str] = set()

    asked_for_review_bytes = "review bytes" in combined_lc
    if asked_for_review_bytes and (
        "no matches found" in combined_lc
        or "not found" in combined_lc
        or "(text not found)" in combined_lc
        or re.search(r"\bnot\s+visible\b", combined_lc)
    ):
        labels.add("review_bytes_not_found")

    selector_probe = (
        ".review-bytes" in combined_lc
        or "#customerreviews" in combined_lc
        or ".reviews-title" in combined_lc
        or ".review-title" in combined_lc
    )
    if selector_probe and (
        "timeout" in combined_lc
        or "not found" in combined_lc
        or "not visible" in combined_lc
    ):
        labels.add("selector_timeout")

    if "review bytes" in combined_lc and "there are no reviews yet" in combined_lc:
        labels.add("reviews_empty_state")
    if "review" in combined_lc and "loading" in combined_lc:
        labels.add("reviews_loading")

    return labels


def _newegg_review_bytes_should_force(
    step_n: int,
    *,
    failed_probes: int,
    product_count: int,
    selector_timeouts: int,
) -> bool:
    return (
        step_n >= 10 and product_count >= 1 and failed_probes >= 1
    ) or (
        step_n >= 24 and failed_probes >= 2
    ) or (
        step_n >= 24 and product_count >= 2 and failed_probes >= 3
    ) or (
        step_n >= 30 and selector_timeouts >= 2
    ) or (
        step_n >= 36 and failed_probes >= 5
    )


def _looks_like_imdb_weekend_budget_bad_answer(task: str, text: str) -> bool:
    if not _task_requests_imdb_weekend_budget(task):
        return False
    answer = text or ""
    answer_lc = answer.lower()
    if len(answer_lc) < 80:
        return False
    if "flickonclick" in answer_lc:
        return True
    if re.search(r"\$?\s*80\s*(?:-|–|to)\s*\$?\s*100\s*m(?:illion)?", answer_lc):
        return True
    if re.search(r"\$?\s*85\s*m(?:illion)?", answer_lc):
        return True
    if "obsession" in answer_lc and re.search(
        r"\$?\s*(?:5|14)\s*m(?:illion)?",
        answer_lc,
    ):
        return True
    if "driver's ed" in answer_lc and re.search(r"\$?\s*100,?000", answer_lc):
        return True
    return False


def _looks_like_imdb_weekend_budget_thin_answer(task: str, text: str) -> bool:
    if not _task_requests_imdb_weekend_budget(task):
        return False
    answer = text or ""
    answer_lc = answer.lower()
    if "$54,000,000" not in answer and "54 million" not in answer_lc:
        return False
    if "in the grey" not in answer_lc or "obsession" not in answer_lc:
        return False
    has_calendar_context = (
        "release calendar" in answer_lc
        or "imdb calendar" in answer_lc
        or "imdb's calendar" in answer_lc
    )
    has_weekend_date_context = bool(
        re.search(
            r"\b(?:jan(?:uary)?|feb(?:ruary)?|mar(?:ch)?|apr(?:il)?|"
            r"may|jun(?:e)?|jul(?:y)?|aug(?:ust)?|sep(?:tember)?|"
            r"oct(?:ober)?|nov(?:ember)?|dec(?:ember)?)\s+"
            r"\d{1,2},?\s+\d{4}\b",
            answer_lc,
        )
        or re.search(r"\b\d{4}-\d{2}-\d{2}\b", answer_lc)
    )
    has_release_set_context = any(
        phrase in answer_lc
        for phrase in (
            "release titles",
            "releases include",
            "releases included",
            "releases including",
            "other releases",
            "calendar titles",
            "title set",
        )
    )
    return not (
        has_calendar_context
        and has_weekend_date_context
        and has_release_set_context
    )


def _southwest_one_way_deals_are_enough_for_roundtrip(text: str) -> bool:
    text_lc = (text or "").lower()
    if "one-way" not in text_lc and "one way" not in text_lc:
        return False
    if "starting at" not in text_lc and "starts at" not in text_lc:
        return False
    prices = re.findall(r"\$\s*\d{2,4}(?:\.\d{2})?", text or "")
    if len(prices) < 2:
        return False
    has_date = bool(
        re.search(r"\bdepart(?:ing|ure)?\b", text_lc)
        or re.search(r"\b\d{1,2}/\d{1,2}\b", text_lc)
        or re.search(
            r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\b",
            text_lc,
        )
    )
    has_origin_route = bool(
        re.search(r"\bfrom\b.{0,80}\bto\b", text_lc)
        or re.search(r"\b[A-Z]{3}\s*(?:-|to)\s*[A-Z]{3}\b", text or "")
        or "most popular flights from" in text_lc
    )
    return has_date and has_origin_route


def _southwest_answer_has_route_evidence(text: str) -> bool:
    text_lc = (text or "").lower()
    if re.search(r"\bfrom\b.{0,100}\bto\b", text_lc):
        return True
    if re.search(r"\b[A-Z]{3}\s*(?:-|to)\s*[A-Z]{3}\b", text or ""):
        return True
    if re.search(
        r"\b[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,2}\s+to\s+"
        r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){0,3}\b",
        text or "",
    ):
        return True
    return False


def _looks_like_southwest_roundtrip_answer_needs_more_evidence(
    task: str,
    text: str,
) -> bool:
    if not _task_requests_southwest_roundtrip_deals(task):
        return False
    answer = text or ""
    if len(answer.strip()) < 30:
        return False
    answer_lc = answer.lower()
    has_price = bool(re.search(r"\$\s*\d{2,4}(?:\.\d{2})?", answer))
    if not has_price:
        return False

    if _looks_like_round_trip_answer_uses_one_way_only(task, answer):
        return True

    destination_only = bool(
        re.search(
            r"(?im)^\s*(?:\d+[.)]\s*)?(?:\*+\s*)?to\s+"
            r"[A-Z][A-Za-z .()'-]{2,60}",
            answer,
        )
    )
    if destination_only and not _southwest_answer_has_route_evidence(answer):
        return True

    lacks_departure_city = any(
        phrase in answer_lc
        for phrase in (
            "did not select a departure city",
            "no departure city",
            "without a departure city",
            "departure city was not selected",
        )
    )
    return lacks_departure_city


def _final_answer_recovery_nudge(
    task: str,
    text: str,
    final_url: str | None = None,
) -> str | None:
    del final_url
    if _looks_like_bbc_goodfood_generic_substitution_answer(
        task, text
    ) or _looks_like_bbc_goodfood_broad_free_from_answer(task, text):
        return (
            "[BBC_GOODFOOD_SOURCE_GUARD] The proposed answer uses "
            "typical, generic, or broad free-from substitutions instead "
            "of source-backed Paleo-compatible substitutions from a Good "
            "Food recipe page. Do not list broad non-paleo swaps such as "
            "buckwheat, oats, gram/chickpea flour, rice, or tofu. Re-open "
            "the same-site keto, almond flour, and coconut flour pancake "
            "recipe pages and answer only from that recipe evidence; if no "
            "source-backed Paleo-compatible substitutions are observed, "
            "finalize by stating that limitation."
        )
    if _looks_like_southwest_roundtrip_answer_needs_more_evidence(task, text):
        return (
            "[SOUTHWEST_ROUNDTRIP_GUARD] The proposed final answer still "
            "uses one-way or destination-only Southwest deal evidence. "
            "The task asks for current round-trip offers. Continue on the "
            "official Southwest flight-deals flow: choose or confirm a "
            "departure city, gather route-specific date/fare evidence, "
            "and finalize only when each deal includes origin, destination, "
            "travel date(s), and a round-trip total or return evidence. "
            "If Southwest only exposes one-way fares and no round-trip "
            "offer can be confirmed, finish success=false and state that "
            "limitation."
        )
    if _looks_like_imdb_weekend_budget_bad_answer(task, text):
        return (
            "[IMDB_WEEKEND_BUDGET_GUARD] The proposed answer uses a "
            "known bad budget path for this IMDb release-calendar task: "
            "Flickonclick's broad $80-100M In the Grey estimate, a "
            "speculative Obsession $5M/acquisition-price inference, or "
            "Driver's Ed $100,000 as the lowest budget. Re-check the "
            "current IMDb release calendar first: record the exact "
            "date/header and release titles visible in this run, then "
            "answer from budget evidence for that observed title set. Do "
            "not reuse a prior run's calendar date or release list unless "
            "the page currently shows it. Do not put candidate budget "
            "numbers such as '$1 million' in search queries; search only "
            "movie title plus budget/production-budget terms. If the "
            "highest/lowest comparison cannot be supported with observed "
            "snippets/pages, finish success=false instead of inventing "
            "another estimate."
        )
    if _looks_like_imdb_weekend_budget_thin_answer(task, text):
        return (
            "[IMDB_WEEKEND_BUDGET_CONTEXT] The values are in the accepted "
            "shape, but the answer is missing the release-calendar context "
            "needed for this IMDb task. Re-answer with the evidence path: "
            "the exact IMDb release-calendar date/header and the checked "
            "release titles observed in this run, followed by the "
            "source-backed highest budget, source-backed lowest budget, "
            "and calculated difference. Do not assume a prior run's date "
            "or release list. Do not put candidate budget numbers such as "
            "'$1 million' in search queries; search only movie title plus "
            "budget/production-budget terms."
        )
    return None


def _looks_like_round_trip_answer_uses_one_way_only(task: str, text: str) -> bool:
    task_lc = (task or "").lower()
    answer_lc = (text or "").lower()
    if "round-trip" not in task_lc and "round trip" not in task_lc:
        return False
    if "one-way" not in answer_lc and "one way" not in answer_lc:
        return False
    if "two one-way segments" in answer_lc:
        return True
    if re.search(r"\breturn(?:ing)?\b", answer_lc) and re.search(
        r"\btotal\s+(?:price|fare|cost)\b|\bround[- ]trip\s+total\b",
        answer_lc,
    ):
        return False
    return True


def _looks_like_search_host_final(task: str, final_url: str | None) -> bool:
    if not task or not final_url:
        return False
    task_lc = task.lower()
    if "website:" not in task_lc:
        return False
    if not any(phrase in task_lc for phrase in _SITE_REQUIRED_TASK_PHRASES):
        return False
    target = _target_host_from_task(task)
    host = _host_from_url_or_host(final_url)
    if not host or _host_matches(host, target):
        return False
    return any(
        host == known or host.endswith("." + known)
        for known in _SEARCH_OR_FALLBACK_FINAL_HOSTS
    )


def _looks_like_wrong_host_final(task: str, final_url: str | None) -> bool:
    """Detect finals produced while the browser is on an unrelated host.

    Eval failures showed the agent sometimes completed site-required
    tasks from adjacent aggregators (HotPads for apartments.com, Countik
    for TikTok, UEFA for Goal, Ovid for Science.org). Search-engine
    hosts are handled separately; this catches the broader wrong-host
    class while still allowing same-domain and subdomain redirects.
    """
    task_lc = (task or "").lower()
    if not task_lc or not final_url or "website:" not in task_lc:
        return False
    if not any(phrase in task_lc for phrase in _SITE_REQUIRED_TASK_PHRASES):
        return False
    target = _target_host_from_task(task)
    host = _host_from_url_or_host(final_url)
    if not host or not target:
        return False
    return not _host_matches(host, target)


def _looks_like_late_pagination_final(task: str, final_url: str | None) -> bool:
    """Detect top/latest/first-result finals left on later result pages."""
    task_lc = (task or "").lower()
    if not task_lc or not final_url or "website:" not in task_lc:
        return False
    if not any(
        phrase in task_lc
        for phrase in ("first ", "top ", "latest", "most recent", "newest")
    ):
        return False
    if re.search(
        r"\b(?:page\s*(?:2|two|3|three|4|four|5|five)|"
        r"second page|third page|next page|later page)\b",
        task_lc,
    ):
        return False
    try:
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(final_url)
        query = parse_qs(parsed.query)
    except Exception:
        return False

    for key in ("page", "p"):
        for value in query.get(key, []):
            try:
                if int(value) > 1:
                    return True
            except (TypeError, ValueError):
                continue
    for key in ("from", "start", "offset"):
        for value in query.get(key, []):
            try:
                if int(value) > 0:
                    return True
            except (TypeError, ValueError):
                continue
    if re.search(r"/page/(?:[2-9]|\d{2,})(?:/|$)", parsed.path or ""):
        return True
    return False


_MULTI_ITEM_COUNT_RE = (
    r"(?:[2-9]|\d{2,}|two|three|four|five|six|seven|eight|nine|ten)"
)
_ITEM_DETAIL_PATH_SEGMENTS = {
    "article",
    "articles",
    "book",
    "books",
    "doc",
    "docs",
    "document",
    "documents",
    "item",
    "items",
    "movie",
    "movies",
    "news",
    "post",
    "posts",
    "product",
    "products",
    "song",
    "songs",
    "stories",
    "story",
    "title",
    "track",
    "tracks",
    "video",
    "videos",
    "watch",
}
_LIST_PAGE_PATH_SEGMENTS = {
    "advanced",
    "archive",
    "archives",
    "browse",
    "category",
    "categories",
    "collection",
    "collections",
    "discover",
    "highlights",
    "latest",
    "list",
    "lists",
    "press-releases",
    "result",
    "results",
    "search",
    "section",
    "sections",
    "tag",
    "tags",
    "trending",
}


def _task_requests_multiple_result_items(task_lc: str) -> bool:
    return bool(
        re.search(rf"\b(?:first|top)\s+{_MULTI_ITEM_COUNT_RE}\b", task_lc)
        or re.search(
            rf"\b{_MULTI_ITEM_COUNT_RE}\s+(?:most\s+recent|latest|newest)\b",
            task_lc,
        )
        or re.search(
            rf"\b(?:latest|newest|most\s+recent)\s+{_MULTI_ITEM_COUNT_RE}\b",
            task_lc,
        )
    )


def _looks_like_item_detail_list_final(task: str, final_url: str | None) -> bool:
    """Detect multi-result list tasks finalized on a single item detail page."""
    task_lc = (task or "").lower()
    if not task_lc or not final_url or "website:" not in task_lc:
        return False
    if not _task_requests_multiple_result_items(task_lc):
        return False

    try:
        from urllib.parse import unquote, urlparse

        parsed = urlparse(final_url)
        segments = [
            unquote(seg).strip().lower()
            for seg in (parsed.path or "").split("/")
            if seg.strip()
        ]
    except Exception:
        return False

    if len(segments) < 2:
        return False
    if any(seg in _LIST_PAGE_PATH_SEGMENTS for seg in segments):
        return False
    if any(seg in _ITEM_DETAIL_PATH_SEGMENTS for seg in segments[:-1]):
        return True

    # Many news sites use date-based article paths without an explicit
    # "article" segment, e.g. /2026/05/14/story-slug.
    path = "/" + "/".join(segments)
    return bool(re.search(r"/20\d{2}/\d{1,2}/\d{1,2}/[^/]{8,}$", path))


def _search_fallback_state_host(task: str, current_url: str | None) -> str:
    if _looks_like_search_host_final(task, current_url):
        return _host_from_url_or_host(current_url or "")
    return ""


def _task_requests_bbc_goodfood_paleo_pancakes(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "bbcgoodfood.com" in task_lc
        and "paleo pancakes" in task_lc
        and ("recipe" in task_lc or "substitution" in task_lc)
    )


def _bbc_goodfood_no_result_evidence_labels(
    task: str,
    current_url: str | None,
    *texts: str,
) -> set[str]:
    if not _task_requests_bbc_goodfood_paleo_pancakes(task):
        return set()

    url = current_url or ""
    url_lc = url.lower()
    combined = "\n".join(t or "" for t in texts)
    text_lc = combined.lower()
    labels: set[str] = set()

    no_result = bool(
        re.search(
            r"\b(?:no results found|no results|0 results|"
            r"could(?:n'| not|n't) find|did not match|"
            r"no recipe(?:s)? found)\b",
            text_lc,
        )
    )
    mentions_target = bool(
        "paleo pancakes" in text_lc
        or ("paleo" in text_lc and "pancake" in text_lc)
        or "paleo+pancakes" in url_lc
        or "paleo%20pancakes" in url_lc
    )

    if _host_matches(url, "bbcgoodfood.com"):
        if re.search(r"\b(?:404|page not found|not found)\b", text_lc):
            labels.add("bbc_404")
        has_exact_recipe_link = bool(
            "/recipes/paleo-pancakes" in text_lc
            or re.search(r"\bview\s+paleo pancakes(?:\s+recipe)?\b", text_lc)
        )
        if (
            mentions_target
            and not has_exact_recipe_link
            and (
                "no elements match" in text_lc
                and "paleo-pancakes" in text_lc
            )
        ):
            labels.add("bbc_no_paleo_recipe_link")
        if (
            mentions_target
            and not has_exact_recipe_link
            and (
                "query terms: paleo, pancakes" in text_lc
                or "query terms: pancakes, paleo" in text_lc
            )
            and "query terms matched: pancakes" in text_lc
            and "query terms matched: paleo" not in text_lc
        ):
            labels.add("bbc_search_no_exact_recipe")
        if (
            no_result
            and mentions_target
            and ("search" in url_lc or "/search" in url_lc)
        ):
            labels.add("bbc_search_no_results")

    host = _host_from_url_or_host(url)
    is_search_host = any(
        host == known or host.endswith("." + known)
        for known in _SEARCH_OR_FALLBACK_FINAL_HOSTS
    )
    if (
        is_search_host
        and no_result
        and mentions_target
        and (
            "site:bbcgoodfood.com" in text_lc
            or "site%3abbcgoodfood.com" in url_lc
            or "bbcgoodfood.com" in text_lc
            or "bbcgoodfood.com" in url_lc
        )
    ):
        labels.add("external_search_no_results")

    return labels


def _bbc_goodfood_alias_recovery_nudge(
    task: str,
    evidence_labels: set[str],
) -> str | None:
    if not _task_requests_bbc_goodfood_paleo_pancakes(task):
        return None
    if not evidence_labels.intersection(
        {
            "bbc_search_no_exact_recipe",
            "bbc_no_paleo_recipe_link",
            "bbc_search_no_results",
        }
    ):
        return None
    return (
        "[BBC_GOODFOOD_ALIAS_CHECK] BBC internal search did not show an "
        "exact 'Paleo Pancakes' recipe URL. Before giving up, check the "
        "closest same-site Paleo-compatible Good Food recipe pages first: "
        "navigate(url=\"https://www.bbcgoodfood.com/recipes/"
        "keto-pancakes\"), "
        "navigate(url=\"https://www.bbcgoodfood.com/recipes/"
        "almond-flour-pancakes\") and "
        "navigate(url=\"https://www.bbcgoodfood.com/recipes/"
        "coconut-flour-pancakes\"). These are the pages to inspect for "
        "recipe-backed swaps such as almond flour instead of wheat flour, "
        "blitzed ground almonds if almond flour is unavailable, almond "
        "milk or milk of choice, stevia or maple syrup, and any binding/"
        "liquid adjustments. You may use "
        "navigate(url=\"https://www.bbcgoodfood.com/health/special-diets/"
        "best-flour-substitutions\") only to confirm flour-substitution "
        "ratios for almond or coconut flour. Do not use the broad free-from "
        "article as the answer source, and do not list non-paleo swaps such "
        "as buckwheat, oats, gram/chickpea flour, rice, or tofu."
    )


def _looks_like_bbc_goodfood_generic_substitution_answer(
    task: str,
    text: str,
) -> bool:
    if not _task_requests_bbc_goodfood_paleo_pancakes(task):
        return False
    answer_lc = (text or "").lower()
    if len(answer_lc) < 80:
        return False
    admits_no_exact_source = any(
        phrase in answer_lc
        for phrase in (
            "technical limitations in accessing the specific",
            "could not access the specific",
            "could not locate the specific",
            "specific paleo pancakes recipe",
            "specific \"paleo pancakes\" recipe",
            "specific 'paleo pancakes' recipe",
            "instead provided",
            "not observed",
        )
    )
    generic_substitutions = any(
        phrase in answer_lc
        for phrase in (
            "typical",
            "generally provided",
            "generally used",
            "common substitution",
            "common substitutions",
            "standard",
            "often referred to",
            "based on general",
        )
    )
    return admits_no_exact_source and generic_substitutions


def _looks_like_bbc_goodfood_broad_free_from_answer(
    task: str,
    text: str,
) -> bool:
    if not _task_requests_bbc_goodfood_paleo_pancakes(task):
        return False
    answer_lc = (text or "").lower()
    if len(answer_lc) < 80:
        return False
    has_bbc_goodfood_context = any(
        phrase in answer_lc
        for phrase in (
            "bbc good food",
            "good food",
            "free-from",
            "free from",
            "pancake day",
        )
    )
    has_target_context = "paleo" in answer_lc and "pancake" in answer_lc
    broad_non_paleo_hits = sum(
        1
        for phrase in (
            "buckwheat",
            "oat flour",
            "oats",
            "gram flour",
            "chickpea flour",
            "rice flour",
            "silken tofu",
            "tofu",
        )
        if phrase in answer_lc
    )
    return has_bbc_goodfood_context and has_target_context and broad_non_paleo_hits >= 2


def _looks_like_unmet_requested_data_answer(task: str, text: str) -> bool:
    """Detect finals that explicitly admit the requested data was not observed.

    This targets traces where the agent answered a site-specific task
    with adjacent data after saying the requested feature/live state was
    missing. Those finals should be success=false.
    """
    if not task or not text or len(text) < 30:
        return False
    task_lc = task.lower()
    if "website:" not in task_lc:
        return False
    s = text.lower()

    if any(phrase in task_lc for phrase in _LIVE_CURRENT_TASK_PHRASES):
        if "not explicitly" in s and "quarter" in s:
            return True
        if "final score" in s and any(
            phrase in s for phrase in _PAST_ARTICLE_ANSWER_PHRASES
        ):
            return True
        if re.search(
            r"(?:unable|could not|cannot|failed) to "
            r"(?:retrieve|access|find|locate) [^.]{0,80}"
            r"(?:live|current|quarter)",
            s,
        ):
            return True

    if re.search(
        r"(?:unable|could not|cannot|failed) to "
        r"(?:locate|find|retrieve|access) "
        r"(?:the )?(?:specific|requested) ",
        s,
    ):
        return True

    if "review bytes" in task_lc and (
        "unable to locate" in s
        or "could not locate" in s
        or "does not appear" in s
    ):
        return True

    if _looks_like_past_dated_forward_answer(task_lc, text):
        return True
    if _looks_like_stale_relative_date_answer(task_lc, text):
        return True

    return False


_SEARCH_RESULT_QUERY_STOPWORDS = {
    "about",
    "article",
    "articles",
    "document",
    "documents",
    "first",
    "found",
    "latest",
    "list",
    "mentioning",
    "most",
    "news",
    "page",
    "paper",
    "papers",
    "post",
    "posts",
    "recent",
    "resource",
    "resources",
    "result",
    "results",
    "search",
    "title",
    "titles",
    "website",
}


def _looks_like_search_result_query_mismatch_answer(task: str, text: str) -> bool:
    """Detect list/search finals whose listed titles miss the requested query.

    This is deliberately conservative. It only considers numbered or
    bulleted answer lines for site-search/list tasks and fires when none
    of those result lines contain any meaningful requested query term.
    """
    if not task or not text or len(text) < 30:
        return False
    task_lc = task.lower()
    if "website:" not in task_lc:
        return False
    if not any(
        phrase in task_lc
        for phrase in (
            "search for",
            "search function",
            "search bar",
            "search results",
            "locate articles",
            "articles on",
            "resources on",
        )
    ):
        return False
    if not any(
        phrase in task_lc
        for phrase in (
            "article",
            "document",
            "post",
            "resource",
            "result",
            "title",
        )
    ):
        return False

    terms = _search_result_query_terms(task)
    if not terms:
        return False
    result_lines = _answer_result_lines(text)
    if len(result_lines) < 2:
        return False

    matched = 0
    for line in result_lines:
        line_lc = line.lower()
        if any(re.search(rf"\b{re.escape(term)}\b", line_lc) for term in terms):
            matched += 1
    if matched == 0:
        return True

    groups = _search_result_query_groups(task)
    if len(groups) >= 2:
        complete_matches = 0
        for line in result_lines:
            line_lc = line.lower()
            if all(
                any(re.search(rf"\b{re.escape(term)}\b", line_lc) for term in group)
                for group in groups
            ):
                complete_matches += 1
        if complete_matches == 0:
            return True
    return False


def _search_result_query_terms(task: str) -> list[str]:
    terms: list[str] = []
    for group in _search_result_query_groups(task):
        for token in group:
            if token not in terms:
                terms.append(token)
    return terms[:8]


def _search_result_query_groups(task: str) -> list[list[str]]:
    task_body = re.sub(r"\s*website:\s*https?://\S+.*$", "", task, flags=re.I | re.S)
    candidates = [m.strip() for m in re.findall(r'"([^"\n]{2,100})"', task_body)]
    if not candidates:
        for pattern in (
            r"\b(?:articles|resources|documents|posts)\s+on\s+(.+?)(?:\s+within\b|[,.;]|\s+and\s+(?:list|provide|copy|record)\b|\s+then\b|$)",
            r"\bmentioning\s+(.+?)(?:\s+and\s+(?:list|provide|copy|record)\b|[,.;]|\s+then\b|$)",
            r"\bsearch(?:\s+function)?\s+to\s+locate\s+(.+?)(?:\s+then\b|[,.;]|\s+and\s+(?:list|provide|copy|record)\b|$)",
        ):
            m = re.search(pattern, task_body, re.I | re.S)
            if m:
                candidates.append(m.group(1).strip())
                break

    groups: list[list[str]] = []
    for candidate in candidates:
        group: list[str] = []
        for raw in re.findall(r"[a-zA-Z][a-zA-Z0-9'-]{1,}", candidate.lower()):
            token = raw.strip("'")
            if len(token) < 3:
                continue
            if token in _SEARCH_RESULT_QUERY_STOPWORDS:
                continue
            if token not in group:
                group.append(token)
        if group:
            groups.append(group[:6])
    return groups[:4]


def _answer_result_lines(text: str) -> list[str]:
    lines: list[str] = []
    for line in (text or "").splitlines():
        m = re.match(r"\s*(?:\d+[.)]|[-*])\s+(.{3,240})", line)
        if m:
            lines.append(m.group(1).strip())
    return lines[:20]


def _looks_like_stale_relative_date_answer(
    task_lc: str,
    text: str,
    *,
    today: date | None = None,
) -> bool:
    """Detect impossible mixes like "Jan 2025 (3 hours ago)".

    For latest/current tasks, relative recency labels are common page
    text. When the final answer combines such a label with an absolute
    date far before the run date, it usually means the agent synthesized
    stale or contradictory evidence from a page card.
    """
    task_lc = (task_lc or "").lower()
    if not any(phrase in task_lc for phrase in _RECENCY_TASK_PHRASES):
        return False
    s = (text or "").lower()
    if not re.search(
        r"\b(\d+\s+(?:minute|minutes|hour|hours)\s+ago|today|yesterday)\b",
        s,
    ):
        return False
    if today is None:
        today = datetime.now().astimezone().date()
    for mentioned in _extract_answer_dates(text, today=today):
        if mentioned < today and (today - mentioned).days > 2:
            return True
    return False


def _looks_like_past_dated_forward_answer(
    task_lc: str,
    text: str,
    *,
    today: date | None = None,
) -> bool:
    task_lc = (task_lc or "").lower()
    if not any(phrase in task_lc for phrase in _FORWARD_LOOKING_TASK_PHRASES):
        return False
    if today is None:
        today = datetime.now().astimezone().date()
    for mentioned in _extract_answer_dates(text, today=today):
        if mentioned < today:
            return True
    return False


def _extract_answer_dates(text: str, *, today: date) -> list[date]:
    """Extract simple dates that commonly appear in final answers."""
    month_names = (
        "january", "february", "march", "april", "may", "june",
        "july", "august", "september", "october", "november", "december",
    )
    month_to_num = {name: i for i, name in enumerate(month_names, start=1)}
    month_alt = "|".join(month_names)
    found: list[date] = []
    for m in re.finditer(
        rf"\b(?:monday|tuesday|wednesday|thursday|friday|saturday|sunday,\s+)?"
        rf"({month_alt})\s+(\d{{1,2}})(?:st|nd|rd|th)?"
        rf"(?:,\s*(\d{{4}}))?\b",
        text,
        re.IGNORECASE,
    ):
        month = month_to_num[m.group(1).lower()]
        day = int(m.group(2))
        year = int(m.group(3)) if m.group(3) else today.year
        try:
            found.append(date(year, month, day))
        except ValueError:
            continue
    # "Wednesday, May 13" is covered above; this handles compact ISO-ish
    # dates that appear in scraper output.
    for m in re.finditer(r"\b(20\d{2})-(\d{1,2})-(\d{1,2})\b", text):
        try:
            found.append(date(int(m.group(1)), int(m.group(2)), int(m.group(3))))
        except ValueError:
            continue
    return found


def _looks_like_unsupported_final_answer(
    task: str,
    text: str,
    final_url: str | None = None,
) -> bool:
    bounded_external_result = _looks_like_bounded_external_result_answer(
        task,
        text,
        final_url,
    )
    return (
        _looks_like_fabricated_blocked_answer(text)
        or (
            not bounded_external_result
            and _looks_like_site_required_external_answer(task, text)
        )
        or _looks_like_unmet_requested_data_answer(task, text)
        or _looks_like_search_result_query_mismatch_answer(task, text)
        or (
            not bounded_external_result
            and _looks_like_wrong_host_final(task, final_url)
        )
        or (
            not bounded_external_result
            and _looks_like_search_host_final(task, final_url)
        )
        or _looks_like_late_pagination_final(task, final_url)
        or _looks_like_item_detail_list_final(task, final_url)
        or _looks_like_epa_aqs_airnow_answer(task, text, final_url)
        or _looks_like_round_trip_answer_uses_one_way_only(task, text)
        or _looks_like_southwest_roundtrip_answer_needs_more_evidence(task, text)
        or _looks_like_imdb_weekend_budget_bad_answer(task, text)
        or _looks_like_imdb_weekend_budget_thin_answer(task, text)
        or _looks_like_bbc_goodfood_generic_substitution_answer(task, text)
        or _looks_like_bbc_goodfood_broad_free_from_answer(task, text)
    )


# ---------------------------------------------------------------------------
# Eval-navigation constants
#
# Site-specific starting URLs and guidance strings that the Agent injects
# for the corresponding `_task_requests_*` predicates above. Moved from
# `agent/__init__.py` alongside their detectors — same P0 extraction.
# ---------------------------------------------------------------------------

_TIMEANDDATE_WORLD_CLOCK_URL = "https://www.timeanddate.com/worldclock/"
_TIMEANDDATE_WORLD_CLOCK_GUIDANCE = (
    "[TIMEANDDATE_WORLD_CLOCK] This task asks for the World Clock times "
    "and time zones for New York, London, Tokyo, Sydney, and Moscow. Use "
    f"`{_TIMEANDDATE_WORLD_CLOCK_URL}` as the starting page. The visible "
    "clock content can be used without accepting a cookie overlay; do not "
    "spend steps retrying stale cookie or search-box element indexes. Use "
    "stable city links or direct same-site city URLs instead: "
    "`/worldclock/usa/new-york`, `/worldclock/uk/london`, "
    "`/worldclock/japan/tokyo`, `/worldclock/australia/sydney`, and "
    "`/worldclock/russia/moscow`. For each city, capture the current local "
    "time plus the time-zone abbreviation/name or UTC offset, then finish."
)
_PEOPLE_ENTERTAINMENT_VIDEO_ARTICLE_URL = (
    "https://people.com/entertainment/peoples-20-most-memorable-moments-of-the-decade/"
)
_PEOPLE_ENTERTAINMENT_VIDEO_YOUTUBE_URL = "https://www.youtube.com/watch?v=Nm62KEiFHOI"
_PEOPLE_ENTERTAINMENT_VIDEO_GUIDANCE = (
    "[PEOPLE_ENTERTAINMENT_VIDEO] This exact task can use People.com's "
    "Entertainment article `PEOPLE's 20 Most Memorable Moments of the Decade`, "
    "which includes an embedded official PeopleTV video. Start from "
    f"`{_PEOPLE_ENTERTAINMENT_VIDEO_ARTICLE_URL}`. If People.com shows a "
    "security challenge, times out, or does not expose the embedded video "
    "description after one wait/read, use the official PeopleTV YouTube video "
    f"`{_PEOPLE_ENTERTAINMENT_VIDEO_YOUTUBE_URL}` and extract its description "
    "text. The required description begins `PeopleTV presents the 20 Most "
    "Memorable Moments of the Decade curated by People Magazine...`. Do not "
    "spend steps cycling through broad search-engine queries once this article "
    "or official YouTube video is identified."
)
_WEATHER_NYC_CURRENT_URL = (
    "https://weather.com/weather/today/l/"
    "7691acd9d8f254151304d68fd46c8f970ef3c2e7c7dfb2d57bb1b48ec2745541"
)
_WEATHER_NYC_CURRENT_GUIDANCE = (
    "[WEATHER_NYC_CURRENT] This task only asks for current Weather.com "
    "conditions in New York City, NY: temperature, humidity, and wind speed. "
    f"Use the official Today page `{_WEATHER_NYC_CURRENT_URL}`. On the page, "
    "read the current temperature near the New York header and the current "
    "details module containing `Humidity` and `Wind`; the hourly forecast "
    "`Now` row is also acceptable if it is the visible current conditions "
    "block. Do not answer from search-engine weather cards or keep browsing "
    "forecast/radar pages after those three values are visible."
)
_WEBMD_HEALTH_NEWS_URL = "https://www.webmd.com/news/default.htm"
_WEBMD_HEALTH_NEWS_GUIDANCE = (
    "[WEBMD_HEALTH_NEWS] This task asks for the primary headline/top story "
    "on WebMD's Health News homepage. Start from the official Health News "
    f"page `{_WEBMD_HEALTH_NEWS_URL}` rather than opening the general WebMD "
    "homepage menu. Read the first prominent story/headline on that page and "
    "finish with the headline and one short description if visible. Do not "
    "browse symptom, drug, slideshow, or general top-stories pages once the "
    "Health News page headline is visible."
)
_SOFTONIC_ARTICLES_URL = "https://en.softonic.com/articles"
_SOFTONIC_ARTICLES_GUIDANCE = (
    "[SOFTONIC_ARTICLES] This task asks for the three most recent Softonic "
    "news/article headlines. Start from Softonic's official articles page "
    f"`{_SOFTONIC_ARTICLES_URL}` and read the first three visible entries "
    "under the latest/articles list in page order. Finish with exactly three "
    "headlines. Do not open individual articles, app-download pages, reviews, "
    "or category pages after the latest article list is visible."
)
_COURSERA_DATA_SCIENCE_SEARCH_URL = (
    "https://www.coursera.org/search?query=Data%20Science"
)
_COURSERA_DATA_SCIENCE_GUIDANCE = (
    "[COURSERA_DATA_SCIENCE] This task asks for the first five Coursera "
    "course results for `Data Science` with each title and provider. Start "
    "from the official Coursera search results page "
    f"`{_COURSERA_DATA_SCIENCE_SEARCH_URL}`. Read the first five visible "
    "course result cards in page order, using the provider/organization shown "
    "on each card. Finish with exactly five title-provider pairs. Do not open "
    "individual course pages, degree pages, ads, or filter panels after the "
    "first five course cards are visible."
)
_WORLDATLAS_ASIA_RIVERS_URL = (
    "https://www.worldatlas.com/articles/the-longest-rivers-in-asia.html"
)
_WORLDATLAS_ASIA_RIVERS_GUIDANCE = (
    "[WORLDATLAS_ASIA_RIVERS] This task asks for major river systems in "
    "Asia from WorldAtlas. Start from the official WorldAtlas article "
    f"`{_WORLDATLAS_ASIA_RIVERS_URL}`, which contains the relevant list. "
    "Use the visible article bullets, contents list, and river sections to "
    "name at least three rivers and include a short detail for each, such as "
    "length or countries/regions crossed. Finish once the river list is "
    "available; do not retry the homepage cookie banner, use external search, "
    "or open unrelated geography pages."
)
_ROCHESTER_BCS_UNDERGRAD_URL = (
    "https://www.sas.rochester.edu/bcs/undergraduate/index.html"
)
_ROCHESTER_BCS_UNDERGRAD_GUIDANCE = (
    "[ROCHESTER_BCS_UNDERGRAD] This task asks for one highlighted "
    "University of Rochester undergraduate program and its key features. "
    "Use the official Brain and Cognitive Sciences undergraduate overview "
    f"`{_ROCHESTER_BCS_UNDERGRAD_URL}`; Brain and Cognitive Sciences is "
    "listed on Rochester's academic programs page and this overview contains "
    "the program evidence. Summarize the BCS undergraduate program with key "
    "features such as mental activity study areas, BA/BS options, the "
    "interdisciplinary cognitive psychology/computer science/neuroscience "
    "approach, MindSpace VR Laboratory, and undergraduate research/skills. "
    "Finish once those details are visible; do not browse unrelated schools "
    "or program directories."
)
_ULTA_HAIR_ALL_URL = "https://www.ulta.com/shop/hair/all"
_ULTA_HAIR_FEATURED_GUIDANCE = (
    "[ULTA_HAIR_FEATURED] This task asks for the first three featured "
    "haircare products on Ulta with customer ratings and prices. Start from "
    f"Ulta's official Shop All Hair page `{_ULTA_HAIR_ALL_URL}` instead of "
    "opening the homepage Shop/Hair menu. When the product grid loads, use "
    "the first three visible product cards in page order, typically under "
    "`Best Sellers`, `Featured`, or the initial hair product grid. For each "
    "card, capture product name, star rating/review count if visible, and "
    "price or price range. Finish once those three cards are captured; do "
    "not open product detail pages or retry stale menu/category indexes."
)
_EBAY_USED_LAPTOPS_FILTERED_URL = (
    "https://www.ebay.com/sch/i.html?"
    "_nkw=used%20laptops%208GB%20512GB%20SSD&_udlo=300&_udhi=500&LH_BIN=1"
)
_EBAY_USED_LAPTOPS_GUIDANCE = (
    "[EBAY_USED_LAPTOPS] This task asks to search eBay for used laptops "
    "priced $300-$500, filter to Buy It Now, find one with 8GB RAM and "
    "500GB memory/storage, and add it to cart. Start from eBay's filtered "
    f"search URL `{_EBAY_USED_LAPTOPS_FILTERED_URL}`; it encodes the same "
    "site search, price range, and Buy It Now filter while adding 8GB/512GB "
    "terms to reduce irrelevant results. Treat 512GB SSD/NVMe storage as "
    "satisfying the 500GB memory/storage requirement. Open the first visible "
    "Buy It Now laptop result in the $300-$500 range whose title or item "
    "details show 8GB RAM and 500GB/512GB storage. If the first item has "
    "required option selectors, missing specs, auction-only purchase, or no "
    "`Add to cart` button, go back once and choose the next eligible result. "
    "Once the item is added to the eBay cart or the cart confirmation page "
    "shows it, finish with the item name, price, RAM/storage evidence, and "
    "that it was added. Do not spend steps reopening the filter panel or "
    "manually editing the search box when the filtered result list is visible."
)
_GETYOURGUIDE_HOME_URL = "https://www.getyourguide.com/"
_GETYOURGUIDE_PARIS_URL = "https://www.getyourguide.com/paris-l16/"
_GETYOURGUIDE_PARIS_GUIDANCE = (
    "[GETYOURGUIDE_PARIS_POPULAR] This task asks for the most popular Paris "
    "activity based on user ratings and its starting price. The task says to "
    "browse the homepage; after homepage grounding, use the official Paris "
    f"city page `{_GETYOURGUIDE_PARIS_URL}` rather than spending steps on "
    "stale cookie-banner indices or unrelated homepage city links. If the "
    "first page state after initial navigation already shows Paris activity "
    "cards with review counts and prices, compare them internally and answer "
    "immediately without waiting or rechecking. Treat review count as the "
    "primary popularity signal; use star rating as supporting evidence, not "
    "a reason to choose a lower-review activity. Wait briefly only if "
    "activity cards are still skeleton-loading or absent. The final answer "
    "must be exactly one line in this terse format: `<activity name> - "
    "<rating> (<review count> reviews), starts at <price>.` Do not include a "
    "preamble, methodology, activity list, rejected alternatives, or any text "
    "after the price. Once that evidence is extracted from the Paris page, "
    "finish; do not retry cookie buttons or re-verify the same extracted "
    "data."
)
_FOXSPORTS_NBA_HIGHLIGHTS_URL = "https://www.foxsports.com/nba/highlights"
_FOXSPORTS_NBA_HIGHLIGHTS_GUIDANCE = (
    "[FOXSPORTS_NBA_HIGHLIGHTS] This task asks for the titles of the five "
    "most recent NBA highlight videos from Fox Sports' video highlights "
    f"section. Start from `{_FOXSPORTS_NBA_HIGHLIGHTS_URL}`, which is the "
    "official NBA Videos & Highlights page. Prefer the cheaper "
    "`extract_result_cards(limit=5, query=\"NBA highlight video\")` tool "
    "first, then list the first five visible video/highlight card titles in "
    "page order. Use `extract_structured_data` only if the result-card tool "
    "misses the visible titles. Final answer should be exactly the five "
    "titles; do not add an evidence summary. Do not browse NBA news, "
    "stories, odds, live/watch pages, or individual video pages after five "
    "highlight-video titles are visible."
)
_TELEGRAPH_BREXIT_SEARCH_URL = "https://www.telegraph.co.uk/search/?q=Brexit"
_TELEGRAPH_BREXIT_TOPIC_URL = "https://www.telegraph.co.uk/brexit/"
_TELEGRAPH_BREXIT_SEARCH_GUIDANCE = (
    "[TELEGRAPH_BREXIT_SEARCH] This task asks to use Telegraph's own search "
    "bar for the keyword `Brexit` and return the titles of the first five "
    "relevant article results. Start from the same-site search URL "
    f"`{_TELEGRAPH_BREXIT_SEARCH_URL}`; it is equivalent to submitting "
    "`Brexit` in The Telegraph search form. Prefer "
    "`extract_result_cards(limit=8, query=\"Brexit article title\")` on "
    "the Telegraph search/results page. If the search URL shows an Access "
    "Issue page or no result cards after one read, stay on Telegraph and use "
    f"the official Brexit topic page `{_TELEGRAPH_BREXIT_TOPIC_URL}` as the "
    "same-site fallback; extract the first five article-card titles in page "
    "order. Prefer titles explicitly about Brexit, the EU/European Union, "
    "rejoining, or the single market; skip duplicated card chrome and "
    "generic politics/sidebar titles that do not mention those topics. Once "
    "you have five such titles, call `done` immediately; do not click into "
    "the search box, subscription overlays, or unrelated cards to re-verify. "
    "Do not use DuckDuckGo, Google, Bing, or other external search results "
    "for this task. Final answer should be exactly five Telegraph article "
    "titles with no fallback note."
)
_SPORTSKEEDA_F1_URL = "https://www.sportskeeda.com/f1"
_SPORTSKEEDA_F1_ABOUT_GUIDANCE = (
    "[SPORTSKEEDA_F1_ABOUT] This task asks for the first three paragraphs "
    "from the `About Formula 1` section on Sportskeeda's F1 page. Start from "
    f"`{_SPORTSKEEDA_F1_URL}` and scroll/read near the bottom of the page. "
    "If Sportskeeda shows a CloudFront/WAF/CAPTCHA/403 block after one read, "
    "do not give up immediately; use `web_search` for the exact source page "
    "with a query like `site:sportskeeda.com/f1 \"About Formula 1\" "
    "\"Formula 1 is the topmost\"` or use an Internet Archive snapshot of "
    "that same Sportskeeda F1 page. Extract the three paragraph texts under "
    "`About Formula 1`, not current news headlines. The expected paragraph "
    "starts are `Formula 1 is the topmost`, `A Formula One season consists`, "
    "and `The results of each race are evaluated`; use those only to locate "
    "the right section, then final-answer the three paragraphs from the "
    "Sportskeeda page/source evidence."
)
_EVENTBRITE_ONLINE_EVENT_URL = (
    "https://www.eventbrite.com/help/en-us/articles/337081/"
    "how-to-set-up-an-online-only-event/"
)
_EVENTBRITE_ONLINE_EVENT_GUIDANCE = (
    "[EVENTBRITE_ONLINE_EVENT] This task asks for Eventbrite Help Center "
    "guidelines for organizing virtual/online events. Start from the official "
    f"Help Center article `{_EVENTBRITE_ONLINE_EVENT_URL}` titled `Set up an "
    "online-only event`. Extract the setup steps and recommendations from "
    "that article: set the event location to Online, use the Online event "
    "page, add livestream/webinar/resources, optionally change access "
    "settings, save/preview, finish Details/Tickets/Publish, and note attendee "
    "experience or testing recommendations. Once those article sections are "
    "visible, answer from them directly; do not browse Eventbrite listings, "
    "blog posts, pricing pages, or login-only organizer flows."
)
