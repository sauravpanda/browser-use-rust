"""Scratchpad for long tool outputs.

When a tool returns more text than fits comfortably in the LLM's working
context (long article reads, dense link lists, full DOM dumps), we write
the full content to disk and hand the LLM a head+tail preview plus a
recovery hint. The LLM can then drill into specifics via `grep_scratchpad`
or `read_scratchpad` without re-running the original tool.

Lifted from OpenCode's `packages/opencode/src/tool/truncate.ts` — the
single most reusable pattern from that codebase. Trade is one
filesystem write per long tool call (negligible) against keeping the
conversation context bounded as runs get long.

Files live under `<scratchpad_dir>/<agent_id>/<step>-<tool>-<n>.txt`
where `agent_id` is a per-Agent UUID set at construction. We don't auto-
delete: a separate sweeper or per-host TMP cleanup handles that. For
eval and smoke tests the volume is small (a few MB per run).
"""

from __future__ import annotations

import os
import tempfile
import uuid
from pathlib import Path
from typing import NamedTuple

# Tool outputs above either threshold get spilled to disk. Numbers picked
# to match OpenCode (50KB, 2000 lines) but tightened for browser-agent
# runs where typical reads are smaller and context windows tighter.
DEFAULT_MAX_BYTES = 32 * 1024
DEFAULT_MAX_LINES = 1000

# Lines kept inline in the truncated preview (head + tail). Sized so the
# LLM still gets ~80% of a typical page in the prompt without paying for
# the whole thing on tasks where it doesn't need details.
HEAD_LINES = 200
TAIL_LINES = 100


class TruncatedResult(NamedTuple):
    """Returned by `maybe_spill` when a tool output exceeded the threshold.

    `preview` is what to send the LLM (head + truncation banner + tail);
    `path` is the absolute on-disk location of the full content (passed
    back via `grep_scratchpad`/`read_scratchpad`).
    """

    preview: str
    path: str
    full_lines: int
    full_bytes: int


def _scratchpad_root() -> Path:
    """Per-host scratchpad root. Honors $BROWSER_USE_RS_SCRATCHPAD if set,
    else falls back to a stable subdir of $TMPDIR / /tmp."""
    override = os.environ.get("BROWSER_USE_RS_SCRATCHPAD")
    if override:
        return Path(override)
    return Path(tempfile.gettempdir()) / "browser-use-rs-scratchpad"


def new_agent_id() -> str:
    """UUID for an Agent's scratchpad subdir. Stable across the run so
    `grep_scratchpad` calls find the file written by an earlier `page_text`.
    """
    return uuid.uuid4().hex[:12]


def maybe_spill(
    content: str,
    *,
    agent_id: str,
    step: int,
    tool_name: str,
    call_idx: int = 0,
    max_bytes: int = DEFAULT_MAX_BYTES,
    max_lines: int = DEFAULT_MAX_LINES,
) -> TruncatedResult | None:
    """If `content` exceeds either threshold, write the full text to a
    scratchpad file and return a `TruncatedResult` with the LLM-facing
    preview. Returns None when the content fits — caller should pass it
    through unchanged.

    Preview format (matches OpenCode's pattern, adapted for browser tools):

        <first HEAD_LINES of content>

        [SCRATCHPAD] truncated K more lines (full N lines / B bytes)
        Full content saved to: <abs_path>
        Drill in with:
          grep_scratchpad(path="<abs_path>", pattern="<regex or substring>")
          read_scratchpad(path="<abs_path>", offset=<line_n>, limit=<count>)

        <last TAIL_LINES of content>
    """
    n_bytes = len(content.encode("utf-8", errors="replace"))
    lines = content.splitlines()
    n_lines = len(lines)
    if n_bytes <= max_bytes and n_lines <= max_lines:
        return None

    # Write full content to disk before composing the preview so the
    # path we hand the LLM is always valid.
    root = _scratchpad_root() / agent_id
    root.mkdir(parents=True, exist_ok=True)
    fname = f"step{step:03d}-{tool_name}-{call_idx}.txt"
    path = root / fname
    path.write_text(content, encoding="utf-8", errors="replace")

    head = "\n".join(lines[:HEAD_LINES])
    tail = "\n".join(lines[-TAIL_LINES:]) if n_lines > HEAD_LINES + TAIL_LINES else ""
    truncated_lines = max(0, n_lines - HEAD_LINES - TAIL_LINES)

    banner = (
        f"\n\n[SCRATCHPAD] truncated {truncated_lines} more lines "
        f"(full {n_lines} lines / {n_bytes} bytes)\n"
        f"Full content saved to: {path}\n"
        f"Drill in with:\n"
        f"  grep_scratchpad(path=\"{path}\", pattern=\"<regex or substring>\")\n"
        f"  read_scratchpad(path=\"{path}\", offset=<line_n>, limit=<count>)\n\n"
    )
    preview = head + banner + tail if tail else head + banner.rstrip("\n")

    return TruncatedResult(
        preview=preview,
        path=str(path),
        full_lines=n_lines,
        full_bytes=n_bytes,
    )


def grep(path: str, pattern: str, *, max_matches: int = 50, context: int = 1) -> str:
    """Return matching lines from a scratchpad file.

    `pattern` is treated as a Python regex; if it fails to compile we
    fall back to substring search. `context` is lines of context per
    match (1 = include 1 line before + 1 after). Caps at `max_matches`
    to keep output bounded; if more matches exist we say so.
    """
    import re as _re

    p = Path(path)
    if not p.exists():
        return f"(scratchpad file not found: {path})"

    try:
        rx: _re.Pattern[str] | None = _re.compile(pattern)
    except _re.error:
        rx = None

    matches: list[tuple[int, str]] = []
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, line in enumerate(lines):
        if rx is not None:
            if rx.search(line):
                matches.append((i, line))
        else:
            if pattern in line:
                matches.append((i, line))
        if len(matches) >= max_matches:
            break

    if not matches:
        return f"(no matches for {pattern!r} in {path})"

    # Render with line numbers + context windows.
    out: list[str] = []
    out.append(f"({len(matches)} match(es) for {pattern!r}, showing line# : line)")
    for line_n, line in matches:
        lo = max(0, line_n - context)
        hi = min(len(lines), line_n + context + 1)
        for j in range(lo, hi):
            marker = ">>" if j == line_n else "  "
            out.append(f"{marker} {j+1:>5}: {lines[j]}")
        out.append("")
    if len(matches) >= max_matches:
        out.append(f"(stopped at {max_matches} matches; refine pattern to see more)")
    return "\n".join(out)


def read_offset(path: str, offset: int = 0, limit: int = 100) -> str:
    """Return `limit` lines starting at 1-based `offset`. For browsing
    long files in chunks when grep isn't precise enough."""
    p = Path(path)
    if not p.exists():
        return f"(scratchpad file not found: {path})"
    if offset < 1:
        offset = 1
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines()
    chunk = lines[offset - 1 : offset - 1 + limit]
    if not chunk:
        return f"(offset {offset} past end of file; total lines: {len(lines)})"
    out: list[str] = [f"(lines {offset}-{offset + len(chunk) - 1} of {len(lines)})"]
    for i, line in enumerate(chunk):
        out.append(f"{offset + i:>5}: {line}")
    return "\n".join(out)
