"""Compare an eval-platform run against a baseline run.

Usage:
    EVALUATION_TOOL_URL=... EVALUATION_TOOL_SECRET_KEY=... \
        python3 bench/compare_eval_run.py <candidate_run_id>

Defaults to comparing against the 2026-05-14 baseline run from the
current optimization thread. Prints JSON so the output can be archived
with a release candidate.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import urllib.parse
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from env_file import load_dotenv  # noqa: E402


DEFAULT_BASELINE_RUN_ID = "kh74n8rcqs8bestere2sjjqag186nb7q"


def _env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"missing required env var: {name}")
    return value


def _get_json(base_url: str, secret: str, path: str, params: dict[str, Any]) -> Any:
    url = f"{base_url.rstrip('/')}{path}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {secret}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        return json.loads(resp.read())


def _parse_usage(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:
            return {}
    return {}


def _cost(task: dict[str, Any]) -> float:
    usage = _parse_usage(task.get("usage"))
    for key in ("total_cost", "cost", "costUsd", "totalCost"):
        try:
            value = usage.get(key) if key in usage else task.get(key)
            if value is not None:
                return float(value)
        except (TypeError, ValueError):
            continue
    return 0.0


def _has_cost(task: dict[str, Any]) -> bool:
    usage = _parse_usage(task.get("usage"))
    for key in ("total_cost", "cost", "costUsd", "totalCost"):
        source = usage if key in usage else task
        if key not in source:
            continue
        try:
            return float(source[key]) > 0
        except (TypeError, ValueError):
            return False
    return False


def _mean(values: list[float]) -> float | None:
    return round(statistics.mean(values), 6) if values else None


def _p90(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return round(ordered[int(len(ordered) * 0.9)], 6)


def summarize_run(
    run: dict[str, Any],
    tasks: list[dict[str, Any]],
    failed_tasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    total = int(run.get("totalTasks") or len(tasks) or 0)
    successful = int(run.get("successfulTasks") or 0)
    tasks_with_cost = [t for t in tasks if _has_cost(t)]
    costs = [_cost(t) for t in tasks_with_cost]
    steps = [
        float(t["steps"])
        for t in tasks
        if isinstance(t.get("steps"), (int, float))
    ]
    durations = [
        float(t["taskDuration"])
        for t in tasks
        if isinstance(t.get("taskDuration"), (int, float))
    ]
    failed = [
        t for t in (failed_tasks if failed_tasks is not None else tasks)
        if t.get("errorCategory")
    ]
    return {
        "runId": run.get("runId"),
        "status": run.get("status"),
        "gitCommitHash": run.get("gitCommitHash"),
        "gitBranch": run.get("gitBranch"),
        "model": run.get("model"),
        "totalTasks": total,
        "successfulTasks": successful,
        "successRate": round(successful / total, 6) if total else None,
        "failedTasks": max(0, total - successful) if total else len(failed),
        "tasksWithCost": len(tasks_with_cost),
        "costCoverage": (
            round(len(tasks_with_cost) / total, 6)
            if total
            else None
        ),
        "avgCostUsd": _mean(costs),
        "totalCostUsd": round(sum(costs), 6) if costs else None,
        "avgSteps": _mean(steps),
        "p90Steps": _p90(steps),
        "avgDurationSec": _mean(durations),
        "actionErrorCount": run.get("actionErrorCount"),
        "accessDeniedCount": run.get("accessDeniedCount"),
        "errorCategories": Counter(
            t.get("errorCategory") for t in failed
        ).most_common(),
    }


def compare_summaries(
    baseline: dict[str, Any],
    candidate: dict[str, Any],
) -> dict[str, Any]:
    def delta(key: str) -> float | int | None:
        b = baseline.get(key)
        c = candidate.get(key)
        if isinstance(b, (int, float)) and isinstance(c, (int, float)):
            return round(c - b, 6)
        return None

    return {
        "successRateDeltaPp": (
            round(delta("successRate") * 100, 2)
            if delta("successRate") is not None
            else None
        ),
        "avgCostDeltaUsd": delta("avgCostUsd"),
        "costCoverageDelta": delta("costCoverage"),
        "avgCostRatio": (
            round(candidate["avgCostUsd"] / baseline["avgCostUsd"], 4)
            if baseline.get("avgCostUsd") and candidate.get("avgCostUsd")
            else None
        ),
        "avgStepsDelta": delta("avgSteps"),
        "p90StepsDelta": delta("p90Steps"),
        "failedTasksDelta": delta("failedTasks"),
        "actionErrorsDelta": delta("actionErrorCount"),
        "accessDeniedDelta": delta("accessDeniedCount"),
    }


def fetch_summary(base_url: str, secret: str, run_id: str) -> dict[str, Any]:
    run = _get_json(base_url, secret, "/api/getRun", {"runId": run_id})
    results = _get_json(
        base_url,
        secret,
        "/api/getRunResults",
        {"runId": run_id, "format": "only_judge", "limit": 500},
    )
    tasks = results.get("judgeData", {}).get("tasks") or results.get("tasks") or []
    failed_results = _get_json(
        base_url,
        secret,
        "/api/getRunResults",
        {"runId": run_id, "onlyFailedTasks": "true", "limit": 500},
    )
    failed_tasks = failed_results.get("tasks") or []
    return summarize_run(run, tasks, failed_tasks)


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate_run_id")
    parser.add_argument("--baseline", default=DEFAULT_BASELINE_RUN_ID)
    args = parser.parse_args()

    base_url = _env("EVALUATION_TOOL_URL")
    secret = _env("EVALUATION_TOOL_SECRET_KEY")
    baseline = fetch_summary(base_url, secret, args.baseline)
    candidate = fetch_summary(base_url, secret, args.candidate_run_id)
    json.dump(
        {
            "baseline": baseline,
            "candidate": candidate,
            "delta": compare_summaries(baseline, candidate),
        },
        sys.stdout,
        indent=2,
        ensure_ascii=False,
    )
    print()


if __name__ == "__main__":
    main()
