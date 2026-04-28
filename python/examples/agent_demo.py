"""End-to-end agent demo using native Anthropic tool calling.

Requires:
    export ANTHROPIC_API_KEY=...

Usage:
    python python/examples/agent_demo.py
"""

import asyncio

from browser_use_rs._browser_tools import BROWSER_TOOLS
from browser_use_rs.agent import Agent


async def main() -> None:
    agent = Agent(
        task=(
            "Go to https://news.ycombinator.com and tell me the title and "
            "current points of the top story."
        ),
        tools=BROWSER_TOOLS,
        max_steps=10,
    )
    result = await agent.run()

    print("\n=== FINAL ANSWER ===")
    print(result)
    print("\n=== USAGE PER STEP ===")
    total_in = total_out = total_cache_read = total_cache_write = 0
    for u in agent.usage_log:
        print(
            f"step {u['step']}: in={u['input']} out={u['output']} "
            f"cache_read={u['cache_read']} cache_write={u['cache_creation']}"
        )
        total_in += u["input"]
        total_out += u["output"]
        total_cache_read += u["cache_read"]
        total_cache_write += u["cache_creation"]
    print(
        f"TOTAL: in={total_in} out={total_out} "
        f"cache_read={total_cache_read} cache_write={total_cache_write}"
    )


if __name__ == "__main__":
    asyncio.run(main())
