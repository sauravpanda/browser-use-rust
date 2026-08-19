//! Integration tests for the CDP connection against an in-process
//! WebSocket server — exercises request/response multiplexing, protocol
//! error mapping, event fan-out, and closed-connection handling without
//! a real Chrome.

use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use tokio::net::TcpListener;
use tokio_tungstenite::tungstenite::Message;

use bu_cdp::{CdpError, Connection};

/// Spawn a WebSocket server that answers CDP-shaped calls via `handler`
/// and returns its ws:// URL. The handler receives each parsed request
/// and returns any number of messages to send back (responses and/or
/// events).
async fn spawn_server<F>(handler: F) -> String
where
    F: Fn(Value) -> Vec<Value> + Send + Sync + 'static,
{
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let ws = tokio_tungstenite::accept_async(stream).await.unwrap();
        let (mut sink, mut source) = ws.split();
        while let Some(Ok(msg)) = source.next().await {
            let Message::Text(txt) = msg else { continue };
            let req: Value = serde_json::from_str(&txt).unwrap();
            for out in handler(req.clone()) {
                sink.send(Message::Text(out.to_string().into()))
                    .await
                    .unwrap();
            }
        }
    });
    format!("ws://{addr}")
}

#[tokio::test]
async fn send_matches_response_by_id() {
    let url = spawn_server(|req| {
        let id = req["id"].as_i64().unwrap();
        vec![json!({"id": id, "result": {"echo": req["method"]}})]
    })
    .await;

    let conn = Connection::connect(&url).await.unwrap();
    let r = conn.send("Page.enable", json!({}), None).await.unwrap();
    assert_eq!(r["echo"], "Page.enable");
}

#[tokio::test]
async fn protocol_error_is_mapped() {
    let url = spawn_server(|req| {
        let id = req["id"].as_i64().unwrap();
        vec![json!({
            "id": id,
            "error": {"code": -32000, "message": "target closed"}
        })]
    })
    .await;

    let conn = Connection::connect(&url).await.unwrap();
    let err = conn.send("Page.navigate", json!({}), None).await.unwrap_err();
    match err {
        CdpError::Protocol { code, message } => {
            assert_eq!(code, -32000);
            assert_eq!(message, "target closed");
        }
        other => panic!("expected Protocol error, got {other:?}"),
    }
}

#[tokio::test]
async fn session_id_is_attached_to_envelope() {
    let url = spawn_server(|req| {
        let id = req["id"].as_i64().unwrap();
        // Echo the sessionId back so the test can assert on it.
        vec![json!({"id": id, "result": {"sid": req["sessionId"]}})]
    })
    .await;

    let conn = Connection::connect(&url).await.unwrap();
    let r = conn
        .send("DOM.getDocument", json!({}), Some("session-123"))
        .await
        .unwrap();
    assert_eq!(r["sid"], "session-123");
}

#[tokio::test]
async fn events_are_broadcast_to_subscribers() {
    let url = spawn_server(|req| {
        let id = req["id"].as_i64().unwrap();
        vec![
            // Event first (no id), then the response.
            json!({
                "method": "Page.loadEventFired",
                "sessionId": "s1",
                "params": {"timestamp": 1.0}
            }),
            json!({"id": id, "result": {}}),
        ]
    })
    .await;

    let conn = Connection::connect(&url).await.unwrap();
    let mut events = conn.events();
    conn.send("Page.enable", json!({}), None).await.unwrap();

    let event = events.recv().await.unwrap();
    assert_eq!(event.method, "Page.loadEventFired");
    assert_eq!(event.session_id.as_deref(), Some("s1"));
    assert_eq!(event.params["timestamp"], 1.0);
}

#[tokio::test]
async fn pending_calls_fail_when_connection_closes() {
    // Server that reads one message then drops the socket without replying.
    let listener = TcpListener::bind("127.0.0.1:0").await.unwrap();
    let addr = listener.local_addr().unwrap();
    tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let ws = tokio_tungstenite::accept_async(stream).await.unwrap();
        let (_sink, mut source) = ws.split();
        let _ = source.next().await; // consume the request, then drop
    });
    let url = format!("ws://{addr}");

    let conn = Connection::connect(&url).await.unwrap();
    let err = conn.send("Page.enable", json!({}), None).await.unwrap_err();
    assert!(matches!(err, CdpError::Closed));
}
