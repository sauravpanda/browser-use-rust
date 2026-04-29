"""Run one benchmark task with our unified Agent + ChatGoogle. Prints JSON to stdout.

Invoked by bench.py via subprocess. Reads task from argv[1].
"""

import asyncio
import json
import sys
import time

from browser_use_rs import Agent
from browser_use_rs.llm import ChatGoogle

# Gemini 2.5 Flash pricing per 1M tokens (regular, non-cached).
COST_INPUT_PER_M = 0.30
COST_OUTPUT_PER_M = 2.50
COST_CACHE_READ_PER_M = COST_INPUT_PER_M * 0.25


async def main() -> None:
    task = sys.argv[1]
    max_steps = int(sys.argv[2]) if len(sys.argv) > 2 else 12

    t0 = time.monotonic()
    agent = Agent(
        task=task,
        llm=ChatGoogle(model="gemini-3-flash-preview"),
        max_steps=max_steps,
        # The bench's shape mirrors what eval consumers do — vision off
        # since the model decides when to call screenshot itself.
        use_vision=False,
    )
    try:
        history = await agent.run()
        answer = history.final_result() or ""
        completed = history.is_done()
    except Exception as e:
        answer = f"ERROR: {type(e).__name__}: {e}"
        completed = False
    elapsed = time.monotonic() - t0

    in_tok = sum(u["input"] for u in agent.usage_log)
    out_tok = sum(u["output"] for u in agent.usage_log)
    cache_tok = sum(u["cache_read"] for u in agent.usage_log)
    cost = (
        in_tok * COST_INPUT_PER_M
        + out_tok * COST_OUTPUT_PER_M
        + cache_tok * COST_CACHE_READ_PER_M
    ) / 1_000_000

    print(
        json.dumps(
            {
                "system": "ours",
                "task": task,
                "completed": completed,
                "answer": answer,
                "elapsed_s": round(elapsed, 2),
                "steps": len(agent.usage_log),
                "in_tokens": in_tok,
                "out_tokens": out_tok,
                "cache_read_tokens": cache_tok,
                "cost_usd": round(cost, 5),
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
