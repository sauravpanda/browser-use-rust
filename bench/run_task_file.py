"""Run tasks from a local JSON task file through local bench runners.

Usage:
    .venv/bin/python bench/sample_posthog_tasks.py \
        --profile eval-friendly --limit 20 --out /tmp/posthog_tasks.json
    .venv/bin/python bench/run_task_file.py /tmp/posthog_tasks.json --dry-run
    export GEMINI_API_KEY=...  # or GOOGLE_API_KEY
    .venv/bin/python bench/run_task_file.py /tmp/posthog_tasks.json --system ours

The input can be a JSON list of tasks or an object with a `tasks` list.
Rows may use `task`, `prompt`, `description`, `goal`, or `input` for the
task text and `task_id`, `taskId`, or `id` for the task identifier.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

import rerun_failed_tasks as rerun  # noqa: E402


def _raw_tasks(payload: Any) -> list[Any]:
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict) and isinstance(payload.get("tasks"), list):
        return payload["tasks"]
    raise SystemExit("Task file must be a JSON list or an object with a tasks list")


def _task_id(row: Any, index: int) -> str:
    if isinstance(row, dict):
        for key in ("task_id", "taskId", "id"):
            value = row.get(key)
            if value is not None and str(value).strip():
                return str(value)
    return str(index)


def _task_source(row: Any) -> str | None:
    if isinstance(row, dict):
        value = row.get("source")
        if value is not None and str(value).strip():
            return str(value)
    return None


def _task_text_from_row(row: Any) -> str:
    if isinstance(row, dict):
        for key in ("task", "prompt", "description", "goal", "input"):
            value = row.get(key)
            if value:
                return str(value)
    return rerun._task_text(row)


def load_tasks(path: Path, *, limit: int = 0) -> list[dict[str, Any]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows: list[dict[str, Any]] = []
    for i, raw in enumerate(_raw_tasks(payload), start=1):
        task = _task_text_from_row(raw).strip()
        if not task:
            continue
        row: dict[str, Any] = {
            "taskId": _task_id(raw, i),
            "task": task,
        }
        source = _task_source(raw)
        if source:
            row["source"] = source
        rows.append(row)
        if limit > 0 and len(rows) >= limit:
            break
    return rows


def _summary(rows: list[dict[str, Any]], *, dry_run: bool) -> dict[str, Any]:
    if dry_run:
        return {
            "tasks": len(rows),
            "plannedRuns": sum(len(row.get("plannedCommands", {})) for row in rows),
        }

    by_system: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        for system, result in row.get("results", {}).items():
            by_system.setdefault(system, []).append(result)

    out: dict[str, Any] = {"tasks": len(rows)}
    for system, results in by_system.items():
        out[system] = {
            "runs": len(results),
            "completed": sum(1 for r in results if r.get("completed")),
            "success": sum(1 for r in results if r.get("success") is True),
            "steps": sum(int(r.get("steps") or 0) for r in results),
            "elapsed_s": round(sum(float(r.get("elapsed_s") or 0) for r in results), 3),
            "cost_usd": round(sum(float(r.get("cost_usd") or 0) for r in results), 6),
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("task_file", type=Path, help="JSON task file to run")
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Maximum tasks to run from the file. Default 0 means all tasks.",
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
            "Write planned runner commands without requiring a model key "
            "or executing local agents."
        ),
    )
    parser.add_argument(
        "--out",
        default="",
        help="Output JSON path. Defaults to bench/results.tasks.<stem>.json",
    )
    args = parser.parse_args()

    if args.limit < 0:
        raise SystemExit("--limit must be non-negative")
    if not args.dry_run and not rerun._has_model_key():
        raise SystemExit("Set GEMINI_API_KEY or GOOGLE_API_KEY")

    task_path = args.task_file
    tasks = load_tasks(task_path, limit=args.limit)
    systems = ["ours", "theirs"] if args.system == "both" else [args.system]
    runners = {
        "ours": "bench/run_ours.py",
        "theirs": "bench/run_theirs.py",
    }

    rows: list[dict[str, Any]] = []
    for i, task_row in enumerate(tasks, start=1):
        task = task_row["task"]
        print(f"[{i}/{len(tasks)}] taskId={task_row['taskId']}", flush=True)
        row = dict(task_row)
        if args.dry_run:
            row["plannedCommands"] = rerun._planned_commands(
                python_bin=args.python,
                runners=runners,
                systems=systems,
                task=task,
                max_steps=args.max_steps,
            )
            rows.append(row)
            continue

        row["results"] = {
            system: rerun._run_one(
                python_bin=args.python,
                runner=runners[system],
                task=task,
                max_steps=args.max_steps,
                timeout_s=args.timeout_s,
            )
            for system in systems
        }
        rows.append(row)

    out_path = (
        Path(args.out)
        if args.out
        else rerun.REPO / "bench" / f"results.tasks.{task_path.stem}.json"
    )
    payload = {
        "taskFile": str(task_path),
        "dryRun": bool(args.dry_run),
        "systems": systems,
        "maxSteps": args.max_steps,
        "summary": _summary(rows, dry_run=bool(args.dry_run)),
        "review": [] if args.dry_run else rerun._review_rows(rows),
        "tasks": rows,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    print(json.dumps(payload["summary"], indent=2))
    print(f"Wrote {out_path}")


if __name__ == "__main__":
    main()
