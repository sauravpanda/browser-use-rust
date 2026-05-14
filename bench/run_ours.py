"""Run one benchmark task with our unified Agent + ChatGoogle. Prints JSON to stdout.

Invoked by bench.py via subprocess. Reads task from argv[1].
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))
sys.path.insert(0, str(REPO / "python"))

from env_file import load_dotenv  # noqa: E402

# Gemini 2.5 Flash pricing per 1M tokens (regular, non-cached).
COST_INPUT_PER_M = 0.30
COST_OUTPUT_PER_M = 2.50
COST_CACHE_READ_PER_M = COST_INPUT_PER_M * 0.25


async def main() -> None:
    load_dotenv()
    task = sys.argv[1]
    max_steps = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        print(
            json.dumps(
                {
                    "system": "ours",
                    "task": task,
                    "completed": False,
                    "success": False,
                    "answer": (
                        "ERROR: missing GEMINI_API_KEY or GOOGLE_API_KEY "
                        "for ChatGoogle"
                    ),
                    "elapsed_s": 0,
                    "steps": 0,
                    "in_tokens": 0,
                    "out_tokens": 0,
                    "cache_read_tokens": 0,
                    "cost_usd": 0,
                }
            )
        )
        return

    from browser_use_rs import Agent
    from browser_use_rs.llm import ChatGoogle

    t0 = time.monotonic()
    agent = None
    try:
        agent = Agent(
            task=task,
            llm=ChatGoogle(model="gemini-3-flash-preview"),
            max_steps=max_steps,
            # The bench's shape mirrors what eval consumers do — vision off
            # since the model decides when to call screenshot itself.
            use_vision=False,
        )
        history = await agent.run()
        answer = history.final_result() or ""
        completed = history.is_done()
        success = history.is_successful()
    except Exception as e:
        answer = f"ERROR: {type(e).__name__}: {e}"
        completed = False
        success = False
    elapsed = time.monotonic() - t0

    usage_log = agent.usage_log if agent is not None else []
    in_tok = sum(u["input"] for u in usage_log)
    out_tok = sum(u["output"] for u in usage_log)
    cache_tok = sum(u["cache_read"] for u in usage_log)
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
                "success": success,
                "answer": answer,
                "elapsed_s": round(elapsed, 2),
                "steps": len(usage_log),
                "in_tokens": in_tok,
                "out_tokens": out_tok,
                "cache_read_tokens": cache_tok,
                "cost_usd": round(cost, 5),
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
