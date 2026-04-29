"""High-level BrowserSession wrapper + BrowserProfile config object.

Mirrors browser_use's two-object pattern:
    profile = BrowserProfile(headless=False, allowed_domains=["example.com"])
    session = BrowserSession(browser_profile=profile, cdp_url="wss://...")

Internally we construct one Rust session; this module just translates the
profile kwargs and proxies attribute access to the native object so the
existing async API (`navigate`, `screenshot`, `dom_snapshot`, ...) keeps
working without enumeration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from browser_use_rs._native import BrowserSession as _NativeBrowserSession


@dataclass
class BrowserProfile:
    """Browser launch / runtime configuration. Matches browser_use's
    BrowserProfile shape; many fields are accepted for API compat but
    not yet enforced (see notes on each).
    """

    headless: bool = True
    # window_size is the browser_use name; viewport is our internal name.
    # If both are set, window_size wins.
    window_size: dict[str, int] | None = None
    viewport: tuple[int, int] | None = (1280, 900)
    chrome_path: str | None = None
    extra_args: list[str] = field(default_factory=list)
    # The following fields are accepted for parity with browser_use but
    # are NOT yet wired through to the Rust browser layer:
    keep_alive: bool = False
    highlight_elements: bool = True
    allowed_domains: list[str] | None = None
    prohibited_domains: list[str] | None = None
    block_ip_addresses: bool = False
    cookies: list[dict[str, Any]] | None = None
    local_storage: dict[str, Any] | None = None
    storage_state_path: str | None = None
    downloads_path: str | None = None

    def to_native_kwargs(self) -> dict[str, Any]:
        """Translate to the native BrowserSession constructor kwargs."""
        viewport = self.viewport
        if self.window_size is not None:
            w = int(self.window_size.get("width", 1280))
            h = int(self.window_size.get("height", 900))
            viewport = (w, h)
        return {
            "headless": self.headless,
            "viewport": viewport,
            "chrome_path": self.chrome_path,
            "extra_chrome_args": list(self.extra_args) if self.extra_args else None,
        }


class BrowserSession:
    """Wrapper supporting browser_use's `browser_profile=` and `cdp_url=`
    constructor pattern. Proxies all other attribute access to the
    underlying native session, so existing API surface is unchanged.

    Direct kwarg form still works:
        BrowserSession(headless=True)
        BrowserSession(cdp_url="wss://...")
    """

    def __init__(
        self,
        *,
        browser_profile: BrowserProfile | None = None,
        cdp_url: str | None = None,
        is_local: bool = True,  # accepted for compat; ignored locally
        downloads_path: str | None = None,
        # Direct kwargs (also forwarded if browser_profile not given):
        headless: bool = True,
        viewport: tuple[int, int] | None = (1280, 900),
        chrome_path: str | None = None,
        extra_chrome_args: list[str] | None = None,
        # Compat-only kwargs we silently swallow (keep_alive,
        # cross_origin_iframes, allowed_domains, ...):
        **_compat_kwargs: Any,
    ):
        if browser_profile is not None:
            kwargs = browser_profile.to_native_kwargs()
            if downloads_path is None:
                downloads_path = browser_profile.downloads_path
        else:
            kwargs = {
                "headless": headless,
                "viewport": viewport,
                "chrome_path": chrome_path,
                "extra_chrome_args": extra_chrome_args,
            }
        if cdp_url is not None:
            kwargs["cdp_url"] = cdp_url
        self._native = _NativeBrowserSession(**kwargs)
        self._is_local = is_local
        self._downloads_path = downloads_path

    def __getattr__(self, name: str) -> Any:
        # Proxy everything we don't override to the native session. This
        # avoids enumerating ~30 methods just to add a constructor.
        return getattr(object.__getattribute__(self, "_native"), name)
