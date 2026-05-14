"""Small .env loader for bench scripts.

The benchmark scripts are often run from Codex or subprocesses where
shell exports are easy to lose. This reads repo-root `.env` files without
adding python-dotenv to the base benchmark path. Existing environment
variables always win.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path


REPO = Path(__file__).resolve().parent.parent


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _parse_line(line: str) -> tuple[str, str] | None:
    raw = line.strip()
    if not raw or raw.startswith("#"):
        return None
    if raw.startswith("export "):
        raw = raw[len("export ") :].lstrip()
    if "=" not in raw:
        return None
    key, value = raw.split("=", 1)
    key = key.strip()
    if not key or not key.replace("_", "").isalnum() or key[0].isdigit():
        return None
    value = value.strip()
    try:
        parts = shlex.split(value, comments=True, posix=True)
    except ValueError:
        parts = [value]
    return key, " ".join(parts) if parts else ""


def load_dotenv(*, path: Path | None = None) -> None:
    """Load missing values from `.env` unless explicitly disabled."""
    if _truthy(os.environ.get("BROWSER_USE_RS_DISABLE_DOTENV")):
        return
    env_path = path or (REPO / ".env")
    if not env_path.exists():
        return
    for line in env_path.read_text(encoding="utf-8", errors="replace").splitlines():
        parsed = _parse_line(line)
        if parsed is None:
            continue
        key, value = parsed
        os.environ.setdefault(key, value)


def require_env(name: str) -> str:
    """Return an env var after loading `.env`, or exit with a clear message."""
    load_dotenv()
    value = os.environ.get(name)
    if not value:
        raise SystemExit(f"Set {name}")
    return value
