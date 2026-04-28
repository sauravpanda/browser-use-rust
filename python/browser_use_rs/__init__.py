"""browser_use_rs — lean Rust runtime for browser-use, exposed to Python.

The Rust extension is imported as `browser_use_rs._native`. Higher-level
Python APIs (Agent, LLM providers, tool registry, prompts) live in this
package and call into the native layer for browser/CDP/DOM work.
"""

from browser_use_rs._native import (
    Bbox,
    BrowserSession,
    DomElement,
    DomState,
    version,
)

__all__ = ["Bbox", "BrowserSession", "DomElement", "DomState", "version"]
__version__ = version()
