"""Summarize failed tasks from an eval dashboard run.

Usage:
    EVALUATION_TOOL_URL=... EVALUATION_TOOL_SECRET_KEY=... \
        python3 bench/summarize_failed_run.py <run_id> --top 20

Prints JSON with aggregate tool/error patterns plus the highest-cost
failed-task examples. This is intended for trace-driven fixes: look for
repeated mechanical failures before changing prompts or schemas.
It also surfaces self-reported-success incorrect finals that current
static guards do not classify, which are the residual cases most likely
to need human review or new trace-driven heuristics.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import os
import re
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


try:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "python"))
    from browser_use_rs.agent import (
        _looks_like_item_detail_list_final,
        _looks_like_late_pagination_final,
        _looks_like_search_result_query_mismatch_answer,
        _looks_like_search_host_final,
        _looks_like_stale_relative_date_answer,
        _looks_like_unsupported_final_answer,
        _looks_like_wrong_host_final,
    )
except Exception:
    def _looks_like_item_detail_list_final(task: str, final_url: str | None) -> bool:
        return False

    def _looks_like_late_pagination_final(task: str, final_url: str | None) -> bool:
        return False

    def _looks_like_search_result_query_mismatch_answer(task: str, text: str) -> bool:
        return False

    def _looks_like_unsupported_final_answer(
        task: str,
        text: str,
        final_url: str | None = None,
    ) -> bool:
        return False

    def _looks_like_search_host_final(task: str, final_url: str | None) -> bool:
        return False

    def _looks_like_stale_relative_date_answer(task: str, text: str) -> bool:
        return False

    def _looks_like_wrong_host_final(task: str, final_url: str | None) -> bool:
        return False


INTERESTING_MARKERS = (
    "unexpected keyword argument",
    "tool timed out",
    "javascript error",
    "about:blank",
    "chrome-error://",
    "google.com/sorry",
    "showcaptcha",
    "cloudflare",
    "captcha",
    "skipped:",
    "unknown element index",
    "no longer present",
    "cdp protocol error -32001",
)

FINAL_MARKERS = (
    "based on search",
    "search results",
    "unable to",
    "blocked",
    "captcha",
    "cloudflare",
    "not explicitly",
    "not found",
    "direct access",
    "restricted",
    "secondary",
)

SEARCH_OR_FALLBACK_HOSTS = (
    "duckduckgo.com",
    "google.com",
    "bing.com",
    "yahoo.com",
    "yandex.com",
    "search.brave.com",
    "startpage.com",
)


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
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.load(resp)


def _post_json(base_url: str, secret: str, path: str, payload: dict[str, Any]) -> Any:
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Authorization": f"Bearer {secret}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=90) as resp:
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


def _website_host(task_text: str) -> str:
    match = re.search(r"website:\s*(https?://\S+)", task_text, re.IGNORECASE)
    if not match:
        return ""
    try:
        return (urllib.parse.urlparse(match.group(1)).hostname or "").lower()
    except Exception:
        return ""


def _host(url: str) -> str:
    try:
        return (urllib.parse.urlparse(url).hostname or "").lower()
    except Exception:
        return ""


def _host_matches(host: str, target: str) -> bool:
    if not host or not target:
        return False
    h = host.removeprefix("www.")
    t = target.removeprefix("www.")
    return h == t or h.endswith("." + t) or t.endswith("." + h)


def _is_search_or_fallback_host(host: str) -> bool:
    h = host.removeprefix("www.")
    return any(h == known or h.endswith("." + known) for known in SEARCH_OR_FALLBACK_HOSTS)


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


def _summarize_detail(task: dict[str, Any], detail: dict[str, Any]) -> dict[str, Any]:
    hist = detail.get("completeHistory") or []
    tools: Counter[str] = Counter()
    errors: Counter[str] = Counter()
    interesting: Counter[str] = Counter()
    task_text = _task_text(task.get("task"))
    target_host = _website_host(task_text)
    visited_hosts: list[str] = []
    final_url = ""
    final_host = ""
    final = detail.get("finalResultResponse") or ""
    final_lc = final.lower()
    for step in hist:
        state_url = ((step.get("state") or {}).get("url") or "").strip()
        if state_url:
            step_host = _host(state_url)
            if step_host and step_host not in visited_hosts:
                visited_hosts.append(step_host)
            final_url = state_url
            final_host = step_host or final_host
        for tc in ((step.get("model_output") or {}).get("tool_calls") or []):
            if tc.get("name"):
                tools[str(tc["name"])] += 1
        for result in step.get("result") or []:
            err = result.get("error")
            content = result.get("extracted_content")
            if err:
                errors[str(err).replace("\n", " ")[:240]] += 1
            blob = f"{err or ''} {content or ''}".lower()
            for marker in INTERESTING_MARKERS:
                if marker in blob:
                    interesting[marker] += 1
    return {
        "taskId": task.get("taskId"),
        "taskResultId": task.get("_taskResultId"),
        "errorCategory": task.get("errorCategory"),
        "steps": task.get("steps"),
        "cost": _cost(task),
        "selfReportSuccess": detail.get("selfReportSuccess"),
        "task": task_text,
        "targetHost": target_host,
        "visitedHosts": visited_hosts[:20],
        "finalUrl": final_url,
        "finalHost": final_host,
        "finalHostMatchesTarget": _host_matches(final_host, target_host),
        "finalHostIsSearchOrFallback": _is_search_or_fallback_host(final_host),
        "finalFlags": [m for m in FINAL_MARKERS if m in final_lc],
        "searchHostFinal": _looks_like_search_host_final(task_text, final_url),
        "staleRelativeDateFinal": _looks_like_stale_relative_date_answer(
            task_text,
            final,
        ),
        "wrongHostFinal": _looks_like_wrong_host_final(task_text, final_url),
        "latePaginationFinal": _looks_like_late_pagination_final(
            task_text,
            final_url,
        ),
        "itemDetailListFinal": _looks_like_item_detail_list_final(
            task_text,
            final_url,
        ),
        "queryMismatchFinal": _looks_like_search_result_query_mismatch_answer(
            task_text,
            final,
        ),
        "unsupportedEvidenceFinal": _looks_like_unsupported_final_answer(
            task_text,
            final,
            final_url,
        ),
        "toolCounts": tools.most_common(10),
        "errorCounts": errors.most_common(5),
        "interesting": interesting.most_common(),
        "finalResultResponse": final[:600],
    }


def _is_unclassified_incorrect_final(row: dict[str, Any]) -> bool:
    return bool(
        row.get("errorCategory") == "Incorrect Result"
        and row.get("selfReportSuccess") is True
        and not row.get("unsupportedEvidenceFinal")
    )


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_id", help="Eval dashboard run id")
    parser.add_argument("--top", type=int, default=20, help="Top cost examples to include")
    parser.add_argument("--workers", type=int, default=10, help="Concurrent detail fetches")
    args = parser.parse_args()

    base_url = _env("EVALUATION_TOOL_URL")
    secret = _env("EVALUATION_TOOL_SECRET_KEY")

    results = _get_json(
        base_url,
        secret,
        "/api/getRunResults",
        {"runId": args.run_id, "onlyFailedTasks": "true", "limit": 500},
    ).get("tasks", [])
    traces = _post_json(
        base_url,
        secret,
        "/api/getRunTracesForJudging",
        {"runId": args.run_id},
    )
    by_task_text = {
        (trace.get("task") or "").strip(): trace.get("taskResultId")
        for trace in traces
    }
    failed = sorted(results, key=_cost, reverse=True)
    for task in failed:
        task["_taskResultId"] = by_task_text.get(_task_text(task.get("task")).strip())

    def fetch(task: dict[str, Any]) -> dict[str, Any]:
        task_result_id = task.get("_taskResultId")
        if not task_result_id:
            return {"task": task, "detail": {"_error": "missing taskResultId"}}
        try:
            detail = _post_json(
                base_url,
                secret,
                "/api/getTaskDetailsForJudging",
                {"taskResultId": task_result_id},
            )
        except Exception as e:
            detail = {"_error": f"{type(e).__name__}: {e}"}
        return {"task": task, "detail": detail}

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        rows = [
            _summarize_detail(item["task"], item["detail"])
            for item in ex.map(fetch, failed)
        ]

    aggregate = {
        "errorCategories": Counter(r.get("errorCategory") for r in rows).most_common(),
        "selfReportSuccess": Counter(str(r.get("selfReportSuccess")) for r in rows).most_common(),
        "tools": Counter(
            name for r in rows for name, count in r["toolCounts"] for _ in range(count)
        ).most_common(25),
        "interesting": Counter(
            name for r in rows for name, count in r["interesting"] for _ in range(count)
        ).most_common(),
        "finalFlags": Counter(
            flag for r in rows for flag in r["finalFlags"]
        ).most_common(),
        "unsupportedEvidenceFinals": sum(
            1 for r in rows if r.get("unsupportedEvidenceFinal")
        ),
        "searchHostFinals": sum(1 for r in rows if r.get("searchHostFinal")),
        "staleRelativeDateFinals": sum(
            1 for r in rows if r.get("staleRelativeDateFinal")
        ),
        "wrongHostFinals": sum(1 for r in rows if r.get("wrongHostFinal")),
        "latePaginationFinals": sum(
            1 for r in rows if r.get("latePaginationFinal")
        ),
        "itemDetailListFinals": sum(
            1 for r in rows if r.get("itemDetailListFinal")
        ),
        "queryMismatchFinals": sum(
            1 for r in rows if r.get("queryMismatchFinal")
        ),
        "unclassifiedIncorrectFinals": sum(
            1 for r in rows if _is_unclassified_incorrect_final(r)
        ),
        "finalHostMatchesTarget": Counter(
            str(r.get("finalHostMatchesTarget")) for r in rows
        ).most_common(),
        "finalHostIsSearchOrFallback": Counter(
            str(r.get("finalHostIsSearchOrFallback")) for r in rows
        ).most_common(),
        "finalHosts": Counter(r.get("finalHost") for r in rows).most_common(20),
        "topErrors": Counter(
            err for r in rows for err, count in r["errorCounts"] for _ in range(count)
        ).most_common(25),
    }

    json.dump(
        {
            "runId": args.run_id,
            "failedTasks": len(rows),
            "aggregate": aggregate,
            "topExamples": rows[: max(args.top, 0)],
            "unclassifiedExamples": [
                r for r in rows if _is_unclassified_incorrect_final(r)
            ][: max(args.top, 0)],
        },
        sys.stdout,
        indent=2,
        ensure_ascii=False,
    )
    print()


if __name__ == "__main__":
    main()
