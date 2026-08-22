//! Browser session lifecycle. Spawns Chromium, attaches via CDP, and
//! exposes the small surface the agent loop actually needs:
//! `start`, `navigate`, `screenshot`, `dom_snapshot`, `click_index`,
//! `type_index`, `scroll`, plus tab management.
//!
//! Multi-tab model: there's always one active tab. Operations that act on
//! the page (navigate, click, snapshot, ...) target the active tab. Use
//! `switch_tab` to change which tab is active. New tabs created via
//! `new_tab` (or page-driven `window.open` followed by `list_tabs` +
//! `switch_tab`) attach lazily — we hold one CDP session per tab in the
//! `attached` map.

use std::collections::HashMap;
use std::path::PathBuf;
use std::process::Stdio;
use std::sync::Arc;
use std::time::Duration;

use base64::engine::general_purpose::STANDARD;
use base64::Engine;
use bu_cdp::{CdpError, Connection};
use bu_dom::{DomElement, DomState};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::broadcast::error::RecvError;
use tokio::sync::Mutex;

mod events;
mod types;
mod url_match;

pub use types::{
    BrowserError, Cookie, DownloadInfo, LaunchOptions, Result, TabInfo,
};

use events::track_browser_events;
use types::{parse_cookie, ActiveTab, DownloadState};
use url_match::{is_new_tab_url, match_url_with_domain_pattern};


pub struct BrowserSession {
    child: Option<Child>,
    conn: Connection,
    user_data_dir: Option<PathBuf>,
    viewport: Option<(u32, u32)>,
    /// Navigation policy. Empty allowed_domains means no restriction.
    /// Both lists use the same domain-pattern syntax as browser_use.
    allowed_domains: Vec<String>,
    prohibited_domains: Vec<String>,
    /// True when we attached to an existing CDP endpoint (cdp_url=...).
    /// stop() must NOT call Browser.close for remote-owned browsers — the
    /// cloud provider manages the lifecycle and would error or leak state.
    attached_only: bool,
    active: Mutex<ActiveTab>,
    /// target_id -> session_id for tabs we've attached.
    attached: Mutex<HashMap<String, String>>,
    last_snapshot: Mutex<Option<DomState>>,
    /// guid -> latest state, populated by a background event task.
    downloads: Arc<Mutex<HashMap<String, DownloadState>>>,
    download_dir: PathBuf,
    /// Timestamp of the most recent main-frame navigation (Page.frameNavigated
    /// or loadEventFired). wait_for_navigation uses this to detect "a
    /// navigation just happened" without depending on a known prior URL —
    /// click() returns after the navigation has already started, so URL
    /// polling alone misses these.
    last_navigation: Arc<Mutex<Option<tokio::time::Instant>>>,
    /// Owned only to keep the event task alive; not joined explicitly —
    /// the task exits when the broadcast channel closes (i.e. Connection
    /// is dropped).
    _event_task: tokio::task::JoinHandle<()>,
}

impl BrowserSession {
    pub async fn start() -> Result<Self> {
        Self::launch(LaunchOptions::default()).await
    }

    pub async fn launch(opts: LaunchOptions) -> Result<Self> {
        if let Some(url) = opts.cdp_url.clone() {
            return Self::attach(url, opts).await;
        }
        let chrome = opts
            .chrome_path
            .clone()
            .or_else(find_chrome)
            .ok_or(BrowserError::ChromeNotFound)?;

        let user_data_dir = match opts.user_data_dir.clone() {
            Some(p) => p,
            None => {
                let p = std::env::temp_dir().join(format!(
                    "bu-rs-{}-{}",
                    std::process::id(),
                    rand_suffix()
                ));
                std::fs::create_dir_all(&p)?;
                p
            }
        };

        let mut cmd = Command::new(&chrome);
        cmd.arg("--remote-debugging-port=0")
            .arg("--no-first-run")
            .arg("--no-default-browser-check")
            .arg("--disable-dev-shm-usage")
            .arg("--disable-background-timer-throttling")
            .arg("--disable-renderer-backgrounding")
            .arg("--disable-backgrounding-occluded-windows")
            .arg(format!("--user-data-dir={}", user_data_dir.display()));
        if opts.headless {
            cmd.arg("--headless=new");
        }
        if let Some((w, h)) = opts.viewport {
            cmd.arg(format!("--window-size={w},{h}"));
        }
        if opts.stealth {
            // Reduce headless-detection signals. Doesn't make automation
            // undetectable, but flips the most-checked CDP flags.
            cmd.arg("--disable-blink-features=AutomationControlled")
                .arg("--exclude-switches=enable-automation")
                .arg("--disable-features=IsolateOrigins,site-per-process,AutomationControlled");
        }
        for a in &opts.extra_args {
            cmd.arg(a);
        }
        cmd.stderr(Stdio::piped())
            .stdout(Stdio::null())
            .stdin(Stdio::null());

        let mut child = cmd.spawn()?;
        let stderr = child
            .stderr
            .take()
            .ok_or_else(|| std::io::Error::other("chrome stderr unavailable"))?;
        let mut reader = BufReader::new(stderr).lines();

        let ws_url = tokio::time::timeout(Duration::from_secs(15), async {
            while let Ok(Some(line)) = reader.next_line().await {
                if let Some(idx) = line.find("ws://") {
                    return Some(line[idx..].trim().to_string());
                }
            }
            None
        })
        .await
        .ok()
        .flatten()
        .ok_or(BrowserError::NoDevToolsUrl)?;

        tokio::spawn(async move {
            while let Ok(Some(_)) = reader.next_line().await {}
        });

        let conn = Connection::connect(&ws_url).await?;

        // Configure where downloads land + start the background tracker
        // BEFORE creating the first tab so we don't miss early events.
        let download_dir = user_data_dir.join("downloads");
        std::fs::create_dir_all(&download_dir)?;
        let downloads: Arc<Mutex<HashMap<String, DownloadState>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let last_navigation: Arc<Mutex<Option<tokio::time::Instant>>> =
            Arc::new(Mutex::new(None));
        let task_events = conn.events();
        let task_downloads = downloads.clone();
        let task_last_nav = last_navigation.clone();
        let event_task =
            tokio::spawn(track_browser_events(task_events, task_downloads, task_last_nav));

        conn.send(
            "Browser.setDownloadBehavior",
            json!({
                "behavior": "allow",
                "downloadPath": download_dir.to_string_lossy(),
                "eventsEnabled": true,
            }),
            None,
        )
        .await?;

        // Create + attach the initial tab.
        let (target_id, session_id) =
            Self::create_and_attach(&conn, "about:blank", opts.viewport).await?;

        let mut attached = HashMap::new();
        attached.insert(target_id.clone(), session_id.clone());

        Ok(Self {
            child: Some(child),
            conn,
            user_data_dir: Some(user_data_dir),
            viewport: opts.viewport,
            allowed_domains: opts.allowed_domains.clone(),
            prohibited_domains: opts.prohibited_domains.clone(),
            attached_only: false,
            active: Mutex::new(ActiveTab {
                target_id,
                session_id,
            }),
            attached: Mutex::new(attached),
            last_snapshot: Mutex::new(None),
            downloads,
            download_dir,
            last_navigation,
            _event_task: event_task,
        })
    }

    /// Connect to an already-running browser via its CDP WebSocket. The
    /// remote owner manages the browser process; stop() detaches but does
    /// not call Browser.close. Used for cloud browser providers (Anchor,
    /// Browserbase, Hyperbrowser, Daytona, BrightData), local long-lived
    /// Chromium instances, or test fixtures.
    ///
    /// `cdp_url` must be the WebSocket URL (`ws://...` or `wss://...`).
    /// Pre-resolve `http://host:port/json/version` if your provider only
    /// gives an HTTP endpoint.
    async fn attach(cdp_url: String, opts: LaunchOptions) -> Result<Self> {
        let conn = Connection::connect(&cdp_url).await?;

        // Tracking state: same shape as the spawn path. download_dir is a
        // local temp path used for cdp event bookkeeping only — actual
        // bytes land wherever the remote browser was configured to write.
        let download_dir = std::env::temp_dir().join(format!(
            "bu-rs-attached-{}-{}",
            std::process::id(),
            rand_suffix()
        ));
        let _ = std::fs::create_dir_all(&download_dir);
        let downloads: Arc<Mutex<HashMap<String, DownloadState>>> =
            Arc::new(Mutex::new(HashMap::new()));
        let last_navigation: Arc<Mutex<Option<tokio::time::Instant>>> =
            Arc::new(Mutex::new(None));
        let task_events = conn.events();
        let task_downloads = downloads.clone();
        let task_last_nav = last_navigation.clone();
        let event_task =
            tokio::spawn(track_browser_events(task_events, task_downloads, task_last_nav));

        // Best-effort: ask remote for download events. Some providers
        // reject Browser.setDownloadBehavior; ignore if so.
        let _ = conn
            .send(
                "Browser.setDownloadBehavior",
                json!({
                    "behavior": "allowAndName",
                    "eventsEnabled": true,
                }),
                None,
            )
            .await;

        // Find an existing top-level page to attach to. If none, create one.
        let (target_id, session_id) = match Self::find_existing_page(&conn).await? {
            Some(tid) => {
                let attach = conn
                    .send(
                        "Target.attachToTarget",
                        json!({ "targetId": tid, "flatten": true }),
                        None,
                    )
                    .await?;
                let sid = attach
                    .get("sessionId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| BrowserError::BadResponse {
                        method: "Target.attachToTarget",
                        detail: "missing sessionId".into(),
                    })?
                    .to_string();
                let _ = conn.send("Page.enable", json!({}), Some(&sid)).await;
                let _ = conn.send("DOM.enable", json!({}), Some(&sid)).await;
                if let Some((w, h)) = opts.viewport {
                    let _ = conn
                        .send(
                            "Emulation.setDeviceMetricsOverride",
                            json!({
                                "width": w,
                                "height": h,
                                "deviceScaleFactor": 0,
                                "mobile": false,
                            }),
                            Some(&sid),
                        )
                        .await;
                }
                (tid, sid)
            }
            None => Self::create_and_attach(&conn, "about:blank", opts.viewport).await?,
        };

        let mut attached = HashMap::new();
        attached.insert(target_id.clone(), session_id.clone());

        Ok(Self {
            child: None,
            conn,
            user_data_dir: None,
            viewport: opts.viewport,
            allowed_domains: opts.allowed_domains.clone(),
            prohibited_domains: opts.prohibited_domains.clone(),
            attached_only: true,
            active: Mutex::new(ActiveTab {
                target_id,
                session_id,
            }),
            attached: Mutex::new(attached),
            last_snapshot: Mutex::new(None),
            downloads,
            download_dir,
            last_navigation,
            _event_task: event_task,
        })
    }

    /// Find a usable existing page target, if any. Filters out workers,
    /// the browser target, and detached/empty entries.
    async fn find_existing_page(conn: &Connection) -> Result<Option<String>> {
        let r = conn.send("Target.getTargets", json!({}), None).await?;
        let Some(arr) = r.get("targetInfos").and_then(Value::as_array) else {
            return Ok(None);
        };
        for ti in arr {
            let ttype = ti.get("type").and_then(Value::as_str).unwrap_or("");
            if ttype != "page" {
                continue;
            }
            let attached = ti.get("attached").and_then(Value::as_bool).unwrap_or(false);
            // Prefer unattached pages so we don't fight with another client.
            if attached {
                continue;
            }
            if let Some(tid) = ti.get("targetId").and_then(Value::as_str) {
                return Ok(Some(tid.to_string()));
            }
        }
        // Fallback: any page, even attached.
        for ti in arr {
            let ttype = ti.get("type").and_then(Value::as_str).unwrap_or("");
            if ttype != "page" {
                continue;
            }
            if let Some(tid) = ti.get("targetId").and_then(Value::as_str) {
                return Ok(Some(tid.to_string()));
            }
        }
        Ok(None)
    }

    pub fn download_dir(&self) -> &std::path::Path {
        &self.download_dir
    }

    // ---------- cookies ----------

    /// All cookies across all domains. Network domain commands need a page
    /// session to dispatch — we use the active tab's session.
    pub async fn get_cookies(&self) -> Result<Vec<Cookie>> {
        let sid = self.session_id().await;
        let r = self
            .conn
            .send("Network.getAllCookies", json!({}), Some(&sid))
            .await?;
        let arr = r
            .get("cookies")
            .and_then(Value::as_array)
            .cloned()
            .unwrap_or_default();
        Ok(arr.into_iter().filter_map(parse_cookie).collect())
    }

    /// Set or replace a cookie. `expires < 0` makes it a session cookie.
    pub async fn set_cookie(&self, cookie: &Cookie) -> Result<()> {
        let sid = self.session_id().await;
        let mut params = json!({
            "name": cookie.name,
            "value": cookie.value,
            "domain": cookie.domain,
            "path": cookie.path,
            "secure": cookie.secure,
            "httpOnly": cookie.http_only,
        });
        if cookie.expires >= 0.0 {
            params["expires"] = json!(cookie.expires);
        }
        self.conn
            .send("Network.setCookie", params, Some(&sid))
            .await?;
        Ok(())
    }

    /// Delete a cookie by name. Pass domain/path to scope, or None to
    /// delete from any matching cookie store (CDP defaults to current doc).
    pub async fn delete_cookie(
        &self,
        name: &str,
        domain: Option<&str>,
        path: Option<&str>,
    ) -> Result<()> {
        let sid = self.session_id().await;
        let mut params = json!({ "name": name });
        if let Some(d) = domain {
            params["domain"] = json!(d);
        }
        if let Some(p) = path {
            params["path"] = json!(p);
        }
        self.conn
            .send("Network.deleteCookies", params, Some(&sid))
            .await?;
        Ok(())
    }

    /// Clear ALL browser cookies.
    pub async fn clear_cookies(&self) -> Result<()> {
        let sid = self.session_id().await;
        self.conn
            .send("Network.clearBrowserCookies", json!({}), Some(&sid))
            .await?;
        Ok(())
    }

    /// Snapshot the current downloads tracked by this session. State is
    /// "inProgress" | "completed" | "canceled".
    pub async fn list_downloads(&self) -> Vec<DownloadInfo> {
        let map = self.downloads.lock().await;
        map.iter()
            .map(|(guid, s)| DownloadInfo {
                guid: guid.clone(),
                suggested_filename: s.suggested_filename.clone(),
                url: s.url.clone(),
                state: s.state.clone(),
                received_bytes: s.received_bytes,
                total_bytes: s.total_bytes,
                file_path: self.download_dir.join(guid),
            })
            .collect()
    }

    /// Create a new target with the given initial URL and attach to it
    /// (flat session model). Enables Page + DOM domains. Returns
    /// (target_id, session_id).
    async fn create_and_attach(
        conn: &Connection,
        url: &str,
        viewport: Option<(u32, u32)>,
    ) -> Result<(String, String)> {
        let target = conn
            .send("Target.createTarget", json!({ "url": url }), None)
            .await?;
        let target_id = target
            .get("targetId")
            .and_then(Value::as_str)
            .ok_or_else(|| BrowserError::BadResponse {
                method: "Target.createTarget",
                detail: "missing targetId".into(),
            })?
            .to_string();

        let attach = conn
            .send(
                "Target.attachToTarget",
                json!({ "targetId": target_id, "flatten": true }),
                None,
            )
            .await?;
        let session_id = attach
            .get("sessionId")
            .and_then(Value::as_str)
            .ok_or_else(|| BrowserError::BadResponse {
                method: "Target.attachToTarget",
                detail: "missing sessionId".into(),
            })?
            .to_string();

        conn.send("Page.enable", json!({}), Some(&session_id))
            .await?;
        conn.send("DOM.enable", json!({}), Some(&session_id))
            .await?;

        if let Some((w, h)) = viewport {
            conn.send(
                "Emulation.setDeviceMetricsOverride",
                json!({
                    "width": w,
                    "height": h,
                    "deviceScaleFactor": 0,
                    "mobile": false,
                }),
                Some(&session_id),
            )
            .await?;
        }

        Ok((target_id, session_id))
    }

    /// Returns the active tab's CDP session id (cloned). Use this wherever
    /// you'd otherwise pass `Some(&session_id)` to `conn.send`.
    async fn session_id(&self) -> String {
        self.active.lock().await.session_id.clone()
    }

    /// Returns the active tab's target id.
    pub async fn active_tab_target_id(&self) -> String {
        self.active.lock().await.target_id.clone()
    }

    /// Enforce the navigation policy for a candidate URL.
    ///
    /// - `prohibited_domains` always blocks (checked first).
    /// - If `allowed_domains` is non-empty, the URL must match one of them.
    /// - Empty allowed_domains = no allow-list restriction.
    /// - `about:blank` and other new-tab URLs are always permitted.
    fn check_navigation_allowed(&self, url: &str) -> Result<()> {
        if is_new_tab_url(url) {
            return Ok(());
        }
        for pattern in &self.prohibited_domains {
            if match_url_with_domain_pattern(url, pattern) {
                return Err(BrowserError::NavigationBlocked {
                    url: url.to_string(),
                });
            }
        }
        if !self.allowed_domains.is_empty() {
            let allowed = self
                .allowed_domains
                .iter()
                .any(|p| match_url_with_domain_pattern(url, p));
            if !allowed {
                return Err(BrowserError::NavigationBlocked {
                    url: url.to_string(),
                });
            }
        }
        Ok(())
    }

    // ---------- tab management ----------

    /// List all attachable browseable targets — pages (tabs/windows) and
    /// cross-origin iframes (out-of-process frames). The agent can switch
    /// to any of them via switch_tab. Workers and the browser target itself
    /// are filtered out. is_active marks the target that operations target.
    pub async fn list_tabs(&self) -> Result<Vec<TabInfo>> {
        let r = self
            .conn
            .send("Target.getTargets", json!({}), None)
            .await?;
        let active_target = self.active.lock().await.target_id.clone();
        let mut out = Vec::new();
        if let Some(arr) = r.get("targetInfos").and_then(Value::as_array) {
            for ti in arr {
                let ttype = ti
                    .get("type")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                // "page" = top-level tab; "iframe" = OOP frame. Other types
                // (service_worker, shared_worker, browser, other) are not
                // useful for our control surface.
                if ttype != "page" && ttype != "iframe" {
                    continue;
                }
                let target_id = ti
                    .get("targetId")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                if target_id.is_empty() {
                    continue;
                }
                out.push(TabInfo {
                    is_active: target_id == active_target,
                    target_id,
                    url: ti
                        .get("url")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string(),
                    title: ti
                        .get("title")
                        .and_then(Value::as_str)
                        .unwrap_or("")
                        .to_string(),
                    target_type: ttype,
                });
            }
        }
        Ok(out)
    }

    /// Switch the active tab. Attaches to the target if not already attached.
    /// Clears the cached snapshot so the next dom_snapshot reflects the new
    /// tab's DOM.
    pub async fn switch_tab(&self, target_id: &str) -> Result<()> {
        // Already active? No-op.
        if self.active.lock().await.target_id == target_id {
            return Ok(());
        }

        // Attach if needed.
        let session_id = {
            let mut attached = self.attached.lock().await;
            if let Some(sid) = attached.get(target_id) {
                sid.clone()
            } else {
                // Verify the target exists before trying to attach — gives
                // a clean error rather than a CDP protocol error.
                let exists = self
                    .list_tabs()
                    .await?
                    .into_iter()
                    .any(|t| t.target_id == target_id);
                if !exists {
                    return Err(BrowserError::UnknownTab(target_id.to_string()));
                }
                let attach = self
                    .conn
                    .send(
                        "Target.attachToTarget",
                        json!({ "targetId": target_id, "flatten": true }),
                        None,
                    )
                    .await?;
                let sid = attach
                    .get("sessionId")
                    .and_then(Value::as_str)
                    .ok_or_else(|| BrowserError::BadResponse {
                        method: "Target.attachToTarget",
                        detail: "missing sessionId".into(),
                    })?
                    .to_string();
                self.conn
                    .send("Page.enable", json!({}), Some(&sid))
                    .await?;
                self.conn
                    .send("DOM.enable", json!({}), Some(&sid))
                    .await?;
                if let Some((w, h)) = self.viewport {
                    // Emulation.setDeviceMetricsOverride is top-level only;
                    // OOP iframe targets reject it. Best-effort: ignore the
                    // error so we can still attach to and operate on iframes.
                    let _ = self
                        .conn
                        .send(
                            "Emulation.setDeviceMetricsOverride",
                            json!({
                                "width": w,
                                "height": h,
                                "deviceScaleFactor": 0,
                                "mobile": false,
                            }),
                            Some(&sid),
                        )
                        .await;
                }
                attached.insert(target_id.to_string(), sid.clone());
                sid
            }
        };

        *self.active.lock().await = ActiveTab {
            target_id: target_id.to_string(),
            session_id,
        };
        // Snapshot is per-tab; invalidate.
        *self.last_snapshot.lock().await = None;
        Ok(())
    }

    /// Open a new tab, attach to it, make it active. If `url` is non-empty,
    /// the new tab is navigated to it via the same load-event-driven path
    /// as `navigate`, so `current_url` reflects the requested URL on
    /// return rather than `about:blank`.
    pub async fn new_tab(&self, url: &str) -> Result<TabInfo> {
        // Enforce navigation policy BEFORE we burn a tab on a blocked URL.
        if !url.is_empty() {
            self.check_navigation_allowed(url)?;
        }
        let (target_id, session_id) =
            Self::create_and_attach(&self.conn, "about:blank", self.viewport).await?;
        self.attached
            .lock()
            .await
            .insert(target_id.clone(), session_id.clone());
        *self.active.lock().await = ActiveTab {
            target_id: target_id.clone(),
            session_id,
        };
        *self.last_snapshot.lock().await = None;

        if !url.is_empty() {
            self.navigate(url).await?;
        }

        Ok(TabInfo {
            target_id,
            url: if url.is_empty() {
                "about:blank".into()
            } else {
                url.into()
            },
            title: String::new(),
            target_type: "page".into(),
            is_active: true,
        })
    }

    /// Close a tab. If it was the active tab, switch to whichever other
    /// tab the browser still has open. Errors if it would close the last
    /// page — call stop() instead.
    pub async fn close_tab(&self, target_id: &str) -> Result<()> {
        let tabs = self.list_tabs().await?;
        if tabs.len() <= 1 {
            return Err(BrowserError::LastTab);
        }
        if !tabs.iter().any(|t| t.target_id == target_id) {
            return Err(BrowserError::UnknownTab(target_id.to_string()));
        }

        self.conn
            .send(
                "Target.closeTarget",
                json!({ "targetId": target_id }),
                None,
            )
            .await?;
        self.attached.lock().await.remove(target_id);

        let was_active = self.active.lock().await.target_id == target_id;
        if was_active {
            // Pick any remaining tab and switch.
            let next = tabs
                .into_iter()
                .find(|t| t.target_id != target_id)
                .ok_or(BrowserError::LastTab)?;
            self.switch_tab(&next.target_id).await?;
        }
        Ok(())
    }

    // ---------- per-page operations ----------

    /// Navigate and wait for `Page.loadEventFired` matching the active
    /// tab's session. Subscribes before sending Page.navigate to avoid a
    /// race. Returns Ok on the 30s timeout too — pages that legitimately
    /// never fire load (infinite SPAs) are still usable.
    ///
    /// Enforces the navigation policy (allowed_domains / prohibited_domains)
    /// configured on this session — returns NavigationBlocked without
    /// touching the browser if the URL isn't permitted.
    /// Navigate and wait for the NEW document to load. Returns `true` when
    /// readiness was confirmed, `false` when the wait timed out (the page
    /// may still be loading — callers should say so instead of claiming
    /// "loaded").
    ///
    /// v0.12.36: readiness is matched on the `loaderId` returned by
    /// Page.navigate via Page.lifecycleEvent (`load` or `networkIdle`),
    /// mirroring browser-use's session.py. Before this, a late
    /// loadEventFired from the PREVIOUS document could unblock the wait and
    /// the next snapshot captured the stale page. Cap 10s (was 30s — SPAs
    /// whose load event never fires stalled every navigate for 30s).
    pub async fn navigate(&self, url: &str) -> Result<bool> {
        self.check_navigation_allowed(url)?;
        let sid = self.session_id().await;
        // Idempotent; needed for Page.lifecycleEvent to be emitted.
        let _ = self
            .conn
            .send(
                "Page.setLifecycleEventsEnabled",
                json!({ "enabled": true }),
                Some(&sid),
            )
            .await;
        let mut events = self.conn.events();

        let resp = self
            .conn
            .send("Page.navigate", json!({ "url": url }), Some(&sid))
            .await?;
        if let Some(err) = resp.get("errorText").and_then(Value::as_str) {
            // ERR_ABORTED = navigation superseded (redirect chain, download
            // trigger) — not a failure of the request itself.
            if !err.contains("ERR_ABORTED") {
                return Err(BrowserError::BadResponse {
                    method: "navigate",
                    detail: format!("{url}: {err}"),
                });
            }
        }
        let loader_id = resp
            .get("loaderId")
            .and_then(Value::as_str)
            .map(str::to_owned);

        let target = sid.clone();
        let wait = async move {
            loop {
                match events.recv().await {
                    Ok(event) => {
                        if event.session_id.as_deref() != Some(&target) {
                            continue;
                        }
                        if event.method == "Page.lifecycleEvent" {
                            let name = event
                                .params
                                .get("name")
                                .and_then(Value::as_str)
                                .unwrap_or("");
                            let lid = event
                                .params
                                .get("loaderId")
                                .and_then(Value::as_str);
                            let same_loader = match (&loader_id, lid) {
                                (Some(want), Some(got)) => want == got,
                                (None, _) => true,
                                (Some(_), None) => false,
                            };
                            if same_loader && (name == "load" || name == "networkIdle") {
                                return Ok::<(), BrowserError>(());
                            }
                        } else if loader_id.is_none() && event.method == "Page.loadEventFired" {
                            // No loaderId to match on (older Chrome) — fall
                            // back to the session-scoped load event.
                            return Ok::<(), BrowserError>(());
                        }
                    }
                    Err(RecvError::Lagged(_)) => continue,
                    Err(RecvError::Closed) => {
                        return Err(BrowserError::Cdp(CdpError::Closed));
                    }
                }
            }
        };

        match tokio::time::timeout(Duration::from_secs(10), wait).await {
            Ok(Ok(())) => Ok(true),
            Ok(Err(e)) => Err(e),
            Err(_) => Ok(false),
        }
    }

    /// Render the active page to PDF bytes. Headless-only — headful mode's
    /// printToPDF requires extra Chrome flags we don't set.
    pub async fn pdf(&self) -> Result<Vec<u8>> {
        let sid = self.session_id().await;
        let r = self
            .conn
            .send("Page.printToPDF", json!({}), Some(&sid))
            .await?;
        let b64 = r
            .get("data")
            .and_then(Value::as_str)
            .ok_or_else(|| BrowserError::BadResponse {
                method: "Page.printToPDF",
                detail: "missing data".into(),
            })?;
        Ok(STANDARD.decode(b64)?)
    }

    pub async fn screenshot(&self) -> Result<Vec<u8>> {
        self.screenshot_with_format("png", None).await
    }

    pub async fn screenshot_jpeg(&self, quality: u8) -> Result<Vec<u8>> {
        self.screenshot_with_format("jpeg", Some(quality.clamp(1, 100))).await
    }

    async fn screenshot_with_format(
        &self,
        format: &str,
        quality: Option<u8>,
    ) -> Result<Vec<u8>> {
        let sid = self.session_id().await;
        let mut params = json!({ "format": format });
        if format == "jpeg" {
            if let Some(quality) = quality {
                params["quality"] = json!(quality);
            }
        }
        let r = self
            .conn
            .send(
                "Page.captureScreenshot",
                params,
                Some(&sid),
            )
            .await?;
        let b64 = r
            .get("data")
            .and_then(Value::as_str)
            .ok_or_else(|| BrowserError::BadResponse {
                method: "Page.captureScreenshot",
                detail: "missing data".into(),
            })?;
        Ok(STANDARD.decode(b64)?)
    }

    pub async fn current_url(&self) -> Result<String> {
        let sid = self.session_id().await;
        let r = self
            .conn
            .send(
                "Runtime.evaluate",
                json!({
                    "expression": "window.location.href",
                    "returnByValue": true,
                }),
                Some(&sid),
            )
            .await?;
        Ok(r.get("result")
            .and_then(|x| x.get("value"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string())
    }

    pub async fn dom_snapshot(&self) -> Result<DomState> {
        let sid = self.session_id().await;
        let snap = bu_dom::snapshot(&self.conn, &sid).await?;
        *self.last_snapshot.lock().await = Some(snap.clone());
        Ok(snap)
    }

    async fn lookup(&self, index: u32) -> Result<DomElement> {
        let guard = self.last_snapshot.lock().await;
        let snap = guard.as_ref().ok_or(BrowserError::NoSnapshot)?;
        Ok(snap.get(index)?.clone())
    }

    /// Find the element by its `data-bu-idx` attribute, scroll it into the
    /// viewport, and return its current center in the top window's
    /// coordinate space. Walks into same-origin iframes if necessary.
    /// None if the element no longer exists.
    async fn fresh_center(&self, index: u32) -> Result<Option<(f64, f64, bool)>> {
        let _ = self.lookup(index).await?;
        let sid = self.session_id().await;
        let script = format!(
            r#"(() => {{
                {finder}
                const el = findByIdx(document);
                if (!el) return null;
                el.scrollIntoView({{block: 'center', behavior: 'instant'}});
                const r = el.getBoundingClientRect();
                let x = r.left, y = r.top;
                let win = el.ownerDocument.defaultView;
                while (win && win !== window) {{
                    const fr = win.frameElement;
                    if (!fr) break;
                    const frR = fr.getBoundingClientRect();
                    x += frR.left;
                    y += frR.top;
                    win = win.parent;
                }}
                // v0.12.36: occlusion check in the element's own document
                // (sticky headers / overlays after scrollIntoView). Mirrors
                // python's default_action_watchdog fallback.
                let occluded = false;
                try {{
                    const hit = el.ownerDocument.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
                    if (hit && hit !== el && !el.contains(hit) && !hit.contains(el)) {{
                        // v0.12.37: only TRUE overlays count — fixed/sticky
                        // surfaces and dialogs (modals, cookie banners,
                        // sticky headers). Stretched-link siblings and
                        // transparent hit-targets are legitimate click
                        // receivers, and a synthetic click on the element
                        // beneath them loses user activation (popup-blocked
                        // target=_blank, ignored isTrusted checks). The
                        // v0.12.36 rule flagged those too and printed 76.3.
                        let n = hit;
                        for (let d = 0; n && d < 4; d++, n = n.parentElement) {{
                            const cs = n.ownerDocument.defaultView.getComputedStyle(n);
                            if (cs.position === 'fixed' || cs.position === 'sticky'
                                || n.tagName === 'DIALOG' || n.getAttribute('role') === 'dialog') {{
                                occluded = true; break;
                            }}
                        }}
                    }}
                }} catch (e) {{}}
                return {{ x: x + r.width / 2, y: y + r.height / 2, occluded }};
            }})()"#,
            finder = find_by_idx_js(index)
        );
        let r = self
            .conn
            .send(
                "Runtime.evaluate",
                json!({ "expression": script, "returnByValue": true }),
                Some(&sid),
            )
            .await?;
        let val = r.get("result").and_then(|x| x.get("value"));
        match val {
            None | Some(&Value::Null) => Ok(None),
            Some(Value::Object(o)) => {
                let x = o.get("x").and_then(Value::as_f64).unwrap_or(0.0);
                let y = o.get("y").and_then(Value::as_f64).unwrap_or(0.0);
                let occluded = o.get("occluded").and_then(Value::as_bool).unwrap_or(false);
                Ok(Some((x, y, occluded)))
            }
            _ => Ok(None),
        }
    }

    /// JS `element.click()` on the indexed element — the fallback when the
    /// trusted coordinate click would land on an overlay instead. v0.12.36.
    async fn js_click_index(&self, index: u32) -> Result<()> {
        let sid = self.session_id().await;
        let script = format!(
            r#"(() => {{ {finder} const el = findByIdx(document); if (!el) return false; el.click(); return true; }})()"#,
            finder = find_by_idx_js(index)
        );
        let r = self
            .conn
            .send(
                "Runtime.evaluate",
                json!({ "expression": script, "returnByValue": true }),
                Some(&sid),
            )
            .await?;
        let ok = r
            .get("result")
            .and_then(|x| x.get("value"))
            .and_then(Value::as_bool)
            .unwrap_or(false);
        if ok {
            Ok(())
        } else {
            Err(BrowserError::ElementGone(index))
        }
    }

    pub async fn click_index(&self, index: u32) -> Result<()> {
        let (cx, cy, occluded) = self
            .fresh_center(index)
            .await?
            .ok_or(BrowserError::ElementGone(index))?;
        if occluded {
            // Trusted path would hit the overlay; element.click() reaches
            // the element's own listeners regardless of stacking.
            return self.js_click_index(index).await;
        }
        self.dispatch_click(cx, cy).await
    }

    /// v0.12.35: trusted CDP click at viewport coordinates. Public so the
    /// `activate_control` tool can click candidates that bu-dom never
    /// indexed (bare `<li>` chips with delegated listeners and the like)
    /// after resolving their center in JS.
    pub async fn click_at(&self, x: f64, y: f64) -> Result<()> {
        self.dispatch_click(x, y).await
    }

    async fn dispatch_click(&self, x: f64, y: f64) -> Result<()> {
        let sid = self.session_id().await;
        self.conn
            .send(
                "Input.dispatchMouseEvent",
                json!({
                    "type": "mouseMoved",
                    "x": x, "y": y,
                }),
                Some(&sid),
            )
            .await?;
        self.conn
            .send(
                "Input.dispatchMouseEvent",
                json!({
                    "type": "mousePressed",
                    "x": x, "y": y,
                    "button": "left",
                    "clickCount": 1,
                }),
                Some(&sid),
            )
            .await?;
        self.conn
            .send(
                "Input.dispatchMouseEvent",
                json!({
                    "type": "mouseReleased",
                    "x": x, "y": y,
                    "button": "left",
                    "clickCount": 1,
                }),
                Some(&sid),
            )
            .await?;
        Ok(())
    }

    /// Set files on an `<input type="file">` element identified by its
    /// data-bu-idx. Paths must be absolute. Same-frame only — the element
    /// must live in the active page's main document.
    pub async fn upload_file(&self, index: u32, paths: &[String]) -> Result<()> {
        let _ = self.lookup(index).await?;
        let sid = self.session_id().await;
        let script = format!(
            r#"(() => {{
                const el = document.querySelector('[data-bu-idx="{index}"]');
                if (!el) return null;
                if (el.tagName !== 'INPUT' || (el.type || '').toLowerCase() !== 'file') return 'NOT_FILE_INPUT';
                return el;
            }})()"#
        );
        let r = self
            .conn
            .send(
                "Runtime.evaluate",
                json!({ "expression": script, "returnByValue": false }),
                Some(&sid),
            )
            .await?;
        let res = r.get("result");
        // Reject the not-a-file-input sentinel before trying to bind objectId.
        if res.and_then(|x| x.get("value")).and_then(Value::as_str) == Some("NOT_FILE_INPUT") {
            return Err(BrowserError::BadResponse {
                method: "upload_file",
                detail: format!("element [{index}] is not <input type=\"file\">"),
            });
        }
        let object_id = res
            .and_then(|x| x.get("objectId"))
            .and_then(Value::as_str)
            .ok_or(BrowserError::ElementGone(index))?;
        self.conn
            .send(
                "DOM.setFileInputFiles",
                json!({
                    "files": paths,
                    "objectId": object_id,
                }),
                Some(&sid),
            )
            .await?;
        // Release the JS reference we held.
        let _ = self
            .conn
            .send(
                "Runtime.releaseObject",
                json!({ "objectId": object_id }),
                Some(&sid),
            )
            .await;
        Ok(())
    }

    pub async fn type_index(&self, index: u32, text: &str) -> Result<()> {
        // Date-family inputs ignore trusted `Input.insertText` entirely
        // (Chrome routes it to the segmented date editor, which drops
        // free text). Set the value through the native property setter
        // and fire input/change so frameworks (React et al.) observe
        // the update. v0.12.21 — the shadow-DOM/date challenge failed
        // exactly here: "the date field remained unfilled".
        let probe = format!(
            "(() => {{ {finder} const el = findByIdx(document); \
             return el && el.tagName === 'INPUT' ? (el.type || '').toLowerCase() : ''; }})()",
            finder = find_by_idx_js(index)
        );
        let input_type = self.evaluate(&probe).await.unwrap_or_default();
        if matches!(
            input_type.as_str(),
            "date" | "time" | "month" | "week" | "datetime-local"
        ) {
            let value_json =
                serde_json::to_string(text).unwrap_or_else(|_| "\"\"".to_string());
            let script = format!(
                "(() => {{ {finder} const el = findByIdx(document); \
                 if (!el) return 'GONE'; \
                 el.focus(); \
                 const desc = Object.getOwnPropertyDescriptor(\
                     Object.getPrototypeOf(el), 'value') \
                     || Object.getOwnPropertyDescriptor(\
                        HTMLInputElement.prototype, 'value'); \
                 if (desc && desc.set) {{ desc.set.call(el, {value}); }} \
                 else {{ el.value = {value}; }} \
                 el.dispatchEvent(new Event('input', {{bubbles: true}})); \
                 el.dispatchEvent(new Event('change', {{bubbles: true}})); \
                 return 'OK'; }})()",
                finder = find_by_idx_js(index),
                value = value_json
            );
            let out = self.evaluate(&script).await?;
            if out.contains("GONE") {
                return Err(BrowserError::ElementGone(index));
            }
            return Ok(());
        }
        self.click_index(index).await?;
        tokio::time::sleep(Duration::from_millis(50)).await;
        let sid = self.session_id().await;
        self.conn
            .send(
                "Input.insertText",
                json!({ "text": text }),
                Some(&sid),
            )
            .await?;
        Ok(())
    }

    /// Dispatch a real CDP keyboard event for a named special key.
    ///
    /// v0.8.19. The previous `send_keys` Python tool dispatched JS
    /// `KeyboardEvent` objects via `evaluate()`; those events are
    /// "untrusted" per the WHATWG spec and do NOT trigger default
    /// browser behavior — Enter does not submit forms, Tab does not
    /// move focus, etc. Many "search-then-Enter-to-submit" eval flows
    /// were silently no-oping. CDP `Input.dispatchKeyEvent` issues
    /// "trusted" events that fire defaults correctly.
    ///
    /// Supported keys (case-insensitive on the Python side, but pass
    /// them in the canonical form here):
    ///   Enter Tab Escape Backspace Delete Space
    ///   ArrowUp ArrowDown ArrowLeft ArrowRight
    ///   Home End PageUp PageDown
    pub async fn dispatch_key(&self, key_name: &str) -> Result<()> {
        let (key, code, vk) = match key_name {
            "Enter" => ("Enter", "Enter", 13u32),
            "Tab" => ("Tab", "Tab", 9),
            "Escape" => ("Escape", "Escape", 27),
            "Backspace" => ("Backspace", "Backspace", 8),
            "Delete" => ("Delete", "Delete", 46),
            "Space" => (" ", "Space", 32),
            "ArrowUp" => ("ArrowUp", "ArrowUp", 38),
            "ArrowDown" => ("ArrowDown", "ArrowDown", 40),
            "ArrowLeft" => ("ArrowLeft", "ArrowLeft", 37),
            "ArrowRight" => ("ArrowRight", "ArrowRight", 39),
            "Home" => ("Home", "Home", 36),
            "End" => ("End", "End", 35),
            "PageUp" => ("PageUp", "PageUp", 33),
            "PageDown" => ("PageDown", "PageDown", 34),
            other => {
                return Err(BrowserError::BadResponse {
                    method: "dispatch_key",
                    detail: format!("unsupported key name {other:?}"),
                })
            }
        };
        let sid = self.session_id().await;
        // Step 1: keyDown
        self.conn
            .send(
                "Input.dispatchKeyEvent",
                json!({
                    "type": "keyDown",
                    "key": key,
                    "code": code,
                    "windowsVirtualKeyCode": vk,
                }),
                Some(&sid),
            )
            .await?;
        // Step 2: char (only for Enter — needed for textareas to insert
        // a newline, and matches upstream's pattern at
        // browser_use/browser/watchdogs/default_action_watchdog.py:1166).
        // Not needed for navigation keys (Tab/Arrow/etc.).
        if key_name == "Enter" {
            self.conn
                .send(
                    "Input.dispatchKeyEvent",
                    json!({ "type": "char", "text": "\r" }),
                    Some(&sid),
                )
                .await?;
        }
        // Step 3: keyUp
        self.conn
            .send(
                "Input.dispatchKeyEvent",
                json!({
                    "type": "keyUp",
                    "key": key,
                    "code": code,
                    "windowsVirtualKeyCode": vk,
                }),
                Some(&sid),
            )
            .await?;
        Ok(())
    }

    pub async fn get_text(&self, selector: &str) -> Result<String> {
        let sid = self.session_id().await;
        let sel = serde_json::to_string(selector)?;
        let script = format!(
            r#"(() => {{
                const el = document.querySelector({sel});
                if (!el) return "";
                return (el.innerText || el.textContent || "").trim();
            }})()"#
        );
        let r = self
            .conn
            .send(
                "Runtime.evaluate",
                json!({ "expression": script, "returnByValue": true }),
                Some(&sid),
            )
            .await?;
        Ok(r.get("result")
            .and_then(|x| x.get("value"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string())
    }

    pub async fn page_text(&self, max_chars: usize) -> Result<String> {
        let sid = self.session_id().await;
        let cap = if max_chars == 0 { 10_000 } else { max_chars };
        // v0.12.21: body.innerText alone is blind to shadow roots and
        // same-origin iframes — on the shadow/iframe challenge pages the
        // agent filled and submitted correctly but page_text returned
        // ~250 chars of chrome, so it could never SEE the success state
        // and reported failure. Compose text across frames and open
        // shadow roots (styles/scripts skipped; slot-projected text may
        // appear twice, which is acceptable over invisibility).
        let script = format!(
            r#"(() => {{
                const parts = [];
                const pushShadow = (sr) => {{
                    for (const child of sr.children) {{
                        if (/^(STYLE|SCRIPT|NOSCRIPT|TEMPLATE)$/.test(child.tagName)) continue;
                        const t = child.innerText;
                        if (t && t.trim()) parts.push(t.trim());
                    }}
                    for (const host of sr.querySelectorAll('*')) {{
                        if (host.shadowRoot) pushShadow(host.shadowRoot);
                    }}
                    for (const f of sr.querySelectorAll('iframe')) {{
                        try {{ if (f.contentDocument) pushDoc(f.contentDocument); }} catch (e) {{}}
                    }}
                }};
                const pushDoc = (doc) => {{
                    const t = doc.body && (doc.body.innerText || doc.body.textContent);
                    if (t && t.trim()) parts.push(t.trim());
                    for (const host of doc.querySelectorAll('*')) {{
                        if (host.shadowRoot) pushShadow(host.shadowRoot);
                    }}
                    for (const f of doc.querySelectorAll('iframe')) {{
                        try {{ if (f.contentDocument) pushDoc(f.contentDocument); }} catch (e) {{}}
                    }}
                }};
                pushDoc(document);
                return parts.join('\n').trim().slice(0, {cap});
            }})()"#
        );
        let r = self
            .conn
            .send(
                "Runtime.evaluate",
                json!({ "expression": script, "returnByValue": true }),
                Some(&sid),
            )
            .await?;
        Ok(r.get("result")
            .and_then(|x| x.get("value"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string())
    }

    /// Evaluate an arbitrary JavaScript expression in the page context.
    /// Returns the JSON-stringified result (or "" if the result wasn't
    /// representable). Used by Python-side tools (search_page,
    /// find_elements, find_text, get_dropdown_options, send_keys, etc.)
    /// to escape into the page DOM without needing per-tool Rust glue.
    /// v0.6.0.
    pub async fn evaluate(&self, expression: &str) -> Result<String> {
        let sid = self.session_id().await;
        let r = self
            .conn
            .send(
                "Runtime.evaluate",
                json!({
                    "expression": expression,
                    "returnByValue": true,
                    "awaitPromise": true,
                    // v0.12.35: expose the Command Line API so tool JS can
                    // call getEventListeners(el) for listener attribution
                    // (delegated-listener chips). Page globals are never
                    // shadowed — the CLI API only fills undefined names.
                    "includeCommandLineAPI": true,
                }),
                Some(&sid),
            )
            .await?;
        // Surface JS exceptions as errors instead of silently returning ""
        if let Some(exc) = r.get("exceptionDetails") {
            let msg = exc
                .get("text")
                .and_then(Value::as_str)
                .unwrap_or("JS exception");
            return Err(BrowserError::BadResponse {
                method: "evaluate",
                detail: format!("{msg}: {exc}"),
            });
        }
        let result = r.get("result");
        // value may be string, number, bool, array, object, or undefined
        let v = result.and_then(|x| x.get("value"));
        match v {
            Some(Value::String(s)) => Ok(s.clone()),
            Some(other) => Ok(other.to_string()),
            None => Ok(String::new()),
        }
    }

    pub async fn get_links(&self) -> Result<Vec<(String, String)>> {
        let sid = self.session_id().await;
        let script = r#"(() => {
            const out = [];
            for (const a of document.querySelectorAll('a[href]')) {
                const r = a.getBoundingClientRect();
                if (r.width < 1 || r.height < 1) continue;
                out.push({
                    href: a.href,
                    text: (a.innerText || a.textContent || '').replace(/\s+/g, ' ').trim().slice(0, 200)
                });
            }
            return JSON.stringify(out);
        })()"#;
        let r = self
            .conn
            .send(
                "Runtime.evaluate",
                json!({ "expression": script, "returnByValue": true }),
                Some(&sid),
            )
            .await?;
        let s = r
            .get("result")
            .and_then(|x| x.get("value"))
            .and_then(Value::as_str)
            .ok_or_else(|| BrowserError::BadResponse {
                method: "get_links",
                detail: "expected JSON string".into(),
            })?;
        let parsed: Vec<Value> = serde_json::from_str(s)?;
        Ok(parsed
            .into_iter()
            .filter_map(|v| {
                let href = v.get("href")?.as_str()?.to_string();
                let text = v.get("text")?.as_str()?.to_string();
                Some((href, text))
            })
            .collect())
    }

    pub async fn scroll(&self, dy: f64) -> Result<()> {
        let sid = self.session_id().await;
        self.conn
            .send(
                "Input.dispatchMouseEvent",
                json!({
                    "type": "mouseWheel",
                    "x": 100.0,
                    "y": 100.0,
                    "deltaX": 0.0,
                    "deltaY": dy,
                }),
                Some(&sid),
            )
            .await?;
        Ok(())
    }

    pub async fn scroll_to_index(&self, index: u32) -> Result<()> {
        self.fresh_center(index)
            .await?
            .ok_or(BrowserError::ElementGone(index))?;
        Ok(())
    }

    pub async fn scroll_to_top(&self) -> Result<()> {
        let sid = self.session_id().await;
        self.conn
            .send(
                "Runtime.evaluate",
                json!({
                    "expression": "window.scrollTo(0, 0)",
                    "returnByValue": true,
                }),
                Some(&sid),
            )
            .await?;
        Ok(())
    }

    pub async fn scroll_to_bottom(&self) -> Result<()> {
        let sid = self.session_id().await;
        self.conn
            .send(
                "Runtime.evaluate",
                json!({
                    "expression": "window.scrollTo(0, document.body.scrollHeight)",
                    "returnByValue": true,
                }),
                Some(&sid),
            )
            .await?;
        Ok(())
    }

    /// Returns true if a main-frame navigation happened recently (within
    /// 1.5s before this call) or fires before the timeout. The recency
    /// window handles the click-completes-then-navigates race: by the time
    /// the agent calls wait_for_navigation, the URL has often already
    /// changed, so naive polling-from-now misses it. We rely on the
    /// background event task to stamp Page.frameNavigated / loadEventFired
    /// times.
    pub async fn wait_for_navigation(&self, timeout_ms: u64) -> Result<bool> {
        let call_time = tokio::time::Instant::now();
        let recent = Duration::from_millis(1500);
        let deadline = call_time + Duration::from_millis(timeout_ms);
        loop {
            if let Some(ts) = *self.last_navigation.lock().await {
                // Navigation either happened just before the call (the
                // race) or fired since the call (the wait).
                if ts + recent >= call_time {
                    tokio::time::sleep(Duration::from_millis(200)).await;
                    return Ok(true);
                }
            }
            if tokio::time::Instant::now() >= deadline {
                return Ok(false);
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }

    pub async fn wait_for_selector(&self, selector: &str, timeout_ms: u64) -> Result<bool> {
        let sid = self.session_id().await;
        let sel = serde_json::to_string(selector)?;
        let script = format!(
            r#"(() => {{
                const findIn = (doc) => {{
                    if (doc.querySelector({sel})) return true;
                    for (const iframe of doc.querySelectorAll('iframe')) {{
                        try {{
                            const sub = iframe.contentDocument;
                            if (sub && findIn(sub)) return true;
                        }} catch (e) {{}}
                    }}
                    return false;
                }};
                return findIn(document);
            }})()"#
        );
        let deadline = tokio::time::Instant::now() + Duration::from_millis(timeout_ms);
        loop {
            let r = self
                .conn
                .send(
                    "Runtime.evaluate",
                    json!({ "expression": &script, "returnByValue": true }),
                    Some(&sid),
                )
                .await?;
            let found = r
                .get("result")
                .and_then(|x| x.get("value"))
                .and_then(Value::as_bool)
                .unwrap_or(false);
            if found {
                return Ok(true);
            }
            if tokio::time::Instant::now() >= deadline {
                return Ok(false);
            }
            tokio::time::sleep(Duration::from_millis(100)).await;
        }
    }

    pub async fn stop(mut self) -> Result<()> {
        // Attached sessions: detach from the active tab and let the remote
        // owner keep the browser alive. Calling Browser.close here would
        // kill someone else's resource.
        if self.attached_only {
            let sid = self.session_id().await;
            let _ = self
                .conn
                .send("Target.detachFromTarget", json!({ "sessionId": sid }), None)
                .await;
        } else {
            let _ = self.conn.send("Browser.close", json!({}), None).await;
            if let Some(mut child) = self.child.take() {
                let _ = child.kill().await;
            }
            if let Some(dir) = self.user_data_dir.take() {
                let _ = std::fs::remove_dir_all(dir);
            }
        }
        Ok(())
    }
}

impl Drop for BrowserSession {
    fn drop(&mut self) {
        if !self.attached_only {
            if let Some(mut child) = self.child.take() {
                let _ = child.start_kill();
            }
            if let Some(dir) = self.user_data_dir.take() {
                let _ = std::fs::remove_dir_all(dir);
            }
        }
    }
}

fn find_chrome() -> Option<PathBuf> {
    if let Ok(p) = std::env::var("CHROME") {
        return Some(PathBuf::from(p));
    }
    let candidates = [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/google-chrome-stable",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ];
    candidates.iter().map(PathBuf::from).find(|p| p.exists())
}




/// JS prelude defining `findByIdx(root)`: resolve an element by its
/// `data-bu-idx` across same-origin iframes AND open shadow roots.
/// `querySelector` alone cannot see either, which made snapshot-indexed
/// frame/shadow elements unclickable (v0.12.21).
fn find_by_idx_js(index: u32) -> String {
    format!(
        r#"const findByIdx = (root) => {{
            const el = root.querySelector('[data-bu-idx="{index}"]');
            if (el) return el;
            for (const iframe of root.querySelectorAll('iframe')) {{
                try {{
                    const sub = iframe.contentDocument;
                    if (sub) {{ const f = findByIdx(sub); if (f) return f; }}
                }} catch (e) {{}}
            }}
            for (const host of root.querySelectorAll('*')) {{
                try {{
                    if (host.shadowRoot) {{
                        const f = findByIdx(host.shadowRoot);
                        if (f) return f;
                    }}
                }} catch (e) {{}}
            }}
            return null;
        }};"#
    )
}

fn rand_suffix() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    format!("{nanos:x}")
}
