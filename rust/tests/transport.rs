#![cfg(target_os = "linux")]
use futures_util::{SinkExt, StreamExt};
use mimic_browser::{Client, Error, Transport};
use serde_json::{json, Value};
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio_tungstenite::tungstenite::Message;

#[tokio::test]
async fn raw_extensions_preserve_envelopes_errors_sessions_and_closure() {
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let endpoint = format!("ws://{}", listener.local_addr().unwrap());
    let requests = Arc::new(Mutex::new(Vec::new()));
    let observed = requests.clone();
    let server = tokio::spawn(async move {
        let (stream, _) = listener.accept().await.unwrap();
        let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
        while let Some(Ok(Message::Text(text))) = socket.next().await {
            let request: Value = serde_json::from_str(&text).unwrap();
            observed.lock().unwrap().push(request.clone());
            if request["method"] == "Mimic.neverReplies" {
                continue;
            }
            let mut response = json!({"id":request["id"]});
            if let Some(session) = request.get("sessionId") {
                response["sessionId"] = session.clone();
            }
            if request["method"] == "Mimic.futureError" {
                response["error"] = json!({"code":-32123,"message":"future failure","data":{"nullable":null,"unicode":"界"},"future":false});
            } else {
                response["result"] = json!({"futureValue":false});
            }
            socket
                .send(Message::Text(response.to_string().into()))
                .await
                .unwrap();
            socket.send(Message::Text(json!({"method":"Mimic.futureEvent","params":{"n":3},"sessionId":"page-session"}).to_string().into())).await.unwrap();
        }
    });
    let transport = Transport::connect(&endpoint).await.unwrap();
    let mut events = transport.subscribe();
    let client = Client::new(transport.clone()).session("page-session");
    assert_eq!(
        client
            .experimental()
            .domain("Mimic")
            .call("futureCommand", None)
            .await
            .unwrap(),
        json!({"futureValue":false})
    );
    assert_eq!(events.recv().await.unwrap()["sessionId"], "page-session");
    client
        .experimental()
        .call("Mimic.futureCommand", Some(Value::Null))
        .await
        .unwrap();
    match client
        .experimental()
        .call("Mimic.futureError", Some(json!({"enabled":false})))
        .await
        .unwrap_err()
    {
        Error::Protocol {
            code,
            message,
            payload,
        } => {
            assert_eq!(code, -32123);
            assert_eq!(message, "future failure");
            assert_eq!(payload["data"], json!({"nullable":null,"unicode":"界"}));
            assert_eq!(payload["future"], false);
        }
        error => panic!("unexpected error: {error}"),
    }
    assert!(matches!(
        transport
            .call("Mimic.neverReplies", None, None, Duration::from_millis(20))
            .await,
        Err(Error::Timeout(_))
    ));
    let sent = requests.lock().unwrap().clone();
    assert_eq!(
        sent.len(),
        4,
        "experimental never probes method availability"
    );
    assert!(sent[0].get("params").is_none());
    assert_eq!(sent[1]["params"], Value::Null);
    assert_eq!(sent[2]["params"], json!({"enabled":false}));
    assert_eq!(sent[0]["sessionId"], "page-session");
    let pending = transport.call("Mimic.neverReplies", None, None, Duration::from_secs(10));
    let closing = async {
        tokio::time::sleep(Duration::from_millis(20)).await;
        transport.close().await;
    };
    let (response, _) = tokio::join!(pending, closing);
    assert!(matches!(response, Err(Error::Closed(_))));
    server.await.unwrap();
}
