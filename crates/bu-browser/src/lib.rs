//! Browser session lifecycle. Spawns Chromium, attaches via CDP, and
//! exposes the small surface the agent loop actually needs:
//! `start`, `navigate`, `screenshot`, `dom_snapshot`, `click_index`,
//! `type_index`, `scroll`, `stop`.
//!
//! Click/type are resolved via the most-recent snapshot's element bbox —
//! call `dom_snapshot()` before acting on an index.

use std::path::PathBuf;
use std::process::Stdio;
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
    #[error("no dom snapshot taken yet — call dom_snapshot() before acting on an index")]
    NoSnapshot,
    #[error("element [{0}] is no longer present in the DOM — re-snapshot before acting")]
    ElementGone(u32),
}

pub type Result<T> = std::result::Result<T, BrowserError>;

#[derive(Debug, Clone)]
pub struct LaunchOptions {
    pub headless: bool,
    pub chrome_path: Option<PathBuf>,
    pub user_data_dir: Option<PathBuf>,
    pub extra_args: Vec<String>,
}

impl Default for LaunchOptions {
    fn default() -> Self {
        Self {
            headless: true,
            chrome_path: None,
            user_data_dir: None,
            extra_args: Vec::new(),
        }
    }
}

pub struct BrowserSession {
    child: Option<Child>,
    conn: Connection,
    session_id: String,
    #[allow(dead_code)]
    target_id: String,
    user_data_dir: Option<PathBuf>,
    last_snapshot: Mutex<Option<DomState>>,
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

        let target = conn
            .send(
                "Target.createTarget",
                json!({ "url": "about:blank" }),
                None,
            )
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

        Ok(Self {
            child: Some(child),
            conn,
            session_id,
            target_id,
            user_data_dir: Some(user_data_dir),
            last_snapshot: Mutex::new(None),
        })
    }

    /// Navigate and wait for `Page.loadEventFired` matching this session.
    /// Subscribes to events *before* sending Page.navigate to avoid a race
    /// where the load event arrives before we listen. Returns Ok on the
    /// 30-second timeout too — pages that legitimately never fire load
    /// (e.g., infinite-loading SPAs) are still usable.
    pub async fn navigate(&self, url: &str) -> Result<()> {
        let mut events = self.conn.events();

        self.conn
            .send(
                "Page.navigate",
                json!({ "url": url }),
                Some(&self.session_id),
            )
            .await?;

        let session_id = self.session_id.clone();
        let wait = async move {
            loop {
                match events.recv().await {
                    Ok(event) => {
                        if event.method == "Page.loadEventFired"
                            && event.session_id.as_deref() == Some(&session_id)
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

    pub async fn screenshot(&self) -> Result<Vec<u8>> {
        let r = self
            .conn
            .send(
                "Page.captureScreenshot",
                json!({ "format": "png" }),
                Some(&self.session_id),
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
        let r = self
            .conn
            .send(
                "Runtime.evaluate",
                json!({
                    "expression": "window.location.href",
                    "returnByValue": true,
                }),
                Some(&self.session_id),
            )
            .await?;
        Ok(r.get("result")
            .and_then(|x| x.get("value"))
            .and_then(Value::as_str)
            .unwrap_or("")
            .to_string())
    }

    /// Snapshot the visible interactive elements and cache the result so
    /// subsequent click_index/type_index can resolve indices.
    pub async fn dom_snapshot(&self) -> Result<DomState> {
        let snap = bu_dom::snapshot(&self.conn, &self.session_id).await?;
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
        // Make sure the index actually came from a recent snapshot.
        let _ = self.lookup(index).await?;
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
                // Walk up through nested iframes accumulating offsets so the
                // returned coords are in the top window's viewport.
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
                Some(&self.session_id),
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
        self.conn
            .send(
                "Input.dispatchMouseEvent",
                json!({
                    "type": "mouseMoved",
                    "x": x, "y": y,
                }),
                Some(&self.session_id),
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
                Some(&self.session_id),
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
                Some(&self.session_id),
            )
            .await?;
        Ok(())
    }

    pub async fn type_index(&self, index: u32, text: &str) -> Result<()> {
        // Click via fresh_center to focus the element, then insertText.
        // Errors propagate (ElementGone if the element vanished).
        self.click_index(index).await?;
        tokio::time::sleep(Duration::from_millis(50)).await;
        self.conn
            .send(
                "Input.insertText",
                json!({ "text": text }),
                Some(&self.session_id),
            )
            .await?;
        Ok(())
    }

    /// Wheel scroll. `dy` is positive for downward scroll (CSS pixels).
    pub async fn scroll(&self, dy: f64) -> Result<()> {
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
                Some(&self.session_id),
            )
            .await?;
        Ok(())
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
    candidates
        .iter()
        .map(PathBuf::from)
        .find(|p| p.exists())
}

fn rand_suffix() -> String {
    use std::time::{SystemTime, UNIX_EPOCH};
    let nanos = SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .map(|d| d.subsec_nanos())
        .unwrap_or(0);
    format!("{nanos:x}")
}
