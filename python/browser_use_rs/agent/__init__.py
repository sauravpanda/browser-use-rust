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
import inspect
import logging
import time
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

# Flash-mode prompt — terse variant matching upstream's
# system_prompt_flash.md. Used when flash_mode=True is passed to the
# Agent (eval framework default for many setups). Mirrors upstream's
# convention of swapping prompt templates based on mode. v0.7.1.
FLASH_SYSTEM_PROMPT = """\
You are an AI agent designed to operate in an iterative loop to automate browser tasks. Your ultimate goal is accomplishing the task provided in <user_request>.

<browser_state>Elements: [N]<tag attrs>text</tag>. Only [indexed] elements are interactive. Lines starting with <tag> "..." are static text content (not clickable). Indented lines are children of the element above.</browser_state>

<action_rules>
Check the browser state each step to verify your previous action achieved its goal. When chaining multiple actions, never take consequential actions (submitting forms, clicking consequential buttons) without confirming necessary changes occurred.

Dynamic pages: if `[N]` returns "index not available" or "no longer present", do NOT retry [N] — the page state has shifted and that index is dead. Read the FRESH snapshot's [N] numbers and pick from those.


For extraction tasks (find/list/answer): PREFER `extract_structured_data(query=...)` over scrolling and reading raw page_text. The extractor uses an LLM over the cleaned page — far more reliable than reasoning manually.

LOCATE-THEN-EXTRACT: when the task names a specific NAMED section/category/page that is likely to exist as a navigable region ("Politics", "Reviews", "About", "Technology category"), first narrow scope by clicking that section/category/page or by including that named region in the extraction query.

For time windows ("past week", "current week", "today", "latest", "most recent"), counts ("top 3", "first 5", "next three"), prices/attributes ("under $100", "with private pool"), do NOT search for the filter text as a section. Instead inspect the current results/list, use visible sort/filter controls if present, and extract matching items from the list.

For multi-page tasks: use the file system. write_file("notes.md", content) saves partial extractions; replace_file_str("todo.md", "[ ]", "[x]") tracks progress; the file survives history collapse.

Finalize via `done(text="<your answer>", success=true|false)`. Set success=true only if you completed the task with observed page evidence; success=false if blocked, data unavailable, or unsure. For "list N items / top N / first N" tasks, your answer should contain EXACTLY N items unless the page legitimately had fewer (state how many were available in that case). A plain-text turn (no tool calls) still works as a fallback but `done(...)` is preferred because it makes finalization explicit.
</action_rules>

<blocked_sites>
If the target site returns 403 / Cloudflare bot-detection / Turnstile / login wall / paywall, do NOT retry the same URL and do NOT invent content. Required fallbacks in order: (1) `web_search(query=...)` — search engine snippets often contain the answer; (2) try alternative endpoints (mobile.* / m.* / /amp/ / sitemap / RSS); (3) if still blocked after 2-3 attempts, set `success=False` and state what blocked you. Confidently-wrong fabricated answers fail the judge harder than honest "I was blocked" answers. CAPTCHAs auto-resolve — wait one turn before treating one as a hard block.
</blocked_sites>

<state_emission>
On every turn that calls a tool, prefix your message with three short XML blocks so progress survives history compaction:
  <evaluation_previous_goal>Did your last action achieve what you intended? Yes/Partial/No + 1 sentence.</evaluation_previous_goal>
  <memory>Key facts you've learned so far that are NOT in the current page snapshot — running list of items collected, filters applied, search queries tried, things ruled out. Keep under 5 lines.</memory>
  <next_goal>What you're trying to do next, in one short sentence.</next_goal>
These blocks are automatically extracted and re-injected on subsequent turns so you don't lose context when older messages get collapsed. Skip them only on the final-answer turn.
</state_emission>

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
Maybe later, No thanks, X). Do NOT conclude "task impossible" on
your first turn — the real content is almost always one click away.

Blocked sites — alternative approaches REQUIRED:
If the target site returns 403 / "access denied" / Cloudflare bot-
detection / Turnstile / persistent login wall / paywall, do NOT
repeatedly retry the same URL and do NOT invent content.

  1. Try `web_search(query="<specific information needed>")` — search
     engine snippets and cached results often contain the answer the
     blocked page would have shown. This is the single most useful
     fallback — use it whenever a target site blocks you.
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
     honest "I was blocked" answers.

When calling tools: never invent values for required arguments. If the
snapshot doesn't show what you need (no [N] for the element, no text
to read), scroll, navigate, or extract first to get real values.

For extraction tasks (find/list/answer questions about page content):
PREFER `extract_structured_data(query=...)` over reading raw page_text.
The extractor uses an LLM to answer your specific question over a
cleaned page — far more reliable than dumping page_text and reasoning
manually. Use `find_elements(selector, attributes)` to enumerate
matching DOM nodes when you need raw HTML. Use `search_page(pattern)`
when you just want to know "is X mentioned anywhere".

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
    "right entity.\n"
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
        history_window_steps: int = 3,
        use_vision: bool = True,
        sensitive_data: dict[str, str] | None = None,
        system_prompt: str | None = None,
        extend_system_message: str | None = None,
        override_system_message: str | None = None,
        initial_actions: list[dict] | None = None,
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
        # flash_mode swaps the system prompt to a terser, eval-style
        # variant matching upstream's system_prompt_flash.md. v0.7.1.
        # use_thinking is still no-op (no thinking-disabled variant yet).
        self.flash_mode: bool = bool(_compat_kwargs.pop("flash_mode", False))
        _use_thinking = _compat_kwargs.pop("use_thinking", None)
        if _use_thinking is not None and not _use_thinking:
            logger.info(
                "agent: use_thinking=False received; we don't have a "
                "no-thinking template yet. Using the standard prompt.",
            )
        # Anything left is genuinely unused — warn loudly so the user
        # sees it in eval logs and can either patch the agent or stop
        # passing the kwarg.
        if _compat_kwargs:
            logger.warning(
                "agent: ignored kwargs (silent compat-pass-through): %s",
                sorted(_compat_kwargs.keys()),
            )
        # Tool source: explicit tools= wins, then controller=, then defaults.
        controller = _compat_kwargs.pop("controller", None)
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
            # flash_mode (eval default in many setups) selects the
            # terser, upstream-matching prompt. Otherwise our richer
            # default. v0.7.1.
            base_prompt = (
                system_prompt
                or (FLASH_SYSTEM_PROMPT if self.flash_mode else DEFAULT_SYSTEM_PROMPT)
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
        self.initial_actions = initial_actions or []
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
        self.usage_log: list[dict[str, Any]] = []
        self.error_log: list[tuple[int, str]] = []

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
        # v0.9.8 step-bloat reduction (codex-reviewed).
        # Pattern A "early pivot": track step-of-last-meaningful-progress
        # so we can detect agents stuck on the wrong domain. "Progress"
        # means a URL path change, a non-empty/non-"NOT FOUND" extract,
        # or a clicked search result. Reset to current step on any
        # progress signal.
        self._last_progress_step: int = 0
        self._last_progress_url_path: str = ""  # eTLD+1 + path for change detection
        self._pivot_nudged_domains: set[str] = set()  # fire ONCE per domain
        # Pattern C "no-match streak": consecutive read-tool turns that
        # returned a negative result ("no matches", "NOT FOUND", "(no
        # such ...)") on the SAME url. Reset on URL change.
        self._negative_read_streak: int = 0
        self._negative_read_url: str = ""
        self._abandon_page_nudged_urls: set[str] = set()  # fire once per URL
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
        # Running collapsed history of older steps. Each entry: a
        # one-line "<step N> <action> → <result-summary>" string.
        self._collapsed_history: list[str] = []
        # Track the last K turns' (step_n, tool_calls, results) so
        # _collapse_old_history can render them when popping. We need
        # this because by the time we pop a turn from self._messages,
        # we've lost the structured tool-call args (the messages
        # carried only the wire format).
        self._recent_turn_records: list[
            tuple[int, list[ToolCall], list[ActionResult]]
        ] = []
        # Self-validation: when the LLM is about to finalize an answer
        # (no tool calls in text-mode, or first done() call in
        # output-model mode), we let it through ONCE with a "re-check
        # before committing" prompt injected. This addresses the
        # observed self-report ↔ judge-score gap (eval data showed
        # ~30pp delta where the agent confidently submitted answers
        # the judge marked wrong — wrong sort order, wrong section,
        # missing required parts).
        self._validation_step_used = False
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
            self._messages.append(UserMessage(content=self.task))
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
        self._messages.append(UserMessage(content=new_task))

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
                        f"[STAGNATION] The page state has not changed "
                        f"for {self._page_fp_streak} consecutive steps "
                        f"(same URL, same elements, same content). Your "
                        f"recent actions are not having any effect. Try "
                        f"a fundamentally different approach:\n"
                        f"  - If you've been clicking, try scrolling or "
                        f"a different element.\n"
                        f"  - If a popup/overlay may be blocking, try "
                        f"dismissing it first (Accept, Close, X).\n"
                        f"  - If on the wrong page, navigate elsewhere.\n"
                        f"  - If the answer is already on the page, "
                        f"call extract_structured_data(query=...) and "
                        f"finalize.\n"
                        f"Do NOT repeat the same action type."
                    )
                    self._messages.append(UserMessage(content=nudge))
                    logger.info(
                        "agent: stagnation nudge injected at step %d "
                        "(streak=%d)", step_n, self._page_fp_streak,
                    )
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

                # Self-validation intercept: on the FIRST proposed
                # answer, append the validation prompt and let the LLM
                # respond once more. The second answer (or revision) is
                # what we commit. See _VALIDATION_PROMPT for the
                # rationale and prompt text.
                if (
                    self.self_validate
                    and not self._validation_step_used
                    and done_text
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
                done_text = self._strip_state_tags_for_answer(done_text)
                _success = bool(done_text)
                if _success and _looks_like_fabricated_blocked_answer(done_text):
                    _success = False
                    logger.info(
                        "agent: success downgraded to False at step %d "
                        "(blocked-fabrication detected in plain-text answer)",
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
                        if s_flag and _looks_like_fabricated_blocked_answer(payload):
                            s_flag = False
                            logger.info(
                                "agent: success downgraded to False at step %d "
                                "(blocked-fabrication detected in done payload)",
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
                        if _looks_like_fabricated_blocked_answer(text):
                            s_flag = False
                            logger.info(
                                "agent: success downgraded to False at step %d "
                                "(blocked-fabrication, malformed-marker fallback)",
                                step_n,
                            )
                        done_result = ActionResult(
                            extracted_content=text,
                            is_done=True,
                            success=s_flag,
                        )

            if done_result is not None:
                # Replace the parsed-out tool result with the done flag set
                # so history.is_done() / final_result() see it directly,
                # then exit the loop. Other tool results from the same turn
                # are kept (they ran and may have side effects).
                tool_results = [
                    done_result if r.extracted_content
                    and isinstance(r.extracted_content, str)
                    and r.extracted_content.startswith("__DONE__:")
                    else r
                    for r in tool_results
                ]

            self._append_history(state_summary, output, tool_results, t0, step_n)

            # Record this turn for later sliding-window collapse. We
            # snapshot the structured tool calls + results because by
            # the time _collapse_old_history runs, we've only got the
            # wire-format messages left in self._messages.
            self._recent_turn_records.append(
                (step_n, list(completion.tool_calls), list(tool_results))
            )

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
                if (
                    self.self_validate
                    and not self._validation_step_used
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

            self._maybe_inject_loop_nudge(
                state_summary, completion.tool_calls, step_n, max_steps,
                tool_results=tool_results,
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
        "search_page",
    })

    def _maybe_inject_loop_nudge(
        self,
        state: BrowserStateSummary,
        tool_calls: list[ToolCall],
        step_n: int,
        max_steps: int,
        tool_results: list[ActionResult] | None = None,
    ) -> None:
        """Detect five classes of stall and inject a one-shot nudge.

        1. **Tight loop** — same canonical (name+args) signature emitted
           3+ times in the last 6 turns.
        2. **No extract** — fewer than 25% of recent turns called a
           read tool AND >1/3 of step budget used.
        3. **Budget warning** — at step == max_steps - 5.
        4. **Early pivot (v0.9.8)** — 5+ consecutive steps on the same
           eTLD+1 with NO meaningful progress (no URL/path change, no
           successful extract). Codex-reviewed scoping: requires both
           the same-domain count AND zero-progress signals together;
           exempts login/form flows where multi-step same-domain is
           legitimate. Fires once per domain.
        5. **No-match streak (v0.9.8)** — 3+ consecutive read-tool
           turns on the SAME url returning negative results ("no
           matches", "NOT FOUND", "(no such ...)"). Streak resets on
           any URL change, any positive read result, or any non-read
           tool. Fires once per URL.

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
                f"[BUDGET_WARNING] You have ~{remaining} turn(s) left "
                f"before the run ends. Stop exploring — read whatever "
                f"content is on the current page and finish with the "
                f"best answer you have. A partial answer is better than "
                f"no answer."
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
                f"[LOOP_DETECTED] You have called the EXACT same action "
                f"sequence {repeat_count} times in your last {WINDOW} "
                f"turns. Repeating it again will not work. Try a "
                f"materially different approach: pick different elements, "
                f"reformulate the search query, scroll to a different "
                f"region, switch tabs, or finish with `done` if you have "
                f"enough information. Do NOT issue the same tool calls "
                f"again."
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
                    f"[STUCK_NO_EXTRACT] In your last "
                    f"{len(self._recent_tool_names)} turns you have only "
                    f"called a content-read tool {extract_turns} time(s) "
                    f"(target: every other turn). You've been navigating/"
                    f"clicking across {len(domains)} domain(s) without "
                    f"reading much. The data you need is on the page "
                    f"already — call page_text or get_text NOW to read "
                    f"it, or call done with the best answer you have. "
                    f"More navigation is unlikely to help."
                )
                self._messages.append(UserMessage(content=nudge))
                logger.info(
                    "agent: STUCK_NO_EXTRACT fired at step %d "
                    "(extracts=%d/%d, domains=%d)",
                    step_n, extract_turns, len(self._recent_tool_names),
                    len(domains),
                )
                self._loop_nudge_cooldown = COOLDOWN_STEPS
                return

        # ---- v0.9.8 progress tracking + Patterns 4 & 5 ----
        # Update progress signals based on this turn's results. We need
        # tool_results to determine if a read was negative (NOT FOUND /
        # no matches) or positive.
        self._update_progress_signals(state, tool_calls, tool_results, step_n)

        # ---- 4. EARLY PIVOT (codex Pattern A) ----
        # If we've been on the same eTLD+1 for 5+ steps with no progress,
        # nudge to web_search. Fire ONCE per domain — repeating wastes
        # budget. Skip if the agent's been doing typing or form work
        # (those legitimately stay on one domain).
        EARLY_PIVOT_STALE_STEPS = 5
        cur_domain = self._domain_of(state.url or "")
        if (
            cur_domain
            and cur_domain not in self._pivot_nudged_domains
            and self._last_progress_step
            and step_n - self._last_progress_step >= EARLY_PIVOT_STALE_STEPS
            # Only fire if recent turns ARE same-domain (not just
            # carrying forward from earlier).
            and sum(
                1 for u in self._recent_urls
                if self._domain_of(u) == cur_domain
            ) >= EARLY_PIVOT_STALE_STEPS
            # Skip if we've recently typed (login/form flow exemption).
            and not any(
                "type_text" in names or "press_keys" in names
                for names in self._recent_tool_names[-3:]
            )
        ):
            nudge = (
                f"[EARLY_PIVOT] You've been on `{cur_domain}` for "
                f"{step_n - self._last_progress_step} steps without "
                f"meaningful progress (no URL change, no successful "
                f"extract). The site likely doesn't have what you need "
                f"in an accessible form. Try `web_search(query=...)` "
                f"with a SPECIFIC query for the information needed — "
                f"search-engine snippets often answer the question "
                f"directly. If web_search has already been tried, try "
                f"a different SITE (mobile.* / m.* / amp / RSS, or a "
                f"competitor). Do NOT keep clicking/scrolling on this "
                f"page."
            )
            self._messages.append(UserMessage(content=nudge))
            logger.info(
                "agent: EARLY_PIVOT fired at step %d (domain=%s, "
                "stale_for=%d steps)",
                step_n, cur_domain, step_n - self._last_progress_step,
            )
            self._pivot_nudged_domains.add(cur_domain)
            self._loop_nudge_cooldown = COOLDOWN_STEPS
            return

        # ---- 5. NO-MATCH STREAK (codex Pattern C) ----
        # 3+ consecutive read tools on the SAME url returning negative
        # results → nudge to abandon this page. Tracked in
        # _update_progress_signals; here we just consume the streak.
        NO_MATCH_THRESHOLD = 3
        if (
            self._negative_read_streak >= NO_MATCH_THRESHOLD
            and self._negative_read_url
            and self._negative_read_url not in self._abandon_page_nudged_urls
        ):
            nudge = (
                f"[ABANDON_PAGE] Your last {self._negative_read_streak} "
                f"read attempts on this page returned negative results "
                f"(no matches / NOT FOUND / empty). The page does not "
                f"contain what you're looking for. Stop reading from "
                f"`{self._negative_read_url[:80]}` — navigate to a "
                f"different page (search the site for the topic, try "
                f"web_search, or load a different section)."
            )
            self._messages.append(UserMessage(content=nudge))
            logger.info(
                "agent: ABANDON_PAGE fired at step %d "
                "(neg_streak=%d, url=%s)",
                step_n, self._negative_read_streak,
                self._negative_read_url[:80],
            )
            self._abandon_page_nudged_urls.add(self._negative_read_url)
            self._loop_nudge_cooldown = COOLDOWN_STEPS
            self._negative_read_streak = 0  # reset after nudge

    def _update_progress_signals(
        self,
        state: BrowserStateSummary,
        tool_calls: list[ToolCall],
        tool_results: list[ActionResult] | None,
        step_n: int,
    ) -> None:
        """Update progress + no-match-streak counters used by Patterns 4/5.

        "Progress" = URL path change OR a successful (non-negative)
        read result. Resets _last_progress_step to current step.
        Also maintains the consecutive-negative-read streak per URL.
        """
        cur_url_path = state.url or ""
        # Drop query string for path-change detection — URLs that only
        # differ in tracking params don't count as progress.
        if "?" in cur_url_path:
            cur_url_path = cur_url_path.split("?", 1)[0]

        url_changed = cur_url_path != self._last_progress_url_path

        # Detect any positive read result this turn. A "positive read"
        # is a read-tool that returned non-empty, non-"NOT FOUND",
        # non-"(no ...)" content.
        NEGATIVE_MARKERS = (
            "NOT FOUND", "(no matches", "(no such", "(empty page",
            "(start_from_char=", "is past end",
            "(no tabs)", "(no elements match", "(no occurrences",
        )
        had_positive_read = False
        had_negative_read = False
        had_any_read = False
        if tool_results:
            for tc, res in zip(tool_calls, tool_results):
                if tc.name not in self._READ_ONLY_TOOLS:
                    continue
                had_any_read = True
                content = (res.extracted_content or "").strip()
                if not content:
                    had_negative_read = True
                    continue
                # Negative if it starts with NOT FOUND, "(no ...", etc.
                lc_head = content[:60]
                if any(m in lc_head for m in NEGATIVE_MARKERS):
                    had_negative_read = True
                else:
                    had_positive_read = True

        # Initialize on first turn.
        if not self._last_progress_url_path:
            self._last_progress_url_path = cur_url_path
            self._last_progress_step = step_n
            self._negative_read_url = cur_url_path
            return

        # Pattern 4 progress: URL change OR positive read = progress.
        if url_changed or had_positive_read:
            self._last_progress_step = step_n
            self._last_progress_url_path = cur_url_path

        # Pattern 5 streak: count consecutive negative reads on same URL.
        # Reset on URL change (we're on a new page) OR on any positive
        # read OR on any non-read tool that mutates state (click,
        # type_text, etc).
        had_state_mutation = any(
            tc.name not in self._READ_ONLY_TOOLS for tc in tool_calls
        )
        if url_changed or had_positive_read or had_state_mutation:
            # Reset streak; new context.
            self._negative_read_streak = 0
            self._negative_read_url = cur_url_path
        elif had_negative_read and not had_positive_read and had_any_read:
            # Pure-negative read turn on same URL → tick streak.
            if self._negative_read_url != cur_url_path:
                self._negative_read_url = cur_url_path
                self._negative_read_streak = 1
            else:
                self._negative_read_streak += 1

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
            f"in your last turn errored. Multi-action turns work when "
            f"the actions are independent or pre-planned; when one fails "
            f"the rest usually fail too because they assumed the page "
            f"state would advance. For the NEXT turn, emit ONE tool "
            f"call only so its result can inform the next decision. "
            f"Resume batching only after you confirm the page is in the "
            f"expected state."
        )
        self._messages.append(UserMessage(content=nudge))
        logger.info(
            "agent: BATCH_FAILED hint fired (errors=%d/%d, batch=%s)",
            error_count, len(tool_calls), [tc.name for tc in tool_calls],
        )

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

        async def _safe_screenshot() -> str | None:
            try:
                png = await asyncio.wait_for(
                    self.session.screenshot(), timeout=15.0
                )
                return base64.b64encode(png).decode("ascii")
            except Exception:
                return None

        # Capture both the rendered text AND the index→selector map
        # in a single CDP roundtrip. The selector map drives
        # agent_history rendering: when we collapse step N's tool_call
        # `click(index=5)` into a history line, we look up element 5's
        # selector here so the rendered line reads
        # `Clicked button "Sign In"` instead of `clicked [5]` —
        # cross-turn references stay stable through DOM mutation.
        async def _safe_dom() -> tuple[str, dict[int, str]]:
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
                return dom_text, idx_to_sel
            except Exception:
                # No active page yet (very first step before
                # initial_actions navigate, or a frame transition
                # mid-step). Skip — the LLM gets a "(no page state
                # available)" placeholder instead of crashing the run.
                return "", {}

        url, screenshot_b64, dom_pair = await asyncio.gather(
            _safe_url(), _safe_screenshot(), _safe_dom()
        )
        dom_text, self._index_to_selector = dom_pair

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
            elements_text=dom_text,
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
        # `_recent_turn_records` is per-turn; the live message list
        # holds 1 AssistantMessage + N ToolResultMessage(s) per turn.
        # Only count action-only turns toward the window (read-tool
        # turns are exempt and stay native), so we skip ahead through
        # records that contain reads.
        action_only_indices = [
            i
            for i, (_, tcs, _) in enumerate(self._recent_turn_records)
            if not any(tc.name in self._READ_ONLY_TOOLS for tc in tcs)
        ]
        excess = len(action_only_indices) - self.history_window_steps
        if excess <= 0:
            return
        # Indices into _recent_turn_records that we'll actually collapse.
        collapse_indices = action_only_indices[:excess]
        records_to_collapse = [self._recent_turn_records[i] for i in collapse_indices]

        for record in records_to_collapse:
            step_n, tool_calls, results = record
            # Render one history line per (call, result) pair. If the
            # batch had only one call, this is a single line; for
            # multi_act batches we list each.
            for tc, res in zip(tool_calls, results):
                self._collapsed_history.append(
                    self._format_action_line(step_n, tc, res)
                )
            # Pop the matching messages from self._messages: 1
            # AssistantMessage followed by len(tool_calls) ToolResultMessages.
            # Walk forward looking for the next AssistantMessage that
            # matches; once found, drop it + the next N ToolResultMessages.
            pop_idx = None
            for i, msg in enumerate(self._messages):
                if isinstance(msg, AssistantMessage):
                    msg_call_ids = [tc.id for tc in msg.tool_calls]
                    record_call_ids = [tc.id for tc in tool_calls]
                    if msg_call_ids == record_call_ids:
                        pop_idx = i
                        break
            if pop_idx is None:
                # Couldn't find the matching native pair (e.g. it was
                # already popped, or messages were re-shuffled by a
                # nudge inject). Skip the prune for this turn — better
                # to keep the message than risk corrupting the order.
                continue
            # Drop the AssistantMessage and the immediately-following
            # ToolResultMessages whose tool_call_id matches one in
            # this batch. Stop at the first non-matching message.
            wanted_ids = {tc.id for tc in tool_calls}
            del self._messages[pop_idx]
            while pop_idx < len(self._messages) and isinstance(
                self._messages[pop_idx], ToolResultMessage
            ) and self._messages[pop_idx].tool_call_id in wanted_ids:
                del self._messages[pop_idx]

        # Drop the collapsed records (by index, since they may not be
        # contiguous — read-tool turns are skipped over).
        keep_set = set(range(len(self._recent_turn_records))) - set(collapse_indices)
        self._recent_turn_records = [
            self._recent_turn_records[i] for i in sorted(keep_set)
        ]

        if self._collapsed_history:
            logger.info(
                "agent: collapsed %d old turns (%d history lines now, "
                "%d native turns kept)",
                excess, len(self._collapsed_history),
                len(self._recent_turn_records),
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
                f"{_PAGE_STATE_TAG}\n{state_block}URL: {state.url}\n"
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
                body = f"{_PAGE_STATE_TAG}\n{state_block}{hint}\n\n{dom_text}"
            else:
                body = f"{_PAGE_STATE_TAG}\n{state_block}{dom_text}"

        if self.use_vision and state.screenshot:
            self._messages.append(
                UserMessage(
                    content=[
                        TextPart(text=body),
                        ImagePart(data=state.screenshot, media_type="image/png"),
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
                        f"skipped: page navigated mid-batch "
                        f"({start_url!r} → {cur_url!r}). The `[N]` "
                        f"indices in your tool calls were from the old "
                        f"page; re-plan on the next turn using the "
                        f"fresh snapshot."
                    )
                    is_error = True
                elif skipped.name in self._INDEXED_TOOLS:
                    err = (
                        f"skipped: an earlier action in this batch "
                        f"mutated the DOM, so the `[N]` index in this "
                        f"call may now point to a different element "
                        f"(or no element at all). Wait for the next "
                        f"turn's fresh snapshot before clicking / "
                        f"typing / scrolling-to indexed elements. "
                        f"Tip: chaining `[type_text, click]` rarely "
                        f"works because typing triggers autocomplete "
                        f"and form validation — split them across "
                        f"turns."
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
                        ActionResult(error=err),
                        ToolResultMessage(
                            tool_call_id=skipped.id,
                            name=skipped.name,
                            content=err,
                            is_error=is_error,
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
                f"tool timed out after {self.tool_timeout:.0f}s "
                f"(call: {tc.name}({tc.args})). The page may be stuck "
                f"loading or the element unresponsive. Try a different "
                f"approach next turn — wait_for_navigation, switch tabs, "
                f"or pick a different element."
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

        # Long-output spill: if the formatted text is large enough to
        # bloat the conversation, write the full content to a scratchpad
        # file and replace what the LLM sees with a head+tail preview
        # plus a recovery hint pointing at the file. The LLM can drill
        # in via grep_scratchpad / read_scratchpad on the next turn
        # without re-running the original tool. Image-only returns
        # (single ImagePart, no text) are passed through unchanged.
        if (
            self.scratchpad_enabled
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
        metadata = StepMetadata(
            step_number=step_n,
            input_tokens=sum(u.get("input", 0) for u in step_calls),
            output_tokens=sum(u.get("output", 0) for u in step_calls),
            cache_read_tokens=sum(u.get("cache_read", 0) for u in step_calls),
            step_start_time=t_start,
            step_end_time=time.monotonic(),
        )
        self.state.history.history.append(
            AgentHistory(state=state, output=output, result=results, metadata=metadata)
        )

    def _record_usage(self, step_n: int, usage) -> None:
        self.usage_log.append(
            {
                "step": step_n,
                "input": usage.input,
                "output": usage.output,
                "cache_read": usage.cache_read,
                "cache_creation": usage.cache_creation,
            }
        )
        self.state.history.usage = self.state.history.usage + usage

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
