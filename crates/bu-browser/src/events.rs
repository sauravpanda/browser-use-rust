//! Background CDP event tracking (downloads + navigation stamps).
//! Extracted verbatim from `lib.rs`.

use std::collections::HashMap;
use std::sync::Arc;

use serde_json::Value;
use tokio::sync::Mutex;

use crate::types::DownloadState;

/// Background task: subscribe to CDP events and update both the downloads
/// map and the last-navigation timestamp. Exits when the broadcast channel
/// closes (i.e. Connection is dropped).
pub(crate) async fn track_browser_events(
    mut events: tokio::sync::broadcast::Receiver<bu_cdp::CdpEvent>,
    downloads: Arc<Mutex<HashMap<String, DownloadState>>>,
    last_navigation: Arc<Mutex<Option<tokio::time::Instant>>>,
) {
    loop {
        match events.recv().await {
            Ok(event) => {
                let Some(params) = event.params.as_object() else {
                    continue;
                };

                match event.method.as_str() {
                    // Navigation events: stamp the timestamp on main-frame
                    // navigations only. frameNavigated wraps a `frame` object
                    // whose `parentId` is absent when it's the top frame.
                    "Page.frameNavigated" => {
                        let is_main = params
                            .get("frame")
                            .and_then(|f| f.get("parentId"))
                            .is_none();
                        if is_main {
                            *last_navigation.lock().await = Some(tokio::time::Instant::now());
                        }
                    }
                    "Page.loadEventFired" => {
                        *last_navigation.lock().await = Some(tokio::time::Instant::now());
                    }

                    // Download events
                    "Browser.downloadWillBegin" | "Page.downloadWillBegin" => {
                        let guid = params
                            .get("guid")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string();
                        if guid.is_empty() {
                            continue;
                        }
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
                        let guid = params
                            .get("guid")
                            .and_then(Value::as_str)
                            .unwrap_or("")
                            .to_string();
                        if guid.is_empty() {
                            continue;
                        }
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
