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


BROWSER_TOOLS = [navigate, dom_snapshot, click, type_text, scroll, screenshot]
