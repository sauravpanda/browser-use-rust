//! Error, options, and data types for the browser session.
//! Extracted verbatim from `lib.rs`; visibility widened to
//! `pub(crate)` only where `lib.rs` accesses private fields.

use std::path::PathBuf;

use bu_cdp::CdpError;
use bu_dom::DomError;
use serde_json::Value;
use thiserror::Error;

#[derive(Debug, Error)]
pub enum BrowserError {
    #[error("cdp: {0}")]
    Cdp(#[from] CdpError),
    #[error("dom: {0}")]
    Dom(#[from] DomError),
    #[error("io: {0}")]
    Io(#[from] std::io::Error),
    #[error("could not find chrome/chromium binary; set CHROME=<path>")]
    ChromeNotFound,
    #[error("did not see DevTools URL on chrome stderr within timeout")]
    NoDevToolsUrl,
    #[error("unexpected response from {method}: {detail}")]
    BadResponse { method: &'static str, detail: String },
    #[error("base64 decode: {0}")]
    Base64(#[from] base64::DecodeError),
    #[error("serde: {0}")]
    Serde(#[from] serde_json::Error),
    #[error("no dom snapshot taken yet — call dom_snapshot() before acting on an index")]
    NoSnapshot,
    #[error("element [{0}] is no longer present in the DOM — re-snapshot before acting")]
    ElementGone(u32),
    #[error("unknown tab target_id: {0}")]
    UnknownTab(String),
    #[error("cannot close last tab — call stop() to end the session")]
    LastTab,
    #[error(
        "navigation to {url} blocked: hostname not permitted by allowed_domains / prohibited_domains policy"
    )]
    NavigationBlocked { url: String },
}

pub type Result<T> = std::result::Result<T, BrowserError>;

#[derive(Debug, Clone)]
pub struct LaunchOptions {
    pub headless: bool,
    pub chrome_path: Option<PathBuf>,
    pub user_data_dir: Option<PathBuf>,
    pub extra_args: Vec<String>,
    /// Viewport (width, height) in CSS pixels. When set, we pass
    /// --window-size to Chrome and call Emulation.setDeviceMetricsOverride
    /// after attach so JS-visible viewport matches the OS window.
    pub viewport: Option<(u32, u32)>,
    /// If set, skip launching Chromium and attach to an existing CDP
    /// endpoint at this URL (`ws://...` or `http(s)://.../json/version`).
    /// Used by cloud browser providers (Anchor, Browserbase, Hyperbrowser,
    /// Daytona, BrightData). When attached, `stop()` detaches but does
    /// NOT call Browser.close — the remote owner manages the browser.
    pub cdp_url: Option<String>,
    /// If non-empty, navigation is restricted to URLs whose hostname
    /// matches one of these patterns. Empty = no restriction.
    /// Patterns: bare hostname (`example.com`), subdomain wildcard
    /// (`*.example.com` matches both root and subdomains), prefix
    /// wildcard (`*google.com` matches `agoogle.com`, `www.google.com`),
    /// or scheme-qualified (`http*://example.com`, `chrome-extension://*`).
    /// Default scheme is `https` if not specified. Multiple wildcards or
    /// wildcard TLDs (`example.*`) are rejected as unsafe.
    pub allowed_domains: Vec<String>,
    /// If non-empty, navigation to URLs matching any of these patterns
    /// is blocked even if `allowed_domains` would otherwise permit them.
    /// Same pattern syntax as `allowed_domains`.
    pub prohibited_domains: Vec<String>,
    /// Add Chrome flags that reduce headless-detection signals
    /// (`--disable-blink-features=AutomationControlled`, etc.). Helpful
    /// for sites that serve degraded pages to detected automation
    /// (Google, DuckDuckGo). Off by default — these flags can also
    /// trigger different bugs on a small number of sites.
    pub stealth: bool,
}

impl Default for LaunchOptions {
    fn default() -> Self {
        Self {
            headless: true,
            chrome_path: None,
            user_data_dir: None,
            extra_args: Vec::new(),
            viewport: None,
            cdp_url: None,
            allowed_domains: Vec::new(),
            prohibited_domains: Vec::new(),
            stealth: false,
        }
    }
}

#[derive(Debug, Clone)]
pub struct TabInfo {
    pub target_id: String,
    pub url: String,
    pub title: String,
    /// "page" for top-level tabs/windows, "iframe" for cross-origin frames.
    pub target_type: String,
    pub is_active: bool,
}

#[derive(Debug, Clone)]
pub struct Cookie {
    pub name: String,
    pub value: String,
    pub domain: String,
    pub path: String,
    /// Unix timestamp (seconds). -1 means session cookie.
    pub expires: f64,
    pub secure: bool,
    pub http_only: bool,
}

#[derive(Debug, Clone)]
pub struct DownloadInfo {
    pub guid: String,
    pub suggested_filename: String,
    pub url: String,
    pub state: String, // "inProgress" | "completed" | "canceled"
    pub received_bytes: u64,
    pub total_bytes: u64,
    /// Where the file lands on disk. Chrome saves downloads under
    /// `download_dir/<guid>` by default with our setDownloadBehavior config.
    pub file_path: PathBuf,
}

#[derive(Debug, Clone, Default)]
pub(crate) struct DownloadState {
    pub(crate) suggested_filename: String,
    pub(crate) url: String,
    pub(crate) state: String,
    pub(crate) received_bytes: u64,
    pub(crate) total_bytes: u64,
}

#[derive(Debug, Clone)]
pub(crate) struct ActiveTab {
    pub(crate) target_id: String,
    pub(crate) session_id: String,
}

pub(crate) fn parse_cookie(v: Value) -> Option<Cookie> {
    let o = v.as_object()?;
    Some(Cookie {
        name: o.get("name")?.as_str()?.to_string(),
        value: o.get("value")?.as_str()?.to_string(),
        domain: o
            .get("domain")
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string(),
        path: o
            .get("path")
            .and_then(Value::as_str)
            .unwrap_or("/")
            .to_string(),
        expires: o.get("expires").and_then(Value::as_f64).unwrap_or(-1.0),
        secure: o.get("secure").and_then(Value::as_bool).unwrap_or(false),
        http_only: o.get("httpOnly").and_then(Value::as_bool).unwrap_or(false),
    })
}
