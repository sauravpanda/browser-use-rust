"""Run one benchmark task with Python browser-use's Agent + ChatGoogle.

Invoked by bench.py via subprocess using browser-use's own venv. Reads
task from argv[1]. Prints JSON to stdout.
"""

import asyncio
import json
import os
import sys
import time
from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from env_file import load_dotenv  # noqa: E402

COST_INPUT_PER_M = 0.30
COST_OUTPUT_PER_M = 2.50
COST_CACHE_READ_PER_M = COST_INPUT_PER_M * 0.25


async def _aggregate_usage(agent) -> tuple[int, int, int]:
    """browser-use tracks usage centrally on agent.token_cost_service.
    Different versions expose either get_usage_summary() (sync), an async
    coroutine, or a usage_history list. Probe each in turn."""
    tcs = getattr(agent, "token_cost_service", None)
    if tcs is None:
        return 0, 0, 0
    try:
        summary = tcs.get_usage_summary()
        if hasattr(summary, "__await__"):
            summary = await summary
        # summary may be a TokenCostUsage-like object or a dict
        if hasattr(summary, "total_prompt_tokens"):
            return (
                int(getattr(summary, "total_prompt_tokens", 0) or 0),
                int(getattr(summary, "total_completion_tokens", 0) or 0),
                int(getattr(summary, "total_prompt_cached_tokens", 0) or 0),
            )
        if isinstance(summary, dict):
            return (
                int(summary.get("total_prompt_tokens", 0) or 0),
                int(summary.get("total_completion_tokens", 0) or 0),
                int(summary.get("total_prompt_cached_tokens", 0) or 0),
            )
    except Exception:
        pass
    # Fallback: walk usage_history if it exists
    in_tok = out_tok = cache_tok = 0
    for entry in getattr(tcs, "usage_history", []) or []:
        in_tok += int(getattr(entry, "prompt_tokens", 0) or 0)
        out_tok += int(getattr(entry, "completion_tokens", 0) or 0)
        cache_tok += int(getattr(entry, "prompt_cached_tokens", 0) or 0)
    return in_tok, out_tok, cache_tok


async def main() -> None:
    load_dotenv()
    task = sys.argv[1]
    max_steps = int(sys.argv[2]) if len(sys.argv) > 2 else 12
    if not (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")):
        print(
            json.dumps(
                {
                    "system": "theirs",
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

    from browser_use import Agent
    from browser_use.llm import ChatGoogle

    llm = ChatGoogle(model="gemini-3-flash-preview", temperature=0)

    t0 = time.monotonic()
    agent = None
    try:
        # Match the eval workflow's actual upstream config so the bench
        # is comparing like-for-like. Eval invokes upstream browser_use
        # with `--flash-mode --max-actions-per-step 4 --use-vision
        # --images-per-step 1`. Without these, upstream runs in its
        # verbose default mode and our bench overstates our advantage
        # (we've seen 3× faster locally but slower on eval).
        agent = Agent(
            task=task,
            llm=llm,
            flash_mode=True,
            max_actions_per_step=4,
            use_vision=True,
            images_per_step=1,
        )
        history = await agent.run(max_steps=max_steps)
        answer = history.final_result() or ""
        completed = bool(history.is_done())
        success = history.is_successful()
        steps = len(getattr(history, "history", []) or [])
    except Exception as e:
        answer = f"ERROR: {type(e).__name__}: {e}"
        completed = False
        success = False
        steps = 0
    elapsed = time.monotonic() - t0

    if agent is not None:
        in_tok, out_tok, cache_tok = await _aggregate_usage(agent)
    else:
        in_tok = out_tok = cache_tok = 0

    cost = (
        in_tok * COST_INPUT_PER_M
        + out_tok * COST_OUTPUT_PER_M
        + cache_tok * COST_CACHE_READ_PER_M
    ) / 1_000_000

    print(
        json.dumps(
            {
                "system": "theirs",
                "task": task,
                "completed": completed,
                "success": success,
                "answer": answer,
                "elapsed_s": round(elapsed, 2),
                "steps": steps,
                "in_tokens": in_tok,
                "out_tokens": out_tok,
                "cache_read_tokens": cache_tok,
                "cost_usd": round(cost, 5),
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
