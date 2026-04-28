"""End-to-end smoke test: launch Chromium via Rust, navigate, screenshot."""

import asyncio
from pathlib import Path

from browser_use_rs import BrowserSession


async def main() -> None:
    session = BrowserSession()
    await session.start()
    try:
        await session.navigate("https://example.com")
        url = await session.current_url()
        png = await session.screenshot()
        out = Path(__file__).parent / "example.png"
        out.write_bytes(png)
        print(f"loaded: {url}")
        print(f"screenshot: {len(png)} bytes -> {out}")
    finally:
        await session.stop()


if __name__ == "__main__":
    asyncio.run(main())
