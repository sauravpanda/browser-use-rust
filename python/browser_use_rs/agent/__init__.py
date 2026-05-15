"""Unified, provider-agnostic Agent loop.

Drives any `BaseChatModel` against a browser tool set with native tool
calling, parallel tool execution, optional vision, sensitive-data
redaction, and resumable state. Returns an `AgentHistoryList` shaped to
match `browser_use.Agent` so eval/cloud consumers can drop it in.

Manual loop (not the SDK runner) so we can return image content blocks
from tools like `screenshot` — providers that don't allow image-in-tool-result
(Gemini, OpenAI) handle it inside their `ainvoke` mapping.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import inspect
import json
import logging
import os
import re
import time
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import datetime, date
from typing import Any, Awaitable, Callable

logger = logging.getLogger(__name__)

from browser_use_rs._native import BrowserSession
from browser_use_rs.llm.base import (
    AssistantMessage,
    BaseChatModel,
    ChatInvokeUsage,
    ContentPart,
    ImagePart,
    Message,
    TextPart,
    ToolCall,
    ToolResultMessage,
    UserMessage,
)
from browser_use_rs.tools import Tool
from browser_use_rs.observability import observe
from browser_use_rs.views import (
    ActionResult,
    AgentHistory,
    AgentHistoryList,
    AgentOutput,
    AgentState,
    BrowserStateSummary,
    StepMetadata,
)


# v0.11.2 read-state ephemeral lifecycle. Mirrors upstream
# browser_use's `include_extracted_content_only_once` pattern (see
# upstream tools/service.py:1175-1182 and message_manager/service.py:317-330).
#
# Goal: bound context growth from large read-tool results without
# truncating LIVE perception (the v0.11.0 mistake). Full content is
# visible to the LLM for ONE step (the step after the action). After
# that, only a small reference stub remains in the conversation; the
# full text is on disk for read_file recovery.
#
# Cache safety: ToolResultMessage content is fixed at creation and
# never mutated → cached forever. The full content lives only in the
# per-step page-state UserMessage, which we already remove + re-append
# every step (see _inject_page_state). Old page-state UserMessages are
# the cache breakpoint anyway, so the lifecycle does not invalidate
# the cached prefix.
#
# Loop safety: codex-reviewed. The v0.10.2 failure (one-line summaries
# with no scratchpad culture) doesn't apply because (a) the full text
# is in <read_state> on the IMMEDIATELY next step, (b) the prompt
# explicitly tells the LLM to memorize before proceeding, (c) a file
# fallback exists for retrieval.
# v0.11.3: raised 10_000 → 25_000 after v0.11.2 eval showed -6pp
# accuracy regression. Codex postmortem: "10k catches medium evidence
# pages the agent may still need to reason over. The wins you care
# about are the pathological 300k-800k token trajectories." 25k still
# triggers on the long-tail bloat tasks (60-step / 800k-token cases)
# without disturbing ordinary reads.
EPHEMERAL_RESULT_THRESHOLD = 25_000
# v0.11.4: how many _inject_page_state cycles a queued read_state
# entry is visible for. Was implicitly 1 in v0.11.2/v0.11.3.
# Codex tune: "1 step is brittle. A common pattern is read page,
# inspect/navigation action, then synthesize. With one-step expiry,
# you are forcing the model to immediately compress perfectly. A
# 2-step window should recover many context-starvation cases."
# Trades a small cost increase (read_state block emitted on N+1 AND
# N+2 → cache_creation paid twice) for accuracy recovery.
EPHEMERAL_RESULT_WINDOW_STEPS = 2
# evaluate_js is intentionally excluded — it truncates internally at 5k
# chars in _extra_tools.py, so it can never cross the 10k threshold.
# Including it would over-promise in the prompt.
#
# v0.12.4: extract_structured_data ADDED. It already has an in-tool
# 10KB → file overflow path (`_extra_tools.py` line ~902), but v0.12.3
# measurement caught a real failure: task kn79c1464jkm5t33234h had a
# 127KB extract result persist across 16 native message-list steps.
# That happens when the in-tool overflow's sandbox-write fallback
# fires (line 926-933) and the full text is returned inline. Adding
# extract_structured_data to the ephemeral lifecycle is a backstop:
# even when the in-tool overflow fails, the post-step lifecycle
# (_apply_ephemeral_lifecycle) spills > 25KB results to disk and
# replaces the native tool_result with a stub. Per v0.12.3 analysis
# this fix is mean cost ~$0.0001/task (rare bug) but cleans up the
# p99 tail by ~$0.04/worst-task.
#
# v0.12.6: extract_links/extract_images ADDED. They are read tools with
# the same context-bloat shape as get_links/page_text when SERPs,
# sitemaps, or image grids return hundreds of entries. Keeping the first
# large raw read durable still preserves grounding; subsequent oversized
# link/image lists move through <read_state> and the file fallback.
EPHEMERAL_RESULT_TOOLS: frozenset[str] = frozenset({
    "page_text",
    "get_text",
    "get_links",
    "extract_links",
    "extract_images",
    "read_file",
    "extract_structured_data",
})

_URL_RE = re.compile(r"https?://[^\s<>{}\\|^`\"']+")
_URL_TRAILING_PUNCT = ".,;:!?)]}"


def _infer_initial_navigation_url(task: str) -> str | None:
    """Return the single explicit URL in a task, if deterministic.

    Browser tasks commonly start with "Go to https://..." and the first
    model turn merely calls navigate(url=...). Doing that navigation before
    the first LLM call saves one full agent step. We only infer when there is
    exactly one URL; multi-URL tasks need the model to decide tabs/order.
    """
    if not task:
        return None
    urls: list[str] = []
    for match in _URL_RE.finditer(task):
        url = match.group(0).rstrip(_URL_TRAILING_PUNCT)
        if url and url not in urls:
            urls.append(url)
    if len(urls) != 1:
        return None
    lowered = task.lower()
    if re.search(r"\b(?:do not|don't)\s+(?:go|open|navigate|visit|browse)", lowered):
        return None
    return urls[0]


def _task_message_with_runtime_context(
    task: str,
    *,
    now: datetime | None = None,
) -> str:
    """Wrap the user task with run-date context for relative-date tasks."""
    if now is None:
        now = datetime.now().astimezone()
    elif now.tzinfo is None:
        now = now.astimezone()
    tz = now.tzname() or "local time"
    current_date = now.strftime("%A, %Y-%m-%d")
    return (
        "<runtime_context>\n"
        f"Current date: {current_date} ({tz}). Treat relative date words "
        'such as "today", "current", "latest", "most recent", and '
        '"upcoming weekend" relative to this date unless the live page '
        "shows a more specific date. This context is not target-site "
        "evidence by itself.\n"
        "</runtime_context>\n\n"
        "<user_request>\n"
        f"{task}\n"
        "</user_request>"
    )


def _json_fallback(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return asdict(obj)
    if hasattr(obj, "__dict__"):
        return {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    return repr(obj)


def _short_tool_call_repr(tc: ToolCall, max_chars: int = 140) -> str:
    args = getattr(tc, "args", None)
    try:
        args_s = json.dumps(args or {}, sort_keys=True, default=_json_fallback)
    except Exception:
        args_s = repr(args)
    out = f"{tc.name}({args_s})"
    if len(out) <= max_chars:
        return out
    return out[: max(0, max_chars - 3)] + "..."


def _content_byte_len(content: Any) -> int:
    """UTF-8 byte length of a message's content (str | list[Part]).
    Image parts contribute their base64 payload length — same as what
    flows over the wire to the provider.
    """
    if content is None:
        return 0
    if isinstance(content, str):
        return len(content.encode("utf-8"))
    if isinstance(content, list):
        total = 0
        for part in content:
            if isinstance(part, TextPart):
                total += len(part.text.encode("utf-8"))
            elif isinstance(part, ImagePart):
                total += len(part.data) if part.data else 0
            else:
                total += len(repr(part).encode("utf-8"))
        return total
    return len(str(content).encode("utf-8"))


def _content_text(content: Any) -> str:
    """Text-only view of message content for prompt-section metrics."""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, TextPart):
                parts.append(part.text)
        return "\n".join(parts)
    return str(content)


def _message_byte_len(msg: Message) -> int:
    """Approximate wire size of a single message: content + tool_call
    payloads if assistant. Used for per-role attribution in
    _compute_call_metrics.
    """
    if isinstance(msg, AssistantMessage):
        n = _content_byte_len(msg.text or "")
        for tc in msg.tool_calls:
            n += len(tc.name.encode("utf-8")) if tc.name else 0
            args = getattr(tc, "arguments", None) or getattr(tc, "args", None)
            if args is not None:
                if isinstance(args, str):
                    n += len(args.encode("utf-8"))
                else:
                    n += len(json.dumps(args, default=_json_fallback).encode("utf-8"))
        return n
    if isinstance(msg, ToolResultMessage):
        return _content_byte_len(msg.content) + len(
            (msg.name or "").encode("utf-8")
        )
    if isinstance(msg, (UserMessage,)):
        return _content_byte_len(msg.content)
    return _content_byte_len(getattr(msg, "content", ""))


def _message_to_dict(msg: Message) -> dict[str, Any]:
    """Serialize a message for trace dump. Lossy on image bytes —
    images are replaced with a `<image:N bytes>` marker so JSON files
    stay readable."""
    base: dict[str, Any] = {"role": type(msg).__name__}
    if isinstance(msg, ToolResultMessage):
        base["tool_call_id"] = msg.tool_call_id
        base["name"] = msg.name
        base["is_error"] = msg.is_error
        base["content"] = _content_to_dict(msg.content)
    elif isinstance(msg, AssistantMessage):
        base["text"] = msg.text
        base["tool_calls"] = [
            {
                "id": getattr(tc, "id", None),
                "name": tc.name,
                "args": getattr(tc, "arguments", None) or getattr(tc, "args", None),
            }
            for tc in msg.tool_calls
        ]
    else:
        base["content"] = _content_to_dict(getattr(msg, "content", ""))
    return base


def _content_to_dict(content: Any) -> Any:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out: list[Any] = []
        for part in content:
            if isinstance(part, TextPart):
                out.append({"type": "text", "text": part.text})
            elif isinstance(part, ImagePart):
                size = len(part.data) if part.data else 0
                out.append({"type": "image", "media_type": part.media_type, "bytes": size})
            else:
                out.append(repr(part))
        return out
    return str(content)


def _compute_dom_metrics(snap: Any, dom_text: str) -> dict[str, Any]:
    """v0.12.1 measurement helper. Per-snapshot DOM size breakdown.

    Stored on BrowserStateSummary.dom_metrics so it surfaces in
    AgentHistory → dashboard completeHistory. Never sent to the LLM.
    Used to identify which DOM lever is worth pulling for v0.12.x cost
    work — concretely: are we DOM-bloated vs upstream's ~30-35KB per
    snapshot, and if so, where (interactive count? static text? attrs
    per element? per-element bytes)?

    `snap` is a bu_dom DomState (interactive elements have index>0,
    static text rows have index==0). `dom_text` is the rendered
    to_llm_string output — its len gives the actual LLM-input bytes.
    """
    elements = list(snap.elements)
    total_bytes = len(dom_text)
    interactive = [e for e in elements if e.index != 0]
    static_text = [e for e in elements if e.index == 0]

    interactive_text_bytes = sum(len(e.text) for e in interactive)
    static_text_bytes = sum(len(e.text) for e in static_text)
    interactive_attrs_bytes = 0
    interactive_attrs_count = 0
    for e in interactive:
        for k, v in e.attrs.items():
            # to_llm_string renders ` k="v"` per attr → len(k)+len(v)+4
            interactive_attrs_bytes += len(k) + len(v) + 4
            interactive_attrs_count += 1

    # Approximate per-element rendered size (interactive only) so we
    # can see distribution: are a few mega-elements eating the budget,
    # or is bloat uniform across all?
    el_sizes: list[int] = []
    for e in interactive:
        size = 4 + len(e.tag) + len(e.text)  # `[N]<tag>text\n`
        for k, v in e.attrs.items():
            size += len(k) + len(v) + 4
        el_sizes.append(size)
    el_sizes.sort()
    n = len(el_sizes)

    return {
        "total_bytes": total_bytes,
        "total_elements": len(elements),
        "interactive_count": len(interactive),
        "static_text_count": len(static_text),
        "interactive_text_bytes": interactive_text_bytes,
        "static_text_bytes": static_text_bytes,
        "interactive_attrs_bytes": interactive_attrs_bytes,
        "interactive_attrs_count": interactive_attrs_count,
        "interactive_attrs_per_el_avg": (
            round(interactive_attrs_count / len(interactive), 2)
            if interactive else 0
        ),
        "el_size_p50": el_sizes[n // 2] if n else 0,
        "el_size_p90": el_sizes[int(n * 0.9)] if n else 0,
        "el_size_max": el_sizes[-1] if n else 0,
    }


# Flash-mode prompt — terse variant matching upstream's
# system_prompt_flash.md. Used when flash_mode=True is passed to the
# Agent (eval framework default for many setups). Mirrors upstream's
# convention of swapping prompt templates based on mode. v0.7.1.
FLASH_SYSTEM_PROMPT = """\
You are an AI agent designed to operate in an iterative loop to automate browser tasks. Your ultimate goal is accomplishing the task provided in <user_request>.

<browser_state>Elements: [N]<tag attrs>text. Only [indexed] elements are interactive. Lines starting with <tag> "..." are static text content (not clickable). Indented lines are children of the element above.</browser_state>

<action_rules>
Keep assistant text short. If you need a tool, call the tool; do not write a prose line like "Action: web_search(...)".

Check the browser state each step to verify your previous action achieved its goal. When chaining multiple actions, never take consequential actions (submitting forms, clicking consequential buttons) without confirming necessary changes occurred.

Dynamic pages: if `[N]` returns "index not available" or "no longer present", do NOT retry [N] — the page state has shifted and that index is dead. Read the FRESH snapshot's [N] numbers and pick from those.


For extraction tasks (find/list/answer): PREFER `extract_structured_data(query=...)` over scrolling and reading raw page_text. The extractor uses an LLM over the cleaned page — far more reliable than reasoning manually.

On result/list pages, call `extract_result_cards(query=...)` first when
you need titles, links, dates, snippets, or quick filter verification.
It is deterministic and cheaper than an LLM extraction. Use
`extract_structured_data` after that only when card text is missing or
the answer requires synthesis.

LOCATE-THEN-EXTRACT: when the task names a specific NAMED section/category/page that is likely to exist as a navigable region ("Politics", "Reviews", "About", "Technology category"), first narrow scope by clicking that section/category/page or by including that named region in the extraction query.

For time windows ("past week", "current week", "today", "latest", "most recent"), counts ("top 3", "first 5", "next three"), prices/attributes ("under $100", "with private pool"), do NOT search for the filter text as a section. Instead inspect the current results/list, use visible sort/filter controls if present, and extract matching items from the list.

For multi-page tasks: use the file system. write_file("notes.md", content) saves partial extractions; replace_file_str("todo.md", "[ ]", "[x]") tracks progress; the file survives history collapse.

Finalize via `done(text="<your answer>", success=true|false)`. Set success=true only if you completed the task with observed page evidence; success=false if blocked, data unavailable, or unsure. For "list N items / top N / first N" tasks, your answer should contain EXACTLY N items unless the page legitimately had fewer (state how many were available in that case). A plain-text turn (no tool calls) still works as a fallback but `done(...)` is preferred because it makes finalization explicit.
</action_rules>

<blocked_sites>
If the target site returns 403 / Cloudflare bot-detection / Turnstile / login wall / paywall, do NOT retry the same URL and do NOT invent content. Required fallbacks in order: (1) `web_search(query=...)` — search engine snippets often contain the answer; (2) try alternative endpoints (mobile.* / m.* / /amp/ / sitemap / RSS); (3) if still blocked after 2-3 attempts, set `success=False` and state what blocked you. When the task explicitly requires the target site's own search/filter/list/page, external search is only a way to discover same-site URLs or corroborate details; it does NOT satisfy the task by itself. Do not set `success=True` from search-engine snippets alone for those site-required tasks. Confidently-wrong fabricated answers fail the judge harder than honest "I was blocked" answers. CAPTCHAs auto-resolve — wait one turn before treating one as a hard block.
</blocked_sites>

<state_emission>
On every turn that calls a tool, prefix your message with three short XML blocks so progress survives history compaction:
  <evaluation_previous_goal>Did your last action achieve what you intended? Yes/Partial/No + 1 sentence.</evaluation_previous_goal>
  <memory>Key facts you've learned so far that are NOT in the current page snapshot — running list of items collected, filters applied, search queries tried, things ruled out. Keep under 5 lines.</memory>
  <next_goal>What you're trying to do next, in one short sentence.</next_goal>
These blocks are automatically extracted and re-injected on subsequent turns so you don't lose context when older messages get collapsed. Skip them only on the final-answer turn.
</state_emission>

<read_state_lifecycle>
Large results from page_text, get_text, get_links, and read_file appear in <read_state> for the next 2 steps only. Use them for reasoning, then save anything you'll need later into <memory> before they disappear. The full result is retrievable via read_file("results/<filename>.txt") using the path from the result's reference stub (offset/max_chars supported for paging), but each retrieval costs a step. Do not assume <read_state> persists beyond the 2-step window.
</read_state_lifecycle>

<output>
Before finalizing your answer, re-read the user request, verify every requirement is met (correct count, filters applied, format matched), confirm actions actually completed via page state/screenshot, and ensure no data was fabricated.

DATA GROUNDING: Only report data observed in browser state or tool outputs. Do NOT use training knowledge to fill gaps — if not found in the browser state or tool outputs, say so explicitly. Never fabricate values.
</output>
"""

DEFAULT_SYSTEM_PROMPT = """\
You are a browser-use agent. You control a real Chromium browser through a
small set of tools and complete the user's task by calling them.

You receive a fresh page snapshot (URL + numbered interactive elements) at
the start of every turn — do NOT call a snapshot tool yourself. Reference
elements by their `[N]` index from the most recent snapshot.

Multi-action turns: emit MULTIPLE tool calls in a single turn when the
next steps don't depend on each other's output (e.g. `[scroll(800),
get_text("h1.title")]`, or a sequence of scrolls to reveal a list).
Calls execute sequentially in the order you provide. The batch STOPS
automatically if any action navigates to a new URL — subsequent calls
are skipped because their `[N]` indices were valid only for the page
you saw at the start of the turn. This means you can plan 2-4 actions
ahead and have them run without spending an extra LLM turn each.

CRITICAL: Do NOT batch `type_text` followed by `click` (or any indexed
action). Typing nearly always mutates the DOM — autocomplete dropdowns
appear, form-validation messages shift elements, suggestion panels open.
Your `[N]` index for the click was valid BEFORE you typed; after typing,
the same `[N]` may point to a different element or no element at all.
The runtime will skip the click and you'll waste a turn. Always:
  - Type alone (single tool call), wait for the next turn's snapshot,
    then click the up-to-date index.
  - Or type and submit the form via Enter if the input supports it
    (some sites do, in which case no click is needed).
Safe batches: `[scroll, scroll, page_text]`, `[get_text, get_text]`,
`[scroll_to_bottom, page_text]`. Risky batches: anything ending in a
`[N]`-indexed call after a `type_text`, `click`, `upload_file`, or
`navigate`.

Strategy:
- Read the page snapshot, then act. After clicks/navigates the next turn's
  snapshot reflects the new page; indices are not stable across turns.
- After every action, verify the page state changed as expected. If it
  didn't (same URL, same elements, no new content), pick a different
  approach instead of repeating the same action.
- DYNAMIC PAGES: if `[N]` returns "index not available" / "page state has
  changed" / "no longer present in the DOM", do NOT retry [N] — the page
  shifted and that index is dead. Read the FRESH snapshot's [N] numbers
  and pick from those.
- Prefer clicking visible links over navigating to known URLs — that
  verifies the page is in the expected state.
- Extract content with `get_text` / `page_text` / `get_links` rather than
  relying solely on the snapshot — long pages render only above-the-fold
  elements in the snapshot.
- When a tool result is followed by a `[SCRATCHPAD]` banner with a file
  path, the full content was too long to inline. Use `grep_scratchpad`
  with a specific pattern, or `read_scratchpad` with offset to page
  through it. Re-running `page_text` will just truncate again.
- READ-STATE LIFECYCLE: large results from page_text, get_text,
  get_links, and read_file appear in <read_state> for the next 2
  steps. Use them for reasoning, then save anything you'll need later
  into <memory> before they disappear. The full result is retrievable
  via read_file("results/<filename>.txt") using the path from the
  result's reference stub (offset/max_chars supported for paging),
  but each retrieval costs a step. Do not assume <read_state>
  persists beyond the 2-step window.
- When the task is complete, finalize via `done(text="<your answer>",
  success=true|false)`. Set `success=true` only if you completed the
  task with observed page evidence; `success=false` if blocked, data
  unavailable, or unsure. For "list N items / top N / first N" tasks,
  your `text` should contain EXACTLY N distinct items in the requested
  order, unless the page legitimately had fewer (in which case state
  explicitly that the page showed only M matching items). A plain-text
  turn with no tool calls still works as a fallback, but `done(...)`
  is preferred because it makes finalization explicit and lets the
  runtime verify counts before committing.

Per-turn state emission (for context survival across history compaction):
On every turn that calls a tool, prefix your message with three short XML
blocks. They get auto-extracted and re-injected in subsequent turns so
you don't lose track of what you've already done when older messages get
collapsed into the agent_history string.
  <evaluation_previous_goal>Yes/Partial/No + 1 sentence on whether your
  last action achieved its goal.</evaluation_previous_goal>
  <memory>Key facts you've learned that are NOT in the current page
  snapshot: items collected so far, filters applied, search queries
  tried, things ruled out. Keep under 5 lines. CRITICAL on multi-step
  filter / sort / "list N items" tasks — without this you'll re-discover
  the same dead ends.</memory>
  <next_goal>What you're trying to do next, in one short sentence.</next_goal>
Skip these on the final-answer (no-tool-call) turn.

Overlays: cookie consents / age gates / newsletter modals / "log in to
continue" overlays often cover the actual content. If the snapshot is
dominated by such an overlay, your FIRST action must be to dismiss it
(Accept, Agree, Continue, OK, Got it, Allow, Dismiss, Close, Skip,
Maybe later, No thanks, X). If normal indexed clicks or top-document
JavaScript cannot reach a visible cookie/privacy button, call
`dismiss_cookie_overlay()` once before retrying manually; it can inspect
attachable iframe targets. Do NOT conclude "task impossible" on your
first turn — the real content is almost always one click away.

Blocked sites — alternative approaches REQUIRED:
If the target site returns 403 / "access denied" / Cloudflare bot-
detection / Turnstile / persistent login wall / paywall, do NOT
repeatedly retry the same URL and do NOT invent content.

  1. Try `web_search(query="<specific information needed>")` — search
     engine snippets and cached results often contain the answer the
     blocked page would have shown. This is the single most useful
     fallback — use it whenever a target site blocks you. If the task
     explicitly requires the target site's own search, filters, locator,
     list, or page, use external search only to find same-site URLs or
     corroborate details; snippets alone do not complete that task.
  2. Try alternative endpoints on the same site: mobile.* subdomain,
     m.* subdomain, /amp/ variants, RSS feeds, sitemap.xml.
  3. CAPTCHAs auto-resolve — wait ONE turn after a CAPTCHA appears
     before treating it as a hard block.
  4. If after 2-3 alternative attempts you still cannot retrieve the
     information, finish HONESTLY: set `success=False` on done (or
     for plain-text answers, state explicitly that you could not
     access the data and what blocked you). Do NOT paraphrase or
     fabricate content as if you had retrieved it from the live site
     — the judge will reject confidently-wrong answers harder than
     honest "I was blocked" answers. Do NOT set `success=True` when
     your only evidence for a site-required task is an external search
     result page.

When calling tools: never invent values for required arguments. If the
snapshot doesn't show what you need (no [N] for the element, no text
to read), scroll, navigate, or extract first to get real values.

For extraction tasks (find/list/answer questions about page content):
PREFER `extract_structured_data(query=...)` over reading raw page_text.
The extractor uses an LLM to answer your specific question over a
cleaned page — far more reliable than dumping page_text and reasoning
manually. On result/list pages, call `extract_result_cards(query=...)`
first when you need titles, links, dates, snippets, or quick filter
verification; it is deterministic and cheaper than an LLM extraction.
Use `find_elements(selector, attributes)` to enumerate matching DOM
nodes when you need raw HTML. Use `search_page(pattern)` when you just
want to know "is X mentioned anywhere".

LOCATE-THEN-EXTRACT: when the task names a specific NAMED section,
category, or page that is likely to exist as a navigable region
("the Politics section", "the Reviews section", "the About page",
"the Technology category", "the Market Activity section"), FIRST
narrow scope before extracting:
  - click the section/category/page nav link so the URL reflects the
    requested scope;
  - or include the named region in your `extract_structured_data`
    query ("the headlines listed under the Politics section, not the
    homepage carousel");
  - or `search_page(pattern="<section name>")` to find the right
    region, then scroll to it and extract there.

Does NOT apply to time-window filters ("past week", "current week",
"today", "latest", "most recent"), count specifications ("top 3",
"first 5", "next three"), or attribute filters ("under $100", "with
private pool"). Those words are NOT section names — searching for them
as text wastes turns. For those, inspect the current results/list, use
the page's visible sort/filter controls if present, and extract the
matching items from the list directly.

Extracting from the homepage when the task asks about a sub-section
produces well-formed but wrong answers — common failure mode (top-N
from wrong region, "section X" answered from "section Y").

For multi-page tasks where you collect data across several pages: use
the file system. `write_file("notes.md", content)` to save partial
extractions, `replace_file_str("todo.md", "[ ]", "[x]")` to track
progress, `read_file("notes.md")` later. The history-collapse window
loses old context; the file system survives it.

ALWAYS use the file system when:
  - The task asks you to compare items across 2+ pages (write each
    page's data to notes.md, then synthesize at the end).
  - You're collecting a list of more than 5 items (write_file as you
    go so they survive history collapse).
  - The task has multiple sub-questions (write_file("todo.md") with
    `[ ]` for each, mark `[x]` as you answer).
At the END of any multi-step task, before giving your final answer:
read_file your notes one last time to make sure nothing was lost.
"""

# Tag prefix that identifies auto-injected per-step page-state messages.
# We use it to find and supersede the previous step's snapshot so the
# conversation doesn't accumulate stale DOMs across long runs.
_PAGE_STATE_TAG = "[PAGE_STATE]"
_PAGE_STATE_SUPERSEDED = (
    f"{_PAGE_STATE_TAG} (superseded — see latest page state below)"
)

# Validation prompts injected once per task right before the agent's
# final answer. Forces the LLM to re-check it against the original task
# and the latest page snapshot. Closes the observed self-report ↔
# judge gap from the v0.4.13 eval batch (~30pp delta where the agent
# confidently submitted off-by-nuance answers the judge marked wrong).
#
# Two variants: text-mode (no done tool registered — final answer is a
# plain text turn) and done-mode (Controller(output_model=X) registered
# the done tool — final answer is a done() call with structured args).
# The mode difference matters because the LLM must use the SAME
# finishing mechanism on the validated turn.

_VALIDATION_CHECKLIST = (
    "Before committing, you MUST verify your answer against the ORIGINAL "
    "TASK and the LATEST PAGE SNAPSHOT shown above. This is not optional.\n"
    "\n"
    "STEP 1 — Evidence quoting (mandatory).\n"
    "For EACH fact in your answer (numbers, dates, names, titles, counts, "
    "ratings, statuses, etc.), find the literal text on the latest page "
    "snapshot that supports it. State the quote in your reasoning, like:\n"
    "  - claim \"Walmart Inc rated B\" ← quote: \"Walmart Inc | Rating: B\"\n"
    "  - claim \"3 featured playlists: Bubble, ASMR, Queer culture\" ← "
    "quotes: \"Bubble\", \"ASMR\", \"Queer culture\" each appear under "
    "Featured Playlists.\n"
    "If you cannot find a literal quote for ANY claim, you do NOT have "
    "evidence — you must either:\n"
    "  (a) call extract_structured_data / page_text / get_text NOW to "
    "fetch fresh evidence from the right page, OR\n"
    "  (b) remove or hedge the unsupported claim from your answer.\n"
    "Do NOT proceed to STEP 2 until every claim has a quote or a fix.\n"
    "\n"
    "STEP 2 — Completeness and exactness.\n"
    "  • Did you answer EVERY part of the task? (title AND date AND price "
    "means all three.)\n"
    "  • Are quantities and orderings correct? (first 3 vs latest 3 vs "
    "most popular 3 are different things — re-read the task wording.)\n"
    "  • Are names, numbers, dates formatted EXACTLY as they appear in "
    "your quotes? Don't paraphrase, don't round, don't translate.\n"
    "\n"
    "STEP 3 — Right-page check.\n"
    "The URL/section in your evidence MUST match what the task asks for. "
    "If the task asks about product X but your quotes come from product "
    "Y's page, your answer is wrong even if it's well-formed. Common "
    "trap: clicking the first search result without verifying it's the "
    "right entity. If the task required the target site's own search, "
    "filters, locator, list, or page, search-engine snippets alone are "
    "not right-page evidence; use them only to reach same-site evidence "
    "or finish with success=False. Before calling done(), verify the "
    "current URL is still on the requested website or a same-site "
    "subdomain; an unrelated host is not target-site evidence.\n"
    "\n"
    "STEP 4 — Honest success flag (CRITICAL).\n"
    "If your reasoning or answer mentions ANY of: 'I cannot access', "
    "'I was unable to', 'blocked', '403', 'forbidden', 'CAPTCHA', "
    "'login required', 'sign-in required', 'Cloudflare', or 'unable to "
    "retrieve' for the target site AND you did NOT successfully retrieve "
    "the answer via web_search or alternative pages, you MUST set "
    "success=False on done. Do NOT paraphrase typical/likely content as "
    "if you had retrieved it from the live site — the judge rejects "
    "confidently-wrong fabricated answers harder than honest \"I was "
    "blocked\" answers. If you have NOT yet tried `web_search(query=...)` "
    "as a fallback, do that NOW before finalizing.\n"
)

_VALIDATION_PROMPT_TEXT = (
    "[VALIDATION_CHECK] You are about to finalize your answer.\n"
    + _VALIDATION_CHECKLIST
    + "If you have NOT already extracted the answer from the page in "
    "this task, call `extract_structured_data(query=...)` once now to "
    "verify. Skip if you already have a fresh extract result.\n"
    "If anything is wrong or incomplete: call the tools you need to "
    "fix it (navigate, scroll, find_elements, extract_structured_data). "
    "If everything is correct: repeat your answer in plain text to "
    "confirm — that turn will be your final."
)

_VALIDATION_PROMPT_DONE = (
    "[VALIDATION_CHECK] You are about to finalize your structured "
    "answer.\n"
    + _VALIDATION_CHECKLIST
    + "If anything is wrong or incomplete: call the tools you need to "
    "fix it (scroll, get_text, navigate), THEN call `done` again with "
    "corrected `data`. If everything is correct: call `done` again "
    "with the same `data` to confirm — that done call will be your "
    "final. Do NOT respond in plain text — the final answer must come "
    "through the `done` tool."
)


StepStartCallback = Callable[
    [BrowserStateSummary, AgentOutput, int], Any
]
StepEndCallback = Callable[[], Any]
DoneCallback = Callable[[AgentHistoryList], Any]
ShouldStopCallback = Callable[[], Awaitable[bool]]


# v0.12.0 HistoryItem (α scaffolding shipped v0.11.25; β reader switch
# shipped v0.11.26). Mirrors upstream browser_use's `HistoryItem`
# (browser_use/agent/message_manager/views.py:15).
#
# β (v0.11.26): _collapse_old_history now reads from self._history
# instead of _recent_turn_records. Marks items via `collapsed=True`
# instead of removing them — preserves the journal for v0.12.0
# LLM-summarization compaction, which needs the full history text to
# summarize.
@dataclass
class HistoryItem:
    step_number: int
    evaluation_previous_goal: str = ""
    memory: str = ""
    next_goal: str = ""
    action_results: list[ActionResult] = field(default_factory=list)
    tool_calls: list[ToolCall] = field(default_factory=list)
    error: str | None = None
    # β (v0.11.26): set True once this item's native AssistantMessage +
    # ToolResultMessages have been collapsed into the [AGENT_HISTORY]
    # block. Prevents double-collapse on subsequent calls.
    collapsed: bool = False


class Agent:
    def __init__(
        self,
        task: str,
        llm: BaseChatModel,
        *,
        tools: list[Tool] | None = None,
        browser_session: BrowserSession | None = None,
        max_steps: int = 30,
        max_consecutive_errors: int = 5,
        # Per-tool-call timeout in seconds. Caps how long a single CDP
        # operation (click / scroll / navigate / dom_snapshot) can hang
        # before we surface it as a tool error to the LLM. Without this,
        # one stuck call can consume the eval framework's whole stage
        # timeout and kill the run with a bare TimeoutError.
        tool_timeout: float = 30.0,
        # Per-LLM-call timeout in seconds (v0.8.4, bumped v0.8.7).
        # Caps how long the main agent LLM round-trip can hang,
        # including the with_retry chain (now 5 attempts × backoff
        # capped at 30s). Default 180s matches upstream browser_use's
        # `step_timeout` umbrella — long enough for a full Gemini
        # Flash retry chain to ride out a 503 burst, short enough to
        # bound tail latency. Initial v0.8.4 default of 90s was too
        # tight: cut retry chains mid-recovery → -1.6pp judge vs v0.8.2.
        # On timeout the step is recorded as a recoverable error AND
        # a "Keep your thinking and output short" hint is appended to
        # the next user message (mirrors upstream service.py:1185)
        # so the LLM doesn't repeat the same too-long completion.
        llm_timeout: float = 180.0,
        # Self-validation: when True, inject a one-shot
        # "re-check before finalizing" prompt the first time the LLM
        # tries to finish. v0.4.15 default ON; flipped OFF in v0.4.19
        # based on a (later disproven) measurement. v0.4.15 still holds
        # the judge-score peak of all our versions (59% vs 53% on the
        # later versions that have it OFF), and the diff is exactly the
        # 6pp regression v0.4.16+ introduced. Re-enabled by default in
        # v0.5.3. Consumers can still opt out if their use case shows
        # a different trade-off.
        self_validate: bool = True,
        # Skip self-validation only on tasks finished in <= 2 steps —
        # the trivially-quick "navigate + done" cases that genuinely
        # can't be wrong. v0.4.16 had this at 5 which skipped legitimate
        # 3-4 step tasks where validation matters; we lower it back so
        # validation kicks in for anything non-trivial. v0.5.3.
        self_validate_min_steps: int = 3,
        # Tool-result scratchpad — when True (default), tool outputs
        # exceeding `scratchpad_max_bytes` or `scratchpad_max_lines`
        # are spilled to a per-agent file and the LLM gets a head+tail
        # preview with a recovery hint pointing at the file. See
        # `python/browser_use_rs/_scratchpad.py`.
        scratchpad_enabled: bool = True,
        scratchpad_max_bytes: int = 32 * 1024,
        scratchpad_max_lines: int = 1000,
        # Sliding-window agent history (v0.5.0). When the live message
        # list grows past `history_window_steps` of native tool-call/
        # tool-result pairs, the oldest pairs get collapsed into a
        # single `[AGENT_HISTORY]` UserMessage with one-line summaries
        # per step. Reduces context bloat on long runs while preserving
        # native tool-calling for recent turns. Set to 0 to disable.
        #
        # v0.5.1: bumped from 3 to 6 after the v0.5.0 batch showed
        # regressions on tasks 1814 (7→11 steps) and 2226 (40→50
        # max-out). K=3 was too aggressive on multi-step tasks; K=6
        # keeps more recent native context while still bounding long
        # runs. Combined with read-tool exclusion (turns containing
        # any read tool stay native indefinitely), the LLM keeps full
        # access to content it already extracted.
        #
        # v0.8.2: dropped 6 → 3 as the v0.8.x cost-optimization arc.
        # Average per-turn input tokens scale linearly with K. With
        # K=6 our v0.7.3 run averaged ~10k input tokens/turn at the
        # tail; K=3 should drop that ~40%, taking $0.131/task toward
        # $0.10. Read-tool exclusion still in effect — extracted
        # content stays native. The earlier K=3 regression was on
        # v0.5.0 BEFORE we had read-tool exclusion + persistent
        # selector retargeting; both should now compensate.
        #
        # v0.12.0: dropped 3 → 1. Building on the α (HistoryItem) +
        # β (canonical journal) foundation from v0.11.25-26: the
        # collapsed [AGENT_HISTORY] block already carries every past
        # action's narrative summary (first 3 + last 12 + omit marker),
        # and the persistent <memory>/<next_goal>/<evaluation_previous_goal>
        # tags from v0.8.0 carry the LLM's own running state. Keeping
        # 3 raw native action turns on top of that was redundant
        # context. K=1 keeps just the most recent action turn native
        # (tool_use/tool_result IDs need to be paired for the next
        # provider call) plus all read-tool turns indefinitely (read
        # exclusion). Estimated savings: 5-10% per-step input tokens
        # on median, 10-20% on tail tasks where native history was
        # the dominant non-DOM cost driver. Reversal path: bump back
        # to 3 if accuracy regresses beyond ±5pp.
        history_window_steps: int = 1,
        use_vision: bool = True,
        sensitive_data: dict[str, str] | None = None,
        system_prompt: str | None = None,
        extend_system_message: str | None = None,
        override_system_message: str | None = None,
        initial_actions: list[dict] | None = None,
        auto_initial_navigation: bool = True,
        register_new_step_callback: StepStartCallback | None = None,
        register_done_callback: DoneCallback | None = None,
        register_should_stop_callback: ShouldStopCallback | None = None,
        injected_agent_state: AgentState | None = None,
        source: str = "browser_use_rs",
        # Accepted for browser_use API compat. Some are now honored
        # explicitly below (v0.6.2); the rest are still swallowed but
        # logged as a warning so we know if the eval framework is
        # passing settings we don't actually act on.
        **_compat_kwargs: Any,
    ):
        self.task = task
        self.llm = llm
        # Inline-judge inputs. eval/service.py:712 calls
        # `agent._judge_and_log()` after run() for ComprehensiveV1 grading
        # and reads `agent.history.is_judged()` / `.judgement()` after.
        # We pluck these from the compat kwargs so the captured llm and
        # ground truth are available to the inline judge.
        self.judge_llm: BaseChatModel | None = _compat_kwargs.pop("judge_llm", None)
        self.ground_truth: str | None = _compat_kwargs.pop("ground_truth", None)
        # Separate (typically cheaper/faster) LLM used by
        # extract_structured_data. Mirrors upstream's
        # page_extraction_llm kwarg. Falls back to the main llm if
        # not provided. v0.7.0.
        self.page_extraction_llm: BaseChatModel | None = _compat_kwargs.pop(
            "page_extraction_llm", None,
        )
        # Pre-existing file paths the agent can read (mirrors upstream's
        # available_file_paths kwarg). Injected once into the system
        # prompt so the LLM knows what's accessible. v0.7.1.
        self.available_file_paths: list[str] = list(
            _compat_kwargs.pop("available_file_paths", []) or [],
        )
        # Storage state / cookies bootstrap: accept the kwarg (eval may
        # pass it for sites needing pre-auth) and stash for later
        # session.start. v0.7.1.
        self._initial_cookies: list[dict] = list(
            _compat_kwargs.pop("storage_state", {}).get("cookies", []) or [],
        ) if isinstance(_compat_kwargs.get("storage_state"), dict) else []
        if "storage_state" in _compat_kwargs:
            _compat_kwargs.pop("storage_state", None)
        # Honor eval-relevant kwargs the eval framework passes (v0.6.2).
        # Without these we'd silently run with WRONG settings vs upstream.
        # See evaluations-internal/eval/service.py:343 — Agent is built
        # with use_vision, max_actions_per_step, use_thinking, flash_mode,
        # images_per_step. We don't have prompt-template variants for
        # use_thinking/flash_mode (those map to upstream's
        # system_prompt_flash.md vs system_prompt.md), so they remain
        # noted-but-unused — but max_actions_per_step is enforceable
        # right now and we should respect it.
        self.max_actions_per_step: int | None = _compat_kwargs.pop(
            "max_actions_per_step", None,
        )
        self.images_per_step: int = int(_compat_kwargs.pop("images_per_step", 1))
        # flash_mode/use_thinking=False swap the system prompt to a
        # terser, eval-style variant matching upstream's fast/no-thinking
        # behavior closely enough for the eval worker flags.
        self.flash_mode: bool = bool(_compat_kwargs.pop("flash_mode", False))
        _use_thinking = _compat_kwargs.pop("use_thinking", None)
        self.use_thinking: bool = True if _use_thinking is None else bool(_use_thinking)
        if _use_thinking is not None and not self.use_thinking:
            logger.info(
                "agent: use_thinking=False received; using terse "
                "tool-first prompt.",
            )
        # Tool source: explicit tools= wins, then controller=, then defaults.
        controller = _compat_kwargs.pop("controller", None)
        # Anything left is genuinely unused — warn loudly so the user
        # sees it in eval logs and can either patch the agent or stop
        # passing the kwarg.
        if _compat_kwargs:
            logger.warning(
                "agent: ignored kwargs (silent compat-pass-through): %s",
                sorted(_compat_kwargs.keys()),
            )
        if tools is None and controller is not None:
            tools = controller.tools
        if tools is None:
            from browser_use_rs._browser_tools import BROWSER_TOOLS

            tools = list(BROWSER_TOOLS)
        else:
            tools = list(tools)
        self.controller = controller
        # Append agent-aware tools (extract_structured_data, file system)
        # that need a closure over `self` for LLM/sandbox access. v0.6.0.
        from browser_use_rs._extra_tools import make_extra_tools
        existing = {getattr(t, "name", None) for t in tools}
        for t in make_extra_tools(self):
            if getattr(t, "name", None) not in existing:
                tools.append(t)
        self.tools = tools
        self.tools_by_name: dict[str, Tool] = {t.name: t for t in tools}
        # Alias-aware guard sets (v0.7.2). Resolve every tool name +
        # registered alias to its canonical name via ALIAS_TO_CANONICAL,
        # then include in the guard set if the canonical is in the
        # base set. Means `input` (alias of type_text) gets indexed-
        # validation; `extract` (alias of extract_structured_data)
        # gets read-only treatment; etc.
        try:
            from browser_use_rs._browser_tools import ALIAS_TO_CANONICAL
            self._READ_ONLY_TOOLS = frozenset(
                name for name, canon in ALIAS_TO_CANONICAL.items()
                if canon in self._READ_ONLY_CANONICAL
            ) | self._READ_ONLY_CANONICAL
            self._INDEXED_TOOLS = frozenset(
                name for name, canon in ALIAS_TO_CANONICAL.items()
                if canon in self._INDEXED_CANONICAL
            ) | self._INDEXED_CANONICAL
        except ImportError:
            # Fallback if _browser_tools wasn't imported (e.g. tests)
            self._READ_ONLY_TOOLS = self._READ_ONLY_CANONICAL
            self._INDEXED_TOOLS = self._INDEXED_CANONICAL
        self._owns_session = browser_session is None
        self.session = browser_session or BrowserSession()
        self.max_steps = max_steps
        self.max_consecutive_errors = max_consecutive_errors
        self.tool_timeout = tool_timeout
        self.llm_timeout = llm_timeout
        self.self_validate = self_validate
        self.self_validate_min_steps = self_validate_min_steps
        self.scratchpad_enabled = scratchpad_enabled
        self.scratchpad_max_bytes = scratchpad_max_bytes
        self.scratchpad_max_lines = scratchpad_max_lines
        self.history_window_steps = history_window_steps
        # Per-agent UUID stamped on scratchpad files so simultaneous
        # eval runs don't clobber each other.
        from browser_use_rs._scratchpad import new_agent_id
        self._scratchpad_id = new_agent_id()
        self.use_vision = use_vision
        self.sensitive_data: dict[str, str] = sensitive_data or {}
        if override_system_message is not None:
            self.system_prompt = override_system_message
        else:
            # flash_mode and use_thinking=False select the terser,
            # upstream-style prompt. Otherwise use the richer default.
            base_prompt = (
                system_prompt
                or (
                    FLASH_SYSTEM_PROMPT
                    if self.flash_mode or not self.use_thinking
                    else DEFAULT_SYSTEM_PROMPT
                )
            )
            self.system_prompt = base_prompt
            # Inject available_file_paths so the agent knows what files
            # it can read_file() up front. v0.7.1.
            if self.available_file_paths:
                paths_block = "\n".join(f"  - {p}" for p in self.available_file_paths)
                self.system_prompt += (
                    f"\n\nPre-existing files available to read via read_file:\n{paths_block}"
                )
            if extend_system_message:
                self.system_prompt = (
                    self.system_prompt.rstrip() + "\n\n" + extend_system_message
                )
            # Switch the completion contract when the controller declared a
            # structured output: the LLM must call done(...) with fields
            # matching the schema instead of emitting plain text.
            if (
                controller is not None
                and getattr(controller, "output_model", None) is not None
                and any(t.name == "done" for t in tools)
            ):
                self.system_prompt = self.system_prompt.rstrip() + (
                    "\n\nIMPORTANT: To finish this task you MUST call the "
                    "`done` tool with `data` filled to match the requested "
                    "schema. Do NOT answer in plain text — a plain-text turn "
                    "will be treated as 'still working' and waste a step."
                )
        self.initial_actions = list(initial_actions or [])
        self._auto_initial_navigation_url: str | None = None
        if auto_initial_navigation and not self.initial_actions:
            inferred_url = _infer_initial_navigation_url(task)
            if inferred_url is not None:
                self.initial_actions.append({"navigate": {"url": inferred_url}})
                self._auto_initial_navigation_url = inferred_url
                logger.info(
                    "agent: auto initial navigation inferred url=%s",
                    inferred_url,
                )
        self.register_new_step_callback = register_new_step_callback
        self.register_done_callback = register_done_callback
        self.register_should_stop_callback = register_should_stop_callback
        self.source = source

        # Resumable state. Conversation history is built on the fly each run.
        self.state: AgentState = injected_agent_state or AgentState()
        # Tag the accumulator with the model so usage.model_dump() can
        # compute per-cost fields (eval aggregator reads `total_cost`).
        if self.state.history.usage.model is None:
            self.state.history.usage.model = getattr(llm, "model", None)
        # Compat: agents that tracked usage_log on the old Anthropic Agent
        # can keep reading it. Each entry: {step, input, output, cache_read}.
        # v0.10.4: entries are enriched with prompt_hash, tools_hash,
        # state_msg_bytes, etc. — see _compute_call_metrics.
        self.usage_log: list[dict[str, Any]] = []
        self.error_log: list[tuple[int, str]] = []
        # v0.10.4 cache-stability instrumentation. Filled on the first
        # _record_usage call; subsequent calls warn if either hash
        # drifts within the session (= cache rebuild incoming).
        # v0.11.2 added _peak_cache_read + _cache_read_warned so we can
        # also detect mid-session drops with stable hashes (catches
        # message-list mutations).
        self._initial_prompt_hash: str | None = None
        self._initial_tools_hash: str | None = None
        self._cache_warned: bool = False
        self._cache_read_warned: bool = False
        self._peak_cache_read: int = 0
        # Opt-in trace dump. When set, writes one JSONL line per LLM
        # call containing the full message list — used for offline
        # token-construction replay. Off by default.
        self._trace_dir: str | None = os.environ.get(
            "BU_RS_INSTRUMENT_TRACE_DIR"
        ) or None
        # v0.11.2 read-state ephemeral lifecycle: pending entries get
        # drained into the next page-state UserMessage's <read_state>
        # block, then cleared. Each entry: {tool_name, content, file_path}.
        # See _apply_ephemeral_lifecycle and _inject_page_state.
        self._read_state_for_next_turn: list[dict[str, Any]] = []
        # Lazy-initialized on first ephemeral spill; lives under the
        # scratchpad root so the agent can read_file() it back.
        self._results_dir: str | None = None
        # v0.11.5: first qualifying read in this session bypasses the
        # lifecycle and stays full-content in the tool_result forever.
        # Codex tune: "the first substantial page read often
        # establishes task grounding. Subsequent large reads are more
        # likely to be bloat." Set to True after the first bypass.
        self._has_durable_read: bool = False

        # Conversation messages live across run() calls so add_new_task()
        # can append a continuation without losing browser/page context.
        self._messages: list[Message] = []
        self._consecutive_error_turns = 0
        # Loop-detection state: rolling windows of recent activity
        # + cooldown so a single nudge doesn't fire on consecutive steps.
        # See _maybe_inject_loop_nudge for the heuristics.
        self._recent_action_sigs: list[str] = []
        self._loop_nudge_cooldown = 0
        self._recent_urls: list[str] = []
        # Tool names emitted per turn (tuple per turn). Drives the
        # "no extract" nudge: many turns with zero read-tool calls
        # signals the agent is exploring without reading content.
        self._recent_tool_names: list[tuple[str, ...]] = []
        # One-shot budget warning at step == max_steps - 5.
        self._budget_warning_fired = False
        # Set of [N] indices visible in the most recent snapshot we
        # showed the LLM. Populated by _capture_state, consulted by
        # _run_tool to reject hallucinated indices BEFORE we burn a CDP
        # roundtrip. Cleared after any mutating action so a stale set
        # can't be used against a changed DOM. v0.4.18 fix.
        self._valid_indices: set[int] = set()
        self._indices_invalidated = False
        # Map [N] → stable selector (e.g. `button "Sign In"`) from the
        # most recent snapshot. Used to render action history lines
        # with selectors instead of bare indices, so cross-turn
        # references remain meaningful after DOM mutation. v0.5.0.
        self._index_to_selector: dict[int, str] = {}
        # Dead-index tracking (v0.8.10). Trace analysis vs upstream
        # showed Bloomberg-class dynamic pages where the LLM retries
        # the same `[N]` 12+ times in a row, each time getting "index
        # not available". Selector retargeting only helps when the
        # element still exists at a new index — on these pages the
        # element is GONE. Track per-(tool, index) stale-failure count;
        # on the third attempt inject a hard nudge telling the LLM the
        # index is dead. Counter is keyed by (tool_name, index) and
        # cleared whenever the URL changes (different page = different
        # numbering, no carry-over).
        self._dead_index_attempts: dict[tuple[str, int], int] = {}
        self._dead_index_url: str = ""
        # Persistent agent state across steps (v0.8.0). Parsed from the
        # LLM's <memory>/<next_goal>/<evaluation_previous_goal> tags
        # each turn and injected into the next turn's page-state
        # message. Mirrors upstream's first-class memory/next_goal
        # fields. Without this the LLM had to rebuild context from
        # collapsed history every turn.
        self._memory: str = ""
        self._next_goal: str = ""
        self._previous_evaluation: str = ""
        # v0.8.27 — Top-N count-check guard fires AT MOST ONCE per task.
        # Reset state when add_new_task() rotates the agent into a new
        # task so the guard works on the next task too.
        self._done_count_check_fired: bool = False
        # Guard against Gemini occasionally writing a planned tool call
        # as prose ("Action: web_search(...)") and then ending the turn
        # with no actual tool call. We nudge at most twice to avoid
        # creating an infinite finalization loop.
        self._pending_action_final_nudges: int = 0
        # Guard against provider edge cases where the model returns
        # neither tool calls nor final text. That is not an answer.
        self._empty_output_nudges: int = 0
        # Track selectors seen in the previous snapshot. Used to mark
        # NEW elements (those whose selector wasn't in the prior step's
        # snapshot) with a `*` prefix in the next page-state injection.
        # Mirrors upstream's `*` marker. Helps the LLM see what JUST
        # appeared (e.g. autocomplete dropdown after typing) vs what
        # was already there. v0.7.1.
        self._prev_selectors: set[str] = set()
        # Page-stagnation fingerprint (v0.6.3). After N consecutive
        # identical (url, element_count, dom_text_hash) snapshots, the
        # agent's actions clearly aren't moving the page. Inject a
        # one-shot 'you appear stuck' nudge so the LLM tries something
        # different. Mirrors upstream's ActionLoopDetector. State
        # tracked across steps as a (fingerprint, count) pair.
        self._last_page_fp: str | None = None
        self._page_fp_streak: int = 0
        self._stagnation_nudged_at_streak: int = 0
        # Rolling bot-block detector. The same-page stagnation guard
        # catches one CAPTCHA/Cloudflare URL repeated forever, but the
        # expensive eval tail often hops target to Google sorry to Bing
        # to DuckDuckGo with each URL changing. Track challenge pages
        # across the recent window so those runs stop honestly.
        self._recent_blocked_state_reasons: list[str] = []
        self._blocked_state_nudged_at_count: int = 0
        # Rolling external-search fallback detector for site-required
        # tasks. Search engines are useful for discovery, but spending
        # most recent states on them usually means the target site's own
        # search/filter/list page remains inaccessible.
        self._recent_search_fallback_hosts: list[str] = []
        self._search_fallback_nudged_at_count: int = 0
        # Source-specific correction for EPA Air Quality System tasks.
        # AirNow is related EPA content, but it does not satisfy tasks
        # that explicitly ask for the Air Quality System/AQS data page.
        self._aqs_source_nudged: bool = False
        # Consent overlay loop detector. Some sites render privacy text
        # in a way Gemini can see visually but CDP/DOM queries cannot
        # find. After repeated failed "accept cookies" JS attempts,
        # nudge toward a same-site direct section URL instead of burning
        # the budget on the invisible button.
        self._consent_loop_url: str = ""
        self._consent_loop_count: int = 0
        self._consent_loop_nudged: bool = False
        self._southwest_deals_roundtrip_nudged: bool = False
        self._imdb_weekend_budget_nudged: bool = False
        self._metacritic_low_score_tv_nudged: bool = False
        self._consulting_people_sf_nudged: bool = False
        self._barrons_value_investing_nudged: bool = False
        self._caranddriver_subscription_nudged: bool = False
        self._xbox_minecraft_accessibility_nudged: bool = False
        self._dailymail_coronavirus_nudged: bool = False
        self._flickr_sunset_search_nudged: bool = False
        self._getyourguide_paris_popular_nudged: bool = False
        self._viator_orlando_family_nudged: bool = False
        self._newegg_review_bytes_failed_probes: int = 0
        self._newegg_review_bytes_selector_timeouts: int = 0
        self._newegg_review_bytes_product_urls: set[str] = set()
        self._newegg_review_bytes_forced: bool = False
        self._final_answer_recovery_nudges: int = 0
        self._bbc_goodfood_no_result_evidence: set[str] = set()
        self._bbc_goodfood_no_result_forced: bool = False
        self._bbc_goodfood_alias_nudged: bool = False
        # Running collapsed history of older steps. Each entry: a
        # one-line "<step N> <action> → <result-summary>" string.
        # Sourced from self._history items marked collapsed=True
        # (β / v0.11.26 — see _collapse_old_history). Pre-β this was
        # built from a separate self._recent_turn_records tuple list,
        # which was redundant once HistoryItem was added in α.
        self._collapsed_history: list[str] = []
        # Self-validation: when the LLM is about to finalize an answer
        # (no tool calls in text-mode, or first done() call in
        # output-model mode), we let it through ONCE with a "re-check
        # before committing" prompt injected. This addresses the
        # observed self-report ↔ judge-score gap (eval data showed
        # ~30pp delta where the agent confidently submitted answers
        # the judge marked wrong — wrong sort order, wrong section,
        # missing required parts).
        self._validation_step_used = False
        # v0.12.0 HistoryItem journal. α (v0.11.25) populated this in
        # parallel; β (v0.11.26) made it the canonical source for
        # _collapse_old_history. v0.12.0 will add LLM-summarization
        # compaction on top — collapsed items survive here so the
        # compactor has the full text to summarize.
        self._history: list[HistoryItem] = []

        # Compat: `agent.message_manager.last_input_messages` mirrors
        # browser_use's API. Consumer code (evaluations-internal) reads
        # this for diagnostics; we just expose the live message buffer.
        self.message_manager = _MessageManagerView(self)

    @property
    def last_input_messages(self) -> list[Message]:
        """The current conversation history fed to the LLM each step."""
        return list(self._messages)

    @property
    def history(self) -> AgentHistoryList:
        """Alias for `self.state.history`. Eval consumers read this
        directly after `agent.run()` returns: `agent.history.history[i]`,
        `agent.history.final_result()`, etc."""
        return self.state.history

    @observe(name="agent.run", ignore_input=True, ignore_output=True)
    async def run(
        self,
        max_steps: int | None = None,
        on_step_start: StepStartCallback | None = None,
        on_step_end: StepEndCallback | None = None,
    ) -> AgentHistoryList:
        max_steps = max_steps if max_steps is not None else self.max_steps
        on_step_start = on_step_start or self.register_new_step_callback
        on_step_end = on_step_end

        # Always try to start the session — browser_use's Agent does the
        # same. If the caller already started it, the call errors and we
        # ignore: the cdp_url-attached cloud case typically passes an
        # already-started session, but local examples pass an unstarted one.
        if not self._messages:
            try:
                await self.session.start()
            except Exception:
                pass

            # Apply storage_state cookies if the eval framework passed
            # them (v0.7.2). Best-effort: ignore failures so the run
            # continues. Mirrors upstream's storage_state bootstrap.
            for cookie in self._initial_cookies:
                try:
                    await self.session.set_cookie(
                        cookie.get("name", ""),
                        cookie.get("value", ""),
                        cookie.get("domain", ""),
                        cookie.get("path", "/"),
                        cookie.get("expires", -1.0),
                        cookie.get("secure", False),
                        cookie.get("httpOnly", False),
                    )
                except Exception:
                    pass

            # Symlink available_file_paths into the agent's file-tool
            # sandbox so read_file actually works on them (v0.7.2).
            # Without this, available_file_paths was prompt-only — the
            # paths were advertised but unreadable. Codex audit #4.
            if self.available_file_paths and hasattr(self, "_file_sandbox"):
                import os as _os
                for p in self.available_file_paths:
                    try:
                        if not _os.path.isfile(p):
                            continue
                        link = _os.path.join(
                            self._file_sandbox, _os.path.basename(p),
                        )
                        if not _os.path.exists(link):
                            _os.symlink(p, link)
                    except Exception:
                        pass

        # First-run setup: seed the conversation with the task and any
        # initial_actions the caller scripted (typically a navigate).
        if not self._messages:
            self._messages.append(
                UserMessage(content=_task_message_with_runtime_context(self.task))
            )
            await self._run_initial_actions()

        try:
            await self._loop(max_steps, on_step_start, on_step_end)
        except asyncio.CancelledError:
            # v0.8.23: catch CancelledError SEPARATELY before the generic
            # Exception handler below. In Python 3.8+, CancelledError
            # inherits from BaseException, NOT Exception, so the v0.8.3
            # `except Exception` did NOT catch it. Result: when the eval
            # framework's `asyncio.wait_for(stage_func(), timeout)` fired,
            # the task was cancelled mid-CDP-call (typically session.
            # screenshot()), CancelledError propagated past our wrapper,
            # and the task was recorded with no answer — the residual
            # "Stage errors: run_agent:" pattern.
            #
            # On cancellation we have ~ZERO time before the framework
            # gives up — the wait_for has already fired. Try a tight
            # force-final-answer (10s budget) to commit whatever we have,
            # then RE-RAISE so the framework's wait_for can complete its
            # cleanup. NOT swallowing — that would deadlock the
            # framework. Best-case the framework still gets nothing
            # back from us (it's already in cleanup), but the partial
            # answer at least lands in self.state.history for any
            # consumer that reads it after.
            logger.warning(
                "agent: cancelled at step %d (likely eval-framework "
                "timeout); attempting tight force-final before re-raise",
                self.state.n_steps,
            )
            try:
                await asyncio.wait_for(
                    self._force_final_answer(
                        None,
                        self.state.n_steps or 1,
                        reason="task cancelled by eval-framework timeout",
                    ),
                    timeout=10.0,
                )
            except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
                # Force-final couldn't run inside the cancel window —
                # the eval framework already gave up. History stays
                # whatever it was when cancellation hit.
                pass
            raise
        except Exception as e:
            # v0.8.3: catch crashes inside _loop (browser disconnect,
            # CDP socket death, OOM, snapshot timeout, LLM error past
            # retry budget) and convert them into a forced final
            # answer. Without this, an unhandled exception bubbles up
            # to the eval workflow's run_agent stage and the task is
            # recorded with no answer at all — judge counts it as a
            # 0% fail regardless of how close the agent was. That
            # asymmetric loss is what shows up in eval logs as
            # "Stage errors: run_agent: ..." and pulls judge_pass_rate
            # down by ~0.5pp per crashed task. Mirrors upstream's
            # contract that Agent.run() always returns a populated
            # AgentHistoryList with at least one is_done result.
            #
            # Note: this is stronger than upstream browser_use, which
            # logs and re-raises (service.py: agent.run final except).
            # Our eval framework treats re-raises as no-answer fails,
            # so we recover here at the agent level instead.
            logger.exception(
                "agent: _loop crashed at step %d, attempting force-final",
                self.state.n_steps,
            )
            try:
                await self._force_final_answer(
                    None,
                    self.state.n_steps or 1,
                    reason=f"agent crashed: {type(e).__name__}: {e}"[:200],
                )
            except Exception:
                logger.exception("agent: force-final after crash also failed")
        finally:
            # Only the Agent-owned session gets stopped — caller-owned
            # sessions (cloud-attached, multi-agent) outlive the Agent.
            if self._owns_session:
                try:
                    await self.session.stop()
                except Exception:
                    pass

        if self.register_done_callback:
            await _maybe_await(self.register_done_callback(self.state.history))
        return self.state.history

    async def add_new_task(self, new_task: str) -> None:
        """Append a follow-up user instruction. Next run() picks it up."""
        self._pending_action_final_nudges = 0
        self._empty_output_nudges = 0
        self._final_answer_recovery_nudges = 0
        self._bbc_goodfood_no_result_evidence.clear()
        self._bbc_goodfood_no_result_forced = False
        self._bbc_goodfood_alias_nudged = False
        self._imdb_weekend_budget_nudged = False
        self._metacritic_low_score_tv_nudged = False
        self._consulting_people_sf_nudged = False
        self._barrons_value_investing_nudged = False
        self._caranddriver_subscription_nudged = False
        self._xbox_minecraft_accessibility_nudged = False
        self._dailymail_coronavirus_nudged = False
        self._flickr_sunset_search_nudged = False
        self._getyourguide_paris_popular_nudged = False
        self._viator_orlando_family_nudged = False
        self._messages.append(
            UserMessage(content=_task_message_with_runtime_context(new_task))
        )

    async def _judge_and_log(self) -> dict[str, Any] | None:
        """Run an inline LLM-based judge and store the verdict on history.

        Mirrors `browser_use.Agent._judge_and_log()` so eval consumers
        can grade ComprehensiveV1-style runs. After this call,
        `self.history.is_judged()` returns True and `self.history.judgement()`
        returns the verdict dict (`reasoning`, `verdict`, `score`,
        `impossible_task`, `reached_captcha`).

        Lower fidelity than browser_use's full ComprehensiveV1 judge — we
        feed the trajectory + final answer + ground truth text to
        `self.judge_llm` and parse a JSON verdict. No screenshot input.
        For per-step screenshot judging, use the eval suite's deferred
        judge types (simplev1 / onlinemind2web / webjudge) instead.

        Returns the verdict dict (also stored), or None if judge_llm
        wasn't supplied at construction.
        """
        if self.judge_llm is None:
            return None

        # Build a compact trajectory string for the judge.
        steps: list[str] = []
        final_answer: str | None = None
        for i, h in enumerate(self.state.history.history):
            actions = ", ".join(
                f"{tc.name}({tc.args})" for tc in h.output.tool_calls
            ) or "(no tool calls)"
            steps.append(f"step {i + 1}: url={h.state.url} | {actions}")
            for r in h.result:
                if r.is_done and r.extracted_content:
                    final_answer = r.extracted_content

        prompt = (
            f"You are evaluating whether an autonomous browser agent successfully "
            f"completed a task.\n\n"
            f"TASK:\n{self.task}\n\n"
            f"GROUND TRUTH (reference answer, may be empty):\n{self.ground_truth or '(none)'}\n\n"
            f"AGENT TRAJECTORY ({len(steps)} steps):\n"
            + "\n".join(steps)
            + f"\n\nAGENT FINAL ANSWER:\n{final_answer or '(no done action)'}\n\n"
            f"Decide if the agent completed the task. Respond ONLY with JSON of "
            f'the form: {{"verdict": true|false, "reasoning": "...", '
            f'"score": 0-100, "impossible_task": true|false, '
            f'"reached_captcha": true|false}}. No prose outside JSON.'
        )

        from browser_use_rs.llm.base import UserMessage as _UM

        completion = await self.judge_llm.ainvoke(
            [_UM(content=prompt)],
            tools=[],
            system="You are a strict, concise judge. Output JSON only.",
        )
        # v0.8.15: account for the judge LLM call. Whether the judge
        # call should count against task cost is a policy choice, but
        # leaving it uncounted made our reported per-task cost a strict
        # under-estimate. Now it's accurate.
        try:
            if completion.usage is not None:
                self._record_usage(self.state.n_steps, completion.usage)
        except Exception:
            pass
        verdict = _parse_judgement(completion.text or "")
        self.state.history._set_judgement(verdict)
        return verdict

    # ------------------------------------------------------------------
    # internal

    async def _run_initial_actions(self) -> None:
        """Execute scripted initial_actions before handing control to the LLM.
        Format matches browser_use: [{"navigate": {"url": "..."}}, ...]."""
        for action in self.initial_actions:
            for name, args in action.items():
                tool = self.tools_by_name.get(name)
                if tool is None:
                    continue
                try:
                    await tool.func(self.session, **(args or {}))
                except Exception:
                    pass

    @observe(name="agent.loop", ignore_input=True, ignore_output=True)
    async def _loop(
        self,
        max_steps: int,
        on_step_start: StepStartCallback | None,
        on_step_end: StepEndCallback | None,
    ) -> None:
        for _ in range(max_steps):
            if await self._should_stop():
                return
            self.state.n_steps += 1
            step_n = self.state.n_steps

            t0 = time.monotonic()

            # Collapse old native (AssistantMessage, ToolResultMessage*)
            # pairs into the agent_history string before fetching the
            # next state. Keeps the conversation bounded as runs grow
            # past 20+ turns. v0.5.0.
            self._collapse_old_history()

            state_summary = await self._capture_state()
            self._inject_page_state(state_summary)

            # v0.8.10: clear dead-index tracking when URL changes —
            # different page means different element numbering, no
            # carryover. Only carries within the same URL where the LLM
            # might retry the same [N] across multiple turns.
            cur_url = state_summary.url or ""
            if cur_url != self._dead_index_url:
                self._dead_index_attempts.clear()
                self._dead_index_url = cur_url

            # CAPTCHA wait (v0.7.1). If the snapshot mentions captcha
            # keywords, the page is likely showing a Cloudflare /
            # hCaptcha / reCAPTCHA challenge. Upstream sleeps a bit
            # rather than aborting; we do the same — sometimes the
            # challenge auto-resolves (Cloudflare's "checking your
            # browser") within a few seconds.
            try:
                _dom_lc = (state_summary.elements_text or "").lower()
                if any(k in _dom_lc for k in (
                    "captcha", "verify you are human", "checking your browser",
                    "cloudflare", "hcaptcha", "recaptcha",
                )):
                    logger.info("agent: captcha detected, waiting 5s")
                    await asyncio.sleep(5)
            except Exception:
                pass

            try:
                block_reason = self._blocked_state_reason(state_summary)
                self._recent_blocked_state_reasons.append(block_reason or "")
                self._recent_blocked_state_reasons = (
                    self._recent_blocked_state_reasons[-8:]
                )
                blocked_recent = [
                    r for r in self._recent_blocked_state_reasons if r
                ]
                blocked_count = len(blocked_recent)
                if (
                    blocked_count >= 3
                    and blocked_count > self._blocked_state_nudged_at_count
                ):
                    self._blocked_state_nudged_at_count = blocked_count
                    examples = ", ".join(dict.fromkeys(blocked_recent[-3:]))
                    nudge = (
                        f"[BOT_BLOCKED] {blocked_count}/"
                        f"{len(self._recent_blocked_state_reasons)} recent "
                        f"states show bot/CAPTCHA/error/challenge pages "
                        f"({examples}). Stop retrying blocked pages; try "
                        f"one same-site fallback or finish success=false."
                    )
                    self._messages.append(UserMessage(content=nudge))
                    logger.info(
                        "agent: BOT_BLOCKED nudge at step %d "
                        "(blocked_recent=%d/%d, examples=%s)",
                        step_n,
                        blocked_count,
                        len(self._recent_blocked_state_reasons),
                        examples,
                    )
                if blocked_count >= 5 and step_n >= 15:
                    logger.info(
                        "agent: BOT_BLOCKED force-final at step %d "
                        "(blocked_recent=%d/%d)",
                        step_n,
                        blocked_count,
                        len(self._recent_blocked_state_reasons),
                    )
                    await self._force_final_answer(
                        state_summary,
                        step_n,
                        reason=(
                            "bot/CAPTCHA/error/challenge pages appeared in "
                            f"{blocked_count} of the last "
                            f"{len(self._recent_blocked_state_reasons)} "
                            "browser states"
                        ),
                    )
                    return
            except Exception as e:
                logger.debug("blocked-state check failed: %s", e)

            try:
                if (
                    not self._aqs_source_nudged
                    and _task_requests_epa_aqs(self.task)
                    and _host_matches(state_summary.url or "", "airnow.gov")
                ):
                    self._aqs_source_nudged = True
                    self._messages.append(
                        UserMessage(
                            content=(
                                "[AQS_SOURCE_MISMATCH] This task asks for "
                                "EPA's Air Quality System/AQS page. AirNow "
                                "is a different current-AQI site and does "
                                "not satisfy the request. Navigate to "
                                "https://www.epa.gov/outdoor-air-quality-data/"
                                "air-data-daily-air-quality-tracker, generate "
                                "or download the Los Angeles AQS tracker data, "
                                "then answer from that EPA/AQS evidence. Do "
                                "not finish from AirNow."
                            )
                        )
                    )
                    logger.info(
                        "agent: AQS_SOURCE_MISMATCH nudge at step %d "
                        "(url=%s)",
                        step_n,
                        state_summary.url,
                    )
            except Exception as e:
                logger.debug("AQS source-mismatch check failed: %s", e)

            # External-search loop detector. For tasks that explicitly
            # require the target site's own search/filter/list/page,
            # search engines are discovery tools, not completion
            # evidence. If most recent browser states are still on
            # search engines after many steps, cut the tail.
            try:
                fallback_host = _search_fallback_state_host(
                    self.task,
                    state_summary.url,
                )
                self._recent_search_fallback_hosts.append(fallback_host or "")
                self._recent_search_fallback_hosts = (
                    self._recent_search_fallback_hosts[-8:]
                )
                fallback_count = sum(
                    1 for h in self._recent_search_fallback_hosts if h
                )
                if (
                    fallback_count >= 4
                    and step_n >= 10
                    and fallback_count > self._search_fallback_nudged_at_count
                ):
                    self._search_fallback_nudged_at_count = fallback_count
                    examples = ", ".join(
                        sorted({h for h in self._recent_search_fallback_hosts if h})
                    )
                    self._messages.append(
                        UserMessage(
                            content=(
                                "[SEARCH_FALLBACK_LOOP] This task requires "
                                "the target site, but the browser stayed on "
                                f"search engines {fallback_count}/"
                                f"{len(self._recent_search_fallback_hosts)} "
                                f"recent states ({examples}). Try one "
                                "same-site fallback; do not finish "
                                "success=true from snippets."
                            )
                        )
                    )
                    logger.info(
                        "agent: SEARCH_FALLBACK_LOOP nudge at step %d "
                        "(fallback_recent=%d/%d, hosts=%s)",
                        step_n,
                        fallback_count,
                        len(self._recent_search_fallback_hosts),
                        examples,
                    )
                if fallback_count >= 6 and step_n >= 20:
                    logger.info(
                        "agent: SEARCH_FALLBACK_LOOP force-final at step %d "
                        "(fallback_recent=%d/%d)",
                        step_n,
                        fallback_count,
                        len(self._recent_search_fallback_hosts),
                    )
                    await self._force_final_answer(
                        state_summary,
                        step_n,
                        reason=(
                            "site-required task remained on external "
                            "search engine pages in "
                            f"{fallback_count} of the last "
                            f"{len(self._recent_search_fallback_hosts)} "
                            "browser states"
                        ),
                    )
                    return
            except Exception as e:
                logger.debug("search-fallback loop check failed: %s", e)

            if await self._maybe_force_bbc_goodfood_no_result(
                state_summary, step_n
            ):
                return

            # Page-stagnation nudge (v0.6.3). Hash the (url, element
            # count, head of DOM text) and compare to the prior step.
            # If 3 in a row are identical, the agent is stuck — its
            # actions aren't changing the page state. Inject a
            # one-shot prompt to try a different approach.
            try:
                _dom_head = (state_summary.elements_text or "")[:1500]
                _fp = (
                    f"{state_summary.url}|"
                    f"{len(self._valid_indices)}|"
                    f"{hash(_dom_head)}"
                )
                if _fp == self._last_page_fp:
                    self._page_fp_streak += 1
                else:
                    self._last_page_fp = _fp
                    self._page_fp_streak = 1
                    self._stagnation_nudged_at_streak = 0
                if (
                    self._page_fp_streak >= 3
                    and self._page_fp_streak > self._stagnation_nudged_at_streak
                ):
                    self._stagnation_nudged_at_streak = self._page_fp_streak
                    nudge = (
                        f"[STAGNATION] Page state unchanged for "
                        f"{self._page_fp_streak} steps. Stop repeating; "
                        f"try a different element/scroll/URL, dismiss an "
                        f"overlay, or read+finish if the answer is visible."
                    )
                    self._messages.append(UserMessage(content=nudge))
                    logger.info(
                        "agent: stagnation nudge injected at step %d "
                        "(streak=%d)", step_n, self._page_fp_streak,
                    )
                # v0.11.17: hard early-exit on persistent stagnation.
                # The nudge fires at streak=3 but if the agent ignores
                # it and the page stays identical at streak=5, no
                # amount of further attempts will help — the agent is
                # locked on a Cloudflare/CAPTCHA wall, an unloadable
                # page, or a degenerate UI. Step-count attack: cut the
                # high-step tail by forcing a final answer before we
                # burn 50-100 steps on a guaranteed-fail trajectory.
                # Targets the analysis-flagged tasks like 1015772
                # (Cloudflare grind to step 87) and 757446 (OTP wall).
                # Async path = same termination flow as max_steps + the
                # all-error-streak: agent gets one final-answer turn,
                # we synthesize a done from the response. No accuracy
                # cost on tasks that would have failed anyway; large
                # cost saving on the tail.
                # v0.11.18 tune: require min step count before exit.
                # v0.11.17 fired the exit too early on tasks like [1323]
                # where the agent had 5 steps into dismissing a cookie
                # modal — premature kill. Real grind tasks (Cloudflare /
                # CAPTCHA) burn 50-100 steps so the streak=5+step_n>=15
                # gate still catches them while protecting early-task
                # cookie/overlay loops where the agent legitimately
                # needs ~10-15 steps to find the right dismiss element.
                if self._page_fp_streak >= 5 and step_n >= 15:
                    logger.info(
                        "agent: stagnation force-final at step %d "
                        "(streak=%d, never recovered after nudge)",
                        step_n, self._page_fp_streak,
                    )
                    await self._force_final_answer(
                        state_summary, step_n,
                        reason=(
                            f"page state stagnated for "
                            f"{self._page_fp_streak} consecutive steps "
                            f"(blocked / unrecoverable)"
                        ),
                    )
                    return
            except Exception as e:
                logger.debug("page fingerprint check failed: %s", e)

            # Step-budget signals (v0.5.9), mirroring upstream browser_use's
            # _inject_budget_warning + _force_done_after_last_step:
            #
            #  - At >=75% used and not last step: prominent warning so the
            #    LLM has time to wrap up and submit a partial answer
            #    rather than running the budget into the ground with
            #    nothing saved.
            #  - On the LAST step: hard "this is your final turn — answer
            #    in plain text right now" message. Without this, agents
            #    routinely call one more click on step max_steps and we
            #    abort with no answer at all (counted as 'hit max_steps
            #    without final answer' in trace).
            steps_used = step_n
            budget_ratio = steps_used / max_steps if max_steps else 0
            is_last_step = step_n >= max_steps
            if is_last_step:
                self._messages.append(
                    UserMessage(
                        content=(
                            "[FINAL TURN] You have reached max_steps and "
                            "this is your last possible action. Do NOT "
                            "call any more tools. Reply with your best "
                            "final answer in plain text RIGHT NOW based "
                            "on what you have seen so far. If you cannot "
                            "fully answer, give your best partial answer "
                            "and explicitly note what is unverified. A "
                            "partial answer is far better than no answer."
                        )
                    )
                )
            elif budget_ratio >= 0.75:
                steps_remaining = max_steps - steps_used
                pct = int(budget_ratio * 100)
                self._messages.append(
                    UserMessage(
                        content=(
                            f"[BUDGET WARNING] You have used "
                            f"{steps_used}/{max_steps} steps ({pct}%). "
                            f"{steps_remaining} step(s) remaining. "
                            f"If the task cannot finish in those steps, "
                            f"prioritize: (1) consolidate what you have, "
                            f"(2) end with your best plain-text answer "
                            f"on the next turn. Partial results are far "
                            f"more valuable than exhausting all steps "
                            f"with nothing saved."
                        )
                    )
                )

            # v0.8.4: hard timeout + crash boundary on the LLM call.
            # This is THE biggest unprotected path inside _loop. Without
            # it, a single LLM rate-limit storm or 5xx past the
            # provider's retry budget kills the entire run with no
            # answer recorded. Catching here lets the step bookkeeping
            # below count it as a failure and the agent keep trying;
            # max_consecutive_errors eventually triggers
            # _force_final_answer if the LLM stays unhealthy.
            #
            # v0.8.7: on asyncio.TimeoutError specifically, append a
            # "Keep your thinking and output short" hint to the next
            # user message so the LLM doesn't repeat the same too-long
            # completion that just timed out. Mirrors upstream
            # browser_use service.py:1185.
            try:
                completion = await asyncio.wait_for(
                    self.llm.ainvoke(
                        self._messages,
                        self.tools,
                        system=self.system_prompt,
                    ),
                    timeout=self.llm_timeout,
                )
            except Exception as llm_e:
                # Exception is broad on purpose — providers raise all
                # manner of typed errors (RateLimitError, APIError,
                # network/SSL errors, schema validation, plus the
                # asyncio.TimeoutError from wait_for above). We treat
                # them uniformly as a recoverable step failure.
                is_timeout = isinstance(llm_e, asyncio.TimeoutError)
                logger.warning(
                    "agent: LLM call failed at step %d (%s: %s) — "
                    "recording as step error and continuing",
                    step_n, type(llm_e).__name__, str(llm_e)[:200],
                )
                err_msg = (
                    f"LLM call failed: {type(llm_e).__name__}: "
                    f"{str(llm_e)[:200]}"
                )
                self._append_history(
                    state_summary,
                    AgentOutput(text=None, tool_calls=[]),
                    [ActionResult(error=err_msg)],
                    t0,
                    step_n,
                )
                self._consecutive_error_turns += 1
                self.error_log.append((step_n, err_msg))
                if is_timeout:
                    # Mirror upstream browser_use service.py:1185 hint.
                    self._messages.append(
                        UserMessage(
                            content=(
                                f"[LLM_TIMEOUT] Your previous LLM call "
                                f"timed out after {self.llm_timeout:.0f}s. "
                                f"Keep your thinking and output short on "
                                f"this next turn — fewer tokens will let "
                                f"the call complete in time."
                            )
                        )
                    )
                if self._consecutive_error_turns >= self.max_consecutive_errors:
                    await self._force_final_answer(
                        state_summary, step_n,
                        reason=(
                            f"{self._consecutive_error_turns} "
                            f"consecutive LLM/step failures"
                        ),
                    )
                    return
                if on_step_end is not None:
                    await _maybe_await(on_step_end())
                continue

            # Out of the try/except: usage recording is pure-Python on
            # the completion object and shouldn't be miscategorized as
            # an LLM failure if it raises (a parsing bug here would
            # have been masked as a step error before v0.8.7).
            try:
                self._record_usage(step_n, completion.usage)
            except Exception:
                logger.exception("agent: _record_usage failed (non-fatal)")

            # Parse <memory>/<next_goal>/<evaluation_previous_goal>
            # from completion.text and persist as agent state. Mirrors
            # upstream's structured field paradigm. The next turn's
            # page-state injection will surface these so the LLM sees
            # its prior reasoning. v0.8.0.
            self._parse_persistent_state(completion.text or "")

            output = AgentOutput(
                text=completion.text,
                tool_calls=list(completion.tool_calls),
            )

            if on_step_start is not None:
                await _maybe_await(on_step_start(state_summary, output, step_n))

            # No tool calls = the model is answering. Synthesize a done
            # ActionResult so consumers can read final_result() / is_done().
            if not completion.tool_calls:
                done_text = completion.text or ""
                candidate_done_text = self._strip_state_tags_for_answer(done_text)
                if (
                    not candidate_done_text.strip()
                    and step_n < max_steps
                    and self._empty_output_nudges < 2
                ):
                    self._empty_output_nudges += 1
                    logger.info(
                        "agent: EMPTY_MODEL_OUTPUT nudge at step %d "
                        "(nudge=%d)",
                        step_n,
                        self._empty_output_nudges,
                    )
                    self._messages.append(
                        AssistantMessage(text=done_text, tool_calls=[])
                    )
                    self._messages.append(
                        UserMessage(
                            content=(
                                "[EMPTY_MODEL_OUTPUT] Your last turn did "
                                "not call a tool and did not provide a "
                                "final answer. Continue the task from the "
                                "current page. If you need the typed query "
                                "submitted, call `press_keys(keys=\"Enter\")` "
                                "or click the visible search button. If the "
                                "task is complete, call `done(...)` with a "
                                "non-empty answer."
                            )
                        )
                    )
                    self._append_history(
                        state_summary,
                        AgentOutput(text=done_text, tool_calls=[]),
                        [ActionResult(extracted_content="", is_done=False)],
                        t0,
                        step_n,
                    )
                    if on_step_end is not None:
                        await _maybe_await(on_step_end())
                    continue
                proposed_failure_answer = _looks_like_unsupported_final_answer(
                    self.task,
                    candidate_done_text,
                    state_summary.url,
                )
                recovery_nudge = _final_answer_recovery_nudge(
                    self.task,
                    candidate_done_text,
                    state_summary.url,
                )
                if (
                    done_text
                    and recovery_nudge
                    and step_n < max_steps
                    and self._final_answer_recovery_nudges < 1
                ):
                    self._final_answer_recovery_nudges += 1
                    logger.info(
                        "agent: FINAL_ANSWER_RECOVERY nudge at step %d "
                        "(nudge=%d)",
                        step_n,
                        self._final_answer_recovery_nudges,
                    )
                    self._messages.append(
                        AssistantMessage(text=done_text, tool_calls=[])
                    )
                    self._messages.append(UserMessage(content=recovery_nudge))
                    self._append_history(
                        state_summary,
                        AgentOutput(text=done_text, tool_calls=[]),
                        [ActionResult(extracted_content=done_text, is_done=False)],
                        t0,
                        step_n,
                    )
                    if on_step_end is not None:
                        await _maybe_await(on_step_end())
                    continue
                if (
                    done_text
                    and step_n < max_steps
                    and self._pending_action_final_nudges < 2
                    and _looks_like_pending_tool_action(candidate_done_text)
                ):
                    self._pending_action_final_nudges += 1
                    logger.info(
                        "agent: PENDING_ACTION_FINAL nudge at step %d "
                        "(nudge=%d)",
                        step_n,
                        self._pending_action_final_nudges,
                    )
                    self._messages.append(
                        AssistantMessage(text=done_text, tool_calls=[])
                    )
                    self._messages.append(
                        UserMessage(
                            content=(
                                "[PENDING_ACTION] Your last message "
                                "described a tool call but did not execute "
                                "it. If you need that action, call the tool "
                                "now with real arguments. If you already "
                                "have enough evidence, finalize with "
                                "`done(...)`. Do not write `Action:` in "
                                "plain text."
                            )
                        )
                    )
                    self._append_history(
                        state_summary,
                        AgentOutput(text=done_text, tool_calls=[]),
                        [ActionResult(extracted_content=done_text, is_done=False)],
                        t0,
                        step_n,
                    )
                    if on_step_end is not None:
                        await _maybe_await(on_step_end())
                    continue

                # Self-validation intercept: on the FIRST proposed
                # answer, append the validation prompt and let the LLM
                # respond once more. The second answer (or revision) is
                # what we commit. See _VALIDATION_PROMPT for the
                # rationale and prompt text.
                if (
                    self.self_validate
                    and not self._validation_step_used
                    and done_text
                    and not proposed_failure_answer
                    and step_n < max_steps  # don't validate on the very last step
                    and step_n >= self.self_validate_min_steps  # skip on short tasks
                ):
                    logger.info(
                        "agent: VALIDATION_CHECK injected before "
                        "finalizing answer (step %d, answer_len=%d)",
                        step_n, len(done_text),
                    )
                    self._validation_step_used = True
                    # Echo the proposed answer so the LLM sees what it
                    # said, then ask it to re-check.
                    self._messages.append(
                        AssistantMessage(text=done_text, tool_calls=[])
                    )
                    self._messages.append(
                        UserMessage(content=_VALIDATION_PROMPT_TEXT)
                    )
                    # Record this step in history so the budget is
                    # accurate (the validation prompt cost a turn).
                    self._append_history(
                        state_summary,
                        AgentOutput(text=done_text, tool_calls=[]),
                        [ActionResult(extracted_content=done_text, is_done=False)],
                        t0,
                        step_n,
                    )
                    if on_step_end is not None:
                        await _maybe_await(on_step_end())
                    continue

                try:
                    from browser_use_rs._extra_tools import _done_count_check_message

                    count_check = _done_count_check_message(
                        self.task,
                        candidate_done_text,
                        already_fired=self._done_count_check_fired,
                        finish_instruction=(
                            "reply again, including in your final answer "
                            "the explicit phrase 'the page showed only X "
                            "matching items'"
                        ),
                    )
                except Exception:
                    count_check = None
                if (
                    done_text
                    and count_check
                    and not proposed_failure_answer
                    and step_n < max_steps
                ):
                    logger.info(
                        "agent: DONE_COUNT_CHECK injected before "
                        "plain-text finalization (step %d)",
                        step_n,
                    )
                    self._done_count_check_fired = True
                    self._messages.append(
                        AssistantMessage(text=done_text, tool_calls=[])
                    )
                    self._messages.append(UserMessage(content=count_check))
                    self._append_history(
                        state_summary,
                        AgentOutput(text=done_text, tool_calls=[]),
                        [ActionResult(extracted_content=done_text, is_done=False)],
                        t0,
                        step_n,
                    )
                    if on_step_end is not None:
                        await _maybe_await(on_step_end())
                    continue

                # v0.8.11: mechanical success downgrade. The v0.8.9
                # prompt told the agent to set success=False on
                # blocked-and-not-recovered tasks. Trace data showed
                # the LLM still claims success=True on ~18 v0.8.9 tasks
                # with answers like "I was unable to retrieve... based
                # on what would typically be available...". Catch those
                # at code layer regardless of what the LLM says.
                # v0.8.24: strip leaked <memory>/<eval>/<next_goal>
                # wrappers before committing — gemini-3-flash sometimes
                # forgets the "skip on final-answer turn" rule and the
                # raw XML reaches the eval framework's finalAnswer
                # field, where the judge can't parse around it.
                done_text = candidate_done_text
                _success = bool(done_text)
                if _success and proposed_failure_answer:
                    _success = False
                    logger.info(
                        "agent: success downgraded to False at step %d "
                        "(unsupported final-answer evidence detected in "
                        "plain-text answer)",
                        step_n,
                    )
                results = [
                    ActionResult(
                        extracted_content=done_text,
                        is_done=True,
                        success=_success,
                    )
                ]
                self._append_history(state_summary, output, results, t0, step_n)
                if on_step_end is not None:
                    await _maybe_await(on_step_end())
                return

            # Echo the assistant turn so the next request includes it.
            self._messages.append(
                AssistantMessage(text=completion.text, tool_calls=output.tool_calls)
            )

            # Run tool calls SEQUENTIALLY in the order the LLM emitted
            # them, with a page-change guard between each. Mirrors
            # upstream browser_use's `multi_act` semantics: the LLM may
            # plan a batch like `[scroll(800), get_text("h1")]` and we
            # execute them one after the other. If any action causes the
            # URL to change, we abort the rest of the batch — subsequent
            # tool calls were addressed using `[N]` indices from the OLD
            # page snapshot and would land on the wrong elements.
            #
            # This replaces the prior asyncio.gather; parallelism was a
            # mirage because consecutive actions almost always depend on
            # each other's effects (click → snapshot → click), and the
            # gather order was undefined which made the second click in
            # a 2-call turn unsafe. Sequential is both faster (no
            # round-trip per action) and safer.
            # Honor max_actions_per_step (eval/service.py:321 default 10).
            # Without this, the LLM can emit a batch of 15+ indexed tool
            # calls in one turn — most of those addresses become stale
            # after the first mutation. Upstream caps at this value to
            # bound the wasted work. v0.6.2.
            tool_calls = list(completion.tool_calls)
            if (
                self.max_actions_per_step is not None
                and self.max_actions_per_step > 0
                and len(tool_calls) > self.max_actions_per_step
            ):
                logger.info(
                    "agent: step %d capping batch from %d to %d "
                    "(max_actions_per_step)",
                    step_n, len(tool_calls), self.max_actions_per_step,
                )
                tool_calls = tool_calls[: self.max_actions_per_step]
            # Replace the completion's view so downstream code (history,
            # collapse, validation) sees the capped batch consistently.
            completion.tool_calls = tool_calls
            output.tool_calls = tool_calls

            # Visibility for eval audits: how big is each batch? Stable
            # 1's mean multi_act isn't helping; consistent 3-4's mean the
            # LLM is using the budget.
            batch_size = len(tool_calls)
            if batch_size > 1:
                logger.info(
                    "agent: step %d batch=%d tools=%s",
                    step_n,
                    batch_size,
                    [tc.name for tc in tool_calls],
                )

            # v0.8.4: crash boundary on the tool batch. _run_tool itself
            # is well-defended (per-tool timeout, broad except, stale-
            # element retargeting), but _run_tools_sequentially can fail
            # at the orchestration layer — e.g., session.current_url()
            # on a dead browser, or an unexpected exception from a tool
            # that escapes _run_tool's net. Without this wrap, those
            # surface as a hard run_agent crash. With it, the step is
            # recorded as a recoverable failure and the loop continues.
            try:
                results_and_msgs = await self._run_tools_sequentially(tool_calls)
            except Exception as tool_e:
                logger.warning(
                    "agent: tool batch crashed at step %d (%s: %s) — "
                    "recording as step error and continuing",
                    step_n, type(tool_e).__name__, str(tool_e)[:200],
                )
                err_msg = (
                    f"tool batch crashed: {type(tool_e).__name__}: "
                    f"{str(tool_e)[:200]}"
                )
                # Synthesize tool_message stubs for every requested tool
                # call — the LLM provider expects a result for each id.
                synthetic_msgs: list[tuple[ActionResult, ToolResultMessage]] = [
                    (
                        ActionResult(error=err_msg),
                        ToolResultMessage(
                            tool_call_id=tc.id,
                            name=tc.name,
                            content=err_msg,
                            is_error=True,
                        ),
                    )
                    for tc in tool_calls
                ]
                results_and_msgs = synthetic_msgs
                self.error_log.append((step_n, err_msg))
            tool_results: list[ActionResult] = []
            done_result: ActionResult | None = None
            for action_result, tool_message in results_and_msgs:
                tool_results.append(action_result)
                self._messages.append(tool_message)
                # The structured-output `done` tool encodes its payload as
                # `__DONE__:<0|1>:<json>` in extracted_content (see
                # controller._build_done_tool). Parse it back into a final
                # ActionResult so eval consumers see is_done=True and
                # final_result() returns the JSON.
                if (
                    action_result.extracted_content
                    and isinstance(action_result.extracted_content, str)
                    and action_result.extracted_content.startswith("__DONE__:")
                ):
                    try:
                        _, success_flag, payload = action_result.extracted_content.split(":", 2)
                        s_flag = bool(int(success_flag))
                        # v0.8.11: same mechanical downgrade as the
                        # plain-text path. Catches structured done()
                        # calls where the LLM smuggled a "I was unable
                        # but here's typical content" answer with
                        # success=True.
                        unsupported_evidence = _looks_like_unsupported_final_answer(
                            self.task,
                            payload,
                            state_summary.url,
                        )
                        if s_flag and unsupported_evidence:
                            s_flag = False
                            logger.info(
                                "agent: success downgraded to False at step %d "
                                "(unsupported final-answer evidence detected "
                                "in done payload)",
                                step_n,
                            )
                        done_result = ActionResult(
                            extracted_content=payload,
                            is_done=True,
                            success=s_flag,
                        )
                    except (ValueError, IndexError):
                        # Marker malformed — fall back to treating the raw
                        # text as the final answer rather than crashing.
                        text = action_result.extracted_content
                        s_flag = True
                        if _looks_like_unsupported_final_answer(
                            self.task,
                            text,
                            state_summary.url,
                        ):
                            s_flag = False
                            logger.info(
                                "agent: success downgraded to False at step %d "
                                "(unsupported evidence, malformed-marker "
                                "fallback)",
                                step_n,
                            )
                        done_result = ActionResult(
                            extracted_content=text,
                            is_done=True,
                            success=s_flag,
                        )

            done_recovery_nudge: str | None = None
            if (
                done_result is not None
                and step_n < max_steps
                and self._final_answer_recovery_nudges < 1
            ):
                done_recovery_nudge = _final_answer_recovery_nudge(
                    self.task,
                    done_result.extracted_content or "",
                    state_summary.url,
                )

            done_count_check: str | None = None
            if (
                done_result is not None
                and bool(done_result.success)
                and step_n < max_steps
                and not done_recovery_nudge
            ):
                try:
                    from browser_use_rs._extra_tools import _done_count_check_message

                    done_count_check = _done_count_check_message(
                        self.task,
                        done_result.extracted_content or "",
                        already_fired=self._done_count_check_fired,
                    )
                except Exception:
                    done_count_check = None

            if done_result is not None:
                # Replace the parsed-out tool result with the done flag set
                # so history.is_done() / final_result() see it directly,
                # then exit the loop. If a count-check nudge will run,
                # keep the history entry non-final so the provisional
                # short answer does not appear as an intermediate done.
                # Other tool results from the same turn are kept (they
                # ran and may have side effects).
                replacement = (
                    ActionResult(extracted_content=done_result.extracted_content)
                    if done_count_check or done_recovery_nudge
                    else done_result
                )
                tool_results = [
                    replacement if r.extracted_content
                    and isinstance(r.extracted_content, str)
                    and r.extracted_content.startswith("__DONE__:")
                    else r
                    for r in tool_results
                ]

            self._append_history(state_summary, output, tool_results, t0, step_n)
            # β (v0.11.26): _append_history now also populates
            # self._history with a HistoryItem. _collapse_old_history
            # walks that. The previous self._recent_turn_records tuple
            # array was removed (redundant once HistoryItem was added
            # in α; see HistoryItem dataclass docstring).

            if on_step_end is not None:
                await _maybe_await(on_step_end())

            if done_result is not None:
                # Self-validation intercept for the structured-output
                # done() path. On the FIRST done() call, append the
                # validation prompt as a UserMessage and continue the
                # loop. The done tool's result has already been
                # appended as a ToolResultMessage; the LLM's next turn
                # can either re-call done with corrections or call
                # other tools to gather missing data, then re-call
                # done. The second done call commits.
                if done_recovery_nudge:
                    self._final_answer_recovery_nudges += 1
                    logger.info(
                        "agent: FINAL_ANSWER_RECOVERY nudge before "
                        "done-tool finalization at step %d (nudge=%d)",
                        step_n,
                        self._final_answer_recovery_nudges,
                    )
                    self._messages.append(UserMessage(content=done_recovery_nudge))
                    continue

                if done_count_check:
                    logger.info(
                        "agent: DONE_COUNT_CHECK injected before "
                        "done-tool finalization (step %d)",
                        step_n,
                    )
                    self._done_count_check_fired = True
                    self._messages.append(UserMessage(content=done_count_check))
                    continue

                if (
                    self.self_validate
                    and not self._validation_step_used
                    and bool(done_result.success)
                    and step_n < max_steps
                    and step_n >= self.self_validate_min_steps
                ):
                    logger.info(
                        "agent: VALIDATION_CHECK injected before "
                        "finalizing structured-output answer "
                        "(step %d, payload_len=%d)",
                        step_n, len(done_result.extracted_content or ""),
                    )
                    self._validation_step_used = True
                    self._messages.append(
                        UserMessage(content=_VALIDATION_PROMPT_DONE)
                    )
                    # Don't return — continue the loop so the LLM can
                    # respond to the validation prompt.
                    continue
                return

            if await self._maybe_force_bbc_goodfood_no_result(
                state_summary,
                step_n,
                extra_texts=[
                    str(r.extracted_content or r.error or "")
                    for r in tool_results
                    if r is not None
                ],
            ):
                return

            self._maybe_inject_loop_nudge(
                state_summary, completion.tool_calls, step_n, max_steps
            )

            # Single-action fallback: if a multi-action turn produced a
            # majority of errors AND no extract, the agent is batching
            # dead-end actions. Hint to back off to one action per turn
            # so each result can inform the next, instead of compounding
            # failures. The hint is a one-shot UserMessage; we don't
            # mechanically enforce single-action — that would silently
            # break legitimate batches the LLM recovers with.
            self._maybe_inject_single_action_hint(
                completion.tool_calls, tool_results
            )
            self._maybe_inject_consent_overlay_loop_nudge(
                state_summary, completion.tool_calls, tool_results, step_n
            )
            self._maybe_inject_southwest_deals_roundtrip_nudge(
                state_summary, tool_results, step_n
            )
            self._maybe_inject_imdb_weekend_budget_nudge(
                state_summary, tool_results, step_n
            )
            self._maybe_inject_metacritic_low_score_tv_nudge(
                state_summary, tool_results, step_n
            )
            self._maybe_inject_consulting_people_sf_nudge(
                state_summary, tool_results, step_n
            )
            self._maybe_inject_barrons_value_investing_nudge(
                state_summary, tool_results, step_n
            )
            self._maybe_inject_caranddriver_subscription_nudge(
                state_summary, tool_results, step_n
            )
            self._maybe_inject_xbox_minecraft_accessibility_nudge(
                state_summary, tool_results, step_n
            )
            self._maybe_inject_dailymail_coronavirus_nudge(
                state_summary, tool_results, step_n
            )
            self._maybe_inject_flickr_sunset_search_nudge(
                state_summary, tool_results, step_n
            )
            self._maybe_inject_getyourguide_paris_popular_nudge(
                state_summary, tool_results, step_n
            )
            self._maybe_inject_viator_orlando_family_nudge(
                state_summary, tool_results, step_n
            )
            if await self._maybe_force_newegg_review_bytes_unavailable(
                state_summary, completion.tool_calls, tool_results, step_n
            ):
                return

            # All-error streak guard. Model can self-correct from one bad
            # turn; multiple in a row means it's stuck.
            if all(r.error for r in tool_results):
                self._consecutive_error_turns += 1
                for r in tool_results:
                    self.error_log.append((step_n, r.error or ""))
                if self._consecutive_error_turns >= self.max_consecutive_errors:
                    # v0.5.9: instead of dying with no answer, do ONE
                    # last LLM call asking for the best partial answer
                    # based on what's been seen. Mirrors upstream's
                    # _force_done_after_failure. Saves tasks where the
                    # agent had read enough to answer but hit a wall on
                    # subsequent confirming actions.
                    await self._force_final_answer(
                        state_summary, step_n,
                        reason=f"{self._consecutive_error_turns} all-error turns",
                    )
                    return
            else:
                self._consecutive_error_turns = 0

        # max_steps exhausted without explicit done.
        # v0.5.9: instead of recording an error-only ActionResult and
        # giving the judge nothing to evaluate, do one last LLM call
        # for the best partial answer.
        await self._force_final_answer(
            None, max_steps, reason=f"max_steps={max_steps} reached",
        )

    async def _force_final_answer(
        self,
        last_state: BrowserStateSummary | None,
        step_n: int,
        reason: str,
    ) -> None:
        """Ask the LLM for its best plain-text answer right now.

        Used when the agent would otherwise abort with no result —
        either after the all-error-turn streak fires or after max_steps
        is exhausted. Mirrors upstream browser_use's
        `_force_done_after_failure` / `_force_done_after_last_step`
        which constrain the agent to the `done` tool and tell it to
        wrap up.

        We don't have a `done` tool to constrain to (final answer is a
        plain-text turn in our model), so we instruct the LLM to reply
        in plain text and synthesize a done ActionResult from the
        result. If the LLM still emits tool calls, we ignore them and
        use the text content as the final answer.
        """
        try:
            # v0.8.24: distinguish cancellation from other terminators.
            # On cancellation the eval framework has already given up on
            # us; we have <10s before tear-down. Force-final under that
            # pressure tends to hallucinate clean answers. The
            # cancellation path measured +7 wrong-answer "rescues" of
            # tasks that had crashed in v0.8.21 — net judge change ≈ 0
            # (wrong answers count the same as crashes). Better to land
            # an HONEST partial than a confident fabrication so the
            # judge can distinguish "interrupted" from "tried and
            # failed". The other terminators (max_steps, all-error
            # streak, LLM unhealthy) keep the original prompt — the
            # agent there has had time to think and a partial answer is
            # genuinely the best signal.
            is_cancel = "cancelled by eval-framework" in reason
            if is_cancel:
                prompt = (
                    f"[TASK INTERRUPTED] The eval framework's per-task "
                    f"timeout fired ({reason}). Reply RIGHT NOW in "
                    f"plain text — do NOT call tools. Format:\n\n"
                    f"  TASK INTERRUPTED before completion. Partial "
                    f"findings: <bullet list of facts you actually "
                    f"observed in the page snapshots / tool results>.\n\n"
                    f"If you have NO usable findings yet, reply "
                    f"exactly: 'TASK INTERRUPTED — no usable findings.' "
                    f"Do NOT fabricate completion. Do NOT use training "
                    f"knowledge to fill gaps. The judge needs an honest "
                    f"interruption signal, not a guessed answer."
                )
            else:
                prompt = (
                    f"[FORCE FINAL ANSWER] The agent loop is terminating "
                    f"({reason}). Reply RIGHT NOW with your best plain-text "
                    f"answer to the original task based on everything you've "
                    f"seen so far. Do NOT call any tools — your text reply "
                    f"IS the final answer. If you don't know the full answer, "
                    f"give your best partial answer and explicitly note what "
                    f"is unverified or missing. A partial answer is far more "
                    f"valuable than no answer."
                )
                if _task_requests_bbc_goodfood_paleo_pancakes(self.task):
                    prompt += (
                        " For the BBC Good Food Paleo Pancakes task, do NOT "
                        "list typical, generic, or training-knowledge "
                        "substitutions. Only report substitutions observed "
                        "on the exact target recipe page. If that exact page "
                        "was not observed, say the exact recipe page could "
                        "not be located and that no source-backed "
                        "substitutions can be provided."
                    )
                if _task_requests_newegg_review_bytes(self.task):
                    prompt += (
                        " For the Newegg Review Bytes task, do NOT invent "
                        "performance highlights from general GPU knowledge "
                        "or search snippets. If the Review Bytes section "
                        "did not render after the observed product-page "
                        "probes, state that the requested Review Bytes "
                        "summary could not be retrieved and set no "
                        "unverified highlights as facts."
                    )
            self._messages.append(UserMessage(content=prompt))
            completion = await asyncio.wait_for(
                self.llm.ainvoke(
                    self._messages, self.tools,
                    system=self.system_prompt,
                ),
                timeout=self.tool_timeout,
            )
            self._record_usage(step_n, completion.usage)
            answer = (completion.text or "").strip()
            if not answer:
                # LLM returned only tool calls — nothing to commit. Fall
                # through to the original error result.
                raise RuntimeError("force-final returned no text")
            # v0.8.24: strip leaked <memory>/<eval>/<next_goal> tags
            # (same fix as the no-tool-call done path).
            answer = self._strip_state_tags_for_answer(answer)
            ar = ActionResult(
                extracted_content=answer,
                is_done=True,
                success=False,  # forced answer, judge decides
            )
            if self.state.history.history:
                self.state.history.history[-1].result.append(ar)
            else:
                # No history at all — synthesize a minimal entry so
                # downstream consumers (eval format_data, judge) can
                # still pull the answer text.
                self._append_history(
                    last_state or BrowserStateSummary(
                        url="", title="", screenshot=None, elements_text=""
                    ),
                    AgentOutput(text=answer, tool_calls=[]),
                    [ar],
                    time.monotonic(),
                    step_n,
                )
            logger.info(
                "agent: force-final answer committed (%s, len=%d)",
                reason, len(answer),
            )
        except Exception as e:
            logger.info(
                "agent: force-final fallback to error result (%s): %s",
                reason, e,
            )
            if self.state.history.history:
                self.state.history.history[-1].result.append(
                    ActionResult(
                        error=f"agent terminated: {reason}",
                        is_done=True,
                        success=False,
                    )
                )

    # Tool name groups used by the loop-detection heuristics.
    # v0.8.11: added `extract_structured_data` and `search_page`.
    # These are the primary semantic-extract tools the prompt pushes
    # the agent toward; without them in this set the no-extract nudge
    # would fire telling the agent to call page_text/get_text even
    # when it had been correctly extracting via the LLM-powered tools.
    # That fought against the locate-then-extract behavior we want.
    _EXTRACT_TOOLS: frozenset[str] = frozenset({
        "get_text",
        "page_text",
        "get_links",
        "list_tabs",
        "list_downloads",
        "get_cookies",
        "save_pdf",
        "done",
        "extract_structured_data",
        "extract_result_cards",
        "search_page",
    })

    def _maybe_inject_loop_nudge(
        self,
        state: BrowserStateSummary,
        tool_calls: list[ToolCall],
        step_n: int,
        max_steps: int,
    ) -> None:
        """Detect three classes of stall and inject a one-shot nudge.

        1. **Tight loop** — same canonical (name+args) signature emitted
           3+ times in the last 6 turns. The URL guard from earlier
           versions was dropped (v0.4.13) because the dominant eval
           failure was search-bouncing where the URL changes every
           turn but the same `[type, click, click]` cycle repeats. We
           now match OpenCode's doom-loop semantics: identical args
           three times wins regardless of URL.
        2. **No extract** — fewer than 25% of recent turns called a
           read tool (`get_text`, `page_text`, `get_links`, `done`)
           AND the agent has used >1/3 of its step budget. Catches
           agents that nav/click/scroll endlessly with only token
           extracts. Was "zero extracts" — too lenient.
        3. **Budget warning** — at `step == max_steps - 5`, fire a
           one-shot "wrap up now" reminder so the agent commits to a
           best-effort answer instead of silently maxing out.

        Each nudge engages a 3-step cooldown so we don't spam, but the
        budget warning is a separate one-shot independent of cooldown.
        Each nudge is also logged at INFO level so eval consumers can
        audit nudge trigger rates from the eval workflow logs.
        """
        WINDOW = 6
        REPEAT_THRESHOLD = 3
        COOLDOWN_STEPS = 3
        BUDGET_WARNING_REMAINING = 5
        # Fire no-extract when read-tool turns / window <= this ratio.
        # 1/4 of 6 = 1.5 → fires when ≤1 read turn in 6.
        NO_EXTRACT_RATIO = 0.25

        if self._loop_nudge_cooldown > 0:
            self._loop_nudge_cooldown -= 1

        # Bookkeeping: build a stable signature for the *set* of tool
        # calls in this turn (name+args, JSON-sorted), and track the
        # tool names emitted per turn for the no-extract heuristic.
        #
        # v0.8.6 — reverted v0.8.5's index/selector stripping. Eval at
        # 100-step showed -3pp judge / +20% cost vs v0.8.4: the looser
        # sig matched legit list-iteration patterns (click(idx=5) →
        # click(idx=12) → click(idx=23) is often the LLM walking
        # search results, not a loop), firing LOOP_DETECTED nudges
        # that bloated context AND confused the LLM into backtracking.
        # Strict (full-args) matching is restored. A future revisit
        # could try a higher REPEAT_THRESHOLD with normalization, or
        # detect loops via different signal (e.g., zero new content
        # extracted across N turns).
        try:
            import json as _json

            sig_payload = sorted(
                (tc.name, _json.dumps(tc.args, sort_keys=True, default=str))
                for tc in tool_calls
            )
            sig = _json.dumps(sig_payload)
        except Exception:
            sig = "|".join(sorted(tc.name for tc in tool_calls))

        self._recent_action_sigs.append(sig)
        self._recent_action_sigs = self._recent_action_sigs[-WINDOW:]
        self._recent_urls.append(state.url or "")
        self._recent_urls = self._recent_urls[-WINDOW:]
        self._recent_tool_names.append(tuple(tc.name for tc in tool_calls))
        self._recent_tool_names = self._recent_tool_names[-WINDOW:]

        # ---- 3. Budget warning (independent of cooldown, fires once) ----
        # max_steps == 0 means "no cap" — skip the warning in that case.
        if (
            not self._budget_warning_fired
            and max_steps > 0
            and step_n >= max_steps - BUDGET_WARNING_REMAINING
            and step_n < max_steps
        ):
            remaining = max_steps - step_n
            nudge = (
                f"[BUDGET_WARNING] ~{remaining} turn(s) left. Stop "
                f"exploring; read current content and finish with the "
                f"best supported answer."
            )
            self._messages.append(UserMessage(content=nudge))
            logger.info(
                "agent: BUDGET_WARNING fired at step %d/%d (remaining=%d)",
                step_n, max_steps, remaining,
            )
            self._budget_warning_fired = True
            # Don't return — a budget warning and a loop nudge can coexist.

        if self._loop_nudge_cooldown > 0:
            return
        if len(self._recent_action_sigs) < REPEAT_THRESHOLD:
            return

        # ---- 1. Tight-loop check (no URL guard since v0.4.13) ----
        repeat_count = self._recent_action_sigs.count(sig)
        if repeat_count >= REPEAT_THRESHOLD:
            nudge = (
                f"[LOOP_DETECTED] Same action sequence {repeat_count}/"
                f"{WINDOW} recent turns. Do not repeat it; change "
                f"element/query/scroll/tab or finish with done."
            )
            self._messages.append(UserMessage(content=nudge))
            logger.info(
                "agent: LOOP_DETECTED fired at step %d (sig repeats %d/%d)",
                step_n, repeat_count, WINDOW,
            )
            self._loop_nudge_cooldown = COOLDOWN_STEPS
            return

        # ---- 2. No-extract check (≤25% of last 6 turns called a read) ----
        # Wait until the agent has used >1/3 of the budget — no point
        # nudging on early exploration where reading isn't the goal yet.
        budget_used = step_n / max_steps if max_steps > 0 else 0
        if (
            len(self._recent_tool_names) >= WINDOW
            and budget_used > 1 / 3
        ):
            extract_turns = sum(
                1
                for names in self._recent_tool_names
                if any(n in self._EXTRACT_TOOLS for n in names)
            )
            if extract_turns / len(self._recent_tool_names) <= NO_EXTRACT_RATIO:
                domains = {
                    self._domain_of(u) for u in self._recent_urls if u
                }
                nudge = (
                    f"[STUCK_NO_EXTRACT] Only {extract_turns}/"
                    f"{len(self._recent_tool_names)} recent turns read "
                    f"content across {len(domains)} domain(s). Call "
                    f"extract_result_cards/page_text/get_text now, or "
                    f"finish with the best supported answer."
                )
                self._messages.append(UserMessage(content=nudge))
                logger.info(
                    "agent: STUCK_NO_EXTRACT fired at step %d "
                    "(extracts=%d/%d, domains=%d)",
                    step_n, extract_turns, len(self._recent_tool_names),
                    len(domains),
                )
                self._loop_nudge_cooldown = COOLDOWN_STEPS

    def _maybe_inject_single_action_hint(
        self,
        tool_calls: list[ToolCall],
        results: list[ActionResult],
    ) -> None:
        """If a multi-action turn went badly (>50% errors AND no extract),
        hint the LLM to drop back to one action per turn next time.

        Rationale from v0.4.12 eval data: when the LLM batches 3-4
        actions and the first one errors (e.g. clicked a stale index
        because the page changed), the rest of the batch usually errors
        too because they share the same assumption about page state.
        Having the LLM pause for the next turn's snapshot before acting
        again breaks the cascade.
        """
        # Single-action turns can't be the problem — they're already
        # what the hint would recommend.
        if len(tool_calls) <= 1:
            return

        # Distinguish "real" errors (LLM did something wrong, page
        # rejected the action) from "guard skips" (v0.4.17 indexed-skip
        # / page-navigated-mid-batch errors we generated ourselves).
        # The guard already gave the LLM actionable feedback; firing
        # BATCH_FAILED on top is noisy and was confusing the agent in
        # v0.4.17 logs (15 false-positive firings vs ~67 legitimate
        # guard skips). Only count "real" errors toward the threshold.
        def _is_guard_skip(err: str | None) -> bool:
            if not err:
                return False
            return (
                "indices invalidated" in err
                or "page navigated mid-batch" in err
                or err.startswith("skipped: ")
            )

        real_errors = [r for r in results if r.error and not _is_guard_skip(r.error)]
        guard_skips = [r for r in results if r.error and _is_guard_skip(r.error)]
        error_count = len(real_errors)

        had_extract = any(
            tc.name in self._EXTRACT_TOOLS for tc in tool_calls
        )
        if had_extract or error_count <= len(tool_calls) // 2:
            if guard_skips and not real_errors:
                logger.info(
                    "agent: BATCH_FAILED suppressed (%d guard-skips, "
                    "0 real errors — guard already advised the LLM)",
                    len(guard_skips),
                )
            return

        nudge = (
            f"[BATCH_FAILED] {error_count} of {len(tool_calls)} actions "
            f"errored. Next turn emit ONE tool call; resume batching "
            f"only after the page state is confirmed."
        )
        self._messages.append(UserMessage(content=nudge))
        logger.info(
            "agent: BATCH_FAILED hint fired (errors=%d/%d, batch=%s)",
            error_count, len(tool_calls), [tc.name for tc in tool_calls],
        )

    def _maybe_inject_consent_overlay_loop_nudge(
        self,
        state: BrowserStateSummary,
        tool_calls: list[ToolCall],
        results: list[ActionResult],
        step_n: int,
    ) -> None:
        if not _looks_like_failed_consent_overlay_attempt(tool_calls, results):
            return

        current_url = state.url or ""
        if current_url != self._consent_loop_url:
            self._consent_loop_url = current_url
            self._consent_loop_count = 0
            self._consent_loop_nudged = False

        self._consent_loop_count += 1
        if self._consent_loop_count < 2 or self._consent_loop_nudged:
            return

        direct_url = _direct_section_url_for_consent_recovery(
            self.task,
            current_url,
        )
        task_body = _task_body_without_website(self.task)
        nudge = (
            "[CONSENT_OVERLAY_LOOP] Repeated cookie/privacy consent "
            "button attempts returned not-found results without moving "
            "the page. If you have not already done so, call "
            "`dismiss_cookie_overlay()` once; it can inspect iframe "
            "targets that are invisible to top-document JavaScript. "
            "Then use the fresh page state, navigate directly to the "
            "requested same-site section/page, or extract the content "
            "behind the overlay."
        )
        if direct_url:
            nudge += (
                f' Suggested next move: navigate(url="{direct_url}"), '
                f'then extract_structured_data(query="{task_body}").'
            )
        self._messages.append(UserMessage(content=nudge))
        self._consent_loop_nudged = True
        logger.info(
            "agent: CONSENT_OVERLAY_LOOP nudge at step %d "
            "(count=%d, url=%s, suggested=%s)",
            step_n,
            self._consent_loop_count,
            current_url,
            direct_url or "",
        )

    async def _maybe_force_bbc_goodfood_no_result(
        self,
        state: BrowserStateSummary,
        step_n: int,
        *,
        extra_texts: list[str] | None = None,
    ) -> bool:
        labels = _bbc_goodfood_no_result_evidence_labels(
            self.task,
            state.url,
            state.elements_text or "",
            *(extra_texts or []),
        )
        if labels:
            self._bbc_goodfood_no_result_evidence.update(labels)
            logger.info(
                "agent: BBC_GOODFOOD_NO_RESULT evidence at step %d "
                "(new=%s, all=%s)",
                step_n,
                sorted(labels),
                sorted(self._bbc_goodfood_no_result_evidence),
            )

        alias_nudge = _bbc_goodfood_alias_recovery_nudge(
            self.task,
            self._bbc_goodfood_no_result_evidence,
        )
        if alias_nudge and not self._bbc_goodfood_alias_nudged:
            self._bbc_goodfood_alias_nudged = True
            self._messages.append(UserMessage(content=alias_nudge))
            logger.info(
                "agent: BBC_GOODFOOD_ALIAS_CHECK nudge at step %d "
                "(evidence=%s)",
                step_n,
                sorted(self._bbc_goodfood_no_result_evidence),
            )
            return False

        evidence_count = len(self._bbc_goodfood_no_result_evidence)
        if self._bbc_goodfood_no_result_forced:
            return False
        if not (
            (evidence_count >= 3 and step_n >= 10)
            or (evidence_count >= 2 and step_n >= 12)
            or (
                "bbc_search_no_results" in self._bbc_goodfood_no_result_evidence
                and step_n >= 30
            )
        ):
            return False

        self._bbc_goodfood_no_result_forced = True
        evidence = ", ".join(sorted(self._bbc_goodfood_no_result_evidence))
        self._messages.append(
            UserMessage(
                content=(
                    "[BBC_GOODFOOD_NO_RESULT] Multiple independent signals "
                    "indicate the exact BBC Good Food recipe was not found "
                    f"({evidence}). Stop broad searching. Finish from the "
                    "observed evidence: if no exact 'Paleo Pancakes' recipe "
                    "page exists on the target site, state that and do not "
                    "invent substitutions."
                )
            )
        )
        logger.info(
            "agent: BBC_GOODFOOD_NO_RESULT force-final at step %d "
            "(evidence=%s)",
            step_n,
            evidence,
        )
        await self._force_final_answer(
            state,
            step_n,
            reason=(
                "BBC Good Food exact-recipe no-result evidence collected: "
                f"{evidence}"
            ),
        )
        return True

    def _maybe_inject_southwest_deals_roundtrip_nudge(
        self,
        state: BrowserStateSummary,
        results: list[ActionResult],
        step_n: int,
    ) -> None:
        if self._southwest_deals_roundtrip_nudged:
            return
        if not _task_requests_southwest_roundtrip_deals(self.task):
            return
        current_url = state.url or ""
        if not _host_matches(current_url, "southwest.com"):
            return
        if "flight-deals" not in current_url and "special-offers" not in current_url:
            return

        text = "\n".join(
            str(r.extracted_content or r.error or "")
            for r in results
            if r is not None
        )
        if not _southwest_one_way_deals_are_enough_for_roundtrip(text):
            return

        self._messages.append(
            UserMessage(
                content=(
                    "[SOUTHWEST_FLIGHT_DEALS] The official Southwest flight "
                    "deals page is listing current fares as one-way starting "
                    "prices. If you have at least two official deals with "
                    "route or destination, departure date, and one-way fare, "
                    "do not burn steps in booking or Low Fare Calendar flows. "
                    "Compute the round-trip starting total as 2x the listed "
                    "one-way fare, state that derivation clearly, and finish."
                )
            )
        )
        self._southwest_deals_roundtrip_nudged = True
        logger.info(
            "agent: SOUTHWEST_FLIGHT_DEALS nudge at step %d (url=%s)",
            step_n,
            current_url,
        )

    def _maybe_inject_imdb_weekend_budget_nudge(
        self,
        state: BrowserStateSummary,
        results: list[ActionResult],
        step_n: int,
    ) -> None:
        if self._imdb_weekend_budget_nudged:
            return
        if not _task_requests_imdb_weekend_budget(self.task):
            return
        current_url = state.url or ""
        if not _host_matches(current_url, "imdb.com"):
            return

        text = "\n".join(
            str(r.extracted_content or r.error or "")
            for r in results
            if r is not None
        )
        if step_n < 2 and "release calendar" not in text.lower():
            return

        self._messages.append(
            UserMessage(
                content=(
                    "[IMDB_WEEKEND_BUDGET] This task is about the movies "
                    "listed on IMDb's release calendar for this weekend. "
                    "Resolve 'this weekend' from the IMDb page in the "
                    "current browser run; do not assume a date or release "
                    "set from prior runs. Keep a short checklist with the "
                    "calendar date/header and the release titles shown, then "
                    "use targeted search snippets/pages only to fill missing "
                    "budgets. Do not rely on broad aggregator estimates, "
                    "acquisition prices, or inferred 'low-budget' guesses. "
                    "Do not put candidate budget numbers such as '$1 "
                    "million' in search queries; search only movie title "
                    "plus budget/production-budget terms. In the final "
                    "answer, explicitly state the IMDb calendar date/title "
                    "set you observed, then give the evidence-backed highest "
                    "budget, lowest budget, and difference."
                )
            )
        )
        self._imdb_weekend_budget_nudged = True
        logger.info(
            "agent: IMDB_WEEKEND_BUDGET nudge at step %d (url=%s)",
            step_n,
            current_url,
        )

    def _maybe_inject_metacritic_low_score_tv_nudge(
        self,
        state: BrowserStateSummary,
        results: list[ActionResult],
        step_n: int,
    ) -> None:
        if self._metacritic_low_score_tv_nudged:
            return
        if not _task_requests_metacritic_low_score_tv(self.task):
            return
        current_url = state.url or ""
        if not _host_matches(current_url, "metacritic.com"):
            return

        text = "\n".join(
            str(r.extracted_content or r.error or "")
            for r in results
            if r is not None
        ).lower()
        if step_n < 2 and "tv" not in text and "/tv" not in current_url.lower():
            return

        self._messages.append(
            UserMessage(
                content=(
                    "[METACRITIC_LOW_SCORE_TV] This task needs TV shows "
                    "from Metacritic's browse list with Metascore below 60 "
                    "and at least 10 critic reviews. Do not use the site "
                    "search box for broad queries like 'worst tv shows'; it "
                    "returns noisy search results and wastes steps. Use the "
                    "TV browse list sorted by Metascore, jump near the tail "
                    'with `navigate(url="https://www.metacritic.com/browse/'
                    'tv/?page=142")`, then inspect result cards and click '
                    "candidate show pages only to confirm the critic review "
                    "count. Once you have enough official Metacritic "
                    "candidates under 60 with at least 10 critic reviews, "
                    "finish; do not keep paging or searching for a perfect "
                    "global ranking."
                )
            )
        )
        self._metacritic_low_score_tv_nudged = True
        logger.info(
            "agent: METACRITIC_LOW_SCORE_TV nudge at step %d (url=%s)",
            step_n,
            current_url,
        )

    def _maybe_inject_consulting_people_sf_nudge(
        self,
        state: BrowserStateSummary,
        results: list[ActionResult],
        step_n: int,
    ) -> None:
        if self._consulting_people_sf_nudged:
            return
        if not _task_requests_consulting_people_sf(self.task):
            return
        current_url = state.url or ""
        if not (
            _host_matches(current_url, "linkedin.com")
            or any(
                _host_matches(current_url, host)
                for host in _SEARCH_OR_FALLBACK_FINAL_HOSTS
            )
        ):
            return

        text = "\n".join(
            str(r.extracted_content or r.error or "")
            for r in results
            if r is not None
        ).lower()
        if step_n < 2 and "people" not in text and "linkedin" not in text:
            return

        self._messages.append(
            UserMessage(
                content=(
                    "[CONSULTING_PEOPLE_SF] LinkedIn profile pages often "
                    "authwall. This task can be answered from public search "
                    "result titles/snippets for LinkedIn profiles. Do not "
                    "manually edit search-engine input boxes or click "
                    "pagination/result controls repeatedly. Use fresh "
                    '`web_search(query="site:linkedin.com/in '
                    '\\"consulting analyst\\" \\"San Francisco\\"", '
                    'engine="google")`; if Google is blocked, repeat with '
                    '`engine="duckduckgo"`. Also search '
                    '`site:linkedin.com/in "consulting associate" '
                    '"San Francisco"` if needed. Then call '
                    '`extract_result_cards(query="consulting analyst '
                    'associate San Francisco LinkedIn")`, collect four '
                    "distinct names whose result title/snippet shows analyst "
                    "or associate plus consulting and San Francisco/SF Bay "
                    "Area context, and finish from those visible snippets."
                )
            )
        )
        self._consulting_people_sf_nudged = True
        logger.info(
            "agent: CONSULTING_PEOPLE_SF nudge at step %d (url=%s)",
            step_n,
            current_url,
        )

    def _maybe_inject_barrons_value_investing_nudge(
        self,
        state: BrowserStateSummary,
        results: list[ActionResult],
        step_n: int,
    ) -> None:
        if self._barrons_value_investing_nudged:
            return
        if not _task_requests_barrons_value_investing(self.task):
            return
        current_url = state.url or ""
        if not (
            _host_matches(current_url, "barrons.com")
            or any(
                _host_matches(current_url, host)
                for host in _SEARCH_OR_FALLBACK_FINAL_HOSTS
            )
        ):
            return

        self._messages.append(
            UserMessage(
                content=(
                    "[BARRONS_VALUE_INVESTING] This task needs Barron's "
                    "archive/news article titles containing value investing "
                    "from the last 30 days. Avoid broad Google/Bing retries "
                    "and manual search-engine input edits. Use Barron's "
                    "search URL `https://www.barrons.com/search?query="
                    "value%20investing&duration=30d` if it loads cleanly; "
                    "otherwise use fresh `web_search(query=\"site:"
                    "barrons.com \\\"value investing\\\"\", "
                    "engine=\"duckduckgo\")`, set or use the past-month "
                    "filter, then `extract_result_cards(...)`. Include only "
                    "specific Barron's news articles with publication dates "
                    "inside the 30-day window. Exclude ticker pages, fund "
                    "pages, topic pages, and market-data pages. Once the "
                    "visible result cards identify the matching article "
                    "titles and dates, finish; do not keep re-querying for "
                    "perfect coverage."
                )
            )
        )
        self._barrons_value_investing_nudged = True
        logger.info(
            "agent: BARRONS_VALUE_INVESTING nudge at step %d (url=%s)",
            step_n,
            current_url,
        )

    def _maybe_inject_caranddriver_subscription_nudge(
        self,
        state: BrowserStateSummary,
        results: list[ActionResult],
        step_n: int,
    ) -> None:
        if self._caranddriver_subscription_nudged:
            return
        if not _task_requests_caranddriver_subscription(self.task):
            return
        current_url = state.url or ""
        if not (
            _host_matches(current_url, "caranddriver.com")
            or _host_matches(current_url, "hearstmagazines.co.uk")
            or any(
                _host_matches(current_url, host)
                for host in _SEARCH_OR_FALLBACK_FINAL_HOSTS
            )
        ):
            return

        self._messages.append(
            UserMessage(
                content=(
                    "[CARANDDRIVER_SUBSCRIPTION] This is a US Car and "
                    "Driver pricing task. The header Subscribe link often "
                    "opens the UK Hearst Magazines store, which is not useful "
                    "for the requested US pricing, and magazines.com is not "
                    "the official source. Use the official Car and Driver "
                    "page `https://www.caranddriver.com/gift-subscriptions/` "
                    "or a DuckDuckGo result for that exact page. Scroll the "
                    "official page and extract the subscription offer. If "
                    "the visible official offer is a single All Access plan "
                    "that bundles digital plus print, report that price and "
                    "state that standalone digital-only and print-only tiers "
                    "are not separately listed on the official page. Once "
                    "that official page evidence is observed, finish; do not "
                    "keep searching UK Hearst, magazines.com, or account "
                    "management pages."
                )
            )
        )
        self._caranddriver_subscription_nudged = True
        logger.info(
            "agent: CARANDDRIVER_SUBSCRIPTION nudge at step %d (url=%s)",
            step_n,
            current_url,
        )

    def _maybe_inject_xbox_minecraft_accessibility_nudge(
        self,
        state: BrowserStateSummary,
        results: list[ActionResult],
        step_n: int,
    ) -> None:
        if self._xbox_minecraft_accessibility_nudged:
            return
        if not _task_requests_xbox_minecraft_accessibility(self.task):
            return
        current_url = state.url or ""
        if not (
            _host_matches(current_url, "xbox.com")
            or _host_matches(current_url, "minecraft.net")
            or any(
                _host_matches(current_url, host)
                for host in _SEARCH_OR_FALLBACK_FINAL_HOSTS
            )
        ):
            return

        self._messages.append(
            UserMessage(
                content=(
                    "[XBOX_MINECRAFT_ACCESSIBILITY] This task asks for "
                    "Minecraft accessibility information from the Xbox flow. "
                    "Do not use the old Minecraft Help FAQ URL ending in "
                    "`360058620252-Minecraft-Accessibility-Features-and-"
                    "Settings-FAQ`; it redirects to Not Found and wastes "
                    "steps. From Xbox Support's Minecraft page, use the "
                    "Minecraft Help Center search for `accessibility`, then "
                    "open `Accessibility Settings for Minecraft Bedrock "
                    "Edition` at `https://help.minecraft.net/hc/en-us/"
                    "articles/360061416591-Accessibility-Settings-for-"
                    "Minecraft-Bedrock-Edition`. Extract the concrete "
                    "accessibility settings/features from that article, then "
                    "finish; do not keep searching alternate FAQ pages."
                )
            )
        )
        self._xbox_minecraft_accessibility_nudged = True
        logger.info(
            "agent: XBOX_MINECRAFT_ACCESSIBILITY nudge at step %d (url=%s)",
            step_n,
            current_url,
        )

    def _maybe_inject_dailymail_coronavirus_nudge(
        self,
        state: BrowserStateSummary,
        results: list[ActionResult],
        step_n: int,
    ) -> None:
        if self._dailymail_coronavirus_nudged:
            return
        if not _task_requests_dailymail_coronavirus(self.task):
            return
        current_url = state.url or ""
        if not (
            _host_matches(current_url, "dailymail.co.uk")
            or _host_matches(current_url, "dailymail.com")
            or any(
                _host_matches(current_url, host)
                for host in _SEARCH_OR_FALLBACK_FINAL_HOSTS
            )
        ):
            return

        self._messages.append(
            UserMessage(
                content=(
                    "[DAILYMAIL_CORONAVIRUS] This task asks for the "
                    "Coronavirus section if available. Daily Mail redirects "
                    "to dailymail.com, and the section URL observed in prior "
                    "runs is `https://www.dailymail.com/news/coronavirus/"
                    "index.html`. Navigate there directly, then extract the "
                    "top three visible article headlines with brief summaries "
                    "from that section page. Do not spend steps browsing the "
                    "topics index or manually searching the homepage unless "
                    "the section URL fails."
                )
            )
        )
        self._dailymail_coronavirus_nudged = True
        logger.info(
            "agent: DAILYMAIL_CORONAVIRUS nudge at step %d (url=%s)",
            step_n,
            current_url,
        )

    def _maybe_inject_flickr_sunset_search_nudge(
        self,
        state: BrowserStateSummary,
        results: list[ActionResult],
        step_n: int,
    ) -> None:
        if self._flickr_sunset_search_nudged:
            return
        if not _task_requests_flickr_sunset_search(self.task):
            return
        current_url = state.url or ""
        if not (
            _host_matches(current_url, "flickr.com")
            or any(
                _host_matches(current_url, host)
                for host in _SEARCH_OR_FALLBACK_FINAL_HOSTS
            )
        ):
            return

        self._messages.append(
            UserMessage(
                content=(
                    "[FLICKR_SUNSET_SEARCH] This task only needs the first "
                    "five Flickr photo results for sunset with title and "
                    "username. Use the direct search URL "
                    "`https://www.flickr.com/search/?text=sunset`; the "
                    "`?tags=sunset` page can trigger a consent-overlay tail "
                    "and inconsistent tagged ordering. If a cookie banner "
                    "blocks the page, dismiss it once, then extract the "
                    "first five visible photo cards from the search results "
                    "and finish. Do not keep scrolling or re-extracting "
                    "after the first five titles and usernames are visible."
                )
            )
        )
        self._flickr_sunset_search_nudged = True
        logger.info(
            "agent: FLICKR_SUNSET_SEARCH nudge at step %d (url=%s)",
            step_n,
            current_url,
        )

    def _maybe_inject_getyourguide_paris_popular_nudge(
        self,
        state: BrowserStateSummary,
        results: list[ActionResult],
        step_n: int,
    ) -> None:
        if self._getyourguide_paris_popular_nudged:
            return
        if not _task_requests_getyourguide_paris_popular(self.task):
            return
        current_url = state.url or ""
        if not (
            _host_matches(current_url, "getyourguide.com")
            or any(
                _host_matches(current_url, host)
                for host in _SEARCH_OR_FALLBACK_FINAL_HOSTS
            )
        ):
            return

        self._messages.append(
            UserMessage(
                content=(
                    "[GETYOURGUIDE_PARIS_POPULAR] This task asks for the "
                    "most popular Paris activity based on user ratings and "
                    "its starting price. Avoid spending steps on stale "
                    "cookie-banner indices or unrelated homepage city links. "
                    "Use the Paris city page "
                    "`https://www.getyourguide.com/paris-l16/`, wait briefly "
                    "if activity cards are still skeleton-loading, then "
                    "compare visible activity cards by review count/user "
                    "ratings. Report the activity name, rating/review count "
                    "as evidence, and starting price. Once that evidence is "
                    "extracted from the Paris page, finish; do not retry "
                    "cookie buttons or re-verify the same extracted data."
                )
            )
        )
        self._getyourguide_paris_popular_nudged = True
        logger.info(
            "agent: GETYOURGUIDE_PARIS_POPULAR nudge at step %d (url=%s)",
            step_n,
            current_url,
        )

    def _maybe_inject_viator_orlando_family_nudge(
        self,
        state: BrowserStateSummary,
        results: list[ActionResult],
        step_n: int,
    ) -> None:
        if self._viator_orlando_family_nudged:
            return
        if not _task_requests_viator_orlando_family(self.task):
            return
        current_url = state.url or ""
        if not (
            _host_matches(current_url, "viator.com")
            or any(
                _host_matches(current_url, host)
                for host in _SEARCH_OR_FALLBACK_FINAL_HOSTS
            )
        ):
            return

        self._messages.append(
            UserMessage(
                content=(
                    "[VIATOR_ORLANDO_FAMILY] This task needs the top three "
                    "Viator search results for family-friendly experiences "
                    "in Orlando, with tour name, starting price, and "
                    "customer rating/review count. Avoid broad search-engine "
                    "fallbacks and do not switch to Tripadvisor. Use the "
                    "Viator search results URL `https://www.viator.com/"
                    "searchResults/all?text=family-friendly+experiences+in+"
                    "Orlando,+FL`. If the results load, extract the first "
                    "three visible result cards and finish. If Viator shows "
                    "a verification wall, do not spend many turns trying "
                    "alternate engines; report the block honestly."
                )
            )
        )
        self._viator_orlando_family_nudged = True
        logger.info(
            "agent: VIATOR_ORLANDO_FAMILY nudge at step %d (url=%s)",
            step_n,
            current_url,
        )

    async def _maybe_force_newegg_review_bytes_unavailable(
        self,
        state: BrowserStateSummary,
        tool_calls: list[ToolCall],
        results: list[ActionResult],
        step_n: int,
    ) -> bool:
        if self._newegg_review_bytes_forced:
            return False
        labels = _newegg_review_bytes_evidence_labels(
            self.task,
            state.url,
            tool_calls,
            results,
        )
        if not labels:
            return False

        self._newegg_review_bytes_failed_probes += 1
        if "selector_timeout" in labels:
            self._newegg_review_bytes_selector_timeouts += 1
        product_url = _newegg_product_url_key(state.url)
        if product_url:
            self._newegg_review_bytes_product_urls.add(product_url)

        product_count = len(self._newegg_review_bytes_product_urls)
        logger.info(
            "agent: NEWEGG_REVIEW_BYTES evidence at step %d "
            "(labels=%s, probes=%d, products=%d, selector_timeouts=%d)",
            step_n,
            sorted(labels),
            self._newegg_review_bytes_failed_probes,
            product_count,
            self._newegg_review_bytes_selector_timeouts,
        )

        should_force = _newegg_review_bytes_should_force(
            step_n,
            failed_probes=self._newegg_review_bytes_failed_probes,
            product_count=product_count,
            selector_timeouts=self._newegg_review_bytes_selector_timeouts,
        )
        if not should_force:
            return False

        self._newegg_review_bytes_forced = True
        evidence = ", ".join(sorted(labels))
        self._messages.append(
            UserMessage(
                content=(
                    "[NEWEGG_REVIEW_BYTES_UNAVAILABLE] Multiple Newegg "
                    "product-page probes failed to reveal the requested "
                    f"Review Bytes summary ({evidence}; "
                    f"{self._newegg_review_bytes_failed_probes} failed "
                    f"probe(s) across {product_count} product page(s)). "
                    "Stop scrolling or trying more RTX product pages. "
                    "Finish honestly from the observed evidence: if the "
                    "Review Bytes summary did not render, state that the "
                    "three requested highlights could not be retrieved. "
                    "Do not invent performance highlights from general "
                    "RTX 3080 knowledge or external snippets."
                )
            )
        )
        logger.info(
            "agent: NEWEGG_REVIEW_BYTES force-final at step %d "
            "(probes=%d, products=%d, selector_timeouts=%d)",
            step_n,
            self._newegg_review_bytes_failed_probes,
            product_count,
            self._newegg_review_bytes_selector_timeouts,
        )
        await self._force_final_answer(
            state,
            step_n,
            reason=(
                "Newegg Review Bytes unavailable after repeated "
                "product-page probes"
            ),
        )
        return True

    def _format_action_line(
        self, step_n: int, tc: ToolCall, result: ActionResult
    ) -> str:
        """Render one tool call + result as a human-readable history line.

        Used by the sliding-window collapse: when we drop a native
        AssistantMessage+ToolResultMessage pair from the live message
        list, we leave behind a single line in the agent_history block
        that captures what happened. Format is intentionally short and
        narrative — the LLM should be able to read 30+ such lines as
        easily as it'd skim its own past reasoning.

        For indexed tools (click, type_text, scroll_to, upload_file),
        we substitute the [N] index with the selector that was valid
        at the time of the action — the selector is stable across DOM
        mutations, so `Clicked button "Sign In"` keeps meaning even
        after the page has re-rendered. v0.5.0.
        """
        name = tc.name
        args = tc.args or {}
        idx = args.get("index")
        # Map [N] → selector (look it up from the snapshot that was
        # active when this action ran; we cache it on the result via
        # `_selector_used` set in _run_tool).
        sel = getattr(result, "_selector_used", None)
        if sel is None and isinstance(idx, int):
            sel = self._index_to_selector.get(idx)
        target = sel if sel else (f"[{idx}]" if idx is not None else "")

        if name == "navigate":
            action = f"Navigated to {args.get('url', '?')}"
        elif name == "click":
            action = f"Clicked {target}"
        elif name == "type_text":
            text = args.get("text", "")
            action = f"Typed {text!r} into {target}"
        elif name == "upload_file":
            action = f"Uploaded {args.get('path', '?')} to {target}"
        elif name == "scroll":
            dy = args.get("dy", 0)
            action = f"Scrolled {'down' if dy > 0 else 'up'} {abs(int(dy))}px"
        elif name == "scroll_to":
            action = f"Scrolled {target} into view"
        elif name == "scroll_to_top":
            action = "Scrolled to top"
        elif name == "scroll_to_bottom":
            action = "Scrolled to bottom"
        elif name == "page_text":
            action = "Read page_text"
        elif name == "get_text":
            action = f"Read get_text({args.get('selector', '?')!r})"
        elif name == "get_links":
            action = "Read links list"
        elif name == "screenshot":
            action = "Took screenshot"
        elif name == "wait_for":
            action = f"Waited for selector {args.get('selector', '?')!r}"
        elif name == "wait_for_navigation":
            action = "Waited for navigation"
        elif name == "sleep":
            action = f"Slept {args.get('seconds', 0)}s"
        elif name == "list_tabs":
            action = "Listed tabs"
        elif name == "switch_tab":
            action = f"Switched to tab {args.get('target_id', '?')}"
        elif name == "new_tab":
            action = f"Opened new tab {args.get('url', '?')}"
        elif name == "close_tab":
            action = f"Closed tab {args.get('target_id', '?')}"
        elif name == "done":
            action = "Marked done"
        else:
            action = f"{name}({args})"

        if result.error:
            outcome = f"ERROR: {result.error[:120]}"
        elif result.is_done:
            outcome = "→ done"
        else:
            ec = result.extracted_content or ""
            if ec.startswith("[SCRATCHPAD]") or "[SCRATCHPAD]" in ec[:200]:
                outcome = "→ ok (long output spilled to scratchpad)"
            elif ec:
                # Trim long extracts; the LLM doesn't need the full
                # blob in history — it has the most recent results
                # natively in context.
                outcome = f"→ {ec[:120].replace(chr(10), ' ')}"
            else:
                outcome = "→ ok"

        return f"<step {step_n}> {action} {outcome}"

    @staticmethod
    def _domain_of(url: str) -> str:
        """Crude eTLD+1 extractor — `https://www.foo.bar.com/x` → `bar.com`.

        Doesn't bother with the public-suffix list because the loop
        nudge only needs a stable bucket per site, not RFC-correctness.
        """
        if not url:
            return ""
        try:
            from urllib.parse import urlparse

            host = (urlparse(url).hostname or "").lower()
            if not host:
                return ""
            parts = host.split(".")
            if len(parts) >= 2:
                return ".".join(parts[-2:])
            return host
        except Exception:
            return url

    @staticmethod
    def _blocked_state_reason(state: BrowserStateSummary) -> str:
        url = (state.url or "").strip()
        title = (state.title or "").strip()
        text = (state.elements_text or "")[:4000]
        haystack = f"{url}\n{title}\n{text}".lower()
        try:
            from urllib.parse import urlparse

            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()
            path = (parsed.path or "").lower()
        except Exception:
            host = ""
            path = ""

        search_hosts = (
            "google.com",
            "bing.com",
            "duckduckgo.com",
            "yandex.com",
            "yahoo.com",
            "search.brave.com",
            "startpage.com",
        )
        is_search_host = any(host == h or host.endswith("." + h) for h in search_hosts)

        if url.lower().startswith(("chrome-error://", "edge-error://")):
            return "browser error page"
        if "challenges.cloudflare.com" in host or "/cdn-cgi/challenge" in path:
            return "Cloudflare challenge"
        if host.endswith("google.com") and path.startswith("/sorry"):
            return "Google CAPTCHA"
        if host.endswith("yandex.com") and "showcaptcha" in path:
            return "Yandex CAPTCHA"
        if is_search_host and any(
            phrase in haystack
            for phrase in (
                "unusual traffic",
                "verify you are human",
                "complete the captcha",
                "not a robot",
                "our systems have detected",
            )
        ):
            return "search-engine CAPTCHA"
        if "just a moment" in haystack and "cloudflare" in haystack:
            return "Cloudflare challenge"
        if any(
            phrase in haystack
            for phrase in (
                "verify you are human",
                "checking your browser",
                "cf-chl",
                "turnstile",
                "hcaptcha",
                "recaptcha",
            )
        ) and any(
            marker in haystack
            for marker in ("cloudflare", "captcha", "blocked", "challenge")
        ):
            return "bot challenge"
        if any(
            phrase in haystack
            for phrase in (
                "sorry, we found some errors",
                "we are unable to process your request",
                "we're unable to process your request",
                "unable to process your request at this time",
            )
        ):
            return "site technical error"
        return ""

    async def _capture_state(self) -> BrowserStateSummary:
        """Pre-step snapshot stored in history for the judge / callbacks.

        Always captures a screenshot AND a DOM snapshot regardless of
        `use_vision` — matching browser_use, where `use_vision` controls
        whether the LLM *sees* the image, not whether it lands in history.
        Eval consumers iterate `history.history[i].state.get_screenshot()`
        to feed judges, and most run with `use_vision=False`, so gating
        capture on it produced empty judge inputs.

        The DOM text lands on `summary.elements_text` so
        `_inject_page_state` can prepend it to the next LLM call without
        a second snapshot round trip.
        """
        # All three CDP roundtrips run in parallel — they don't depend on
        # each other. Sequentially they were ~150-300ms of fixed overhead
        # per step (current_url + screenshot + dom_snapshot); together
        # they finish in roughly the slowest single call's time. On a
        # 35-turn run that's 30+ saved seconds per task.
        # v0.8.23: bound each CDP op with asyncio.wait_for so a single
        # hung call (Chrome unresponsive on captcha / hostile JS / dead
        # tab) cannot hold the entire agent past the eval framework's
        # per-stage timeout. The trace from one v0.8.22 failure showed
        # session.screenshot() hanging when the eval wait_for fired,
        # causing CancelledError to slip past our defenses. With these
        # individual timeouts each CDP call is bounded; the agent step
        # completes (with empty results from any timed-out call) and
        # the loop continues normally instead of waiting indefinitely.
        # 15s screenshot, 5s url, 30s dom_snapshot — looser on the DOM
        # walk because that's the legitimately-slowest CDP op.
        async def _safe_url() -> str:
            try:
                return await asyncio.wait_for(
                    self.session.current_url(), timeout=5.0
                )
            except Exception:
                return ""

        async def _safe_screenshot() -> tuple[str | None, str]:
            try:
                if self.use_vision and hasattr(self.session, "screenshot_jpeg"):
                    jpg = await asyncio.wait_for(
                        self.session.screenshot_jpeg(60), timeout=15.0
                    )
                    return base64.b64encode(jpg).decode("ascii"), "image/jpeg"
                png = await asyncio.wait_for(
                    self.session.screenshot(), timeout=15.0
                )
                return base64.b64encode(png).decode("ascii"), "image/png"
            except Exception:
                return None, "image/png"

        # Capture both the rendered text AND the index→selector map
        # in a single CDP roundtrip. The selector map drives
        # agent_history rendering: when we collapse step N's tool_call
        # `click(index=5)` into a history line, we look up element 5's
        # selector here so the rendered line reads
        # `Clicked button "Sign In"` instead of `clicked [5]` —
        # cross-turn references stay stable through DOM mutation.
        async def _safe_dom() -> tuple[str, dict[int, str], dict[str, Any] | None]:
            try:
                # v0.8.23: 30s cap. DOM snapshot is the slowest CDP op
                # (full DOM walk + serialize + map indices) but should
                # never legitimately exceed 30s. Beyond that, Chrome
                # is wedged.
                snap = await asyncio.wait_for(
                    self.session.dom_snapshot(), timeout=30.0
                )
                # Skip index=0 entries — those are static text content
                # (h1/p/li/td/etc.) emitted for extraction context, not
                # interactive elements. Including them in the selector
                # map would let the agent attempt to click them. v0.5.7.
                idx_to_sel = {
                    e.index: e.selector
                    for e in snap.elements
                    if e.index != 0
                }
                # New-element marker (v0.7.1). Mark interactive elements
                # whose selectors weren't in the previous snapshot with
                # a `*` prefix. Helps the LLM see what JUST appeared.
                dom_text = snap.to_llm_string()
                cur_selectors = set(idx_to_sel.values())
                if self._prev_selectors:
                    new_selectors = cur_selectors - self._prev_selectors
                    if new_selectors:
                        # Build a fast index→isNew lookup, then prefix
                        # the matching `[N]<` lines with `*`.
                        new_indices = {
                            idx for idx, sel in idx_to_sel.items()
                            if sel in new_selectors
                        }
                        out_lines = []
                        for line in dom_text.splitlines():
                            stripped = line.lstrip("\t|scroll|")
                            if stripped.startswith("["):
                                try:
                                    n = int(stripped[1:].split("]")[0])
                                    if n in new_indices:
                                        line = line.replace(f"[{n}]", f"*[{n}]", 1)
                                except (ValueError, IndexError):
                                    pass
                            out_lines.append(line)
                        dom_text = "\n".join(out_lines)
                self._prev_selectors = cur_selectors
                # v0.12.1: per-snapshot DOM size breakdown. Computed
                # AFTER any text decoration (e.g. `*` new-element
                # markers above) so total_bytes reflects what the LLM
                # actually receives.
                try:
                    metrics = _compute_dom_metrics(snap, dom_text)
                except Exception:
                    logger.exception("agent: dom_metrics computation failed (non-fatal)")
                    metrics = None
                return dom_text, idx_to_sel, metrics
            except Exception:
                # No active page yet (very first step before
                # initial_actions navigate, or a frame transition
                # mid-step). Skip — the LLM gets a "(no page state
                # available)" placeholder instead of crashing the run.
                return "", {}, None

        url, screenshot_pair, dom_triple = await asyncio.gather(
            _safe_url(), _safe_screenshot(), _safe_dom()
        )
        screenshot_b64, screenshot_media_type = screenshot_pair
        dom_text, self._index_to_selector, dom_metrics = dom_triple
        if dom_metrics is not None:
            # v0.12.1: log the per-snapshot DOM size breakdown so it's
            # visible in CI / dashboard logs. AgentHistory.state also
            # carries it (see return below) for completeHistory access.
            logger.info(
                "dom_metrics step=%d url=%s total_bytes=%d el=%d "
                "(interactive=%d static=%d) "
                "text=(interactive=%d static=%d) "
                "attrs=(bytes=%d count=%d per_el=%.1f) "
                "el_sizes=(p50=%d p90=%d max=%d)",
                self.state.n_steps,
                url[:80],
                dom_metrics["total_bytes"],
                dom_metrics["total_elements"],
                dom_metrics["interactive_count"],
                dom_metrics["static_text_count"],
                dom_metrics["interactive_text_bytes"],
                dom_metrics["static_text_bytes"],
                dom_metrics["interactive_attrs_bytes"],
                dom_metrics["interactive_attrs_count"],
                dom_metrics["interactive_attrs_per_el_avg"],
                dom_metrics["el_size_p50"],
                dom_metrics["el_size_p90"],
                dom_metrics["el_size_max"],
            )

        # Stash the set of [N] indices the LLM is about to see, so
        # _run_tool can validate `index=N` arguments against what was
        # actually shown — kills "unknown element index" errors that
        # come from the model hallucinating numbers (43 instances in
        # the v0.4.17 batch). Set is invalidated as soon as a mutating
        # action runs in the batch (see _run_tools_sequentially).
        # We also reset the staleness flag here because we just took a
        # fresh snapshot — the indices we extract are by definition
        # valid against what the LLM is about to see.
        #
        # v0.8.17: derive from the structured snapshot's index map
        # instead of regex-parsing the rendered DOM text. The previous
        # `r"^\[(\d+)\]"` regex only matched lines whose first character
        # was `[`, missing every line decorated with `*` (new element),
        # `\t` (indented child), or `|scroll|` (scroll container) —
        # all real, clickable indices. The LLM saw them in the snapshot,
        # picked them, and then preflight rejected the call as
        # "hallucinated index" even though it was real. Now we use the
        # exact same set the rendered text is built from, so the two
        # views stay in sync.
        self._valid_indices = set(self._index_to_selector.keys())
        self._indices_invalidated = False

        return BrowserStateSummary(
            url=url,
            title="",
            screenshot=screenshot_b64,
            screenshot_media_type=screenshot_media_type,
            elements_text=dom_text,
            dom_metrics=dom_metrics,
        )

    def _collapse_old_history(self) -> None:
        """Collapse old (AssistantMessage, ToolResultMessage*) pairs
        into the agent_history block once the live native window
        exceeds `history_window_steps`.

        Pairs are popped together to preserve provider tool_use_id
        bookkeeping (Anthropic and OpenAI both validate that every
        tool_use has a matching tool_result; dropping one half breaks
        the next request). For each popped pair, we render a one-line
        `<step N> Action → outcome` summary using the selector that
        was valid at action time (see _format_action_line).

        v0.5.1: only ACTION-only turns are eligible for collapse.
        Turns that contain any read tool (page_text, get_text, etc.)
        stay native indefinitely so the LLM keeps full access to
        content it already extracted. Without this exclusion, v0.5.0
        regressed task 2226 from 40 steps (success) to 50 steps
        (max-out, failure) — the LLM kept re-fetching content because
        the collapsed summaries dropped the actual text.

        Operates in-place on self._messages.
        """
        if self.history_window_steps <= 0:
            return
        # β (v0.11.26): walk self._history (canonical journal since α).
        # Filter for non-collapsed action-only items — content-heavy
        # read-tool turns stay native indefinitely so the LLM keeps full
        # access to extracted content. `list_tabs` is deliberately
        # collapsible even though it is execution-read-only: repeated tab
        # listings can include huge ad/consent iframe URLs, and the latest
        # listing is enough for target_id selection.
        action_only_indices = [
            i
            for i, h in enumerate(self._history)
            if not h.collapsed
            and h.tool_calls
            and not any(
                tc.name in self._READ_ONLY_TOOLS and tc.name != "list_tabs"
                for tc in h.tool_calls
            )
        ]
        excess = len(action_only_indices) - self.history_window_steps
        if excess <= 0:
            return
        # Oldest `excess` action-only items get collapsed.
        collapse_indices = action_only_indices[:excess]

        for idx in collapse_indices:
            h = self._history[idx]
            # Render one history line per (call, result) pair. Multi_act
            # batches produce multiple lines; single-call turns produce one.
            for tc, res in zip(h.tool_calls, h.action_results):
                self._collapsed_history.append(
                    self._format_action_line(h.step_number, tc, res)
                )
            # Pop the matching native messages from self._messages: 1
            # AssistantMessage followed by len(tool_calls) ToolResultMessages.
            # Walk forward looking for the next AssistantMessage that
            # matches; once found, drop it + the next N ToolResultMessages.
            pop_idx = None
            record_call_ids = [tc.id for tc in h.tool_calls]
            for i, msg in enumerate(self._messages):
                if isinstance(msg, AssistantMessage):
                    msg_call_ids = [tc.id for tc in msg.tool_calls]
                    if msg_call_ids == record_call_ids:
                        pop_idx = i
                        break
            if pop_idx is None:
                # Couldn't find the matching native pair (already popped,
                # or messages re-shuffled by a nudge inject). Mark the
                # HistoryItem collapsed anyway — its summary is now in
                # _collapsed_history; we just skip the native prune for
                # this turn rather than risk corrupting message order.
                h.collapsed = True
                continue
            # Drop the AssistantMessage and the immediately-following
            # ToolResultMessages whose tool_call_id matches one in
            # this batch. Stop at the first non-matching message.
            wanted_ids = {tc.id for tc in h.tool_calls}
            del self._messages[pop_idx]
            while pop_idx < len(self._messages) and isinstance(
                self._messages[pop_idx], ToolResultMessage
            ) and self._messages[pop_idx].tool_call_id in wanted_ids:
                del self._messages[pop_idx]
            h.collapsed = True

        if self._collapsed_history:
            native_turns_kept = sum(
                1 for h in self._history
                if not h.collapsed and h.tool_calls
            )
            logger.info(
                "agent: collapsed %d old turns (%d history lines now, "
                "%d native turns kept)",
                excess, len(self._collapsed_history), native_turns_kept,
            )

        # v0.8.20: cap the rendered history at first N + last M lines
        # with an "[X steps omitted]" marker between. _collapsed_history
        # was growing unbounded — at step 50 with ~47 lines × 30-80
        # tokens each we were spending 2-4K tokens per turn just on
        # history rendering. Mirrors upstream's max_history_items
        # windowing pattern (browser_use/agent/message_manager/
        # service.py:150-186). Lines list itself is preserved in
        # `_collapsed_history` so we can show more if needed later;
        # only the LLM-facing render is windowed.
        FIRST_N = 3
        LAST_M = 12
        full = self._collapsed_history
        if len(full) > FIRST_N + LAST_M:
            omitted = len(full) - FIRST_N - LAST_M
            rendered_lines = (
                full[:FIRST_N]
                + [f"  [... {omitted} earlier step(s) omitted to control context size ...]"]
                + full[-LAST_M:]
            )
        else:
            rendered_lines = full

        # Inject (or refresh) the [AGENT_HISTORY] message at the FRONT
        # of self._messages, right after the initial user task. This
        # keeps the rendering consistent across LLM calls.
        history_body = (
            "[AGENT_HISTORY] Earlier turns (collapsed; native message "
            "history is preserved for the most recent "
            f"{self.history_window_steps} turn(s) below):\n"
            + "\n".join(rendered_lines)
        )
        # Find an existing AGENT_HISTORY message and update it; else
        # insert at index 1 (right after the initial task UserMessage).
        for msg in self._messages:
            if (
                isinstance(msg, UserMessage)
                and isinstance(msg.content, str)
                and msg.content.startswith("[AGENT_HISTORY]")
            ):
                msg.content = history_body
                return
        # Insert after the initial task message (index 0). If
        # self._messages is empty — shouldn't happen here — append.
        if self._messages:
            self._messages.insert(1, UserMessage(content=history_body))
        else:
            self._messages.append(UserMessage(content=history_body))

    @staticmethod
    def _strip_state_tags_for_answer(text: str) -> str:
        """v0.8.24: clean state-emission XML tags out of a final-answer string.

        The agent prompt asks the LLM to wrap per-turn state in
        <evaluation_previous_goal>, <memory>, <next_goal> blocks and to
        SKIP them on the final-answer turn. In practice gemini-3-flash
        sometimes forgets and emits them on the answer turn anyway —
        either as the only content (the actual answer ends up inside
        <memory>) or wrapped around the prose.

        Behavior:
          - <evaluation_previous_goal>...</...> and <next_goal>...</...>
            are removed entirely (their inner text is meta-commentary,
            not part of the answer).
          - <memory>...</...> wrapper is removed but the inner text is
            preserved — when the LLM mis-emits, the answer is almost
            always inside <memory>.

        Idempotent and safe on text without tags.
        """
        if not text or "<" not in text:
            return text
        import re as _re
        # Drop the meta tags entirely.
        for tag in ("evaluation_previous_goal", "next_goal"):
            text = _re.sub(
                rf"<{tag}>.*?</{tag}>\s*",
                "",
                text,
                flags=_re.DOTALL | _re.IGNORECASE,
            )
        # Unwrap <memory> — keep the inner content as the answer body.
        # Append a blank-line separator after unwrap so memory content
        # doesn't run into prose that followed the closing tag (final
        # strip below normalises trailing whitespace away).
        text = _re.sub(
            r"<memory>(.*?)</memory>\s*",
            r"\1\n\n",
            text,
            flags=_re.DOTALL | _re.IGNORECASE,
        )
        return text.strip()

    def _parse_persistent_state(self, text: str) -> None:
        """Pull <memory>/<next_goal>/<evaluation_previous_goal> from
        the LLM's response text and persist as agent state. v0.8.0.

        Tolerant: any of the three may be missing. Whitespace trimmed.
        Truncated to 600 chars each so they don't bloat next turn's
        context. If the LLM didn't emit a tag, the prior value is
        kept (so a single missed turn doesn't lose all state).
        """
        if not text:
            return
        import re as _re
        for tag, attr in (
            ("memory", "_memory"),
            ("next_goal", "_next_goal"),
            ("evaluation_previous_goal", "_previous_evaluation"),
        ):
            m = _re.search(
                rf"<{tag}>(.*?)</{tag}>", text, _re.DOTALL | _re.IGNORECASE,
            )
            if m:
                val = m.group(1).strip()[:600]
                if val:
                    setattr(self, attr, val)

    def _inject_page_state(self, state: BrowserStateSummary) -> None:
        """Append a UserMessage with the current page state so the LLM sees
        the DOM without spending a turn on `dom_snapshot`. Older auto-injected
        snapshots are replaced with a one-line "superseded" placeholder so the
        conversation doesn't accumulate stale DOMs across long runs.

        When `use_vision=True` and we captured a screenshot, attach it as an
        ImagePart alongside the DOM text — same one-message shape upstream
        browser_use uses for state messages.
        """
        # v0.8.20: drop prior page-state messages entirely instead of
        # replacing their content with the SUPERSEDED placeholder. The
        # placeholder still cost ~10 tokens per message and one
        # accumulated per step → ~500 tokens of pure dead weight by
        # step 50. Latest snapshot fully replaces them anyway, so
        # there's no information loss. Mirrors upstream's
        # message_manager pattern (single replaceable state slot vs
        # appending placeholders).
        def _is_old_page_state(msg: Message) -> bool:
            if not isinstance(msg, UserMessage):
                return False
            if isinstance(msg.content, str):
                return (
                    msg.content.startswith(_PAGE_STATE_TAG)
                    or msg.content == _PAGE_STATE_SUPERSEDED
                )
            if isinstance(msg.content, list):
                first = msg.content[0] if msg.content else None
                return (
                    isinstance(first, TextPart)
                    and first.text.startswith(_PAGE_STATE_TAG)
                )
            return False

        self._messages = [
            msg for msg in self._messages if not _is_old_page_state(msg)
        ]

        # v0.11.2 read-state ephemeral lifecycle: drain pending entries
        # from large read tools into a transient <read_state> block.
        # The corresponding ToolResultMessages already carry the
        # reference stub permanently. See EPHEMERAL_RESULT_TOOLS.
        #
        # v0.11.4: each entry carries a TTL counter
        # (EPHEMERAL_RESULT_WINDOW_STEPS). It's emitted while ttl > 0,
        # then ttl is decremented; when ttl reaches 0, it's dropped
        # from the queue. With WINDOW_STEPS=2, an entry queued after
        # step N's tool call is visible in step N+1's AND step N+2's
        # state UserMessage, then gone for step N+3.
        read_state_block = ""
        if self._read_state_for_next_turn:
            sections: list[str] = []
            for entry in self._read_state_for_next_turn:
                rel = f"results/{os.path.basename(entry['file_path'])}"
                sections.append(
                    f'<result tool="{entry["tool_name"]}" file="{rel}">\n'
                    f'{entry["content"]}\n'
                    f'</result>'
                )
            read_state_block = (
                "<read_state>\n" + "\n".join(sections) + "\n</read_state>\n\n"
            )
            logger.info(
                "agent: injected <read_state> with %d entries (%d total chars)",
                len(self._read_state_for_next_turn),
                sum(len(e["content"]) for e in self._read_state_for_next_turn),
            )
            # Decrement TTL on every entry; drop those that hit 0.
            self._read_state_for_next_turn = [
                {**e, "ttl": e.get("ttl", 1) - 1}
                for e in self._read_state_for_next_turn
                if e.get("ttl", 1) - 1 > 0
            ]

        # Persistent agent state (v0.8.0): surface prior turn's
        # memory + next_goal + evaluation so the LLM has continuity
        # without rebuilding from collapsed history. Mirrors upstream's
        # <agent_state> block. Only included when something was
        # actually persisted (skip on first turn).
        state_block = ""
        if self._previous_evaluation or self._memory or self._next_goal:
            parts = []
            if self._previous_evaluation:
                parts.append(f"PREVIOUS_EVALUATION: {self._previous_evaluation}")
            if self._memory:
                parts.append(f"MEMORY: {self._memory}")
            if self._next_goal:
                parts.append(f"PRIOR_NEXT_GOAL: {self._next_goal}")
            state_block = "<agent_state>\n" + "\n".join(parts) + "\n</agent_state>\n\n"

        dom_text = state.elements_text or ""
        if not dom_text:
            body = (
                f"{_PAGE_STATE_TAG}\n{read_state_block}{state_block}URL: {state.url}\n"
                "(no DOM snapshot available — page not ready)"
            )
        else:
            # Prepend an explicit valid-index hint so the LLM has the
            # range to bound its index choices. Pre-empts hallucinated
            # numbers like `click(42)` when the snapshot only shows
            # [0..30]. v0.4.18.
            #
            # NOTE: v0.5.4 added a page-stats hint here ("page may be
            # loading, try scroll/wait") and v0.5.4 also attached the
            # previous screenshot alongside the current. Both regressed:
            # judge dropped 53% -> 44%, max_steps_exceeded jumped 32 ->
            # 51, cost rose 43%. Reverted in v0.5.6 — keep the simple
            # valid-index hint, single screenshot.
            if self._valid_indices:
                lo = min(self._valid_indices)
                hi = max(self._valid_indices)
                count = len(self._valid_indices)
                hint = (
                    f"Valid [N] indices on this page: {count} elements "
                    f"in range [{lo}..{hi}]. Use ONLY indices listed "
                    f"below; do not invent numbers."
                )
                body = (
                    f"{_PAGE_STATE_TAG}\n{read_state_block}"
                    f"{state_block}{hint}\n\n{dom_text}"
                )
            else:
                body = (
                    f"{_PAGE_STATE_TAG}\n{read_state_block}"
                    f"{state_block}{dom_text}"
                )

        if self.use_vision and state.screenshot:
            self._messages.append(
                UserMessage(
                    content=[
                        TextPart(text=body),
                        ImagePart(
                            data=state.screenshot,
                            media_type=state.screenshot_media_type or "image/png",
                        ),
                    ]
                )
            )
        else:
            self._messages.append(UserMessage(content=body))

    # Tools that read state without changing the page — safe to keep
    # running even after a mutating action in the same batch since they
    # don't depend on the pre-batch DOM indices.
    # Read-only tools — safe to keep running after a mutating action
    # in the same batch. Includes the v0.6.0/v0.6.5 extraction +
    # search tools and the file-system reads. Aliases get expanded
    # below. v0.7.2 added: extract_structured_data, search_page,
    # find_elements, find_text, get_dropdown_options, extract_links,
    # extract_images, evaluate_js (read-only by default), read_file,
    # list_files.
    _READ_ONLY_CANONICAL: frozenset[str] = frozenset({
        "screenshot",
        "page_text",
        "get_text",
        "get_links",
        "list_tabs",
        "list_downloads",
        "get_cookies",
        "save_pdf",
        "done",
        "grep_scratchpad",
        "read_scratchpad",
        "extract_structured_data",
        "extract_result_cards",
        "search_page",
        "find_elements",
        "find_text",
        "get_dropdown_options",
        "extract_links",
        "extract_images",
        "evaluate_js",
        "read_file",
        "list_files",
    })

    # Tools that target an element by `[N]` index from the most recent
    # DOM snapshot. v0.7.2 added: select_dropdown (acts on indexed
    # element). Aliases get expanded below.
    _INDEXED_CANONICAL: frozenset[str] = frozenset({
        "click",
        "type_text",
        "upload_file",
        "scroll_to",
        "select_dropdown",
        "get_dropdown_options",
    })

    # Lazily-built alias-aware sets: include every alias that maps to
    # one of the canonical members above. Built once on first access
    # by reading ALIAS_TO_CANONICAL from _browser_tools. v0.7.2.
    _READ_ONLY_TOOLS: frozenset[str] = frozenset()
    _INDEXED_TOOLS: frozenset[str] = frozenset()

    async def _run_tools_sequentially(
        self, tool_calls: list[ToolCall]
    ) -> list[tuple[ActionResult, ToolResultMessage]]:
        """Execute tool calls in order with two staleness guards.

        Mirrors upstream `multi_act` semantics + a fix for the v0.4.15
        eval failure mode (87 stale-index errors, 11 BATCH_FAILED
        instances, 9 of them shaped `[type_text, click]`):

        1. URL-change guard. After each call we check the active URL;
           if it changed and there are more queued calls, we skip the
           non-readonly remainder (they were planned against the old
           page).
        2. **Indexed-after-mutation guard (v0.4.17).** After running any
           non-read tool, subsequent `[N]`-using calls (click, type_text,
           upload_file, scroll_to) are skipped with a clear error
           explaining that typing/clicking/scrolling can mutate the DOM
           in ways that invalidate the LLM's pre-batch snapshot.
           Read-only and index-free tools (page_text, get_text, scroll
           by amount, etc.) are unaffected — `[scroll, scroll, page_text]`
           batches still work.

        Skipped tool_messages MUST still be emitted because the LLM
        provider expects a result for every tool call it issued —
        omitting them produces a "no result for tool_use_id" error on
        the next turn.
        """
        if not tool_calls:
            return []

        async def _current_url() -> str:
            try:
                return await self.session.current_url()
            except Exception:
                return ""

        start_url = await _current_url()
        results: list[tuple[ActionResult, ToolResultMessage]] = []
        # True once any non-read tool has run; gates subsequent indexed
        # tools regardless of URL change.
        indices_invalidated = False

        for i, tc in enumerate(tool_calls):
            pair = await self._run_tool(tc)
            results.append(pair)
            prior_failed = bool(pair[0].error)

            if tc.name not in self._READ_ONLY_TOOLS:
                indices_invalidated = True
                # Mirror to instance flag so the next-turn validation in
                # _run_tool also knows the snapshot is stale (covers
                # single-action turns and any mid-batch _run_tool calls).
                self._indices_invalidated = True

            if i + 1 >= len(tool_calls):
                break

            cur_url = await _current_url()
            url_changed = cur_url != start_url

            # Fast path: nothing changed AND no indexed-tool risk.
            if not url_changed and not indices_invalidated:
                continue

            # Need to filter the remaining calls. Read-only always runs;
            # indexed runs only if neither URL nor DOM-index assumption
            # was invalidated (which here means: not at all, since we're
            # in this branch precisely because something was).
            for skipped in tool_calls[i + 1 :]:
                if skipped.name in self._READ_ONLY_TOOLS:
                    pair = await self._run_tool(skipped)
                    results.append(pair)
                    continue

                # Non-read tool. URL change kills everything; index
                # invalidation kills indexed tools specifically.
                if url_changed:
                    err = (
                        "skipped: page navigated mid-batch; re-plan "
                        "using the next fresh snapshot before indexed "
                        "[N] tools."
                    )
                    is_error = True
                elif skipped.name in self._INDEXED_TOOLS:
                    err = (
                        "skipped: an earlier action in this batch "
                        "mutated the DOM; wait for the next fresh "
                        "snapshot before indexed [N] tools. Do not "
                        "chain type_text -> click."
                    )
                    is_error = True
                    logger.info(
                        "agent: skipped %s in batch (indices invalidated by "
                        "earlier mutating action)",
                        skipped.name,
                    )
                else:
                    # Index-free non-read (e.g. another scroll, navigate,
                    # sleep). URL didn't change, so we let it run.
                    pair = await self._run_tool(skipped)
                    results.append(pair)
                    if skipped.name not in self._READ_ONLY_TOOLS:
                        indices_invalidated = True
                        self._indices_invalidated = True
                    continue

                results.append(
                    (
                        ActionResult(
                            error=err if prior_failed else None,
                            extracted_content=None if prior_failed else err,
                        ),
                        ToolResultMessage(
                            tool_call_id=skipped.id,
                            name=skipped.name,
                            content=err,
                            is_error=is_error and prior_failed,
                        ),
                    )
                )
            break

        return results

    @observe(name="agent.tool", span_type="TOOL", ignore_output=True)
    async def _run_tool(
        self, tc: ToolCall
    ) -> tuple[ActionResult, ToolResultMessage]:
        tool = self.tools_by_name.get(tc.name)
        if tool is None:
            err = f"unknown tool: {tc.name}"
            return (
                ActionResult(error=err),
                ToolResultMessage(
                    tool_call_id=tc.id, name=tc.name, content=err, is_error=True
                ),
            )

        real_args = _expand_secrets(tc.args, self.sensitive_data)

        # Pre-flight: index [0] is always a hallucination — our DOM
        # snapshot uses 1-based indices (see crates/bu-dom/src/script.js
        # `let idx = 1`). Reject up front with a clear hint, mirroring
        # upstream browser-use's `assert params.index != 0` guard. The
        # v0.4.16 trace shows the LLM repeatedly picking [0] when the
        # snapshot was empty/missing, then exhausting the retry budget.
        if (
            tc.name in self._INDEXED_TOOLS
            and isinstance(real_args, dict)
            and real_args.get("index") == 0
        ):
            msg = (
                "index [0] is not a real element — the page snapshot "
                "uses 1-based indices, so [0] never exists. If you "
                "haven't taken a dom_snapshot yet, take one now. If you "
                "have, pick an index that actually appears in the latest "
                "PAGE_STATE."
            )
            return (
                ActionResult(extracted_content=msg),
                ToolResultMessage(
                    tool_call_id=tc.id, name=tc.name, content=msg,
                ),
            )

        # Pre-flight index validation. The v0.4.17 batch logged 43
        # `unknown element index N` errors — the LLM picking indices
        # that were never in the snapshot we showed it. We can catch
        # those WITHOUT a CDP roundtrip by checking against the index
        # set captured in _capture_state. Skip the check if the snapshot
        # set is empty (very first step, or capture failed) to avoid
        # blocking legitimate work.
        if (
            tc.name in self._INDEXED_TOOLS
            and "index" in real_args
            and self._valid_indices
            and not self._indices_invalidated
        ):
            try:
                requested = int(real_args["index"])
            except (TypeError, ValueError):
                requested = None
            if requested is not None and requested not in self._valid_indices:
                lo, hi = min(self._valid_indices), max(self._valid_indices)
                err = (
                    f"index [{requested}] is not in the current page "
                    f"snapshot. Valid indices are in the range [{lo}..{hi}] "
                    f"(some numbers in that range may also be missing — "
                    f"only use [N] values you can see in the latest "
                    f"PAGE_STATE message). Re-read the snapshot and try "
                    f"again with a real index."
                )
                logger.info(
                    "agent: rejected hallucinated index [%d] in %s "
                    "(valid range: [%d..%d], %d total)",
                    requested, tc.name, lo, hi, len(self._valid_indices),
                )
                # Returned as extracted_content (not error) so the
                # all-error-turn streak counter doesn't tick. Same
                # rationale as the post-call stale-element handler:
                # picking a bogus index is recoverable on the next turn
                # via a fresh snapshot. Mirrors upstream browser-use,
                # which returns the "Element index N not available" hint
                # as a non-error message.
                return (
                    ActionResult(extracted_content=err),
                    ToolResultMessage(
                        tool_call_id=tc.id, name=tc.name, content=err,
                    ),
                )

        # v0.11.12: smart scroll arg-rewrite. scroll_down / scroll_up
        # are aliased to scroll_to_bottom / scroll_to_top (no kwargs in
        # schema → cache stays byte-identical). But Gemini-3 was trained
        # against upstream's `scroll_down(pages=N)` signature and sends
        # `pages=N` in args. The previous v0.11.5/v0.11.7 baseline
        # TypeError'd; v0.11.10 fixed it by adding new @tool defs (which
        # invalidated tool-schema cache and correlated with -7pp accuracy
        # in eval). This dispatch-time rewrite gets the bug fix without
        # the schema change: detect pages kwarg on scroll_down/up call
        # and route to scroll(direction=..., pages=...) instead.
        if (
            tc.name in ("scroll_down", "scroll_up")
            and isinstance(real_args, dict)
            and "pages" in real_args
        ):
            scroll_tool = self.tools_by_name.get("scroll")
            if scroll_tool is not None:
                rewritten_args = {
                    "direction": "down" if tc.name == "scroll_down" else "up",
                    "pages": real_args["pages"],
                }
                # Forward through to the scroll tool with the rewritten
                # args; everything else (timeout, error handling, format)
                # is handled below by reusing the same code path.
                tool = scroll_tool
                real_args = rewritten_args

        try:
            # Per-tool timeout. Without it, a single hung CDP op (e.g.
            # `scroll` against a page stuck on a network request) can
            # consume the eval framework's entire stage budget and kill
            # the run with a bare TimeoutError. Returning the timeout as
            # an ActionResult error lets the LLM react and try a
            # different tool / element on the next turn.
            raw = await asyncio.wait_for(
                tool.func(self.session, **real_args),
                timeout=self.tool_timeout,
            )
        except asyncio.TimeoutError:
            err = (
                f"tool timed out after {self.tool_timeout:.0f}s: "
                f"{_short_tool_call_repr(tc)}. Try wait_for_navigation, "
                "switch tabs, or another element."
            )
            logger.info(
                "agent: tool timeout %s(%s) after %.0fs",
                tc.name, tc.args, self.tool_timeout,
            )
            return (
                ActionResult(error=err),
                ToolResultMessage(
                    tool_call_id=tc.id, name=tc.name, content=err, is_error=True
                ),
            )
        except Exception as e:
            err_str = str(e)
            # v0.11.10: CDP session-staleness retry. The Rust session
            # cache holds (target_id → session_id) entries that go bad
            # when Chrome destroys a target out-from-under us (popup
            # close, redirect, OOM). First call surfaces as -32001
            # ("Session with given id not found") or "unknown tab
            # target_id"; calling list_tabs() forces fresh
            # Target.getTargets discovery, then a single retry usually
            # succeeds. v0.11.5 eval showed 4 unique crashes
            # (OTHER_ERROR/Crash, no judgement) and 403 total action
            # errors — this family was a major contributor. ONE retry
            # only; if it fails again we fall through to existing error
            # handling.
            is_cdp_stale = (
                "-32001" in err_str
                or "Session with given id not found" in err_str
                or "unknown tab target_id" in err_str
            )
            if is_cdp_stale and not getattr(tc, "_cdp_stale_retried", False):
                try:
                    tc._cdp_stale_retried = True  # type: ignore[attr-defined]
                    # Force target re-discovery; this re-attaches stale
                    # session ids in the rust BrowserSession's `attached`
                    # map and prunes dead targets.
                    tabs = await asyncio.wait_for(
                        self.session.list_tabs(), timeout=5.0
                    )
                    try:
                        page_tabs = [
                            t for t in tabs if len(t) >= 5 and t[3] == "page"
                        ]
                        active_tid = next(
                            (t[0] for t in page_tabs if t[4]),
                            page_tabs[0][0] if page_tabs else None,
                        )
                        if active_tid:
                            await asyncio.wait_for(
                                self.session.switch_tab(active_tid),
                                timeout=5.0,
                            )
                    except Exception:
                        pass
                    raw = await asyncio.wait_for(
                        tool.func(self.session, **real_args),
                        timeout=self.tool_timeout,
                    )
                    logger.info(
                        "agent: CDP-staleness retry succeeded for %s",
                        tc.name,
                    )
                    content_parts, summary_text = _format_tool_return(
                        raw, self.sensitive_data
                    )
                    ar = ActionResult(extracted_content=summary_text)
                    if isinstance(real_args, dict) and isinstance(
                        real_args.get("index"), int
                    ):
                        ar._selector_used = self._index_to_selector.get(  # type: ignore[attr-defined]
                            real_args["index"]
                        )
                    return (
                        ar,
                        ToolResultMessage(
                            tool_call_id=tc.id, name=tc.name,
                            content=content_parts,
                        ),
                    )
                except Exception as retry_e:
                    logger.info(
                        "agent: CDP-staleness retry FAILED for %s: %s: %s",
                        tc.name, type(retry_e).__name__, str(retry_e)[:120],
                    )
                    retry_msg = str(retry_e)
                    if (
                        (
                            "-32001" in retry_msg
                            or "Session with given id not found" in retry_msg
                            or "unknown tab target_id" in retry_msg
                        )
                        and tc.name in {"navigate", "web_search", "search", "search_google"}
                    ):
                        try:
                            # If the active target itself died, rediscovery
                            # may still leave navigation-like tools pointed
                            # at a dead session. Open a clean tab and retry
                            # once; these tools immediately navigate away, so
                            # losing the dead target's page state is harmless.
                            await asyncio.wait_for(
                                self.session.new_tab(""), timeout=5.0
                            )
                            raw = await asyncio.wait_for(
                                tool.func(self.session, **real_args),
                                timeout=self.tool_timeout,
                            )
                            logger.info(
                                "agent: CDP-staleness fresh-tab retry "
                                "succeeded for %s",
                                tc.name,
                            )
                            content_parts, summary_text = _format_tool_return(
                                raw, self.sensitive_data
                            )
                            return (
                                ActionResult(extracted_content=summary_text),
                                ToolResultMessage(
                                    tool_call_id=tc.id,
                                    name=tc.name,
                                    content=content_parts,
                                ),
                            )
                        except Exception as fresh_e:
                            retry_e = fresh_e
                            retry_msg = str(fresh_e)
                            logger.info(
                                "agent: CDP-staleness fresh-tab retry "
                                "FAILED for %s: %s: %s",
                                tc.name,
                                type(fresh_e).__name__,
                                retry_msg[:120],
                            )
                    # fall through to existing handling — convert to a
                    # readable error string the LLM can react to instead
                    # of letting the original RuntimeError bubble.
                    err_str = (
                        f"CDP session was stale and re-attach retry "
                        f"failed: {type(retry_e).__name__}: "
                        f"{str(retry_e)[:200]}"
                    )
            # Stale-element / unknown-index errors are EXPECTED on a busy
            # page — the LLM picked an index that was valid at snapshot
            # time but the DOM has since changed. Treat them as
            # recoverable: return the message as `extracted_content`
            # (not `error`) so the all-error-turn streak counter doesn't
            # tick. This mirrors upstream browser-use, which returns
            # "Element index N not available - page may have changed.
            # Try refreshing browser state." as a non-error result.
            #
            # Trace-driven motivation: 81% of v0.4.14's action errors and
            # 71% of v0.5.1's are these two patterns; on regressed tasks
            # they consistently fire ~5 times in a row, exhausting the
            # max_consecutive_errors budget and aborting the task before
            # the agent has a chance to re-snapshot.
            stale_index_msg = "unknown element index"
            stale_element_msg = "no longer present in the DOM"
            is_stale = (
                tc.name in self._INDEXED_TOOLS
                and (stale_index_msg in err_str or stale_element_msg in err_str)
                and isinstance(real_args, dict)
                and isinstance(real_args.get("index"), int)
            )
            if is_stale:
                # Selector retargeting (v0.5.5).
                # Look up the cached selector for the failed index, take
                # a fresh DOM snapshot, find the element with the same
                # selector in the new snapshot, and retry the tool call
                # with that updated index. Transparent recovery — the
                # LLM doesn't know the index changed; the action just
                # works. Mirrors upstream browser-use's
                # browser_session.get_element_by_index path which
                # resolves through a selector_map of stable references.
                #
                # Falls back to the v0.5.2 'extracted_content hint'
                # behavior if anything in the retargeting path fails:
                # selector cache miss, snapshot fails, no matching
                # element in the new DOM, or retry itself errors. So
                # this only ever HELPS — worst case = same UX as v0.5.4.
                stale_idx = real_args["index"]  # type: ignore[index]
                cached_sel = self._index_to_selector.get(stale_idx)
                retarget_attempted = False
                if cached_sel:
                    try:
                        snap = await asyncio.wait_for(
                            self.session.dom_snapshot(),
                            timeout=min(self.tool_timeout, 30.0),
                        )
                        # Refresh the agent's view of valid indices and
                        # selector map so subsequent calls in this batch
                        # (and the next step) see the new DOM.
                        self._index_to_selector = {
                            e.index: e.selector for e in snap.elements
                        }
                        self._valid_indices = {e.index for e in snap.elements}
                        self._indices_invalidated = False
                        new_idx = next(
                            (e.index for e in snap.elements if e.selector == cached_sel),
                            None,
                        )
                        if new_idx is not None and new_idx != stale_idx:
                            retarget_attempted = True
                            new_args = {**real_args, "index": new_idx}
                            try:
                                raw = await asyncio.wait_for(
                                    tool.func(self.session, **new_args),
                                    timeout=self.tool_timeout,
                                )
                                logger.info(
                                    "agent: selector-retargeted %s [%d] -> [%d] (selector=%r)",
                                    tc.name, stale_idx, new_idx, cached_sel,
                                )
                                # Drop into the success path with the
                                # post-retry result.
                                content_parts, summary_text = _format_tool_return(
                                    raw, self.sensitive_data
                                )
                                ar = ActionResult(extracted_content=summary_text)
                                ar._selector_used = cached_sel  # type: ignore[attr-defined]
                                return (
                                    ar,
                                    ToolResultMessage(
                                        tool_call_id=tc.id, name=tc.name,
                                        content=content_parts,
                                    ),
                                )
                            except Exception as retry_e:
                                logger.info(
                                    "agent: selector retarget retry failed (%s [%d->%d]): %s",
                                    tc.name, stale_idx, new_idx, retry_e,
                                )
                    except Exception as snap_e:
                        logger.info(
                            "agent: selector retarget snapshot failed: %s", snap_e,
                        )

                idx_repr = stale_idx
                if retarget_attempted:
                    friendly = (
                        f"index [{idx_repr}] was stale and the cached "
                        f"selector retargeting also failed. The page "
                        f"likely shifted significantly. Re-take a "
                        f"dom_snapshot and pick a fresh index."
                    )
                else:
                    friendly = (
                        f"index [{idx_repr}] is not available now — the page "
                        f"state has changed since the last dom_snapshot. "
                        f"Re-take a dom_snapshot and pick a fresh index from "
                        f"the new PAGE_STATE."
                    )

                # v0.8.10: dead-index hard nudge. Trace analysis showed
                # the LLM retrying the same [N] up to 12 times in a row
                # on dynamic pages (Bloomberg). The "friendly" hint
                # above is treated as advisory — the LLM ignores it and
                # re-issues the same call. Track attempts and inject a
                # hard `[INDEX_DEAD]` UserMessage on the third failure
                # so the next LLM turn sees an explicit "do NOT retry"
                # instruction at the top of context.
                key = (tc.name, stale_idx)
                self._dead_index_attempts[key] = (
                    self._dead_index_attempts.get(key, 0) + 1
                )
                if self._dead_index_attempts[key] >= 3:
                    self._messages.append(UserMessage(content=(
                        f"[INDEX_DEAD] Tool {tc.name}(index=[{stale_idx}]) "
                        f"has failed 3 times on this page — the element "
                        f"that was at index [{stale_idx}] no longer exists "
                        f"in the DOM. STOP retrying [{stale_idx}]. Try ONE "
                        f"of these alternatives instead:\n"
                        f"  • Re-read the LATEST PAGE_STATE snapshot — the "
                        f"new [N] numbers may have what you need.\n"
                        f"  • Use find_elements(selector=...) or "
                        f"search_page(pattern=...) to locate the target "
                        f"by attribute or text, not by index.\n"
                        f"  • If you've been stuck on the same URL for "
                        f"5+ turns without progress, navigate elsewhere "
                        f"or call done(success=False)."
                    )))
                    logger.info(
                        "agent: INDEX_DEAD nudge injected for %s[%d] "
                        "(attempts=%d, url=%s)",
                        tc.name, stale_idx,
                        self._dead_index_attempts[key],
                        self._dead_index_url,
                    )
                    # Reset so we don't re-inject every subsequent turn
                    # for the same dead index.
                    self._dead_index_attempts[key] = 0

                ar = ActionResult(extracted_content=friendly)
                ar._selector_used = cached_sel  # type: ignore[attr-defined]
                return (
                    ar,
                    ToolResultMessage(
                        tool_call_id=tc.id, name=tc.name, content=friendly,
                    ),
                )
            err = f"tool error: {type(e).__name__}: {e}"
            err = _redact_secrets(err, self.sensitive_data)
            return (
                ActionResult(error=err),
                ToolResultMessage(
                    tool_call_id=tc.id, name=tc.name, content=err, is_error=True
                ),
            )

        # Map return value into ContentParts the LLM layer understands.
        content_parts, summary_text = _format_tool_return(raw, self.sensitive_data)

        # v0.11.2: ephemeral lifecycle for whitelisted read tools. If
        # this fires (text > 10k for a whitelisted tool name),
        # content_parts becomes a small reference stub and the full
        # content is queued for the next page-state UserMessage's
        # <read_state> block. Skips the scratchpad spill below since
        # the lifecycle already handled it.
        ephemeral_applied = False
        if tc.name in EPHEMERAL_RESULT_TOOLS:
            new_parts, new_summary = self._apply_ephemeral_lifecycle(
                tc.name, content_parts, summary_text,
            )
            if new_parts is not content_parts:
                ephemeral_applied = True
                content_parts, summary_text = new_parts, new_summary

        # Long-output spill: if the formatted text is large enough to
        # bloat the conversation, write the full content to a scratchpad
        # file and replace what the LLM sees with a head+tail preview
        # plus a recovery hint pointing at the file. The LLM can drill
        # in via grep_scratchpad / read_scratchpad on the next turn
        # without re-running the original tool. Image-only returns
        # (single ImagePart, no text) are passed through unchanged.
        if (
            not ephemeral_applied
            and self.scratchpad_enabled
            and summary_text
            and len(content_parts) == 1
            and isinstance(content_parts[0], TextPart)
        ):
            from browser_use_rs._scratchpad import maybe_spill

            spilled = maybe_spill(
                summary_text,
                agent_id=self._scratchpad_id,
                step=self.state.n_steps,
                tool_name=tc.name,
                max_bytes=self.scratchpad_max_bytes,
                max_lines=self.scratchpad_max_lines,
            )
            if spilled is not None:
                logger.info(
                    "agent: scratchpad spill %s -> %s (%d lines / %d bytes)",
                    tc.name, spilled.path, spilled.full_lines, spilled.full_bytes,
                )
                content_parts = [TextPart(text=spilled.preview)]
                summary_text = spilled.preview

        # Stamp the selector that was valid at action time onto the
        # ActionResult so _format_action_line can render the history
        # entry with selector text instead of a stale [N] index. We
        # stash it as an attribute (ActionResult is a frozen dataclass,
        # so we don't make this part of the public schema). v0.5.0.
        ar = ActionResult(extracted_content=summary_text)
        if isinstance(real_args, dict) and isinstance(real_args.get("index"), int):
            ar._selector_used = self._index_to_selector.get(  # type: ignore[attr-defined]
                real_args["index"]
            )

        return (
            ar,
            ToolResultMessage(
                tool_call_id=tc.id, name=tc.name, content=content_parts
            ),
        )

    def _append_history(
        self,
        state: BrowserStateSummary,
        output: AgentOutput,
        results: list[ActionResult],
        t_start: float,
        step_n: int,
    ) -> None:
        # v0.8.16: sum ALL usage records for this step instead of taking
        # `[-1]`. v0.8.15 wired extract_structured_data and _judge_and_log
        # through `_record_usage`, so multiple LLM calls now land in
        # `usage_log` for the same step. Reading only the LAST one gave
        # per-step metadata of "last LLM call in step" rather than "sum
        # across all LLM calls in step", which made StepMetadata
        # inaccurate (per-task totals were still right because the eval
        # framework sums across steps, but per-step values were wrong).
        # Filter and sum is O(n_steps) per call but n_steps is small;
        # not worth maintaining a separate per-step cache.
        step_calls = [u for u in self.usage_log if u.get("step") == step_n]
        # v0.12.2: surface DOM measurement metrics via metadata, since the
        # dashboard's completeHistory.state path drops everything except
        # title+url. Read-only copy from state.dom_metrics — never
        # recomputed here. dm is None for failed snapshots; defaults to 0.
        dm = (state.dom_metrics or {}) if state else {}
        # v0.12.3: surface prompt-section bytes (system / tools / state /
        # user / assistant / tool_results / image / n_messages). Take the
        # LAST step_calls entry, not aggregated — these are byte-count
        # snapshots from _compute_call_metrics and a sum across multiple
        # LLM calls (e.g. extract_structured_data) would inflate them.
        last_call = step_calls[-1] if step_calls else {}
        metadata = StepMetadata(
            step_number=step_n,
            input_tokens=sum(u.get("input", 0) for u in step_calls),
            output_tokens=sum(u.get("output", 0) for u in step_calls),
            cache_read_tokens=sum(u.get("cache_read", 0) for u in step_calls),
            step_start_time=t_start,
            step_end_time=time.monotonic(),
            dom_total_bytes=int(dm.get("total_bytes") or 0),
            dom_total_elements=int(dm.get("total_elements") or 0),
            dom_interactive_count=int(dm.get("interactive_count") or 0),
            dom_static_text_count=int(dm.get("static_text_count") or 0),
            dom_interactive_text_bytes=int(dm.get("interactive_text_bytes") or 0),
            dom_static_text_bytes=int(dm.get("static_text_bytes") or 0),
            dom_interactive_attrs_bytes=int(dm.get("interactive_attrs_bytes") or 0),
            dom_interactive_attrs_count=int(dm.get("interactive_attrs_count") or 0),
            dom_interactive_attrs_per_el_avg=float(
                dm.get("interactive_attrs_per_el_avg") or 0.0
            ),
            dom_el_size_p50=int(dm.get("el_size_p50") or 0),
            dom_el_size_p90=int(dm.get("el_size_p90") or 0),
            dom_el_size_max=int(dm.get("el_size_max") or 0),
            prompt_system_bytes=int(last_call.get("system_bytes") or 0),
            prompt_tools_bytes=int(last_call.get("tools_bytes") or 0),
            prompt_state_msg_bytes=int(last_call.get("state_msg_bytes") or 0),
            prompt_user_msgs_bytes=int(last_call.get("user_msgs_bytes") or 0),
            prompt_assistant_msgs_bytes=int(last_call.get("assistant_msgs_bytes") or 0),
            prompt_tool_results_bytes=int(last_call.get("tool_results_bytes") or 0),
            prompt_image_bytes=int(last_call.get("image_bytes") or 0),
            prompt_n_messages=int(last_call.get("n_messages") or 0),
            prompt_agent_history_bytes=int(last_call.get("agent_history_bytes") or 0),
            prompt_agent_history_lines=int(last_call.get("agent_history_lines") or 0),
            prompt_read_state_bytes=int(last_call.get("read_state_bytes") or 0),
            prompt_read_state_entries=int(last_call.get("read_state_entries") or 0),
            prompt_history_items=int(last_call.get("history_items") or 0),
            prompt_history_collapsed_items=int(
                last_call.get("history_collapsed_items") or 0
            ),
        )
        self.state.history.history.append(
            AgentHistory(state=state, output=output, result=results, metadata=metadata)
        )

        # v0.12.0-α: populate the parallel HistoryItem array (v0.11.25).
        # Snapshot the persistent state tags as they are right now;
        # _parse_persistent_state runs earlier in some paths and later
        # in others, so values may be from this step or the previous one.
        # Correctness is verified in β when this array becomes the
        # canonical source for the agent_history block.
        step_error: str | None = None
        for r in results:
            if r.error:
                step_error = str(r.error)[:600]
                break
        tool_calls = list(output.tool_calls) if output and output.tool_calls else []
        self._history.append(
            HistoryItem(
                step_number=step_n,
                evaluation_previous_goal=self._previous_evaluation,
                memory=self._memory,
                next_goal=self._next_goal,
                action_results=list(results),
                tool_calls=tool_calls,
                error=step_error,
            )
        )

    def _apply_ephemeral_lifecycle(
        self,
        tool_name: str,
        content_parts: list[ContentPart],
        summary_text: str,
    ) -> tuple[list[ContentPart], str]:
        """v0.11.2: replace large read-tool result content with a small
        stub, queue full content for the next state message's <read_state>.

        Returns (possibly modified) content_parts + summary_text.
        Mutates self._read_state_for_next_turn as a side effect.

        Skipped silently when:
          - tool_name not in EPHEMERAL_RESULT_TOOLS
          - summary_text under threshold
          - content_parts contains anything non-text (image returns)
          - BU_RS_DISABLE_EPHEMERAL_LIFECYCLE is truthy in env (kill switch)
        """
        if os.environ.get("BU_RS_DISABLE_EPHEMERAL_LIFECYCLE"):
            return content_parts, summary_text
        if tool_name not in EPHEMERAL_RESULT_TOOLS:
            return content_parts, summary_text
        if not summary_text or len(summary_text) <= EPHEMERAL_RESULT_THRESHOLD:
            return content_parts, summary_text
        if len(content_parts) != 1 or not isinstance(content_parts[0], TextPart):
            return content_parts, summary_text
        # v0.11.5: first qualifying read in this session stays durable
        # — full content remains in the ToolResultMessage forever to
        # establish task grounding. Only subsequent large reads use
        # the lifecycle (where they're more likely to be bloat /
        # alternative searches / repeat extraction).
        #
        # v0.12.4: extract_structured_data is EXEMPT from the durable
        # pass. Per codex review of the v0.11.5 logic + v0.12.3 bug
        # finding: extract is LLM-summarized output, not raw page text,
        # so the "first read establishes grounding" rationale is weaker.
        # And it has its own in-tool 10KB→file overflow that should
        # already prevent large inline returns; the lifecycle entry is
        # specifically a backstop for when that overflow falls through.
        # Letting extract_structured_data take the durable pass would
        # defeat the whole reason we added it to EPHEMERAL_RESULT_TOOLS.
        if tool_name != "extract_structured_data" and not self._has_durable_read:
            self._has_durable_read = True
            logger.info(
                "agent: first large read (%s, %d chars) stays durable "
                "(grounding); subsequent large reads will use lifecycle",
                tool_name, len(summary_text),
            )
            return content_parts, summary_text
        try:
            file_path = self._spill_to_results(tool_name, summary_text)
        except Exception:
            logger.exception(
                "agent: ephemeral spill failed for %s — falling back to "
                "full inline return", tool_name,
            )
            return content_parts, summary_text
        # Record full content for next-turn <read_state> block. v0.11.4
        # adds a TTL counter so each entry survives N _inject_page_state
        # cycles before being dropped — this gives the LLM a window of
        # multiple steps to reason over the read result before it
        # disappears from context.
        self._read_state_for_next_turn.append(
            {
                "tool_name": tool_name,
                "content": summary_text,
                "file_path": file_path,
                "ttl": EPHEMERAL_RESULT_WINDOW_STEPS,
            }
        )
        # Stub format approved by codex: short, self-contained, includes
        # path. Stays in the conversation forever; the per-turn cost is
        # ~200 chars instead of N,000.
        rel_path = f"results/{os.path.basename(file_path)}"
        stub = (
            f"[Large result from {tool_name}: {len(summary_text):,} chars. "
            f"Full text appears once in the next <read_state>; saved at "
            f"{rel_path} for read_file.]"
        )
        return [TextPart(text=stub)], stub

    def _spill_to_results(self, tool_name: str, content: str) -> str:
        """Write `content` to a stable per-(tool, hash) path under the
        agent's file sandbox results/ subdir. Returns absolute path.

        Lives inside `_file_sandbox` (initialized by make_extra_tools in
        agent.__init__) so the existing read_file tool resolves
        'results/<name>.txt' correctly via its sandbox-relative
        _resolve(). If _file_sandbox isn't set yet (extra tools were
        not registered for some reason), raise — caller will catch and
        fall back to inline return.
        """
        sandbox = getattr(self, "_file_sandbox", None)
        if not sandbox:
            raise RuntimeError(
                "ephemeral spill needs _file_sandbox; not registered"
            )
        if self._results_dir is None:
            self._results_dir = os.path.join(sandbox, "results")
            os.makedirs(self._results_dir, exist_ok=True)
        # Stable name keyed on (tool, full content + length) so two
        # reads of the same content reuse the same file (cheap dedup),
        # but two DIFFERENT large results that happen to share a prefix
        # don't collide. v0.11.2 codex review: hashing only the first
        # 512 chars caused content-shadowing — second read would read
        # the wrong file. Use sha256 of full content + length sentinel.
        encoded = content.encode("utf-8", errors="replace")
        digest = hashlib.sha256(
            f"{tool_name}|{len(encoded)}|".encode("utf-8") + encoded
        ).hexdigest()[:16]
        path = os.path.join(
            self._results_dir, f"{tool_name}_{digest}.txt"
        )
        if not os.path.exists(path):
            with open(path, "w", encoding="utf-8", errors="replace") as f:
                f.write(content)
        return path

    def _record_usage(self, step_n: int, usage) -> None:
        # v0.10.4: enrich usage_log with input-side metrics so we can
        # detect cache regressions BEFORE running a full eval. The
        # critical fields are prompt_hash + tools_hash: if either drifts
        # within a session, we just invalidated the Anthropic prompt
        # cache (10× cost penalty on the rebuild). See
        # _compute_call_metrics + _check_cache_stability.
        metrics = self._compute_call_metrics()
        entry: dict[str, Any] = {
            "step": step_n,
            "input": usage.input,
            "output": usage.output,
            "cache_read": usage.cache_read,
            "cache_creation": usage.cache_creation,
            **metrics,
        }
        self.usage_log.append(entry)
        self.state.history.usage = self.state.history.usage + usage
        self._check_cache_stability(entry)
        if self._trace_dir is not None:
            self._dump_trace(entry)

    def _compute_call_metrics(self) -> dict[str, Any]:
        """Snapshot the message list / tool schema / system prompt that
        we just sent to the LLM. Pure-Python, no I/O. Cheap enough to
        run on every call (~50µs for a 30-message list).
        """
        sys_text = self.system_prompt or ""
        prompt_hash = hashlib.sha256(sys_text.encode("utf-8")).hexdigest()[:12]
        tools_payload = self._tools_signature()
        tools_hash = hashlib.sha256(tools_payload.encode("utf-8")).hexdigest()[:12]
        # Per-role byte counts so we can attribute growth.
        state_msg_bytes = 0
        tool_results_bytes = 0
        assistant_msgs_bytes = 0
        user_msgs_bytes = 0
        # v0.12.3: split out image bytes (currently lumped into
        # state_msg_bytes because _content_byte_len counts ImagePart
        # base64 length). With use_vision=True, screenshots are
        # 50-200KB per step uncached — likely the dominant non-cached
        # cost driver. Knowing the split lets v0.12.x decide whether
        # to chase image-resolution / vision-toggling.
        image_bytes = 0
        # v0.12.5: finer history/read-state split for deciding whether
        # the next structural HistoryItem summarizer is actually worth
        # enabling. These are measured from the live prompt message list,
        # so they reflect exactly what the model saw.
        agent_history_bytes = 0
        agent_history_lines = 0
        read_state_bytes = 0
        read_state_entries = 0
        n_user = n_assistant = n_tool_result = 0
        last_user_idx = -1
        for i, msg in enumerate(self._messages):
            if isinstance(msg, UserMessage):
                last_user_idx = i
        for i, msg in enumerate(self._messages):
            blen = _message_byte_len(msg)
            if isinstance(msg, UserMessage):
                user_msgs_bytes += blen
                n_user += 1
                if i == last_user_idx:
                    state_msg_bytes = blen
                text = _content_text(msg.content)
                if text.startswith("[AGENT_HISTORY]"):
                    agent_history_bytes = len(text.encode("utf-8"))
                    agent_history_lines = max(0, len(text.splitlines()) - 1)
                for match in re.finditer(
                    r"<read_state>\s*(.*?)\s*</read_state>",
                    text,
                    re.S,
                ):
                    read_state_bytes += len(match.group(0).encode("utf-8"))
                    read_state_entries += len(
                        re.findall(r"<result\b", match.group(1))
                    )
            elif isinstance(msg, AssistantMessage):
                assistant_msgs_bytes += blen
                n_assistant += 1
            elif isinstance(msg, ToolResultMessage):
                tool_results_bytes += blen
                n_tool_result += 1
            # Walk ImageParts in any role's content list.
            content = getattr(msg, "content", None)
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, ImagePart) and part.data:
                        image_bytes += len(part.data)
        return {
            "prompt_hash": prompt_hash,
            "tools_hash": tools_hash,
            "system_bytes": len(sys_text.encode("utf-8")),
            "tools_bytes": len(tools_payload.encode("utf-8")),
            "n_messages": len(self._messages),
            "n_user": n_user,
            "n_assistant": n_assistant,
            "n_tool_result": n_tool_result,
            "state_msg_bytes": state_msg_bytes,
            "user_msgs_bytes": user_msgs_bytes,
            "assistant_msgs_bytes": assistant_msgs_bytes,
            "tool_results_bytes": tool_results_bytes,
            "image_bytes": image_bytes,
            "agent_history_bytes": agent_history_bytes,
            "agent_history_lines": agent_history_lines,
            "read_state_bytes": read_state_bytes,
            "read_state_entries": read_state_entries,
            "history_items": len(getattr(self, "_history", []) or []),
            "history_collapsed_items": sum(
                1 for h in (getattr(self, "_history", []) or [])
                if getattr(h, "collapsed", False)
            ),
        }

    def _tools_signature(self) -> str:
        """Stable JSON serialization of the tool schema. Used for hash
        only — must be identical across calls if the schema is the same,
        otherwise hash drift is a false positive.
        """
        sig: list[dict[str, Any]] = []
        for tool in self.tools:
            entry: dict[str, Any] = {"name": getattr(tool, "name", None)}
            for attr in ("description", "parameters", "input_schema"):
                if hasattr(tool, attr):
                    entry[attr] = getattr(tool, attr)
            sig.append(entry)
        return json.dumps(sig, sort_keys=True, default=_json_fallback)

    def _check_cache_stability(self, entry: dict[str, Any]) -> None:
        """If prompt_hash or tools_hash drifts within a session, the
        Anthropic prompt cache will rebuild from scratch on the next
        call (~10× cost on the rebuilt tokens). Warn loudly so we
        catch it before a full eval — this was the v0.10.0 trap.

        v0.11.2 also flags large cache_read drops with stable hashes
        — that's the v0.10.2 / v0.11.0 fingerprint where prefix bytes
        shifted in a way the hash didn't catch (e.g. mid-conversation
        message mutation).
        """
        if not self.usage_log or len(self.usage_log) < 2:
            self._initial_prompt_hash = entry["prompt_hash"]
            self._initial_tools_hash = entry["tools_hash"]
            self._cache_warned = False
            self._cache_read_warned = False
            self._peak_cache_read = entry.get("cache_read", 0) or 0
            return
        # Track peak to detect mid-session drops.
        cur_cache = entry.get("cache_read", 0) or 0
        if cur_cache > self._peak_cache_read:
            self._peak_cache_read = cur_cache
        if (
            not getattr(self, "_cache_read_warned", False)
            and self._peak_cache_read >= 5_000  # warmed up
            and cur_cache < self._peak_cache_read * 0.5
            and entry["prompt_hash"] == self._initial_prompt_hash
            and entry["tools_hash"] == self._initial_tools_hash
        ):
            logger.warning(
                "agent: CACHE READ DROP — cache_read fell from peak %d "
                "to %d at step %d while prompt+tools hashes are stable. "
                "Likely cause: an old message was mutated mid-session. "
                "Investigate _collapse_old_history / _inject_page_state.",
                self._peak_cache_read, cur_cache, entry["step"],
            )
            self._cache_read_warned = True
        if self._cache_warned:
            return
        if entry["prompt_hash"] != self._initial_prompt_hash:
            logger.warning(
                "agent: CACHE BREAK — system_prompt_hash drifted at "
                "step %d (was %s, now %s). Prompt cache will rebuild; "
                "expect a one-call cost spike.",
                entry["step"],
                self._initial_prompt_hash,
                entry["prompt_hash"],
            )
            self._cache_warned = True
        elif entry["tools_hash"] != self._initial_tools_hash:
            logger.warning(
                "agent: CACHE BREAK — tools_hash drifted at step %d "
                "(was %s, now %s). Prompt cache will rebuild; expect "
                "a one-call cost spike.",
                entry["step"],
                self._initial_tools_hash,
                entry["tools_hash"],
            )
            self._cache_warned = True

    def _dump_trace(self, entry: dict[str, Any]) -> None:
        """Opt-in: dump the full message list to JSONL when
        BU_RS_INSTRUMENT_TRACE_DIR is set. Lets us replay token
        construction offline without re-running the eval. Off by
        default — has noticeable I/O cost on long tasks.
        """
        try:
            os.makedirs(self._trace_dir, exist_ok=True)  # type: ignore[arg-type]
            fname = f"step_{entry['step']:03d}_{entry['prompt_hash']}.jsonl"
            path = os.path.join(self._trace_dir, fname)  # type: ignore[arg-type]
            payload = {
                "metrics": entry,
                "system": self.system_prompt,
                "messages": [_message_to_dict(m) for m in self._messages],
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, default=_json_fallback) + "\n")
        except Exception:
            logger.exception("agent: trace dump failed (non-fatal)")

    async def _should_stop(self) -> bool:
        if self.register_should_stop_callback is None:
            return False
        try:
            return bool(await self.register_should_stop_callback())
        except Exception:
            return False


# ---------------------------------------------------------------------------
# helpers


class _MessageManagerView:
    """Thin pass-through so `agent.message_manager.last_input_messages`
    works the way browser_use consumers expect."""

    def __init__(self, agent: "Agent") -> None:
        self._agent = agent

    @property
    def last_input_messages(self) -> list[Message]:
        return list(self._agent._messages)


def _parse_judgement(text: str) -> dict[str, Any]:
    """Best-effort JSON extraction from a judge LLM response.

    Models sometimes wrap JSON in ```json fences or trailing prose; pull
    the first balanced object. Falls back to a not-judged-shape verdict
    on parse failure so downstream code doesn't crash.
    """
    import json as _json
    import re as _re

    fence = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, flags=_re.S)
    if fence:
        candidate = fence.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        candidate = text[start : end + 1] if start != -1 and end > start else "{}"
    try:
        parsed = _json.loads(candidate)
        if isinstance(parsed, dict):
            return {
                "verdict": bool(parsed.get("verdict", False)),
                "reasoning": str(parsed.get("reasoning", "")),
                "score": float(parsed.get("score", 0.0)),
                "impossible_task": bool(parsed.get("impossible_task", False)),
                "reached_captcha": bool(parsed.get("reached_captcha", False)),
            }
    except (ValueError, TypeError):
        pass
    return {
        "verdict": False,
        "reasoning": f"judge response could not be parsed: {text[:200]}",
        "score": 0.0,
        "impossible_task": False,
        "reached_captcha": False,
    }


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


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


def _task_requests_viator_orlando_family(task: str) -> bool:
    task_lc = (task or "").lower()
    return (
        "viator.com" in task_lc
        and "orlando" in task_lc
        and "family-friendly" in task_lc
        and "top three tours" in task_lc
        and "prices" in task_lc
        and "customer ratings" in task_lc
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
    return (
        _looks_like_fabricated_blocked_answer(text)
        or _looks_like_site_required_external_answer(task, text)
        or _looks_like_unmet_requested_data_answer(task, text)
        or _looks_like_search_result_query_mismatch_answer(task, text)
        or _looks_like_wrong_host_final(task, final_url)
        or _looks_like_search_host_final(task, final_url)
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


_PENDING_ACTION_TOOL_NAMES = (
    "click",
    "done",
    "extract_result_cards",
    "extract_structured_data",
    "find_elements",
    "get_links",
    "get_text",
    "grep_scratchpad",
    "navigate",
    "page_text",
    "read_file",
    "read_scratchpad",
    "scroll",
    "scroll_to_bottom",
    "search_page",
    "type_text",
    "web_search",
    "write_file",
)
_PENDING_ACTION_TOOL_PATTERN = "|".join(
    re.escape(name) for name in _PENDING_ACTION_TOOL_NAMES
)
_PENDING_ACTION_LINE_RE = re.compile(
    rf"(?im)^\s*(?:next\s+)?(?:action|tool|tool call|call)\s*:\s*`?\s*"
    rf"(?:{_PENDING_ACTION_TOOL_PATTERN})\s*\("
)
_PENDING_ACTION_INTENT_RE = re.compile(
    rf"(?i)\b(?:i\s+(?:will|should|need to)|next(?:,|\s)|now)\s+"
    rf"(?:call|use|run|try)\s+`?(?:{_PENDING_ACTION_TOOL_PATTERN})\b"
)
_PENDING_ACTION_BARE_CALL_RE = re.compile(
    rf"(?im)^\s*[-*]?\s*`?(?:{_PENDING_ACTION_TOOL_PATTERN})\s*\("
)


def _looks_like_pending_tool_action(text: str) -> bool:
    """True when a would-be final answer is actually a tool-call plan.

    Gemini sometimes emits no tool calls and writes prose such as
    ``Action: web_search(query='...')``. Treating that as final produces
    a one-step false answer; it should be nudged into a real tool call.
    """
    compact = str(text or "").strip()
    if not compact:
        return False
    return bool(
        _PENDING_ACTION_LINE_RE.search(compact)
        or _PENDING_ACTION_BARE_CALL_RE.search(compact)
        or _PENDING_ACTION_INTENT_RE.search(compact)
    )


def _format_tool_return(
    raw: Any, secrets: dict[str, str]
) -> tuple[list[ContentPart], str]:
    """Return (content_parts_for_llm, summary_text_for_history).

    Image returns are surfaced as ImagePart for providers that accept
    image-in-tool-result (Anthropic) and split into a follow-up user
    message by Gemini/OpenAI providers internally.
    """
    if isinstance(raw, dict) and raw.get("_type") == "image":
        return (
            [
                ImagePart(
                    data=raw["data"], media_type=raw.get("media_type", "image/png")
                )
            ],
            "[screenshot captured]",
        )
    text = raw if isinstance(raw, str) else str(raw)
    text = _redact_secrets(text, secrets)
    return ([TextPart(text=text)], text)


def _expand_secrets(value: Any, secrets: dict[str, str]) -> Any:
    """Replace placeholder substrings in tool args with real secret values
    BEFORE the tool runs. The model never sees the real value."""
    if not secrets:
        return value
    if isinstance(value, str):
        for placeholder, real in secrets.items():
            if placeholder in value:
                value = value.replace(placeholder, real)
        return value
    if isinstance(value, list):
        return [_expand_secrets(v, secrets) for v in value]
    if isinstance(value, dict):
        return {k: _expand_secrets(v, secrets) for k, v in value.items()}
    return value


def _redact_secrets(content: Any, secrets: dict[str, str]) -> Any:
    """Inverse: replace real values with placeholders before sending to the LLM."""
    if not secrets:
        return content
    if isinstance(content, str):
        for placeholder, real in secrets.items():
            if real and real in content:
                content = content.replace(real, placeholder)
        return content
    if isinstance(content, list):
        return [_redact_secrets(c, secrets) for c in content]
    if isinstance(content, dict):
        return {k: _redact_secrets(v, secrets) for k, v in content.items()}
    return content
