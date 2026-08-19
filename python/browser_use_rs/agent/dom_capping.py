"""DOM byte-budget capping and per-snapshot DOM metrics.

Extracted verbatim from ``agent/__init__.py``. The browser always
captures the full structured snapshot for actions and observability;
these helpers bound what the LLM sees and keep the clickable-index map
in sync with the capped text.
"""

from __future__ import annotations

import logging
import os
from typing import Any

from browser_use_rs.agent.serialization import _utf8_len, _utf8_prefix

logger = logging.getLogger(__name__)

DEFAULT_DOM_MAX_BYTES = 64 * 1024
DOM_MAX_BYTES_ENV_VARS = (
    "BROWSER_USE_RS_DOM_MAX_BYTES",
    "BU_RS_DOM_MAX_BYTES",
)
DEFAULT_STATE_CACHE_MAX_REUSE_STEPS = 3
STATE_CACHE_MAX_REUSE_ENV_VARS = (
    "BROWSER_USE_RS_STATE_CACHE_MAX_REUSE_STEPS",
    "BU_RS_STATE_CACHE_MAX_REUSE_STEPS",
)


def _resolve_dom_max_bytes(value: Any = None) -> int:
    """Return the LLM-facing DOM byte budget.

    A value <= 0 disables capping. The env vars intentionally win only
    when the caller did not pass an explicit value.
    """
    raw = value
    if raw is None:
        for name in DOM_MAX_BYTES_ENV_VARS:
            env_value = os.environ.get(name)
            if env_value not in (None, ""):
                raw = env_value
                break
    if raw is None:
        return DEFAULT_DOM_MAX_BYTES
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "agent: invalid DOM byte budget %r; using default %d",
            raw,
            DEFAULT_DOM_MAX_BYTES,
        )
        return DEFAULT_DOM_MAX_BYTES


def _resolve_state_cache_max_reuse_steps(value: Any = None) -> int:
    """Return how many unchanged states can reuse the prior full state.

    A value <= 0 disables state reuse. Env vars are only consulted when
    no explicit value was passed.
    """
    raw = value
    if raw is None:
        for name in STATE_CACHE_MAX_REUSE_ENV_VARS:
            env_value = os.environ.get(name)
            if env_value not in (None, ""):
                raw = env_value
                break
    if raw is None:
        return DEFAULT_STATE_CACHE_MAX_REUSE_STEPS
    try:
        return int(raw)
    except (TypeError, ValueError):
        logger.warning(
            "agent: invalid state-cache reuse budget %r; using default %d",
            raw,
            DEFAULT_STATE_CACHE_MAX_REUSE_STEPS,
        )
        return DEFAULT_STATE_CACHE_MAX_REUSE_STEPS


def _dom_line_index(line: str) -> int | None:
    stripped = line.lstrip("\t ")
    changed = True
    while changed:
        changed = False
        if stripped.startswith("|scroll|"):
            stripped = stripped[len("|scroll|") :]
            changed = True
        if stripped.startswith("*"):
            stripped = stripped[1:]
            changed = True
    if not stripped.startswith("["):
        return None
    try:
        raw = stripped[1:].split("]", 1)[0]
        return int(raw)
    except (TypeError, ValueError):
        return None


def _dom_truncation_notice(
    *,
    kept_lines: int,
    total_lines: int,
    kept_indices: int,
    total_indices: int,
    max_bytes: int,
    full_bytes: int,
) -> str:
    return (
        "[DOM_TRUNCATED] Showing "
        f"{kept_lines}/{total_lines} DOM lines and "
        f"{kept_indices}/{total_indices} interactive indices within "
        f"{max_bytes} bytes (full snapshot was {full_bytes} bytes). "
        "Use scroll, find_elements, page_text, or extract_result_cards "
        "if the needed item is not listed below."
    )


def _cap_dom_for_llm(
    dom_text: str,
    index_to_selector: dict[int, str],
    max_bytes: int,
    metrics: dict[str, Any] | None = None,
) -> tuple[str, dict[int, str], dict[str, Any] | None]:
    """Cap the rendered DOM at line boundaries and keep indices in sync.

    The browser still captures the full structured snapshot for actions
    and observability, but the LLM only receives the capped text. Any
    omitted indices are removed from `_valid_indices` so the model cannot
    click numbers it did not see.
    """
    full_bytes = _utf8_len(dom_text)
    if max_bytes <= 0 or full_bytes <= max_bytes:
        if metrics is not None:
            metrics = dict(metrics)
            metrics.setdefault("full_total_bytes", full_bytes)
            metrics.setdefault("llm_bytes", full_bytes)
            metrics.setdefault("max_bytes", max_bytes)
            metrics.setdefault("truncated", False)
            metrics.setdefault("omitted_interactive_count", 0)
        return dom_text, index_to_selector, metrics

    lines = dom_text.splitlines()
    try:
        elements_pos = lines.index("ELEMENTS:")
        head_lines = lines[: elements_pos + 1]
        body_lines = lines[elements_pos + 1 :]
    except ValueError:
        head_lines = []
        body_lines = lines

    total_indices = {
        idx for idx in (_dom_line_index(line) for line in body_lines)
        if idx is not None and idx != 0
    }

    kept_lines: list[str] = []
    kept_indices: set[int] = set()

    def _render_with_notice() -> str:
        notice = _dom_truncation_notice(
            kept_lines=len(kept_lines),
            total_lines=len(body_lines),
            kept_indices=len(kept_indices),
            total_indices=len(total_indices) or len(index_to_selector),
            max_bytes=max_bytes,
            full_bytes=full_bytes,
        )
        return "\n".join([*head_lines, notice, *kept_lines])

    # Reserve the header + truncation notice first, then fill the rest
    # with original-order DOM rows that fit the byte budget.
    for line in body_lines:
        kept_lines.append(line)
        idx = _dom_line_index(line)
        if idx is not None and idx != 0:
            kept_indices.add(idx)
        if _utf8_len(_render_with_notice()) > max_bytes:
            kept_lines.pop()
            if idx is not None and idx != 0:
                kept_indices.discard(idx)

    capped_text = _render_with_notice()
    # The final notice may have grown by a few digits after selection.
    while _utf8_len(capped_text) > max_bytes and kept_lines:
        removed = kept_lines.pop()
        idx = _dom_line_index(removed)
        if idx is not None:
            kept_indices = {
                n for n in (_dom_line_index(line) for line in kept_lines)
                if n is not None and n != 0
            }
        capped_text = _render_with_notice()
    if _utf8_len(capped_text) > max_bytes:
        capped_text = _utf8_prefix(capped_text, max_bytes)

    shown_index_to_selector = {
        idx: selector
        for idx, selector in index_to_selector.items()
        if idx in kept_indices
    }

    if metrics is not None:
        metrics = dict(metrics)
        llm_bytes = _utf8_len(capped_text)
        metrics["full_total_bytes"] = full_bytes
        metrics["total_bytes"] = llm_bytes
        metrics["llm_bytes"] = llm_bytes
        metrics["max_bytes"] = max_bytes
        metrics["truncated"] = True
        metrics["omitted_bytes"] = max(0, full_bytes - llm_bytes)
        metrics["shown_interactive_count"] = len(shown_index_to_selector)
        metrics["omitted_interactive_count"] = max(
            0,
            len(index_to_selector) - len(shown_index_to_selector),
        )

    return capped_text, shown_index_to_selector, metrics


def _compute_dom_metrics(snap: Any, dom_text: str) -> dict[str, Any]:
    """v0.12.1 measurement helper. Per-snapshot DOM size breakdown.

    Stored on BrowserStateSummary.dom_metrics so it surfaces in
    AgentHistory → dashboard completeHistory. Never sent to the LLM.
    Used to identify which DOM lever is worth pulling for v0.12.x cost
    work — concretely: are we DOM-bloated vs upstream's ~30-35KB per
    snapshot, and if so, where (interactive count? static text? attrs
    per element? per-element bytes)?

    `snap` is a bu_dom DomState (interactive elements have index>0,
    static text rows have index==0). `dom_text` is the rendered
    to_llm_string output — its len gives the actual LLM-input bytes.
    """
    elements = list(snap.elements)
    total_bytes = len(dom_text)
    interactive = [e for e in elements if e.index != 0]
    static_text = [e for e in elements if e.index == 0]

    interactive_text_bytes = sum(len(e.text) for e in interactive)
    static_text_bytes = sum(len(e.text) for e in static_text)
    interactive_attrs_bytes = 0
    interactive_attrs_count = 0
    for e in interactive:
        for k, v in e.attrs.items():
            # to_llm_string renders ` k="v"` per attr → len(k)+len(v)+4
            interactive_attrs_bytes += len(k) + len(v) + 4
            interactive_attrs_count += 1

    # Approximate per-element rendered size (interactive only) so we
    # can see distribution: are a few mega-elements eating the budget,
    # or is bloat uniform across all?
    el_sizes: list[int] = []
    for e in interactive:
        size = 4 + len(e.tag) + len(e.text)  # `[N]<tag>text\n`
        for k, v in e.attrs.items():
            size += len(k) + len(v) + 4
        el_sizes.append(size)
    el_sizes.sort()
    n = len(el_sizes)

    return {
        "total_bytes": total_bytes,
        "total_elements": len(elements),
        "interactive_count": len(interactive),
        "static_text_count": len(static_text),
        "interactive_text_bytes": interactive_text_bytes,
        "static_text_bytes": static_text_bytes,
        "interactive_attrs_bytes": interactive_attrs_bytes,
        "interactive_attrs_count": interactive_attrs_count,
        "interactive_attrs_per_el_avg": (
            round(interactive_attrs_count / len(interactive), 2)
            if interactive else 0
        ),
        "el_size_p50": el_sizes[n // 2] if n else 0,
        "el_size_p90": el_sizes[int(n * 0.9)] if n else 0,
        "el_size_max": el_sizes[-1] if n else 0,
    }
