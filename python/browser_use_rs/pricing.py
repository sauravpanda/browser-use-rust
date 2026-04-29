"""Per-1M-token pricing for known LLM models.

Used by `ChatInvokeUsage.model_dump()` to compute per-task cost so the
eval-platform aggregator (`convex/mutations/temporaryUpload.ts`) can roll
up `totalCost` / `avgPrice` correctly. Without these fields the aggregator
silently falls through to `taskCost = 0`.

Pricing data is sourced from LiteLLM's canonical table
(https://github.com/BerriAI/litellm/blob/main/litellm/model_prices_and_context_window_backup.json),
filtered to the models the eval suite exercises. Refresh this file when
LiteLLM's snapshot drifts; or override at runtime via
`register_pricing(model, prompt, completion, cached)`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

if TYPE_CHECKING:
    from browser_use_rs.llm.base import ChatInvokeUsage


_USD_PER_M = 1_000_000


# (prompt, completion, cached_prompt) per 1M tokens, USD.
# Synced from LiteLLM 2026-04-29 snapshot.
_MODEL_PRICING: dict[str, tuple[float, float, float]] = {
    # ----- Google Gemini -----
    "gemini-1.5-flash": (0.075, 0.30, 0.01875),
    "gemini-1.5-flash-latest": (0.075, 0.30, 0.01875),
    "gemini-2.0-flash": (0.10, 0.40, 0.025),
    "gemini-2.0-flash-lite": (0.075, 0.30, 0.01875),
    "gemini-2.5-flash": (0.30, 2.50, 0.03),
    "gemini-2.5-flash-lite": (0.10, 0.40, 0.01),
    "gemini-2.5-flash-preview-09-2025": (0.30, 2.50, 0.075),
    "gemini-2.5-pro": (1.25, 10.00, 0.125),
    "gemini-2.5-pro-preview-05-06": (1.25, 10.00, 0.125),
    "gemini-3-flash-preview": (0.50, 3.00, 0.05),
    "gemini-3-pro-preview": (2.00, 12.00, 0.20),
    "gemini-flash-latest": (0.30, 2.50, 0.03),
    "gemini-flash-lite-latest": (0.10, 0.40, 0.01),
    # ----- OpenAI -----
    "gpt-4.1": (2.00, 8.00, 0.50),
    "gpt-4.1-mini": (0.40, 1.60, 0.10),
    "gpt-4.1-nano": (0.10, 0.40, 0.025),
    "gpt-4o": (2.50, 10.00, 1.25),
    "gpt-4o-mini": (0.165, 0.66, 0.075),
    "gpt-5": (1.25, 10.00, 0.125),
    "gpt-5-mini": (0.25, 2.00, 0.025),
    "gpt-5-nano": (0.05, 0.40, 0.005),
    "gpt-5.1": (1.25, 10.00, 0.125),
    "gpt-5.2": (1.75, 14.00, 0.175),
    "o3-2025-04-16": (10.00, 40.00, 2.50),
    "o4-mini": (1.10, 4.40, 0.275),
    # ----- Anthropic Claude -----
    "claude-3-5-haiku-latest": (0.80, 4.00, 0.08),
    "claude-3-5-sonnet-20240620": (3.00, 15.00, 0.30),
    "claude-3-5-sonnet-20241022": (3.00, 15.00, 0.30),
    "claude-3-7-sonnet-20250219": (3.00, 15.00, 0.30),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
    "claude-opus-4-20250514": (15.00, 75.00, 1.50),
    "claude-opus-4-5": (5.00, 25.00, 0.50),
    "claude-opus-4-6": (5.00, 25.00, 0.50),
    "claude-opus-4-7": (5.00, 25.00, 0.50),
    "claude-sonnet-4-20250514": (3.00, 15.00, 0.30),
    "claude-sonnet-4-5": (3.00, 15.00, 0.30),
    "claude-sonnet-4-5-20250929": (3.00, 15.00, 0.30),
    "claude-sonnet-4-6": (3.00, 15.00, 0.30),
    # ----- DeepSeek -----
    "deepseek-chat": (0.28, 0.42, 0.028),
    "deepseek-reasoner": (0.28, 0.42, 0.028),
    # ----- Groq Llama -----
    "llama-3.1-70b-versatile": (0.59, 0.79, 0.1475),
    "llama-3.3-70b-versatile": (0.59, 0.79, 0.1475),
}


_PRICING_OVERRIDES: dict[str, tuple[float, float, float]] = {}


def register_pricing(
    model: str,
    prompt_per_m: float,
    completion_per_m: float,
    cached_prompt_per_m: float | None = None,
) -> None:
    """Override the built-in pricing for a model. Use when the table
    is stale or for custom-priced deployments."""
    cached = cached_prompt_per_m if cached_prompt_per_m is not None else prompt_per_m * 0.25
    _PRICING_OVERRIDES[model] = (prompt_per_m, completion_per_m, cached)


def lookup_pricing(model: str) -> Optional[tuple[float, float, float]]:
    """Return (prompt, completion, cached_prompt) per 1M tokens or None.

    Looks up by exact match first, then strips common provider prefixes
    (`gemini/`, `azure/`, `groq/`, `openai/`), then falls back to a
    longest-prefix match for versioned names (`claude-opus-4-5-20251101`
    matches `claude-opus-4-5`). Returns None if nothing matches —
    `cost_for` then emits zero costs rather than guessing.
    """
    if model in _PRICING_OVERRIDES:
        return _PRICING_OVERRIDES[model]
    if model in _MODEL_PRICING:
        return _MODEL_PRICING[model]

    # Strip provider prefixes
    for prefix in ("gemini/", "azure/", "groq/", "openai/", "anthropic/"):
        if model.startswith(prefix):
            stripped = model[len(prefix) :]
            if stripped in _PRICING_OVERRIDES:
                return _PRICING_OVERRIDES[stripped]
            if stripped in _MODEL_PRICING:
                return _MODEL_PRICING[stripped]
            model = stripped
            break

    # Longest-prefix match — handles versioned model ids that we don't
    # explicitly enumerate (`claude-opus-4-5-20251101`).
    candidates = sorted(
        (k for k in _MODEL_PRICING if model.startswith(k)),
        key=len,
        reverse=True,
    )
    if candidates:
        return _MODEL_PRICING[candidates[0]]
    return None


def cost_for(model: str | None, usage: "ChatInvokeUsage") -> dict[str, float]:
    """Compute per-cost-bucket costs for a usage record.

    Cache-read tokens are billed at the cached rate; the remaining
    `(input - cache_read)` is billed at the regular prompt rate. Returns
    zeros when pricing can't be looked up — caller (eval aggregator) will
    treat the task as missing cost data and skip it from `tasksWithUsage`,
    which is the right fallback rather than a bogus number.
    """
    if not model:
        return _zero_costs()
    p = lookup_pricing(model)
    if p is None:
        return _zero_costs()
    prompt_rate, completion_rate, cached_rate = p

    cached_in = max(0, usage.cache_read)
    fresh_in = max(0, usage.input - cached_in)
    completion = max(0, usage.output)

    prompt_cost = fresh_in * prompt_rate / _USD_PER_M
    cached_cost = cached_in * cached_rate / _USD_PER_M
    completion_cost = completion * completion_rate / _USD_PER_M
    total = prompt_cost + cached_cost + completion_cost

    return {
        "total_prompt_cost": prompt_cost,
        "total_prompt_cached_cost": cached_cost,
        "total_completion_cost": completion_cost,
        "total_cost": total,
    }


def _zero_costs() -> dict[str, float]:
    return {
        "total_prompt_cost": 0.0,
        "total_prompt_cached_cost": 0.0,
        "total_completion_cost": 0.0,
        "total_cost": 0.0,
    }


__all__ = ["cost_for", "lookup_pricing", "register_pricing"]
