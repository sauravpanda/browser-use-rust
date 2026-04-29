"""Benchmark orchestrator: runs the same task list against both systems.

Each system runs in its own venv via subprocess so dependencies don't
collide. Both use gemini-2.5-flash. Results written to bench/results.json
and printed as a markdown table.

Run:
    export GEMINI_API_KEY=...
    python bench/bench.py
"""

import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
OURS_PY = REPO / ".venv/bin/python"
THEIRS_PY = REPO.parent / "browser-use/.venv/bin/python"
OURS_SCRIPT = REPO / "bench/run_ours.py"
THEIRS_SCRIPT = REPO / "bench/run_theirs.py"

TASKS: list[dict] = [
    {
        "id": "ex_headline",
        "task": "Go to https://example.com and tell me the page headline. One sentence.",
    },
    {
        "id": "ex_link",
        "task": "Go to https://example.com and tell me the destination URL of the 'More information...' link.",
    },
    {
        "id": "exorg_headline",
        "task": "Go to https://example.org and tell me the page headline. One sentence.",
    },
    {
        "id": "hn_top_title",
        "task": "Go to https://news.ycombinator.com and tell me the title of the top story. One sentence.",
    },
    {
        "id": "hn_top_with_points",
        "task": "Go to https://news.ycombinator.com and tell me the title and current points of the top story. One sentence.",
    },
    {
        "id": "hn_third_story",
        "task": "Go to https://news.ycombinator.com and tell me the title of the third story.",
    },
    {
        "id": "wiki_first_sentence",
        "task": "Go to https://en.wikipedia.org/wiki/Web_browser and give me the first sentence of the article.",
    },
    {
        "id": "httpbin_form",
        "task": "Go to https://httpbin.org/forms/post, fill the customer name field with 'Bench', click submit, then tell me the URL of the resulting page.",
    },
    {
        "id": "httpbin_html_title",
        "task": "Go to https://httpbin.org/html and tell me the title of the article on the page.",
    },
    {
        "id": "multitab",
        "task": "Open https://example.com in one tab and https://example.org in another. Tell me the headlines of both pages, separated by ' | '.",
    },
]


def run_one(label: str, py: Path, script: Path, task: str, max_steps: int = 12) -> dict:
    """Subprocess one runner. Returns parsed JSON or an error stub."""
    if not py.exists():
        return {
            "system": label,
            "task": task,
            "completed": False,
            "answer": f"ERROR: {py} not found",
            "elapsed_s": 0,
            "steps": 0,
            "in_tokens": 0,
            "out_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": 0,
        }

    env = os.environ.copy()
    t0 = time.monotonic()
    proc = subprocess.run(
        [str(py), str(script), task, str(max_steps)],
        capture_output=True,
        text=True,
        env=env,
        timeout=300,
        cwd=str(REPO),
    )
    wall = time.monotonic() - t0

    out = proc.stdout.strip()
    # Pull the JSON line — runners may also print log noise.
    parsed = None
    for line in reversed(out.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                parsed = json.loads(line)
                break
            except json.JSONDecodeError:
                continue
    if parsed is None:
        return {
            "system": label,
            "task": task,
            "completed": False,
            "answer": f"ERROR: no JSON in stdout. stderr={proc.stderr[-400:]!r}",
            "elapsed_s": round(wall, 2),
            "steps": 0,
            "in_tokens": 0,
            "out_tokens": 0,
            "cache_read_tokens": 0,
            "cost_usd": 0,
        }
    # Wall time from outside is the truth (covers process startup); the
    # inner elapsed_s is closer to the actual agent.run() time.
    parsed["wall_s"] = round(wall, 2)
    return parsed


def md_row(*cells) -> str:
    return "| " + " | ".join(str(c) for c in cells) + " |"


def fmt_answer(ans: str, w: int = 60) -> str:
    if not ans:
        return "(empty)"
    s = ans.replace("|", "\\|").replace("\n", " ")
    return s if len(s) <= w else s[: w - 1] + "…"


def main() -> None:
    if not os.environ.get("GEMINI_API_KEY"):
        print("Set GEMINI_API_KEY", file=sys.stderr)
        sys.exit(1)

    results: list[dict] = []
    for i, t in enumerate(TASKS, 1):
        print(f"\n[{i}/{len(TASKS)}] {t['id']}: {t['task']}", flush=True)

        ours = run_one("ours", OURS_PY, OURS_SCRIPT, t["task"])
        ours["id"] = t["id"]
        print(
            f"  ours:   {ours['wall_s']:>5.1f}s steps={ours['steps']:>2} "
            f"in={ours['in_tokens']:>5} out={ours['out_tokens']:>4} "
            f"cache={ours['cache_read_tokens']:>5} ${ours['cost_usd']:.4f} "
            f"→ {fmt_answer(ours['answer'])}"
        )

        theirs = run_one("theirs", THEIRS_PY, THEIRS_SCRIPT, t["task"])
        theirs["id"] = t["id"]
        print(
            f"  theirs: {theirs['wall_s']:>5.1f}s steps={theirs['steps']:>2} "
            f"in={theirs['in_tokens']:>5} out={theirs['out_tokens']:>4} "
            f"cache={theirs['cache_read_tokens']:>5} ${theirs['cost_usd']:.4f} "
            f"→ {fmt_answer(theirs['answer'])}"
        )

        results.append({"id": t["id"], "task": t["task"], "ours": ours, "theirs": theirs})

    out_json = REPO / "bench/results.json"
    out_json.write_text(json.dumps(results, indent=2))
    print(f"\nWrote {out_json}")

    # Summary table
    print("\n## Per-task results\n")
    print(md_row("task", "ours s", "ours steps", "ours $", "theirs s", "theirs steps", "theirs $"))
    print(md_row("---", "---", "---", "---", "---", "---", "---"))
    for r in results:
        print(
            md_row(
                r["id"],
                f"{r['ours']['wall_s']:.1f}",
                r["ours"]["steps"],
                f"${r['ours']['cost_usd']:.4f}",
                f"{r['theirs']['wall_s']:.1f}",
                r["theirs"]["steps"],
                f"${r['theirs']['cost_usd']:.4f}",
            )
        )

    # Aggregates
    def total(side: str, key: str) -> float:
        return sum(r[side][key] for r in results)

    print("\n## Aggregates\n")
    print(md_row("metric", "ours", "theirs"))
    print(md_row("---", "---", "---"))
    for label, key in [
        ("total wall time (s)", "wall_s"),
        ("total steps", "steps"),
        ("total in tokens", "in_tokens"),
        ("total out tokens", "out_tokens"),
        ("total cache read tokens", "cache_read_tokens"),
        ("total cost ($)", "cost_usd"),
    ]:
        ours_v = total("ours", key)
        theirs_v = total("theirs", key)
        ours_s = f"{ours_v:.4f}" if isinstance(ours_v, float) and key == "cost_usd" else (
            f"{ours_v:.1f}" if isinstance(ours_v, float) else str(ours_v)
        )
        theirs_s = f"{theirs_v:.4f}" if isinstance(theirs_v, float) and key == "cost_usd" else (
            f"{theirs_v:.1f}" if isinstance(theirs_v, float) else str(theirs_v)
        )
        print(md_row(label, ours_s, theirs_s))


if __name__ == "__main__":
    main()
