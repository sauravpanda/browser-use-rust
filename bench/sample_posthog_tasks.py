"""Create a local PostHog task sample without pushing datasets.

This is a safe alternative to ../custom-llm/rollout/create_task_ds_posthog.py
for inspecting candidate tasks before any rollout. It requires the
`datasets` package only when actually sampling; if the dependency is
missing, it exits before doing network or write work.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import re
import sys
import urllib.parse
from pathlib import Path
from typing import Any, NamedTuple


BENCH_DIR = Path(__file__).resolve().parent
if str(BENCH_DIR) not in sys.path:
    sys.path.insert(0, str(BENCH_DIR))

from env_file import load_dotenv  # noqa: E402

DEFAULT_DATASET = "browser-use/posthog-tasks-080925-labeled"
DEFAULT_OUT = Path("bench/posthog_sample_tasks.json")
DEFAULT_EVAL_FRIENDLY_MAX_CHARS = 2000


class SamplingPolicy(NamedTuple):
    max_task_chars: int = 0
    require_url: bool = False
    reject_search_engine_tasks: bool = False
    reject_non_public_targets: bool = False
    reject_sensitive_or_transactional_tasks: bool = False
    reject_high_friction_targets: bool = False


def _target_hosts(task: str) -> list[str]:
    hosts: list[str] = []
    for raw_url in re.findall(r"https?://[^\s)>\]}\"']+", task):
        try:
            parsed = urllib.parse.urlparse(raw_url.rstrip(".,;:"))
        except ValueError:
            continue
        if parsed.hostname:
            hosts.append(parsed.hostname.lower())
    for m in re.finditer(
        r"\b(?:target-url|website|url|site|go to|navigate to|open|visit|read webpage|link)\s*[:=]?\s*"
        r"(?:https?://)?([a-z0-9.-]+\.[a-z]{2,})\b",
        task,
        re.IGNORECASE,
    ):
        hosts.append(m.group(1).lower())
    out: list[str] = []
    for host in hosts:
        if host not in out:
            out.append(host)
    return out


def _is_non_public_host(host: str) -> bool:
    h = (host or "").lower().strip("[]").removeprefix("www.")
    if not h:
        return False
    if h in {"localhost", "0.0.0.0"}:
        return True
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        ip = None
    if ip is not None:
        return ip.is_private or ip.is_loopback or ip.is_link_local
    if h.endswith((".local", ".test", ".invalid", ".localhost")):
        return True
    if h.endswith(".shinobi.security") or ".acmecorp." in h:
        return True
    return False


def _is_high_friction_host(host: str) -> bool:
    h = (host or "").lower().strip("[]").removeprefix("www.")
    if not h:
        return False
    exact_or_suffix = (
        "facebook.com",
        "instagram.com",
        "linkedin.com",
        "tiktok.com",
        "x.com",
        "twitter.com",
    )
    if any(h == name or h.endswith("." + name) for name in exact_or_suffix):
        return True
    return h in {"google.com", "maps.google.com"} or h.endswith(".google.com")


def _has_url_or_domain(task: str) -> bool:
    for m in re.finditer(r"https?://[^\s)>\]}\"']+", task):
        before = task[max(0, m.start() - 160) : m.start()].lower()
        if (
            "visited urls" in before
            or "visited url" in before
            or "already in the list" in before
            or "already in visited" in before
        ):
            continue
        if "not already" in before and "visited" in before:
            continue
        return True
    return bool(
        re.search(
            r"\b(?:website|url|site|go to|navigate to)\s*[:=]?\s*"
            r"(?:https?://)?[a-z0-9.-]+\.[a-z]{2,}\b",
            task,
            re.IGNORECASE,
        )
    )


def _is_search_engine_task(task: str) -> bool:
    task_lc = task.lower()
    search_engines = (
        "google",
        "bing",
        "duckduckgo",
        "yahoo",
        "yandex",
        "brave search",
    )
    if "context web search" in task_lc:
        return True
    if re.search(
        r"\bsearch\s+(?:google|bing|duckduckgo|yahoo|yandex)\s+for\b",
        task_lc,
    ):
        return True
    if re.search(r"\b(search|look up|find)\b[^.]{0,120}\bon google\b", task_lc):
        return True
    if "google search results page" in task_lc:
        return True
    if "google search page" in task_lc:
        return True
    if re.search(
        r"\b(?:go to|navigate to|open|visit)\s+"
        r"(?:https?://)?(?:www\.)?google\.com\b",
        task_lc,
    ):
        return True
    if re.search(r"\byou will get (?:a|the) google search\b", task_lc):
        return True
    if any(f" on {name}" in task_lc for name in search_engines):
        return True
    return False


def _is_sensitive_or_transactional_task(task: str) -> bool:
    task_lc = task.lower()
    sensitive_markers = (
        "social security",
        "ssn",
        "date of birth",
        "dob:",
        "base salary",
        "credit score",
        "credit card",
        "credential card",
        "card credential",
        "tracking number",
        "password",
        "passcode",
        "postcode",
        "postal code",
        "one-time code",
        "two-factor",
        "2fa",
        "log in",
        "login",
        "sign in",
        "signin",
        "create an account",
        "create account",
        "register an account",
        "my account",
        "account settings",
        "payment",
        "checkout",
        "buy the ",
        "purchase ",
        "add to cart",
        "cart page",
        "place an order",
        "book an appointment",
        "schedule an appointment",
        "make a reservation",
        "submit the form",
        "send the message",
        "contact form",
        "newsletter signup",
    )
    if any(marker in task_lc for marker in sensitive_markers):
        return True
    return bool(
        re.search(r"\b(?:add|added|adding|remove|view)\b.{0,50}\bcart\b", task_lc)
        or re.search(
            r"\b(?:fill|filled|filling|complete|submit)\b.{0,80}\bform\b",
            task_lc,
        )
        or re.search(r"\bparcel\b.{0,80}\btracking\b", task_lc)
    )


def _filter_task(x: dict[str, Any]) -> bool:
    task_lower = str(x.get("task") or "").lower()
    task = str(x.get("task") or "")
    return bool(
        x.get("is_browser_task")
        and not x.get("is_vague")
        and x.get("task_category") != "Flight Search"
        and not x.get("includes_login_info")
        and not x.get("requires_login")
        and x.get("is_reproducible")
        and not x.get("is_unethical")
        and (x.get("complexity") or 0) > 2
        and (x.get("complexity") or 0) < 5
        and x.get("is_high_quality")
        and not x.get("requires_custom_actions")
        and x.get("result_present")
        and not task.startswith("Your goal is to go to")
        and "your ultimate task is" not in task_lower
        and "you are an intelligent promo code" not in task_lower
        and "www.myschool.edu.au/school" not in task_lower
        and not (
            "sustainability report" in task_lower
            and "esg report" in task_lower
        )
        and x.get("task_language") == "en"
    )


def _policy_rejection_reason(task: str, policy: SamplingPolicy) -> str | None:
    task = (task or "").strip()
    if policy.max_task_chars > 0 and len(task) > policy.max_task_chars:
        return "over_max_task_chars"
    if policy.require_url and not _has_url_or_domain(task):
        return "missing_url_or_domain"
    if policy.reject_search_engine_tasks and _is_search_engine_task(task):
        return "search_engine_task"
    if policy.reject_non_public_targets and any(
        _is_non_public_host(host) for host in _target_hosts(task)
    ):
        return "non_public_target"
    if policy.reject_high_friction_targets and any(
        _is_high_friction_host(host) for host in _target_hosts(task)
    ):
        return "high_friction_target"
    if (
        policy.reject_sensitive_or_transactional_tasks
        and _is_sensitive_or_transactional_task(task)
    ):
        return "sensitive_or_transactional_task"
    return None


def _make_filter(policy: SamplingPolicy):
    def keep(row: dict[str, Any]) -> bool:
        if not _filter_task(row):
            return False
        task = str(row.get("task") or "")
        return _policy_rejection_reason(task, policy) is None

    return keep


def _load_dataset_or_exit(dataset_name: str):
    load_dotenv()
    try:
        from datasets import load_dataset
    except ModuleNotFoundError as e:
        raise SystemExit(
            "Missing dependency: datasets. Install rollout dependencies "
            "before sampling PostHog tasks."
        ) from e
    return load_dataset(dataset_name, split="train")


def sample_tasks(
    *,
    dataset_name: str,
    limit: int,
    seed: int,
    out_path: Path,
    policy: SamplingPolicy | None = None,
) -> dict[str, Any]:
    policy = policy or SamplingPolicy()
    ds = _load_dataset_or_exit(dataset_name)
    ds = ds.filter(_make_filter(policy))
    total_filtered = len(ds)
    if limit < 0:
        raise SystemExit("--limit must be non-negative")
    count = min(limit, total_filtered)
    sampled = ds.shuffle(seed=seed).select(range(count)) if count else ds.select([])
    tasks = [
        {
            "task_id": str(i + 1),
            "task": str(task).strip(),
            "source": "Posthog",
        }
        for i, task in enumerate(sampled["task"])
    ]
    payload = {
        "dataset": dataset_name,
        "seed": seed,
        "requested": limit,
        "filtered": total_filtered,
        "policy": {
            "maxTaskChars": policy.max_task_chars,
            "requireUrl": policy.require_url,
            "rejectSearchEngineTasks": policy.reject_search_engine_tasks,
            "rejectNonPublicTargets": policy.reject_non_public_targets,
            "rejectSensitiveOrTransactionalTasks": (
                policy.reject_sensitive_or_transactional_tasks
            ),
            "rejectHighFrictionTargets": policy.reject_high_friction_targets,
        },
        "sampled": len(tasks),
        "tasks": tasks,
    }
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n")
    return payload


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dataset", default=DEFAULT_DATASET)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument(
        "--profile",
        choices=("mirror", "eval-friendly"),
        default="mirror",
        help=(
            "mirror keeps the neighboring rollout filters; eval-friendly "
            "adds prompt-length, URL/domain, and search-engine-task filters "
            "for cheaper local smoke sets."
        ),
    )
    parser.add_argument(
        "--max-task-chars",
        type=int,
        default=None,
        help=(
            "Reject prompts above this character count. Defaults to no cap "
            "for mirror and 2000 for eval-friendly."
        ),
    )
    parser.add_argument(
        "--require-url",
        action="store_true",
        help="Reject prompts that do not contain an explicit URL/domain target.",
    )
    parser.add_argument(
        "--reject-search-engine-tasks",
        action="store_true",
        help="Reject prompts centered on generic Google/Bing/etc. searching.",
    )
    parser.add_argument(
        "--reject-non-public-targets",
        action="store_true",
        help="Reject localhost, private IP, and known synthetic/internal targets.",
    )
    parser.add_argument(
        "--reject-sensitive-or-transactional-tasks",
        action="store_true",
        help="Reject prompts involving PII, payment, purchase, checkout, or cart flows.",
    )
    parser.add_argument(
        "--reject-high-friction-targets",
        action="store_true",
        help="Reject known public hosts that commonly trigger login/CAPTCHA walls.",
    )
    args = parser.parse_args()

    eval_friendly = args.profile == "eval-friendly"
    max_task_chars = args.max_task_chars
    if max_task_chars is None:
        max_task_chars = DEFAULT_EVAL_FRIENDLY_MAX_CHARS if eval_friendly else 0
    policy = SamplingPolicy(
        max_task_chars=max_task_chars,
        require_url=args.require_url or eval_friendly,
        reject_search_engine_tasks=args.reject_search_engine_tasks or eval_friendly,
        reject_non_public_targets=args.reject_non_public_targets or eval_friendly,
        reject_sensitive_or_transactional_tasks=(
            args.reject_sensitive_or_transactional_tasks or eval_friendly
        ),
        reject_high_friction_targets=args.reject_high_friction_targets or eval_friendly,
    )
    payload = sample_tasks(
        dataset_name=args.dataset,
        limit=args.limit,
        seed=args.seed,
        out_path=args.out,
        policy=policy,
    )
    json.dump(
        {
            "dataset": payload["dataset"],
            "filtered": payload["filtered"],
            "sampled": payload["sampled"],
            "out": str(args.out),
        },
        sys.stdout,
        indent=2,
    )
    print()


if __name__ == "__main__":
    main()
