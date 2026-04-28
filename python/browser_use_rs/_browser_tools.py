"""Built-in tools wrapping the Rust BrowserSession primitives."""

from __future__ import annotations

import base64

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
async def scroll(session, dy: float) -> str:
    """Scroll the page vertically.

    Args:
        dy: Pixels to scroll. Positive scrolls down, negative scrolls up.
    """
    await session.scroll(dy)
    return f"scrolled {dy} px"


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


BROWSER_TOOLS = [
    navigate,
    dom_snapshot,
    click,
    type_text,
    scroll,
    screenshot,
    get_text,
    page_text,
    get_links,
]
