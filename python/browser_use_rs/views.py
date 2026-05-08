"""Read-shape returned by Agent.run() — modeled on browser_use's
AgentHistoryList so consumer code (evaluations-internal, cloud) can drop
in without changes.

The names and field shapes (`history.history[i].result[j].is_done`,
`history.final_result()`, `history.is_done()`, `state.get_screenshot()`,
`metadata.model_dump()`) match what those callers read.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from browser_use_rs.llm.base import ChatInvokeUsage, ToolCall


@dataclass
class ActionResult:
    """Outcome of one tool invocation. The agent's `done` tool sets
    is_done=True and puts the final answer in extracted_content."""

    extracted_content: str | None = None
    error: str | None = None
    is_done: bool = False
    success: bool | None = None
    long_term_memory: str | None = None
    include_extracted_content_only_once: bool = False

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class StepMetadata:
    step_number: int
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    step_start_time: float
    step_end_time: float
    # v0.12.2 DOM measurement instrumentation. v0.12.1 stashed these on
    # BrowserStateSummary.dom_metrics, but the dashboard's completeHistory
    # serializer drops everything from `state` except title+url. The
    # `metadata` channel passes model_dump keys through verbatim, so we
    # surface them here as flat scalars instead. All fields default to 0
    # so older runs / failed snapshots round-trip cleanly. Populated in
    # _append_history by copying state.dom_metrics; never recomputed.
    dom_total_bytes: int = 0
    dom_total_elements: int = 0
    dom_interactive_count: int = 0
    dom_static_text_count: int = 0
    dom_interactive_text_bytes: int = 0
    dom_static_text_bytes: int = 0
    dom_interactive_attrs_bytes: int = 0
    dom_interactive_attrs_count: int = 0
    dom_interactive_attrs_per_el_avg: float = 0.0
    dom_el_size_p50: int = 0
    dom_el_size_p90: int = 0
    dom_el_size_max: int = 0

    @property
    def duration_seconds(self) -> float:
        return self.step_end_time - self.step_start_time

    def model_dump(self) -> dict[str, Any]:
        d = asdict(self)
        d["duration_seconds"] = self.duration_seconds
        return d

    # browser_use's StepMetadata is dict-subscriptable in places (eval
    # consumers do `metadata['input_tokens']`). Forward to attribute access.
    def __getitem__(self, key: str) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        raise KeyError(key)

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


@dataclass
class AgentOutput:
    """The model's output for one step — text and/or tool calls."""

    text: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)

    def model_dump(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [
                {"id": tc.id, "name": tc.name, "args": tc.args}
                for tc in self.tool_calls
            ],
        }


@dataclass
class BrowserStateSummary:
    """Snapshot of the page at the start of a step."""

    url: str = ""
    title: str = ""
    screenshot: str | None = None
    # browser_use writes screenshots to disk and exposes the path; we keep
    # the base64 in-memory and don't write a file by default. Consumers
    # that need a path can write `screenshot` themselves.
    screenshot_path: str | None = None
    elements_text: str = ""
    # v0.12.1 measurement instrumentation. Per-snapshot DOM size
    # breakdown (total bytes, interactive vs static-text counts, attr
    # bloat distribution). Stored on AgentHistory.state so it surfaces
    # in dashboard completeHistory; never injected into the LLM prompt.
    # Used to identify which DOM lever is actually worth pulling for
    # v0.12.x cost optimization. Optional so older snapshots that don't
    # populate it remain valid.
    dom_metrics: dict[str, Any] | None = None

    def get_screenshot(self) -> str | None:
        return self.screenshot

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class AgentHistory:
    """One step in the agent loop: state observed → output emitted →
    results from each tool call."""

    state: BrowserStateSummary
    output: AgentOutput
    result: list[ActionResult]
    metadata: StepMetadata | None = None

    @property
    def model_output(self) -> AgentOutput:
        """Alias for `output` — browser_use names this `model_output`."""
        return self.output

    def model_dump(self) -> dict[str, Any]:
        return {
            "state": self.state.model_dump(),
            "output": self.output.model_dump(),
            "model_output": self.output.model_dump(),
            "result": [r.model_dump() for r in self.result],
            "metadata": self.metadata.model_dump() if self.metadata else None,
        }


@dataclass
class AgentHistoryList:
    """All steps + total token usage. Returned by Agent.run()."""

    history: list[AgentHistory] = field(default_factory=list)
    usage: ChatInvokeUsage = field(default_factory=ChatInvokeUsage)
    # Set by `Agent._judge_and_log()` when the inline judge runs.
    # Eval consumers read via `is_judged()` / `judgement()`.
    _judgement_data: dict[str, Any] | None = field(default=None, repr=False)

    def _set_judgement(self, data: dict[str, Any]) -> None:
        self._judgement_data = data

    def is_judged(self) -> bool:
        return self._judgement_data is not None

    def judgement(self) -> dict[str, Any] | None:
        return self._judgement_data

    def final_result(self) -> str | None:
        """Last `done` action's extracted_content, falling back to the
        last step's text output if the agent never explicitly emitted done."""
        for step in reversed(self.history):
            for r in step.result:
                if r.is_done and r.extracted_content:
                    return r.extracted_content
        if self.history and self.history[-1].output.text:
            return self.history[-1].output.text
        return None

    def is_done(self) -> bool:
        if not self.history:
            return False
        return any(r.is_done for r in self.history[-1].result)

    def is_successful(self) -> bool | None:
        if not self.is_done():
            return None
        for r in reversed(self.history[-1].result):
            if r.is_done:
                return r.success
        return None

    def errors(self) -> list[str]:
        out: list[str] = []
        for step in self.history:
            for r in step.result:
                if r.error:
                    out.append(r.error)
        return out

    def total_input_tokens(self) -> int:
        return self.usage.input

    def total_output_tokens(self) -> int:
        return self.usage.output

    def model_dump(self) -> dict[str, Any]:
        # v0.8.16: use ChatInvokeUsage.model_dump() instead of asdict()
        # so the cost fields (`total_cost`, `total_prompt_cost`,
        # `total_prompt_cached_cost`, `total_completion_cost`) injected
        # by `pricing.cost_for()` propagate. asdict() only walks
        # @dataclass fields and silently drops dynamically-added cost
        # values, so any consumer reading `history.model_dump()["usage"]`
        # was getting tokens but no cost.
        return {
            "history": [h.model_dump() for h in self.history],
            "usage": self.usage.model_dump(),
        }


class AgentState:
    """Injected into Agent for resumable runs (matches browser_use's shape).

    Permissive constructor — eval consumers pass upstream kwargs we don't
    enumerate (`message_history_token_count`, `paused`, `stopped`, ...);
    those are stashed as attributes for read-back compatibility.
    """

    n_steps: int
    history: AgentHistoryList
    last_plan: str | None

    def __init__(
        self,
        *,
        n_steps: int = 0,
        history: AgentHistoryList | None = None,
        last_plan: str | None = None,
        **extra_kwargs: Any,
    ):
        self.n_steps = n_steps
        self.history = history if history is not None else AgentHistoryList()
        self.last_plan = last_plan
        for k, v in extra_kwargs.items():
            if not hasattr(self, k):
                setattr(self, k, v)
