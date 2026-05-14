"""Release-eval preflight for the local working tree.

This is intentionally read-only. It checks for common mistakes before a
branch/commit is used by the eval platform:

- source files that look like they contain real API secrets
- untracked local files that should not be staged with release changes
- required bench helper scripts and tests that should exist

Usage:
    python3 bench/release_preflight.py
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]

EXPECTED_FILES = {
    "bench/compare_eval_run.py",
    "bench/env_file.py",
    "bench/export_failed_tasks.py",
    "bench/posthog_preflight.py",
    "bench/rerun_failed_tasks.py",
    "bench/run_task_file.py",
    "bench/sample_posthog_tasks.py",
    "bench/summarize_failed_run.py",
    "tests/test_bench_compare_eval_run.py",
    "tests/test_bench_env_file.py",
    "tests/test_bench_rerun_failed_tasks.py",
    "tests/test_bench_run_task_file.py",
    "tests/test_bench_sample_posthog_tasks.py",
    "tests/test_bench_summarize_failed_run.py",
}

LOCAL_ONLY_UNTRACKED = {
    "bench/monitor_runs.sh",
}

SECRET_PATTERNS = (
    re.compile(
        r"\b(?:GEMINI_API_KEY|GOOGLE_API_KEY|BROWSER_USE_API_KEY|"
        r"HF_TOKEN|HUGGINGFACE_HUB_TOKEN|EVALUATION_TOOL_SECRET_KEY)"
        r"\s*=\s*['\"]?[A-Za-z0-9_-]{20,}",
    ),
    re.compile(r"\b[A-Za-z0-9_-]{32,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b"),
)


def _git(args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return proc.stdout


def _status_entries() -> list[tuple[str, str]]:
    entries: list[tuple[str, str]] = []
    for line in _git(["status", "--porcelain=v1", "--untracked-files=all"]).splitlines():
        if not line:
            continue
        status = line[:2]
        path = line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        entries.append((status, path))
    return entries


def _release_candidate_paths(entries: list[tuple[str, str]]) -> list[str]:
    paths: set[str] = set()
    for status, path in entries:
        if path in LOCAL_ONLY_UNTRACKED:
            continue
        if path.startswith(("bench/.run_state/", "bench/.trace_cache/")):
            continue
        if path in {"bench/active_runs.txt"} or re.fullmatch(
            r"bench/results.*\.json",
            path,
        ):
            continue
        if status == "??" and not (
            path.startswith("tests/test_")
            or re.fullmatch(r"bench/[A-Za-z0-9_]+\.py", path)
        ):
            continue
        paths.add(path)
    return sorted(paths)


def _scan_secrets(paths: list[str]) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for rel in paths:
        path = ROOT / rel
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), start=1):
            for pattern in SECRET_PATTERNS:
                if pattern.search(line):
                    findings.append({"path": rel, "line": lineno})
                    break
    return findings


def build_report() -> dict[str, Any]:
    entries = _status_entries()
    release_paths = _release_candidate_paths(entries)
    untracked = sorted(path for status, path in entries if status == "??")
    local_only_present = [path for path in untracked if path in LOCAL_ONLY_UNTRACKED]
    unexpected_untracked = [
        path
        for path in untracked
        if path not in LOCAL_ONLY_UNTRACKED
        and path not in release_paths
    ]
    missing_expected = sorted(
        path for path in EXPECTED_FILES if not (ROOT / path).exists()
    )
    secret_findings = _scan_secrets(release_paths)
    blockers = []
    if secret_findings:
        blockers.append("secretLikeValues")
    if unexpected_untracked:
        blockers.append("unexpectedUntracked")
    if missing_expected:
        blockers.append("missingExpectedFiles")

    return {
        "ok": not blockers,
        "blockers": blockers,
        "releaseCandidateFiles": release_paths,
        "secretFindings": secret_findings,
        "unexpectedUntracked": unexpected_untracked,
        "localOnlyUntracked": local_only_present,
        "missingExpectedFiles": missing_expected,
    }


def main() -> None:
    report = build_report()
    json.dump(report, sys.stdout, indent=2, ensure_ascii=False)
    print()
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
