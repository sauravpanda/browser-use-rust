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
    await session.click_index(index)
    return f"clicked [{index}]"


@tool
async def type_text(session, index: int, text: str) -> str:
    """Type text into an input element by its [N] index. The element is focused first.

    Args:
        index: The [N] index of the input element.
        text: The text to type.
    """
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
async def scroll(session, dy: float) -> str:
    """Scroll the page vertically by a relative offset.

    Args:
        dy: Pixels to scroll. Positive scrolls down, negative scrolls up.
    """
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


BROWSER_TOOLS = [
    navigate,
    dom_snapshot,
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
]
