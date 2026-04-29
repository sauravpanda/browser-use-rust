"""End-to-end agent demo using Gemini 2.5 Flash with native function calling.

Requires:
    export GEMINI_API_KEY=...
    (or GOOGLE_API_KEY)

Usage:
    python python/examples/agent_demo_gemini.py

Cost ceiling: Gemini 2.5 Flash is ~$0.30 input / $2.50 output per 1M
tokens — roughly 10x cheaper than Opus 4.7 for the same workload. Expect
$0.005 - $0.10 for this demo. Total cost printed at the end.
"""

import asyncio

from browser_use_rs._browser_tools import BROWSER_TOOLS
from browser_use_rs.agent_gemini import GeminiAgent

# Gemini 2.5 Flash pricing per 1M tokens (regular, non-cached).
COST_INPUT_PER_M = 0.30
COST_OUTPUT_PER_M = 2.50
COST_CACHE_READ_PER_M = COST_INPUT_PER_M * 0.25


async def main() -> None:
    agent = GeminiAgent(
        task=(
            "Go to https://news.ycombinator.com and tell me the title and "
            "current points of the top story. Be concise — one sentence."
        ),
        tools=BROWSER_TOOLS,
        max_steps=8,
    )

    print(f"task: {agent.task}")
    print(f"model={agent.model} max_steps={agent.max_steps}")
    print()

    result = await agent.run()

    print("\n=== FINAL ANSWER ===")
    print(result)

    print("\n=== USAGE PER STEP ===")
    total_in = total_out = total_cache_read = 0
    for u in agent.usage_log:
        print(
            f"step {u['step']}: in={u['input']:>5} out={u['output']:>5} "
            f"cache_read={u['cache_read']:>5}"
        )
        total_in += u["input"]
        total_out += u["output"]
        total_cache_read += u["cache_read"]

    cost = (
        total_in * COST_INPUT_PER_M
        + total_out * COST_OUTPUT_PER_M
        + total_cache_read * COST_CACHE_READ_PER_M
    ) / 1_000_000

    print(
        f"\nTOTAL tokens: in={total_in} out={total_out} "
        f"cache_read={total_cache_read}"
    )
    print(f"TOTAL cost: ${cost:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
