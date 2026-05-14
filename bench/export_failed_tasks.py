"""Export failed eval tasks from the dashboard for local reruns.

Usage:
    EVALUATION_TOOL_URL=... EVALUATION_TOOL_SECRET_KEY=... \
        python bench/export_failed_tasks.py <run_id> --top 20

Prints a JSON document with failed tasks sorted by cost plus ready-to-run
`bench/run_ours.py` commands. The commands still require a model key
(`GEMINI_API_KEY` or `GOOGLE_API_KEY`) in the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import sys
import urllib.parse
import urllib.request
from typing import Any

from pathlib import Path

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from env_file import load_dotenv  # noqa: E402


def _env(name: str) -> str:
    load_dotenv()
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Set {name}")
    return value


def _get_json(base_url: str, secret: str, path: str, params: dict[str, Any]) -> Any:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}?{query}",
        headers={"Authorization": f"Bearer {secret}"},
    )
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.load(resp)


def _parse_usage(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
            return parsed if isinstance(parsed, dict) else {}
        except json.JSONDecodeError:
            return {}
    return raw if isinstance(raw, dict) else {}


def _task_text(task: Any) -> str:
    if isinstance(task, str):
        return task
    if isinstance(task, dict):
        for key in ("prompt", "task", "description", "goal", "input"):
            value = task.get(key)
            if value:
                return str(value)
    return json.dumps(task, ensure_ascii=False)


def _cost(task: dict[str, Any]) -> float:
    usage = _parse_usage(task.get("usage"))
    for key in ("total_cost", "cost", "costUsd", "totalCost"):
        value = usage.get(key)
        if value is not None:
            try:
                return float(value)
            except (TypeError, ValueError):
                return 0.0
    return 0.0


def _rerun_command(task_text: str, max_steps: int) -> str:
    return (
        ".venv/bin/python bench/run_ours.py "
        f"{shlex.quote(task_text)} {int(max_steps)}"
    )


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="Eval dashboard run id")
    parser.add_argument("--top", type=int, default=0, help="Limit to top N failures by cost")
    parser.add_argument(
        "--max-steps",
        type=int,
        default=100,
        help="max_steps argument to include in local rerun commands",
    )
    args = parser.parse_args()

    base_url = _env("EVALUATION_TOOL_URL")
    secret = _env("EVALUATION_TOOL_SECRET_KEY")
    data = _get_json(
        base_url,
        secret,
        "/api/getRunResults",
        {"runId": args.run_id, "onlyFailedTasks": "true", "limit": 250},
    )
    tasks = data.get("tasks", [])
    tasks = sorted(tasks, key=_cost, reverse=True)
    if args.top > 0:
        tasks = tasks[: args.top]

    out = []
    for task in tasks:
        text = _task_text(task.get("task"))
        command = _rerun_command(text, args.max_steps)
        out.append(
            {
                "taskId": task.get("taskId"),
                "errorCategory": task.get("errorCategory"),
                "steps": task.get("steps"),
                "cost": _cost(task),
                "task": text,
                "judgement": task.get("OM2W_judgement"),
                "finalResultResponse": task.get("finalResultResponse"),
                "rerunCommand": command,
            }
        )

    json.dump(
        {
            "runId": args.run_id,
            "failedTasks": len(out),
            "requiresModelEnv": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
            "tasks": out,
        },
        sys.stdout,
        indent=2,
        ensure_ascii=False,
    )
    print()


if __name__ == "__main__":
    main()
