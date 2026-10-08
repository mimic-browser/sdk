//! Ownership of extension sessions on the SDK socket, separate from native Pages.
use crate::{Client, Error, Result};
use serde_json::{json, Value};
use std::collections::{HashMap, HashSet};
use std::sync::{
    atomic::{AtomicBool, Ordering},
    Arc, Mutex,
};
use tokio::sync::Mutex as AsyncMutex;

struct Attachment {
    id: Option<String>,
    client: Option<Client>,
    active: Arc<AtomicBool>,
    detached: HashSet<String>,
}

#[derive(Default)]
struct State {
    closed: bool,
    pages: HashMap<String, Attachment>,
}

#[derive(Default)]
pub(crate) struct Attachments {
    state: Mutex<State>,
    acquisition: AsyncMutex<()>,
}

impl Attachments {
    pub(crate) fn invalidate(&self, event: &Value) {
        let method = event["method"].as_str().unwrap_or_default();
        if !matches!(
            method,
            "Target.detachedFromTarget" | "Target.targetDestroyed" | "Inspector.detached"
        ) {
            return;
        }
        let id = if method == "Inspector.detached" {
            event["sessionId"].as_str()
        } else {
            event["params"]["sessionId"].as_str()
        };
        let target = event["params"]["targetId"].as_str();
        self.state.lock().unwrap().pages.retain(|target_id, page| {
            if page.id.is_none() {
                if let Some(id) = id {
                    page.detached.insert(id.to_owned());
                }
            }
            let dead = id.is_some() && page.id.as_deref() == id
                || method == "Target.targetDestroyed" && Some(target_id.as_str()) == target;
            if dead {
                page.active.store(false, Ordering::Release);
            }
            !dead
        });
    }

    pub(crate) fn invalidate_all(&self) {
        let mut state = self.state.lock().unwrap();
        state.closed = true;
        for page in state.pages.values() {
            page.active.store(false, Ordering::Release);
        }
    }

    pub(crate) async fn for_target(
        self: &Arc<Self>,
        client: Client,
        target: String,
    ) -> Result<Client> {
        let this = self.clone();
        // Cancelling a caller's wait must not abandon the remote attach reply.
        // This task remains owned by the Session and records or disposes its ID.
        tokio::spawn(async move { this.acquire(client, target).await })
            .await
            .map_err(|error| Error::Invalid(format!("page attachment task: {error}")))?
    }

    async fn acquire(&self, client: Client, target: String) -> Result<Client> {
        let _acquisition = self.acquisition.lock().await;
        let active = Arc::new(AtomicBool::new(true));
        {
            let mut state = self.state.lock().unwrap();
            if state.closed {
                return Err(Error::Closed("integration session closed".into()));
            }
            if let Some(page) = state.pages.get(&target) {
                if page.active.load(Ordering::Acquire) {
                    return Ok(page.client.as_ref().unwrap().clone());
                }
                return Err(Error::Closed("page attachment is detaching".into()));
            }
            state.pages.insert(
                target.clone(),
                Attachment {
                    id: None,
                    client: None,
                    active: active.clone(),
                    detached: HashSet::new(),
                },
            );
        }
        let reply = client
            .call(
                "Target.attachToTarget",
                Some(json!({"targetId": target, "flatten": true})),
            )
            .await;
        let id = match reply {
            Ok(value) => value["sessionId"]
                .as_str()
                .filter(|id| !id.is_empty())
                .map(str::to_owned)
                .ok_or_else(|| Error::Invalid("Target.attachToTarget omitted sessionId".into())),
            Err(error) => Err(error),
        };
        let id = match id {
            Ok(id) => id,
            Err(error) => {
                self.state.lock().unwrap().pages.remove(&target);
                active.store(false, Ordering::Release);
                return Err(error);
            }
        };
        let handle = client.bound_session(id.clone(), active.clone());
        {
            let mut state = self.state.lock().unwrap();
            let closed = state.closed;
            if let Some(page) = state.pages.get_mut(&target) {
                if !closed && page.active.load(Ordering::Acquire) && !page.detached.contains(&id) {
                    page.id = Some(id);
                    page.client = Some(handle.clone());
                    page.detached.clear();
                    return Ok(handle);
                }
            }
            active.store(false, Ordering::Release);
            // Keep ownership until the detach reply succeeds. Close retries a
            // failed cleanup even when the caller cancelled its original wait.
            state.pages.insert(
                target.clone(),
                Attachment {
                    id: Some(id.clone()),
                    client: None,
                    active,
                    detached: HashSet::new(),
                },
            );
        }
        detach_id(&client, &id).await?;
        self.state.lock().unwrap().pages.remove(&target);
        Err(Error::Closed(
            "page closed during extension attachment".into(),
        ))
    }

    pub(crate) async fn detach(&self, client: &Client, target: &str) -> Result<()> {
        let _acquisition = self.acquisition.lock().await;
        let id = {
            let mut state = self.state.lock().unwrap();
            if state.closed {
                return Err(Error::Closed("integration session closed".into()));
            }
            let Some(page) = state.pages.get_mut(target) else {
                return Ok(());
            };
            page.active.store(false, Ordering::Release);
            page.id.clone()
        };
        if let Some(id) = id {
            detach_id(client, &id).await?;
        }
        self.state.lock().unwrap().pages.remove(target);
        Ok(())
    }

    pub(crate) async fn close(&self, client: &Client) -> Result<()> {
        self.invalidate_all();
        let _acquisition = self.acquisition.lock().await;
        let pages = std::mem::take(&mut self.state.lock().unwrap().pages);
        let mut failure = None;
        for page in pages.into_values() {
            if let Some(id) = page.id {
                if let Err(error) = detach_id(client, &id).await {
                    failure.get_or_insert(error);
                }
            }
        }
        failure.map_or(Ok(()), Err)
    }
}

async fn detach_id(client: &Client, id: &str) -> Result<()> {
    match client
        .call("Target.detachFromTarget", Some(json!({"sessionId": id})))
        .await
    {
        Ok(_) => Ok(()),
        Err(Error::Protocol {
            code: -32000,
            message,
            ..
        }) if message == "No session with given id" => Ok(()),
        Err(error) => Err(error),
    }
}

#[cfg(all(test, target_os = "linux"))]
mod tests {
    use super::*;
    use crate::Transport;
    use futures_util::{SinkExt, StreamExt};
    use std::sync::atomic::AtomicUsize;
    use std::time::Duration;
    use tokio::sync::Notify;
    use tokio_tungstenite::tungstenite::Message;

    struct Fixture {
        client: Client,
        pages: Arc<Attachments>,
        entered: Arc<Notify>,
        release: Arc<Notify>,
        attached: Arc<AtomicUsize>,
        detached: Arc<AtomicUsize>,
        fail_detach: Arc<AtomicBool>,
        server: tokio::task::JoinHandle<()>,
    }

    impl Fixture {
        async fn new() -> Self {
            let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
            let address = listener.local_addr().unwrap();
            let entered = Arc::new(Notify::new());
            let release = Arc::new(Notify::new());
            let attached = Arc::new(AtomicUsize::new(0));
            let detached = Arc::new(AtomicUsize::new(0));
            let fail_detach = Arc::new(AtomicBool::new(false));
            let fail_detach_server = fail_detach.clone();
            let (entered_server, release_server) = (entered.clone(), release.clone());
            let (attached_server, detached_server) = (attached.clone(), detached.clone());
            let server = tokio::spawn(async move {
                let (stream, _) = listener.accept().await.unwrap();
                let mut socket = tokio_tungstenite::accept_async(stream).await.unwrap();
                while let Some(Ok(Message::Text(text))) = socket.next().await {
                    let command: Value = serde_json::from_str(&text).unwrap();
                    let mut failure = None;
                    let result = match command["method"].as_str().unwrap() {
                        "Target.attachToTarget" => {
                            let number = attached_server.fetch_add(1, Ordering::SeqCst) + 1;
                            if number == 1 {
                                entered_server.notify_one();
                                release_server.notified().await;
                            }
                            json!({"sessionId": format!("sdk-{number}")})
                        }
                        "Target.detachFromTarget" => {
                            detached_server.fetch_add(1, Ordering::SeqCst);
                            if fail_detach_server.swap(false, Ordering::SeqCst) {
                                failure = Some(
                                    json!({"code":-32000,"message":"cleanup failed","data":{"reason":"fixture"}}),
                                );
                            }
                            json!({})
                        }
                        _ => json!({}),
                    };
                    let mut reply = json!({"id": command["id"], "result": result});
                    if let Some(error) = failure {
                        reply.as_object_mut().unwrap().remove("result");
                        reply["error"] = error;
                    }
                    if let Some(session) = command.get("sessionId") {
                        reply["sessionId"] = session.clone();
                    }
                    if socket
                        .send(Message::Text(reply.to_string().into()))
                        .await
                        .is_err()
                    {
                        break;
                    }
                }
            });
            let client = Client::new(
                Transport::connect(&format!("ws://{address}"))
                    .await
                    .unwrap(),
            );
            Self {
                client,
                pages: Arc::new(Attachments::default()),
                entered,
                release,
                attached,
                detached,
                fail_detach,
                server,
            }
        }

        async fn finish(self) {
            self.pages.close(&self.client).await.unwrap();
            self.client.transport.close().await;
            self.server.await.unwrap();
        }
    }

    #[tokio::test]
    async fn cancelled_waiter_keeps_one_owned_acquisition_and_explicit_detach() {
        let fixture = Fixture::new().await;
        let pages = fixture.pages.clone();
        let client = fixture.client.clone();
        let cancelled =
            tokio::spawn(async move { pages.for_target(client, "native-page".into()).await });
        fixture.entered.notified().await;
        cancelled.abort();
        let _ = cancelled.await;
        fixture.release.notify_one();
        let clients = futures_util::future::try_join_all((0..8).map(|_| {
            fixture
                .pages
                .for_target(fixture.client.clone(), "native-page".into())
        }))
        .await
        .unwrap();
        assert_eq!(fixture.attached.load(Ordering::SeqCst), 1);
        assert!(clients
            .iter()
            .all(|client| client.session_id.as_deref() == Some("sdk-1")));
        fixture
            .pages
            .detach(&fixture.client, "native-page")
            .await
            .unwrap();
        assert_eq!(fixture.detached.load(Ordering::SeqCst), 1);
        for client in clients {
            assert!(matches!(
                client.call("Mimic.getTrace", None).await,
                Err(Error::Closed(_))
            ));
            assert!(matches!(
                client.experimental().call("Mimic.getTrace", None).await,
                Err(Error::Closed(_))
            ));
        }
        fixture.finish().await;
    }

    #[tokio::test]
    async fn detach_before_reply_and_owner_close_cannot_publish_a_stale_handle() {
        for close_owner in [false, true] {
            let fixture = Fixture::new().await;
            let pages = fixture.pages.clone();
            let client = fixture.client.clone();
            let pending =
                tokio::spawn(async move { pages.for_target(client, "native-page".into()).await });
            fixture.entered.notified().await;
            if close_owner {
                fixture.pages.invalidate_all();
            } else {
                fixture.pages.invalidate(&json!({"method":"Target.detachedFromTarget", "params":{"sessionId":"sdk-1","targetId":"native-page"}}));
            }
            fixture.release.notify_one();
            let result = tokio::time::timeout(Duration::from_secs(2), pending)
                .await
                .unwrap()
                .unwrap();
            assert!(matches!(result, Err(Error::Closed(_))));
            assert_eq!(fixture.detached.load(Ordering::SeqCst), 1);
            assert!(fixture.pages.state.lock().unwrap().pages.is_empty());
            fixture.finish().await;
        }
    }

    #[tokio::test]
    async fn failed_late_detach_preserves_protocol_error_and_owner_retries_cleanup() {
        let fixture = Fixture::new().await;
        fixture.fail_detach.store(true, Ordering::SeqCst);
        let pages = fixture.pages.clone();
        let client = fixture.client.clone();
        let pending =
            tokio::spawn(async move { pages.for_target(client, "native-page".into()).await });
        fixture.entered.notified().await;
        fixture.pages.invalidate(
            &json!({"method":"Target.detachedFromTarget", "params":{"sessionId":"sdk-1"}}),
        );
        fixture.release.notify_one();
        let result = pending.await.unwrap();
        assert!(
            matches!(result, Err(Error::Protocol {code: -32000, payload, ..}) if payload["data"] == json!({"reason":"fixture"}))
        );
        assert_eq!(fixture.pages.state.lock().unwrap().pages.len(), 1);
        let detached = fixture.detached.clone();
        fixture.finish().await;
        assert_eq!(
            detached.load(Ordering::SeqCst),
            2,
            "owner must retry its retained cleanup"
        );
    }
}
