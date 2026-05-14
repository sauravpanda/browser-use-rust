"""Rerun failed eval-dashboard tasks locally.

Usage:
    export EVALUATION_TOOL_URL=...
    export EVALUATION_TOOL_SECRET_KEY=...
    export GEMINI_API_KEY=...   # or GOOGLE_API_KEY
    .venv/bin/python bench/rerun_failed_tasks.py <run_id> --top 10
    .venv/bin/python bench/rerun_failed_tasks.py <run_id> --top 10 --dry-run

Fetches failed tasks from the dashboard, sorted by historical cost, then
runs them through bench/run_ours.py and/or bench/run_theirs.py. Results
are written as JSON so cost/latency deltas can be inspected after a
patch. The script intentionally does not judge semantic correctness; use
it for local smoke/cost/step comparisons before launching a full eval.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parent.parent
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


def _has_model_key() -> bool:
    load_dotenv()
    return bool(os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY"))


def _get_json(base_url: str, secret: str, path: str, params: dict[str, Any]) -> Any:
    query = urllib.parse.urlencode(params)
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}?{query}",
        headers={"Authorization": f"Bearer {secret}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=90) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace").strip()
        suffix = f": {detail[-500:]}" if detail else ""
        raise SystemExit(f"Dashboard request failed: HTTP {e.code} {e.reason}{suffix}")
    except urllib.error.URLError as e:
        raise SystemExit(f"Dashboard request failed: {e.reason}") from e


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


def _fetch_failed_tasks(run_id: str, limit: int) -> list[dict[str, Any]]:
    base_url = _env("EVALUATION_TOOL_URL")
    secret = _env("EVALUATION_TOOL_SECRET_KEY")
    data = _get_json(
        base_url,
        secret,
        "/api/getRunResults",
        {"runId": run_id, "onlyFailedTasks": "true", "limit": max(limit, 250)},
    )
    return sorted(data.get("tasks", []), key=_cost, reverse=True)


def _filter_tasks_by_id(
    tasks: list[dict[str, Any]],
    task_ids: list[str],
) -> list[dict[str, Any]]:
    if not task_ids:
        return tasks
    wanted = {str(tid) for tid in task_ids}
    by_id = {str(task.get("taskId")): task for task in tasks}
    missing = [tid for tid in task_ids if str(tid) not in by_id]
    if missing:
        raise SystemExit(f"Task id(s) not found in failed run: {', '.join(missing)}")
    return [by_id[str(tid)] for tid in task_ids]


def _runner_command(
    *,
    python_bin: str,
    runner: str,
    task: str,
    max_steps: int,
) -> list[str]:
    runner_path = Path(runner)
    if not runner_path.is_absolute():
        runner_path = REPO / runner_path
    return [python_bin, str(runner_path), task, str(int(max_steps))]


def _planned_commands(
    *,
    python_bin: str,
    runners: dict[str, str],
    systems: list[str],
    task: str,
    max_steps: int,
) -> dict[str, str]:
    return {
        system: shlex.join(
            _runner_command(
                python_bin=python_bin,
                runner=runners[system],
                task=task,
                max_steps=max_steps,
            )
        )
        for system in systems
    }


def _run_one(
    *,
    python_bin: str,
    runner: str,
    task: str,
    max_steps: int,
    timeout_s: int,
) -> dict[str, Any]:
    cmd = _runner_command(
        python_bin=python_bin,
        runner=runner,
        task=task,
        max_steps=max_steps,
    )
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            text=True,
            capture_output=True,
            timeout=timeout_s,
            check=False,
            cwd=str(REPO),
        )
    except subprocess.TimeoutExpired as e:
        return {
            "completed": False,
            "success": False,
            "answer": f"ERROR: rerun timed out after {timeout_s}s",
            "elapsed_s": round(time.monotonic() - t0, 3),
            "stdout": e.stdout or "",
            "stderr": e.stderr or "",
        }

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()
    try:
        parsed = json.loads(stdout.splitlines()[-1]) if stdout else {}
    except (json.JSONDecodeError, IndexError):
        parsed = {
            "completed": False,
            "success": False,
            "answer": "ERROR: runner did not emit JSON",
        }
    parsed.setdefault("elapsed_s", round(time.monotonic() - t0, 3))
    parsed["returncode"] = proc.returncode
    if stderr:
        parsed["stderr"] = stderr[-2000:]
    return parsed


def _preview_text(value: Any, *, limit: int = 320) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def _review_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Compact per-task review table for finished local reruns."""
    review: list[dict[str, Any]] = []
    for row in rows:
        item: dict[str, Any] = {
            "taskId": row.get("taskId"),
            "errorCategory": row.get("errorCategory"),
            "historicalCost": row.get("historicalCost"),
            "historicalSteps": row.get("historicalSteps"),
            "taskPreview": _preview_text(row.get("task"), limit=220),
            "systems": {},
        }
        for system, result in (row.get("results") or {}).items():
            item["systems"][system] = {
                "completed": bool(result.get("completed")),
                "success": result.get("success"),
                "steps": int(result.get("steps") or 0),
                "elapsed_s": float(result.get("elapsed_s") or 0),
                "cost_usd": float(result.get("cost_usd") or 0),
                "answerPreview": _preview_text(result.get("answer")),
            }
            if result.get("stderr"):
                item["systems"][system]["stderrPreview"] = _preview_text(
                    result.get("stderr"),
                    limit=220,
                )
        review.append(item)
    return review


def _summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_system: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for system, result in row.get("results", {}).items():
            by_system.setdefault(system, []).append(result)

    historical_cost = sum(float(row.get("historicalCost") or 0) for row in rows)
    historical_steps = sum(int(row.get("historicalSteps") or 0) for row in rows)
    out: dict[str, Any] = {
        "historical": {
            "runs": len(rows),
            "steps": historical_steps,
            "cost_usd": round(historical_cost, 6),
        }
    }
    for system, results in by_system.items():
        steps = sum(int(r.get("steps") or 0) for r in results)
        cost = sum(float(r.get("cost_usd") or 0) for r in results)
        out[system] = {
            "runs": len(results),
            "completed": sum(1 for r in results if r.get("completed")),
            "success": sum(1 for r in results if r.get("success") is True),
            "steps": steps,
            "step_delta_vs_historical": steps - historical_steps,
            "elapsed_s": round(sum(float(r.get("elapsed_s") or 0) for r in results), 3),
            "cost_usd": round(cost, 6),
            "cost_delta_vs_historical_usd": round(cost - historical_cost, 6),
            "cost_ratio_vs_historical": (
                round(cost / historical_cost, 4) if historical_cost > 0 else None
            ),
        }
    return out


def _historical_row(task_row: dict[str, Any], task: str) -> dict[str, Any]:
    return {
        "taskId": task_row.get("taskId"),
        "errorCategory": task_row.get("errorCategory"),
        "historicalCost": _cost(task_row),
        "historicalSteps": task_row.get("steps"),
        "task": task,
    }


def _dry_run_summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    historical_cost = sum(float(row.get("historicalCost") or 0) for row in rows)
    historical_steps = sum(int(row.get("historicalSteps") or 0) for row in rows)
    return {
        "plannedRuns": sum(len(row.get("plannedCommands", {})) for row in rows),
        "historical": {
            "runs": len(rows),
            "steps": historical_steps,
            "cost_usd": round(historical_cost, 6),
        },
    }


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="Eval dashboard run id")
    parser.add_argument("--top", type=int, default=10, help="Top-cost failures to rerun")
    parser.add_argument(
        "--task-id",
        action="append",
        default=[],
        help=(
            "Specific dashboard taskId to rerun. Can be repeated. "
            "When set, --top is ignored and task order follows this option."
        ),
    )
    parser.add_argument("--max-steps", type=int, default=100)
    parser.add_argument("--timeout-s", type=int, default=900)
    parser.add_argument(
        "--system",
        choices=("ours", "theirs", "both"),
        default="ours",
        help="Which local runner(s) to execute",
    )
    parser.add_argument(
        "--python",
        default=sys.executable,
        help="Python executable for bench/run_*.py",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help=(
            "Fetch and select failed tasks, then write planned runner commands "
            "without requiring a model key or executing local agents."
        ),
    )
    parser.add_argument(
        "--out",
        default="",
        help="Output JSON path. Defaults to bench/results.failed.<run_id>.json",
    )
    args = parser.parse_args()

    if not args.dry_run and not _has_model_key():
        raise SystemExit("Set GEMINI_API_KEY or GOOGLE_API_KEY")

    fetch_limit = 500 if args.task_id else max(args.top, 1)
    tasks = _fetch_failed_tasks(args.run_id, fetch_limit)
    tasks = (
        _filter_tasks_by_id(tasks, [str(tid) for tid in args.task_id])
        if args.task_id
        else tasks[: max(args.top, 0)]
    )
    systems = ["ours", "theirs"] if args.system == "both" else [args.system]
    runners = {
        "ours": "bench/run_ours.py",
        "theirs": "bench/run_theirs.py",
    }
    rows: list[dict[str, Any]] = []
    for i, task_row in enumerate(tasks, start=1):
        task = _task_text(task_row.get("task"))
        print(
            f"[{i}/{len(tasks)}] taskId={task_row.get('taskId')} "
            f"historical_cost=${_cost(task_row):.4f}",
            flush=True,
        )
        row = _historical_row(task_row, task)
        if args.dry_run:
            row["plannedCommands"] = _planned_commands(
                python_bin=args.python,
                runners=runners,
                systems=systems,
                task=task,
                max_steps=args.max_steps,
            )
            rows.append(row)
            continue

        results = {
            system: _run_one(
                python_bin=args.python,
                runner=runners[system],
                task=task,
                max_steps=args.max_steps,
                timeout_s=args.timeout_s,
            )
            for system in systems
        }
        row["results"] = results
        rows.append(row)

    out_path = (
        Path(args.out)
        if args.out
        else REPO / "bench" / f"results.failed.{args.run_id}.json"
    )
    payload = {
        "runId": args.run_id,
        "dryRun": bool(args.dry_run),
        "systems": systems,
        "maxSteps": args.max_steps,
        "summary": _dry_run_summary(rows) if args.dry_run else _summarize(rows),
        "review": [] if args.dry_run else _review_rows(rows),
        "tasks": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(payload["summary"], indent=2))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
