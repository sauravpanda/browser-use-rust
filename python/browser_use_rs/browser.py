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

from typing import Any

from browser_use_rs._native import BrowserSession as _NativeBrowserSession


class BrowserProfile:
    """Browser launch / runtime configuration. Matches browser_use's
    BrowserProfile shape — accepts the full upstream kwarg surface.

    Fields we honor are stored as typed attributes; everything else is
    silently swallowed and stored on `self` so consumer code can read it
    back. This matches the permissive pattern used by Agent/BrowserSession
    and lets the eval suite's `BrowserProfile(user_data_dir=..., ...)`
    constructions work without us enumerating the entire upstream surface.
    """

    # ------ fields we actually wire through ------
    # (kept as class-level type hints for IDE/typecheck support)
    headless: bool
    window_size: dict[str, int] | None
    viewport: tuple[int, int] | None
    chrome_path: str | None
    extra_args: list[str]
    user_data_dir: str | None
    # Navigation policy — enforced in Rust on navigate() and new_tab().
    allowed_domains: list[str] | None
    prohibited_domains: list[str] | None
    # Anti-bot Chrome flags. Off by default.
    stealth: bool
    # ------ accepted for parity, currently no-op or read-only ------
    keep_alive: bool
    highlight_elements: bool
    block_ip_addresses: bool
    cookies: list[dict[str, Any]] | None
    local_storage: dict[str, Any] | None
    storage_state_path: str | None
    downloads_path: str | None

    def __init__(
        self,
        *,
        headless: bool = True,
        window_size: dict[str, int] | None = None,
        viewport: tuple[int, int] | None = (1280, 900),
        chrome_path: str | None = None,
        extra_args: list[str] | None = None,
        user_data_dir: str | None = None,
        allowed_domains: list[str] | None = None,
        prohibited_domains: list[str] | None = None,
        stealth: bool = False,
        keep_alive: bool = False,
        highlight_elements: bool = True,
        block_ip_addresses: bool = False,
        cookies: list[dict[str, Any]] | None = None,
        local_storage: dict[str, Any] | None = None,
        storage_state_path: str | None = None,
        downloads_path: str | None = None,
        **extra_kwargs: Any,
    ):
        self.headless = headless
        self.window_size = window_size
        self.viewport = viewport
        self.chrome_path = chrome_path
        self.extra_args = list(extra_args) if extra_args else []
        self.user_data_dir = user_data_dir
        self.allowed_domains = allowed_domains
        self.prohibited_domains = prohibited_domains
        self.stealth = stealth
        self.keep_alive = keep_alive
        self.highlight_elements = highlight_elements
        self.block_ip_addresses = block_ip_addresses
        self.cookies = cookies
        self.local_storage = local_storage
        self.storage_state_path = storage_state_path
        self.downloads_path = downloads_path
        # Stash any other kwarg (cookies_file, profile_directory,
        # disable_security, args, ...) so consumer code can read back
        # what it set, without us breaking on unknown names.
        for k, v in extra_kwargs.items():
            if not hasattr(self, k):
                setattr(self, k, v)

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
            "allowed_domains": (
                list(self.allowed_domains) if self.allowed_domains else None
            ),
            "prohibited_domains": (
                list(self.prohibited_domains) if self.prohibited_domains else None
            ),
            "stealth": self.stealth,
            "user_data_dir": self.user_data_dir,
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
        # browser_use's call sites use `profile=` (not `browser_profile=`).
        # Accept both; `browser_profile` wins if both are passed.
        profile: BrowserProfile | None = None,
        cdp_url: str | None = None,
        is_local: bool = True,  # accepted for compat; ignored locally
        downloads_path: str | None = None,
        # Direct kwargs (also forwarded if browser_profile not given):
        headless: bool = True,
        viewport: tuple[int, int] | None = (1280, 900),
        chrome_path: str | None = None,
        extra_chrome_args: list[str] | None = None,
        user_data_dir: str | None = None,
        # Navigation policy + stealth — also accepted directly so
        # consumers don't have to construct a BrowserProfile just for
        # these. browser_profile fields win if both are set.
        allowed_domains: list[str] | None = None,
        prohibited_domains: list[str] | None = None,
        stealth: bool = False,
        # Compat-only kwargs we silently swallow:
        # highlight_elements, keep_alive, cross_origin_iframes, ...
        **_compat_kwargs: Any,
    ):
        if browser_profile is None and profile is not None:
            browser_profile = profile
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
                "user_data_dir": user_data_dir,
                "allowed_domains": list(allowed_domains) if allowed_domains else None,
                "prohibited_domains": (
                    list(prohibited_domains) if prohibited_domains else None
                ),
                "stealth": stealth,
            }
        if cdp_url is not None:
            kwargs["cdp_url"] = cdp_url
        # Drop None entries so the native constructor's defaults apply.
        kwargs = {k: v for k, v in kwargs.items() if v is not None}
        self._native = _NativeBrowserSession(**kwargs)
        self._is_local = is_local
        self._downloads_path = downloads_path

    def __getattr__(self, name: str) -> Any:
        # Proxy everything we don't override to the native session. This
        # avoids enumerating ~30 methods just to add a constructor.
        return getattr(object.__getattribute__(self, "_native"), name)
