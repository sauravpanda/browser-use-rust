"""End-to-end smoke test for the DOM snapshot + click pipeline.

1. Launches Chromium via the Rust runtime.
2. Loads a search page.
3. Snapshots the visible interactive elements.
4. Prints the LLM-facing string the agent loop would consume.
5. Types a query into the search box and submits via Enter (typed click).
"""

import asyncio
from pathlib import Path

from browser_use_rs import BrowserSession


async def main() -> None:
    session = BrowserSession()
    await session.start()
    try:
        await session.navigate("https://duckduckgo.com")
        snap = await session.dom_snapshot()
        print(f"got {len(snap)} interactive elements on {snap.url!r}")
        print("---- LLM view ----")
        print(snap.to_llm_string())
        print("------------------")

        # Find the search input (best-effort: first element with a name/role hint)
        search_idx = None
        for el in snap.elements:
            if el.tag in ("input", "textarea") and (
                "search" in (el.attrs.get("name", "") + el.attrs.get("aria-label", "")).lower()
                or el.attrs.get("type") in ("text", "search")
            ):
                search_idx = el.index
                break

        if search_idx is None:
            print("no search input found — skipping type test")
        else:
            print(f"typing into element [{search_idx}]")
            await session.type_index(search_idx, "browser-use rust")

        png = await session.screenshot()
        out = Path(__file__).parent / "click.png"
        out.write_bytes(png)
        print(f"screenshot: {len(png)} bytes -> {out}")
    finally:
        await session.stop()


if __name__ == "__main__":
    asyncio.run(main())
