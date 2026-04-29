"""Compatibility import path for `from browser_use.agent.views import ...`.

Re-exports the AgentHistoryList family from `browser_use_rs.views` so
consumer code that uses browser_use's dotted path keeps working.
"""

from browser_use_rs.views import (
    ActionResult,
    AgentHistory,
    AgentHistoryList,
    AgentOutput,
    AgentState,
    BrowserStateSummary,
    StepMetadata,
)

__all__ = [
    "ActionResult",
    "AgentHistory",
    "AgentHistoryList",
    "AgentOutput",
    "AgentState",
    "BrowserStateSummary",
    "StepMetadata",
]
