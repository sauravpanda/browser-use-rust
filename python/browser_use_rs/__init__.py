"""browser_use_rs — lean Rust runtime for browser-use, exposed to Python.

The Rust extension is imported as `browser_use_rs._native`. Higher-level
Python APIs (Agent, LLM providers, tool registry, prompts) live in this
package and call into the native layer for browser/CDP/DOM work.
"""

from browser_use_rs._native import (
    Bbox,
    DomElement,
    DomState,
    version,
)
from browser_use_rs.agent import Agent
from browser_use_rs.browser import BrowserProfile, BrowserSession
from browser_use_rs.controller import Controller
from browser_use_rs.tools import Tool, tool
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
    "Agent",
    "AgentHistory",
    "AgentHistoryList",
    "AgentOutput",
    "AgentState",
    "Bbox",
    "BrowserProfile",
    "BrowserSession",
    "BrowserStateSummary",
    "Controller",
    "DomElement",
    "DomState",
    "StepMetadata",
    "Tool",
    "tool",
    "version",
]
__version__ = version()
