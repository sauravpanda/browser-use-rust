"""Built-in tools wrapping the Rust BrowserSession primitives."""

from __future__ import annotations

import asyncio
import base64
import os
import time

from browser_use_rs.tools import tool


@tool
async def navigate(session, url: str) -> str:
    """Navigate the browser to a URL and wait for the page to settle.

    Args:
        url: The full URL to load (must include https:// or http://).
    """
    await session.navigate(url)
    return f"loaded {url}"


@tool
async def dom_snapshot(session) -> str:
    """Snapshot the current page's interactive elements as a numbered list.

    Returns one line per element in the form `[N]<tag attrs>text</tag>`.
    Refer to elements by their [N] index in subsequent click/type calls.
    Indices are NOT stable across page changes — re-snapshot after navigate,
    scroll, click, or type before acting again.
    """
    snap = await session.dom_snapshot()
    return snap.to_llm_string()


@tool
async def click(session, index: int) -> str:
    """Click an element by its [N] index from the most recent dom_snapshot.

    Args:
        index: The [N] index of the element to click.
    """
    # v0.8.31 (codex F2): detect a click that opens a new tab and
    # auto-switch to it so the next dom_snapshot reflects the new
    # page. News/article/review sites frequently open `target=_blank`
    # links from list pages; without auto-switch the agent stays on
    # the list page, snapshots the same elements again, and either
    # loops or fabricates content from the snippet.
    #
    # Tab list shape: list[(tid, url, title, ttype, active)]. Only
    # 'page' targets count as tabs — iframe contexts are common and
    # noisy. Using id-set diff is robust to popups that flash open
    # and close (no spurious switch).
    try:
        tabs_before = await session.list_tabs()
        before_ids = {tid for tid, _u, _t, ttype, _a in tabs_before if ttype == 'page'}
    except Exception:
        before_ids = None

    await session.click_index(index)

    if before_ids is None:
        return f"clicked [{index}]"

    # Brief settle window — Chrome often surfaces the new target a few
    # tens of ms after the click event. Two short polls keep latency
    # bounded if no new tab actually opened.
    new_tid = None
    for _ in range(2):
        try:
            await asyncio.sleep(0.15)
            tabs_after = await session.list_tabs()
        except Exception:
            break
        added = [
            (tid, url, title) for tid, url, title, ttype, _active in tabs_after
            if ttype == 'page' and tid not in before_ids
        ]
        if added:
            # Pick the most recently added tab (last in list); if Chrome
            # opened more than one (rare), the user typically wants the
            # most recent.
            new_tid, new_url, new_title = added[-1]
            break

    if new_tid is None:
        return f"clicked [{index}]"

    try:
        await session.switch_tab(new_tid)
    except Exception as e:
        # Switch failed — agent can recover via list_tabs/switch_tab
        # manually. Report so it knows.
        return (
            f"clicked [{index}] — opened new tab {new_tid} "
            f"({new_url}) but auto-switch failed ({type(e).__name__}); "
            f"call switch_tab({new_tid}) on next turn"
        )
    return (
        f"clicked [{index}] — opened new tab and switched to it "
        f"(target_id={new_tid}, url={new_url}, title={new_title!r}). "
        f"Take a fresh dom_snapshot before the next interaction."
    )


@tool
async def type_text(session, index: int, text: str, clear: bool = True) -> str:
    """Type text into an input element by its [N] index. The element is focused first.

    Args:
        index: The [N] index of the input element.
        text: The text to type.
        clear: When True (default) clear existing input before typing.
            Pass False to append. Mirrors upstream's `input` action.
    """
    if clear:
        # Best-effort clear: select-all + delete via JS, then type.
        try:
            js = (
                f"(() => {{ const el = document.querySelector(`[data-bu-idx=\"{int(index)}\"]`);"
                " if (el) { el.value = ''; el.dispatchEvent(new Event('input', {bubbles:true})); }"
                " return ''; })()"
            )
            await session.evaluate(js)
        except Exception:
            pass
    await session.type_index(index, text)
    return f"typed into [{index}]"


@tool
async def upload_file(session, index: int, path: str) -> str:
    """Attach a file to an `<input type="file">` element by its [N] index.

    The file path must be absolute. Use this for forms that need a real file
    selection (resume upload, profile picture, etc).

    Args:
        index: The [N] index of the file input element.
        path: Absolute path to the file to attach.
    """
    await session.upload_file(index, [path])
    return f"attached {path} to [{index}]"


@tool
async def scroll(
    session,
    dy: float = 0,
    direction: str = "",
    pages: float = 0,
    index: int = 0,
) -> str:
    """Scroll vertically. Page-level by default; in-container when `index` is given.

    Args:
        dy: Pixels to scroll. Positive=down, negative=up. Used when
            non-zero. Original signature.
        direction: 'up' or 'down' — used when `pages` > 0. Mirrors
            upstream's `scroll(down=bool, pages=float)` form.
        pages: Number of viewport-heights (or container-heights when
            `index` is set) to scroll. Combined with `direction`.
        index: Optional [N] of an indexed scroll container (one of the
            elements rendered with the `|scroll|` prefix). When set,
            scroll INSIDE that container instead of the whole page.
            Required for "Show more"-style virtualized lists, filter
            panes, and map/list panels where the parent doesn't scroll.
            Mirrors upstream `ScrollEvent.index`.
    """
    # v0.8.18: in-container scroll via JS evaluate when `index` is set.
    # Doesn't need a Rust API change — uses the data-bu-idx attribute
    # the DOM walker already stamps on every indexed element.
    if index > 0:
        # Compute pixel delta from `dy` / `pages+direction`.
        try:
            ch_raw = await session.evaluate(
                f"(() => {{ const el = document.querySelector('[data-bu-idx=\"{int(index)}\"]'); "
                f"return el ? String(el.clientHeight || el.offsetHeight || 600) : '0'; }})()"
            )
            container_h = float(ch_raw) if ch_raw and ch_raw != "0" else 600.0
        except Exception:
            container_h = 600.0
        if pages > 0 and direction in ("up", "down"):
            delta = container_h * pages * (-1 if direction == "up" else 1)
        elif dy != 0:
            delta = dy
        else:
            delta = container_h  # single container-height down by default
        try:
            await session.evaluate(
                f"(() => {{ const el = document.querySelector('[data-bu-idx=\"{int(index)}\"]'); "
                f"if (!el) return 'no-element'; el.scrollBy({{top: {delta:.0f}, behavior: 'auto'}}); return 'ok'; }})()"
            )
            return f"scrolled inside [{index}] by {delta:.0f}px (container_h={container_h:.0f})"
        except Exception as e:
            return f"(scroll-in-container failed: {type(e).__name__}: {e})"

    if pages > 0 and direction in ("up", "down"):
        # Translate page-based scroll to pixel scroll.
        try:
            vh = await session.evaluate("(() => String(window.innerHeight))()")
            view_h = float(vh) if vh else 800
        except Exception:
            view_h = 800
        delta = view_h * pages * (-1 if direction == "up" else 1)
        await session.scroll(delta)
        return f"scrolled {direction} {pages} pages ({delta:.0f}px)"
    await session.scroll(dy)
    return f"scrolled {dy} px"


@tool
async def scroll_to(session, index: int) -> str:
    """Scroll element [N] from the most recent dom_snapshot into view (centered).

    Args:
        index: The [N] index of the element to bring into view.
    """
    await session.scroll_to_index(index)
    return f"scrolled to [{index}]"


@tool
async def scroll_to_top(session) -> str:
    """Scroll to the very top of the page."""
    await session.scroll_to_top()
    return "scrolled to top"


@tool
async def scroll_to_bottom(session) -> str:
    """Scroll to the very bottom of the page."""
    await session.scroll_to_bottom()
    return "scrolled to bottom"


@tool
async def screenshot(session) -> dict:
    """Capture a PNG of the current viewport. The image is returned to you visually."""
    png = await session.screenshot()
    return {
        "_type": "image",
        "media_type": "image/png",
        "data": base64.b64encode(png).decode("ascii"),
    }


@tool
async def save_pdf(session) -> str:
    """Render the current page to a PDF and save it under the session's
    download directory. Returns the absolute file path. Headless-only.
    """
    pdf_bytes = await session.pdf()
    download_dir = await session.download_dir()
    fname = f"page-{int(time.time())}.pdf"
    path = os.path.join(download_dir, fname)
    with open(path, "wb") as f:
        f.write(pdf_bytes)
    return f"saved {len(pdf_bytes)} bytes to {path}"


@tool
async def get_text(session, selector: str) -> str:
    """Read the visible text of the first element matching a CSS selector.

    Use when you need the content of a specific element you can describe
    with a CSS selector (id, class, tag, attribute) — faster than reading
    the whole page.

    Args:
        selector: A CSS selector, e.g. "#main h1" or "article.story .title".
    """
    text = await session.get_text(selector)
    return text or "(no element matched, or element has no text)"


@tool
async def page_text(session, max_chars: int = 10000) -> str:
    """Read the rendered text of the entire page body.

    Use when you need to read prose / article content that isn't well
    addressed by interactive-element indices. Capped at max_chars to keep
    the context window in check.

    Args:
        max_chars: Maximum characters to return. Default 10000.
    """
    return await session.page_text(max_chars)


@tool
async def get_links(session) -> str:
    """List all visible links on the page as `<text> -> <url>` lines."""
    links = await session.get_links()
    if not links:
        return "(no links on page)"
    return "\n".join(f"{text or '(no text)'} -> {href}" for href, text in links)


@tool
async def sleep(session, seconds: float) -> str:
    """Pause for the given number of seconds. Use for short waits when no
    selector or event reliably signals readiness (animations, debounced
    inputs). Prefer wait_for or wait_for_navigation when a real signal exists.

    Args:
        seconds: Wait duration. Capped at 30 to keep loops responsive.
    """
    capped = max(0.0, min(seconds, 30.0))
    await asyncio.sleep(capped)
    return f"slept {capped}s"


@tool
async def wait_for_navigation(session, timeout_ms: int = 10000) -> str:
    """Wait for the active tab to fire its next page-load event.

    Use after a click that triggers a full navigation (vs an SPA route
    change). Returns whether the event fired before the timeout.

    Args:
        timeout_ms: Max milliseconds to wait. Default 10000.
    """
    fired = await session.wait_for_navigation(timeout_ms)
    return "navigation complete" if fired else f"timeout — no load event in {timeout_ms}ms"


@tool
async def wait_for(session, selector: str, timeout_ms: int = 5000) -> str:
    """Wait for an element matching the CSS selector to appear in the DOM.

    Use after a click or navigation that triggers async content (SPAs,
    lazy-loaded sections). Returns whether the element appeared before the
    timeout. Same-origin iframes are searched.

    Args:
        selector: A CSS selector to wait for, e.g. ".search-result" or "#login-success".
        timeout_ms: Max milliseconds to wait. Default 5000.
    """
    found = await session.wait_for_selector(selector, timeout_ms)
    return f"appeared: {selector!r}" if found else f"timeout — {selector!r} not found in {timeout_ms}ms"


@tool
async def list_tabs(session) -> str:
    """List all attachable contexts — top-level tabs and cross-origin
    iframes. Each line is `* [type:target_id] url — title`, where `*`
    marks the active context. Use the target_id with switch_tab. Switching
    to an iframe target lets you snapshot/click inside that frame.
    """
    tabs = await session.list_tabs()
    if not tabs:
        return "(no tabs)"
    return "\n".join(
        f"{'*' if active else ' '} [{ttype}:{tid}] {url} — {title}"
        for tid, url, title, ttype, active in tabs
    )


@tool
async def switch_tab(session, target_id: str) -> str:
    """Make a different tab active. All subsequent click/snapshot/navigate
    target this tab. The cached snapshot is cleared — call dom_snapshot
    after switching.

    Args:
        target_id: The target_id of the tab (from list_tabs).
    """
    await session.switch_tab(target_id)
    return f"switched to tab {target_id}"


@tool
async def new_tab(session, url: str = "") -> str:
    """Open a new tab and make it active. The cached snapshot is cleared.

    Args:
        url: Initial URL. Empty string means about:blank.
    """
    tid, opened_url, _title, _ttype, _active = await session.new_tab(url)
    return f"opened tab [{tid}] {opened_url}"


@tool
async def close_tab(session, target_id: str) -> str:
    """Close a tab. If it was active, the session switches to another tab.
    Errors if it would close the last remaining tab.

    Args:
        target_id: The target_id of the tab to close (from list_tabs).
    """
    await session.close_tab(target_id)
    return f"closed tab {target_id}"


@tool
async def get_cookies(session) -> str:
    """List all cookies the browser holds, one per line as
    `name=<value> domain=<d> path=<p> [secure] [httpOnly]`. Use to inspect
    auth or session state.
    """
    cookies = await session.get_cookies()
    if not cookies:
        return "(no cookies)"
    lines = []
    for name, value, domain, path, _expires, secure, http_only in cookies:
        flags = []
        if secure:
            flags.append("secure")
        if http_only:
            flags.append("httpOnly")
        flag_str = f" [{','.join(flags)}]" if flags else ""
        lines.append(f"{name}={value} domain={domain} path={path}{flag_str}")
    return "\n".join(lines)


@tool
async def clear_cookies(session) -> str:
    """Clear ALL browser cookies. Useful for resetting auth between tasks."""
    await session.clear_cookies()
    return "all cookies cleared"


@tool
async def list_downloads(session) -> str:
    """List downloads triggered during this session. Each line includes the
    state (inProgress / completed / canceled), the suggested filename, and
    the on-disk path. Read completed files from disk to inspect them.
    """
    rows = await session.list_downloads()
    if not rows:
        return "(no downloads)"
    lines = []
    for guid, name, url, state, recv, total, path in rows:
        bytes_str = f"{recv}/{total}" if total else f"{recv}"
        lines.append(f"[{state}] {name or '(unnamed)'} ({bytes_str} bytes) -> {path}  src: {url}")
    return "\n".join(lines)


@tool
async def grep_scratchpad(session, path: str, pattern: str) -> str:
    """Search a scratchpad file (full text saved when a tool result was too
    long to inline) for matching lines. Use this when an earlier tool call
    returned a `[SCRATCHPAD]` banner pointing at a file path — pass that
    path here with a regex or substring pattern to drill into the content
    without re-running the original tool.

    Args:
        path: Absolute path to the scratchpad file (from a prior `[SCRATCHPAD]` banner).
        pattern: Python regex (preferred) or substring to search for.
    """
    from browser_use_rs._scratchpad import grep as _grep

    return _grep(path, pattern)


@tool
async def read_scratchpad(session, path: str, offset: int = 1, limit: int = 100) -> str:
    """Read a chunk of a scratchpad file by line offset. Use when grep
    isn't precise enough — e.g. to see the next page of a long article
    after locating an interesting region with grep_scratchpad.

    Args:
        path: Absolute path to the scratchpad file.
        offset: 1-based starting line number. Default 1.
        limit: Number of lines to return. Default 100.
    """
    from browser_use_rs._scratchpad import read_offset

    return read_offset(path, offset=offset, limit=limit)


# Note: `dom_snapshot` is intentionally NOT in the default tool set.
# Agent._loop auto-injects a fresh DOM snapshot at the start of every turn
# via _inject_page_state(), so the LLM already has the page state without
# spending a round trip on it. The function remains importable for callers
# who want to explicitly include it in a custom tool list.
BROWSER_TOOLS = [
    navigate,
    click,
    type_text,
    upload_file,
    scroll,
    scroll_to,
    scroll_to_top,
    scroll_to_bottom,
    screenshot,
    save_pdf,
    get_text,
    page_text,
    get_links,
    wait_for,
    wait_for_navigation,
    sleep,
    list_tabs,
    switch_tab,
    new_tab,
    close_tab,
    list_downloads,
    get_cookies,
    clear_cookies,
    # Scratchpad recovery — used when a prior tool result spilled to disk
    # because it exceeded the in-context size threshold. See _scratchpad.py.
    grep_scratchpad,
    read_scratchpad,
]

# Extended stateless tools (v0.6.0). Mirror upstream browser_use's
# search_page / find_elements / find_text / dropdown handling /
# send_keys / go_back / evaluate. Agent-aware tools (extract_structured_data,
# file system) are constructed per-agent in _extra_tools.make_extra_tools.
from browser_use_rs._extra_tools import EXTRA_STATELESS_TOOLS  # noqa: E402

BROWSER_TOOLS.extend(EXTRA_STATELESS_TOOLS)


# Upstream-name aliases (v0.6.2). When the eval framework's prompt
# references upstream's action names (input_text, click_element_by_index,
# scroll_down/up, search, extract, wait, etc.) the LLM may try to call
# them by those names. Without aliases the call fails as `unknown tool`
# and burns a turn. Each alias is registered as the same callable under
# both names so either form works.
def _alias(target_tool, alias_name):
    """Make a copy of `target_tool` re-registered under `alias_name`."""
    import copy
    new = copy.copy(target_tool)
    new.name = alias_name
    return new


_UPSTREAM_NAME_ALIASES = {
    # upstream name -> our tool callable name
    # v0.7.2 corrections per codex audit:
    #   - 'search' previously mapped to search_page, but upstream's
    #     `search` is the web search action. Re-pointed to web_search.
    #     search_page now reachable via 'search_text'/'find_in_page'.
    "input": "type_text",
    "input_text": "type_text",
    "click_element_by_index": "click",
    "scroll_down": "scroll_to_bottom",
    "scroll_up": "scroll_to_top",
    "wait": "sleep",
    "search": "web_search",  # was search_page — fixed v0.7.2
    "search_google": "web_search",
    "extract": "extract_structured_data",
    "extract_structured_data_from_page": "extract_structured_data",
    "save_as_pdf": "save_pdf",
    "evaluate": "evaluate_js",
    "dropdown_options": "get_dropdown_options",
    "select_option": "select_dropdown",
    "press_keys": "send_keys",
    "key_press": "send_keys",
    "back": "go_back",
    "history_back": "go_back",
    "screenshot_page": "screenshot",
    "scroll_to_text": "find_text",
    "find_in_page": "search_page",
    "query_selector_all": "find_elements",
    "css_select": "find_elements",
    "search_text": "search_page",
}

# Reverse map: alias-or-canonical -> canonical name. Used by guards
# (_INDEXED_TOOLS, _READ_ONLY_TOOLS) so they apply to any spelling
# the LLM uses. v0.7.2.
ALIAS_TO_CANONICAL: dict[str, str] = {}
for _t in BROWSER_TOOLS:
    ALIAS_TO_CANONICAL[_t.name] = _t.name  # canonicals map to themselves
for _alias_name, _canon in _UPSTREAM_NAME_ALIASES.items():
    ALIAS_TO_CANONICAL[_alias_name] = _canon

_by_name = {t.name: t for t in BROWSER_TOOLS}
for upstream_name, our_name in _UPSTREAM_NAME_ALIASES.items():
    if upstream_name in _by_name:
        continue  # already registered (don't double-register)
    target = _by_name.get(our_name)
    if target is None:
        continue
    BROWSER_TOOLS.append(_alias(target, upstream_name))
