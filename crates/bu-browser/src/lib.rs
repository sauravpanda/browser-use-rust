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
use bu_dom::{DomElement, DomError, DomState};
use serde_json::{json, Value};
use thiserror::Error;
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};
use tokio::sync::broadcast::error::RecvError;
use tokio::sync::Mutex;

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
}

impl Default for LaunchOptions {
    fn default() -> Self {
        Self {
            headless: true,
            chrome_path: None,
            user_data_dir: None,
            extra_args: Vec::new(),
            viewport: None,
        }
    }
}

#[derive(Debug, Clone)]
pub struct TabInfo {
    pub target_id: String,
    pub url: String,
    pub title: String,
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
struct DownloadState {
    suggested_filename: String,
    url: String,
    state: String,
    received_bytes: u64,
    total_bytes: u64,
}

#[derive(Debug, Clone)]
struct ActiveTab {
    target_id: String,
    session_id: String,
}

pub struct BrowserSession {
    child: Option<Child>,
    conn: Connection,
    user_data_dir: Option<PathBuf>,
    viewport: Option<(u32, u32)>,
    active: Mutex<ActiveTab>,
    /// target_id -> session_id for tabs we've attached.
    attached: Mutex<HashMap<String, String>>,
    last_snapshot: Mutex<Option<DomState>>,
    /// guid -> latest state, populated by a background event task.
    downloads: Arc<Mutex<HashMap<String, DownloadState>>>,
    download_dir: PathBuf,
    /// Owned only to keep the event task alive; not joined explicitly —
    /// the task exits when the broadcast channel closes (i.e. Connection
    /// is dropped).
    _download_task: tokio::task::JoinHandle<()>,
}

impl BrowserSession {
    pub async fn start() -> Result<Self> {
        Self::launch(LaunchOptions::default()).await
    }

    pub async fn launch(opts: LaunchOptions) -> Result<Self> {
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
        let task_events = conn.events();
        let task_downloads = downloads.clone();
        let download_task = tokio::spawn(track_downloads(task_events, task_downloads));

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
            active: Mutex::new(ActiveTab {
                target_id,
                session_id,
            }),
            attached: Mutex::new(attached),
            last_snapshot: Mutex::new(None),
            downloads,
            download_dir,
            _download_task: download_task,
        })
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

    // ---------- tab management ----------

    /// List all page-type targets (i.e. tabs/windows) known to the browser.
    /// Includes tabs we haven't attached to yet (e.g. opened via
    /// window.open). is_active is true for the tab that operations target.
    pub async fn list_tabs(&self) -> Result<Vec<TabInfo>> {
        let r = self
            .conn
            .send("Target.getTargets", json!({}), None)
            .await?;
        let active_target = self.active.lock().await.target_id.clone();
        let mut out = Vec::new();
        if let Some(arr) = r.get("targetInfos").and_then(Value::as_array) {
            for ti in arr {
                if ti.get("type").and_then(Value::as_str) != Some("page") {
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
                    self.conn
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
                        .await?;
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
    pub async fn navigate(&self, url: &str) -> Result<()> {
        let mut events = self.conn.events();
        let sid = self.session_id().await;

        self.conn
            .send("Page.navigate", json!({ "url": url }), Some(&sid))
            .await?;

        let target = sid.clone();
        let wait = async move {
            loop {
                match events.recv().await {
                    Ok(event) => {
                        if event.method == "Page.loadEventFired"
                            && event.session_id.as_deref() == Some(&target)
                        {
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

        match tokio::time::timeout(Duration::from_secs(30), wait).await {
            Ok(Ok(())) => Ok(()),
            Ok(Err(e)) => Err(e),
            Err(_) => Ok(()),
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
        let sid = self.session_id().await;
        let r = self
            .conn
            .send(
                "Page.captureScreenshot",
                json!({ "format": "png" }),
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
    async fn fresh_center(&self, index: u32) -> Result<Option<(f64, f64)>> {
        let _ = self.lookup(index).await?;
        let sid = self.session_id().await;
        let script = format!(
            r#"(() => {{
                const findByIdx = (doc) => {{
                    const el = doc.querySelector('[data-bu-idx="{index}"]');
                    if (el) return el;
                    for (const iframe of doc.querySelectorAll('iframe')) {{
                        try {{
                            const sub = iframe.contentDocument;
                            if (sub) {{
                                const found = findByIdx(sub);
                                if (found) return found;
                            }}
                        }} catch (e) {{}}
                    }}
                    return null;
                }};
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
                return {{ x: x + r.width / 2, y: y + r.height / 2 }};
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
        let val = r.get("result").and_then(|x| x.get("value"));
        match val {
            None | Some(&Value::Null) => Ok(None),
            Some(Value::Object(o)) => {
                let x = o.get("x").and_then(Value::as_f64).unwrap_or(0.0);
                let y = o.get("y").and_then(Value::as_f64).unwrap_or(0.0);
                Ok(Some((x, y)))
            }
            _ => Ok(None),
        }
    }

    pub async fn click_index(&self, index: u32) -> Result<()> {
        let (cx, cy) = self
            .fresh_center(index)
            .await?
            .ok_or(BrowserError::ElementGone(index))?;
        self.dispatch_click(cx, cy).await
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
        let script = format!(
            r#"(() => {{
                const t = (document.body && (document.body.innerText || document.body.textContent)) || "";
                return t.trim().slice(0, {cap});
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
        let _ = self.conn.send("Browser.close", json!({}), None).await;
        if let Some(mut child) = self.child.take() {
            let _ = child.kill().await;
        }
        if let Some(dir) = self.user_data_dir.take() {
            let _ = std::fs::remove_dir_all(dir);
        }
        Ok(())
    }
}

impl Drop for BrowserSession {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            let _ = child.start_kill();
        }
        if let Some(dir) = self.user_data_dir.take() {
            let _ = std::fs::remove_dir_all(dir);
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

fn parse_cookie(v: Value) -> Option<Cookie> {
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

/// Background task: subscribe to CDP events and update the downloads map
/// when Browser.downloadWillBegin / downloadProgress fire. Exits when the
/// broadcast channel closes (i.e. Connection is dropped).
async fn track_downloads(
    mut events: tokio::sync::broadcast::Receiver<bu_cdp::CdpEvent>,
    downloads: Arc<Mutex<HashMap<String, DownloadState>>>,
) {
    loop {
        match events.recv().await {
            Ok(event) => {
                let params = match event.params.as_object() {
                    Some(p) => p,
                    None => continue,
                };
                let guid = params
                    .get("guid")
                    .and_then(Value::as_str)
                    .unwrap_or("")
                    .to_string();
                if guid.is_empty() {
                    continue;
                }
                match event.method.as_str() {
                    "Browser.downloadWillBegin" | "Page.downloadWillBegin" => {
                        let mut map = downloads.lock().await;
                        let entry = map.entry(guid).or_default();
                        entry.suggested_filename = params
                            .get("suggestedFilename")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string();
                        entry.url = params
                            .get("url")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string();
                        if entry.state.is_empty() {
                            entry.state = "inProgress".to_string();
                        }
                    }
                    "Browser.downloadProgress" | "Page.downloadProgress" => {
                        let mut map = downloads.lock().await;
                        let entry = map.entry(guid).or_default();
                        if let Some(s) = params.get("state").and_then(Value::as_str) {
                            entry.state = s.to_string();
                        }
                        if let Some(rb) = params.get("receivedBytes").and_then(Value::as_u64) {
                            entry.received_bytes = rb;
                        }
                        if let Some(tb) = params.get("totalBytes").and_then(Value::as_u64) {
                            entry.total_bytes = tb;
                        }
                    }
                    _ => {}
                }
            }
            Err(tokio::sync::broadcast::error::RecvError::Lagged(_)) => continue,
            Err(tokio::sync::broadcast::error::RecvError::Closed) => break,
        }
    }
}

fn rand_suffix() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    format!("{nanos:x}")
}
