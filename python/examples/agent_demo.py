"""End-to-end agent demo using Claude Opus 4.7 with native tool calling.

Requires:
    export ANTHROPIC_API_KEY=...

Usage:
    python python/examples/agent_demo.py

Cost ceiling: with the safe defaults below (max_steps=8, max_tokens=4000,
effort=high), expect roughly $0.10 - $1.50 depending on how many steps
the model takes and how much the system prompt + tools cache hit. The
full cost is printed at the end.
"""

import asyncio

from browser_use_rs import Agent
from browser_use_rs.llm import ChatAnthropic

# Opus 4.7 pricing per 1M tokens (cached at the time of writing).
COST_INPUT_PER_M = 5.00
COST_OUTPUT_PER_M = 25.00
COST_CACHE_READ_PER_M = COST_INPUT_PER_M * 0.1   # ~0.1× input
COST_CACHE_WRITE_PER_M = COST_INPUT_PER_M * 1.25  # 5-min TTL writes


async def main() -> None:
    llm = ChatAnthropic(
        model="claude-opus-4-7",
        max_tokens=4000,
        effort="high",
    )
    agent = Agent(
        task=(
            "Go to https://news.ycombinator.com and tell me the title and "
            "current points of the top story. Be concise — one sentence."
        ),
        llm=llm,
        max_steps=8,
    )

    print(f"task: {agent.task}")
    print(f"model={llm.model} effort={llm.effort} max_steps={agent.max_steps}")
    print()

    history = await agent.run()

    print("\n=== FINAL ANSWER ===")
    print(history.final_result())

    print("\n=== USAGE PER STEP ===")
    total_in = total_out = total_cache_read = total_cache_write = 0
    for u in agent.usage_log:
        print(
            f"step {u['step']}: in={u['input']:>5} out={u['output']:>5} "
            f"cache_read={u['cache_read']:>6} cache_write={u['cache_creation']:>5}"
        )
        total_in += u["input"]
        total_out += u["output"]
        total_cache_read += u["cache_read"]
        total_cache_write += u["cache_creation"]

    cost = (
        total_in * COST_INPUT_PER_M
        + total_out * COST_OUTPUT_PER_M
        + total_cache_read * COST_CACHE_READ_PER_M
        + total_cache_write * COST_CACHE_WRITE_PER_M
    ) / 1_000_000

    print(
        f"\nTOTAL tokens: in={total_in} out={total_out} "
        f"cache_read={total_cache_read} cache_write={total_cache_write}"
    )
    print(f"TOTAL cost: ${cost:.4f}")


if __name__ == "__main__":
    asyncio.run(main())
