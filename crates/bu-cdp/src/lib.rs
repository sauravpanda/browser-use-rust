//! Typed Chrome DevTools Protocol client.
//!
//! Lowest layer: a thin async WebSocket transport. `Connection::send` sends
//! a CDP method call and awaits the matching response, multiplexed by message
//! id. Events (messages without an `id`) are fanned out via a broadcast
//! channel — subscribe via `Connection::events()`.

use std::collections::HashMap;
use std::sync::atomic::{AtomicI64, Ordering};
use std::sync::Arc;

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use thiserror::Error;
use tokio::sync::{broadcast, mpsc, oneshot, Mutex};
use tokio::task::JoinHandle;
use tokio_tungstenite::connect_async;
use tokio_tungstenite::tungstenite::Message;

#[derive(Debug, Error)]
pub enum CdpError {
    #[error("websocket error: {0}")]
    WebSocket(#[from] tokio_tungstenite::tungstenite::Error),
    #[error("serde error: {0}")]
    Serde(#[from] serde_json::Error),
    #[error("cdp protocol error {code}: {message}")]
    Protocol { code: i64, message: String },
    #[error("unexpected response shape: {0}")]
    Shape(String),
    #[error("connection closed")]
    Closed,
}

pub type Result<T> = std::result::Result<T, CdpError>;

type Pending = Arc<Mutex<HashMap<i64, oneshot::Sender<Result<Value>>>>>;

/// A CDP event (a message from the browser without an `id` — fired in
/// response to a domain being enabled, e.g. `Page.loadEventFired`).
#[derive(Clone, Debug)]
pub struct CdpEvent {
    pub method: String,
    pub session_id: Option<String>,
    pub params: Value,
}

pub struct Connection {
    next_id: AtomicI64,
    pending: Pending,
    outbound: mpsc::UnboundedSender<Message>,
    events_tx: broadcast::Sender<CdpEvent>,
    _reader: JoinHandle<()>,
    _writer: JoinHandle<()>,
}

impl Connection {
    pub async fn connect(ws_url: &str) -> Result<Self> {
        let (ws, _) = connect_async(ws_url).await?;
        let (mut sink, mut stream) = ws.split();

        let pending: Pending = Arc::new(Mutex::new(HashMap::new()));
        let pending_for_reader = pending.clone();

        let (out_tx, mut out_rx) = mpsc::unbounded_channel::<Message>();
        // 1024 is large enough that bursty event traffic (e.g. Network events
        // when DOM.enable is on) doesn't drop a navigate-completion event
        // before slow consumers get to it.
        let (events_tx, _) = broadcast::channel::<CdpEvent>(1024);
        let events_for_reader = events_tx.clone();

        let writer = tokio::spawn(async move {
            while let Some(msg) = out_rx.recv().await {
                if sink.send(msg).await.is_err() {
                    break;
                }
            }
            let _ = sink.close().await;
        });

        let reader = tokio::spawn(async move {
            while let Some(Ok(msg)) = stream.next().await {
                let txt = match msg {
                    Message::Text(t) => t.to_string(),
                    Message::Close(_) => break,
                    _ => continue,
                };
                let Ok(v) = serde_json::from_str::<Value>(&txt) else {
                    continue;
                };
                if let Some(id) = v.get("id").and_then(Value::as_i64) {
                    let tx = pending_for_reader.lock().await.remove(&id);
                    if let Some(tx) = tx {
                        let result = if let Some(err) = v.get("error") {
                            let code = err.get("code").and_then(Value::as_i64).unwrap_or(0);
                            let message = err
                                .get("message")
                                .and_then(Value::as_str)
                                .unwrap_or("")
                                .to_string();
                            Err(CdpError::Protocol { code, message })
                        } else {
                            Ok(v.get("result").cloned().unwrap_or(Value::Null))
                        };
                        let _ = tx.send(result);
                    }
                } else if let Some(method) = v.get("method").and_then(Value::as_str) {
                    let event = CdpEvent {
                        method: method.to_string(),
                        session_id: v
                            .get("sessionId")
                            .and_then(Value::as_str)
                            .map(String::from),
                        params: v.get("params").cloned().unwrap_or(Value::Null),
                    };
                    // err means no current subscribers — fine, drop the event
                    let _ = events_for_reader.send(event);
                }
            }
            // connection closed: notify any pending callers
            let mut p = pending_for_reader.lock().await;
            for (_, tx) in p.drain() {
                let _ = tx.send(Err(CdpError::Closed));
            }
        });

        Ok(Self {
            next_id: AtomicI64::new(1),
            pending,
            outbound: out_tx,
            events_tx,
            _reader: reader,
            _writer: writer,
        })
    }

    /// Subscribe to CDP events. Each subscriber gets every event from the
    /// point of subscription onward; messages prior to subscribe are not
    /// replayed. Slow consumers may receive `Lagged` errors and miss events.
    pub fn events(&self) -> broadcast::Receiver<CdpEvent> {
        self.events_tx.subscribe()
    }

    /// Send a CDP method call and await the response. `session_id` routes the
    /// call to a specific attached target when present (flat session model).
    pub async fn send(
        &self,
        method: &str,
        params: Value,
        session_id: Option<&str>,
    ) -> Result<Value> {
        let id = self.next_id.fetch_add(1, Ordering::Relaxed);
        let (tx, rx) = oneshot::channel();
        self.pending.lock().await.insert(id, tx);

        let mut envelope = json!({
            "id": id,
            "method": method,
            "params": params,
        });
        if let Some(sid) = session_id {
            envelope["sessionId"] = Value::String(sid.to_string());
        }

        let txt = serde_json::to_string(&envelope)?;
        self.outbound
            .send(Message::Text(txt.into()))
            .map_err(|_| CdpError::Closed)?;

        rx.await.map_err(|_| CdpError::Closed)?
    }
}
