"""Preflight checks before running PostHog rollout experiments.

This script is intentionally read-only. It inspects the neighboring
`../custom-llm` rollout scripts and local environment so we can see why
PostHog experiments are or are not runnable without accidentally
creating Browser Use API tasks or pushing Hugging Face datasets.
"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import sys
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parent.parent
BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from env_file import load_dotenv  # noqa: E402

CUSTOM_LLM = (REPO.parent / "custom-llm").resolve()
ROLLOUT = CUSTOM_LLM / "rollout"
POSTHOG_CREATE = ROLLOUT / "create_task_ds_posthog.py"
RUN_TASKS = ROLLOUT / "run_tasks.py"


def _read_ast(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def _assigned_constants(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    tree = _read_ast(path)
    out: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or not target.id.isupper():
            continue
        try:
            out[target.id] = ast.literal_eval(node.value)
        except Exception:
            continue
    return out


def _assigned_names(path: Path, names: set[str]) -> dict[str, Any]:
    if not path.exists():
        return {}
    tree = _read_ast(path)
    out: dict[str, Any] = {}
    for node in tree.body:
        if not isinstance(node, ast.Assign) or len(node.targets) != 1:
            continue
        target = node.targets[0]
        if not isinstance(target, ast.Name) or target.id not in names:
            continue
        try:
            out[target.id] = ast.literal_eval(node.value)
        except Exception:
            continue
    return out


def _has_call(path: Path, attr_name: str) -> bool:
    if not path.exists():
        return False
    tree = _read_ast(path)
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == attr_name:
                return True
            if isinstance(func, ast.Name) and func.id == attr_name:
                return True
    return False


def _has_top_level_call(path: Path, attr_name: str) -> bool:
    if not path.exists():
        return False
    tree = _read_ast(path)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for child in ast.walk(node):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Attribute) and func.attr == attr_name:
                    return True
                if isinstance(func, ast.Name) and func.id == attr_name:
                    return True
    return False


def _has_top_level_env_raise(path: Path, env_name: str) -> bool:
    if not path.exists():
        return False
    tree = _read_ast(path)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        text = ast.get_source_segment(path.read_text(encoding="utf-8"), node) or ""
        if env_name in text and any(isinstance(child, ast.Raise) for child in ast.walk(node)):
            return True
    return False


def _env_status() -> dict[str, bool]:
    load_dotenv()
    names = [
        "BROWSER_USE_API_KEY",
        "HF_TOKEN",
        "HUGGINGFACE_HUB_TOKEN",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
    ]
    return {name: bool(os.environ.get(name)) for name in names}


def _dependency_status() -> dict[str, bool]:
    modules = ["aiohttp", "datasets", "dotenv", "huggingface_hub"]
    return {name: importlib.util.find_spec(name) is not None for name in modules}


def build_report() -> dict[str, Any]:
    load_dotenv()
    run_constants = _assigned_constants(RUN_TASKS)
    create_constants = _assigned_constants(POSTHOG_CREATE)
    create_named = _assigned_names(POSTHOG_CREATE, {"posthog_dataset"})
    env = _env_status()
    deps = _dependency_status()
    state_file_name = run_constants.get("STATE_FILE_NAME")
    state_file = ROLLOUT / state_file_name if isinstance(state_file_name, str) else None
    return {
        "customLlmDir": str(CUSTOM_LLM),
        "scripts": {
            "runTasks": str(RUN_TASKS),
            "createPosthogDataset": str(POSTHOG_CREATE),
        },
        "env": env,
        "pythonDependencies": deps,
        "blocked": {
            "browserUseApiKeyMissing": not env["BROWSER_USE_API_KEY"],
            "huggingFaceTokenMissing": not (
                env["HF_TOKEN"] or env["HUGGINGFACE_HUB_TOKEN"]
            ),
            "pythonDependenciesMissing": [
                name for name, present in deps.items() if not present
            ],
        },
        "runTasksConfig": {
            "dataset": run_constants.get("DATASET_NAME"),
            "stateFile": str(state_file) if state_file else None,
            "stateFileExists": bool(state_file and state_file.exists()),
            "maxConcurrent": run_constants.get("MAX_CONCURRENT"),
            "maxRetries": run_constants.get("MAX_RETRIES"),
            "llm": run_constants.get("LLM"),
            "flashMode": run_constants.get("FLASH_MODE"),
            "rolloutId": run_constants.get("ROLLOUT_ID"),
            "importTimeApiKeyValidation": _has_top_level_env_raise(
                RUN_TASKS,
                "BROWSER_USE_API_KEY",
            ),
        },
        "posthogDatasetConfig": {
            "sourceDataset": create_named.get("posthog_dataset"),
            "finalDataset": create_constants.get("FINAL_DATASET_NAME"),
            "nTotalTasks": create_constants.get("N_TOTAL_TASKS"),
            "pushesToHub": _has_call(POSTHOG_CREATE, "push_to_hub"),
            "topLevelLoadDataset": _has_top_level_call(
                POSTHOG_CREATE,
                "load_dataset",
            ),
            "topLevelPushToHub": _has_top_level_call(
                POSTHOG_CREATE,
                "push_to_hub",
            ),
            "hardcodedHomeUbuntuOutput": (
                "/home/ubuntu" in POSTHOG_CREATE.read_text(encoding="utf-8")
                if POSTHOG_CREATE.exists()
                else False
            ),
        },
        "safeNextSteps": [
            "Set BROWSER_USE_API_KEY before running rollout/run_tasks.py.",
            (
                "Patch create_task_ds_posthog.py to support a dry-run/local "
                "output path before running it; current script pushes to hub."
            ),
            (
                "Lower MAX_CONCURRENT or add a CLI override before a first "
                "smoke rollout; current default is high for experimentation."
            ),
        ],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="Print JSON")
    args = parser.parse_args()

    report = build_report()
    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return

    print("PostHog rollout preflight")
    print(f"custom-llm: {report['customLlmDir']}")
    print("environment:")
    for key, present in report["env"].items():
        print(f"  {key}: {'set' if present else 'unset'}")
    print("python dependencies:")
    for key, present in report["pythonDependencies"].items():
        print(f"  {key}: {'available' if present else 'missing'}")
    print("blocked:")
    for key, blocked in report["blocked"].items():
        print(f"  {key}: {blocked}")
    print("run_tasks:")
    for key, value in report["runTasksConfig"].items():
        print(f"  {key}: {value}")
    print("create_task_ds_posthog:")
    for key, value in report["posthogDatasetConfig"].items():
        print(f"  {key}: {value}")


if __name__ == "__main__":
    main()
