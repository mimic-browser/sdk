use futures_util::{SinkExt, StreamExt};
use serde_json::{json, Value};
use std::collections::BTreeMap;
use std::sync::{
    atomic::{AtomicBool, AtomicU64, Ordering},
    Arc, Mutex,
};
use std::time::Duration;
use tokio::sync::{broadcast, mpsc, oneshot};
use tokio::task::JoinHandle;
use tokio_tungstenite::{connect_async, tungstenite::Message};

pub type Result<T> = std::result::Result<T, Error>;

#[derive(Debug, thiserror::Error)]
pub enum Error {
    #[error("CDP {code}: {message}")]
    Protocol {
        code: i64,
        message: String,
        payload: Value,
    },
    #[error("transport closed: {0}")]
    Closed(String),
    #[error("operation timed out: {0}")]
    Timeout(String),
    #[error("{0}")]
    Invalid(String),
    #[error(transparent)]
    Io(#[from] std::io::Error),
    #[error(transparent)]
    Json(#[from] serde_json::Error),
    #[error(transparent)]
    Http(#[from] reqwest::Error),
    #[error(transparent)]
    WebSocket(#[from] tokio_tungstenite::tungstenite::Error),
    #[cfg(feature = "chromiumoxide")]
    #[error(transparent)]
    Chromiumoxide(#[from] ::chromiumoxide::error::CdpError),
}

struct Pending {
    session_id: Option<String>,
    reply: oneshot::Sender<Result<Value>>,
}
struct State {
    next: AtomicU64,
    closed: AtomicBool,
    pending: Mutex<BTreeMap<u64, Pending>>,
    events: broadcast::Sender<Value>,
}
impl State {
    fn fail_all(&self, message: &str) {
        self.closed.store(true, Ordering::Release);
        let pending = std::mem::take(&mut *self.pending.lock().unwrap());
        for (_, request) in pending {
            let _ = request.reply.send(Err(Error::Closed(message.into())));
        }
    }
}
/// Dropping a cancelled request removes its pending entry, without retrying RPC.
struct PendingGuard {
    state: Arc<State>,
    id: u64,
}
impl Drop for PendingGuard {
    fn drop(&mut self) {
        self.state.pending.lock().unwrap().remove(&self.id);
    }
}

pub struct Transport {
    pub endpoint: String,
    state: Arc<State>,
    outgoing: mpsc::Sender<Message>,
    task: Mutex<Option<JoinHandle<()>>>,
}

pub async fn discover(endpoint: &str) -> Result<String> {
    let mut url = reqwest::Url::parse(endpoint).map_err(|e| Error::Invalid(e.to_string()))?;
    match url.scheme() {
        "ws" | "wss" => return Ok(url.into()),
        "http" | "https" => {}
        _ => return Err(Error::Invalid("Endpoint must use HTTP(S) or WS(S)".into())),
    }
    url.set_path(&format!(
        "{}/json/version",
        url.path().trim_end_matches('/')
    ));
    url.set_query(None);
    url.set_fragment(None);
    let client = reqwest::Client::builder()
        .timeout(Duration::from_secs(15))
        .build()?;
    let version: Value = client
        .get(url)
        .send()
        .await?
        .error_for_status()?
        .json()
        .await?;
    version["webSocketDebuggerUrl"]
        .as_str()
        .filter(|v| v.starts_with("ws://") || v.starts_with("wss://"))
        .map(str::to_owned)
        .ok_or_else(|| Error::Invalid("Discovery omitted browser websocket".into()))
}

impl Transport {
    pub async fn connect(endpoint: &str) -> Result<Arc<Self>> {
        let endpoint = discover(endpoint).await?;
        let (socket, _) = tokio::time::timeout(Duration::from_secs(15), connect_async(&endpoint))
            .await
            .map_err(|_| Error::Timeout("connect".into()))??;
        let (mut write, mut read) = socket.split();
        let (outgoing, mut input) = mpsc::channel::<Message>(128);
        let (events, _) = broadcast::channel(256);
        let state = Arc::new(State {
            next: AtomicU64::new(1),
            closed: AtomicBool::new(false),
            pending: Mutex::new(BTreeMap::new()),
            events,
        });
        let worker = state.clone();
        let task = tokio::spawn(async move {
            loop {
                tokio::select! {
                    command = input.recv() => match command {
                        Some(message) => { let close = matches!(message, Message::Close(_)); if write.send(message).await.is_err() || close { break; } },
                        None => break,
                    },
                    message = read.next() => match message {
                        Some(Ok(Message::Text(text))) => {
                            let value: Value = match serde_json::from_str(&text) { Ok(value) => value, Err(_) => { worker.fail_all("invalid JSON response"); break; } };
                            if let Some(id) = value["id"].as_u64() {
                                if let Some(pending) = worker.pending.lock().unwrap().remove(&id) {
                                    if pending.session_id.as_deref() != value.get("sessionId").and_then(Value::as_str) {
                                        let _ = pending.reply.send(Err(Error::Invalid("Response session identity mismatch".into())));
                                    } else if let Some(error) = value.get("error") {
                                        let result = Error::Protocol { code: error["code"].as_i64().unwrap_or(-32000), message: error["message"].as_str().unwrap_or("Protocol error").to_owned(), payload: error.clone() };
                                        let _ = pending.reply.send(Err(result));
                                    } else if let Some(result) = value.get("result") { let _ = pending.reply.send(Ok(result.clone())); }
                                    else { let _ = pending.reply.send(Err(Error::Invalid("Response has no result or error".into()))); }
                                }
                            } else if value.get("method").and_then(Value::as_str).is_some() { let _ = worker.events.send(value); }
                        },
                        Some(Ok(Message::Ping(data))) => { if write.send(Message::Pong(data)).await.is_err() { break; } },
                        Some(Ok(Message::Close(_))) | Some(Err(_)) | None => break,
                        _ => {},
                    }
                }
            }
            worker.fail_all("socket disconnected");
        });
        Ok(Arc::new(Self {
            endpoint,
            state,
            outgoing,
            task: Mutex::new(Some(task)),
        }))
    }

    /// None omits params; Some(Value::Null) sends explicit JSON null.
    pub async fn call(
        &self,
        method: &str,
        params: Option<Value>,
        session_id: Option<&str>,
        timeout: Duration,
    ) -> Result<Value> {
        if self.state.closed.load(Ordering::Acquire) {
            return Err(Error::Closed("already closed".into()));
        }
        let id = self.state.next.fetch_add(1, Ordering::Relaxed);
        let mut request = json!({"id":id,"method":method});
        if let Some(params) = params {
            request["params"] = params;
        }
        if let Some(session_id) = session_id {
            request["sessionId"] = Value::String(session_id.into());
        }
        let (reply, receiver) = oneshot::channel();
        self.state.pending.lock().unwrap().insert(
            id,
            Pending {
                session_id: session_id.map(str::to_owned),
                reply,
            },
        );
        let _guard = PendingGuard {
            state: self.state.clone(),
            id,
        };
        tokio::time::timeout(timeout, async {
            self.outgoing
                .send(Message::Text(request.to_string().into()))
                .await
                .map_err(|_| Error::Closed("writer closed".into()))?;
            receiver
                .await
                .map_err(|_| Error::Closed("response channel closed".into()))?
        })
        .await
        .map_err(|_| Error::Timeout(method.to_owned()))?
    }
    /// Broadcast lag is an explicit error; events are never silently reordered.
    pub fn subscribe(&self) -> broadcast::Receiver<Value> {
        self.state.events.subscribe()
    }
    pub async fn close(&self) {
        self.state.fail_all("client closed");
        let _ = self.outgoing.try_send(Message::Close(None));
        let task = self.task.lock().unwrap().take();
        if let Some(mut task) = task {
            if tokio::time::timeout(Duration::from_secs(1), &mut task)
                .await
                .is_err()
            {
                task.abort();
            }
        }
    }
    #[cfg(feature = "chromiumoxide")]
    pub(crate) fn abort(&self) {
        self.state.fail_all("integration session dropped");
        if let Some(task) = self.task.lock().unwrap().take() {
            task.abort();
        }
    }
}
impl Drop for Transport {
    fn drop(&mut self) {
        self.state.fail_all("last transport owner dropped");
        if let Some(task) = self.task.get_mut().unwrap().take() {
            task.abort();
        }
    }
}

#[derive(Clone)]
pub struct Client {
    pub transport: Arc<Transport>,
    pub session_id: Option<String>,
    pub timeout: Duration,
    attachment_active: Option<Arc<AtomicBool>>,
}
impl Client {
    pub fn new(transport: Arc<Transport>) -> Self {
        Self {
            transport,
            session_id: None,
            timeout: Duration::from_secs(30),
            attachment_active: None,
        }
    }
    pub fn session(&self, session_id: impl Into<String>) -> Self {
        Self {
            session_id: Some(session_id.into()),
            ..self.clone()
        }
    }
    pub async fn call(&self, method: &str, params: Option<Value>) -> Result<Value> {
        if self
            .attachment_active
            .as_ref()
            .is_some_and(|active| !active.load(Ordering::Acquire))
        {
            return Err(Error::Closed("page extension attachment closed".into()));
        }
        self.transport
            .call(method, params, self.session_id.as_deref(), self.timeout)
            .await
    }
    #[cfg(feature = "chromiumoxide")]
    pub(crate) fn bound_session(&self, id: String, active: Arc<AtomicBool>) -> Self {
        Self {
            session_id: Some(id),
            attachment_active: Some(active),
            ..self.clone()
        }
    }
    pub fn experimental(&self) -> Experimental {
        Experimental {
            client: self.clone(),
            domain: None,
        }
    }
    pub async fn identity(
        &self,
        expected_version: Option<&str>,
    ) -> Result<crate::generated::GetVersionResult> {
        let result: crate::generated::GetVersionResult =
            serde_json::from_value(self.call("Mimic.getVersion", Some(json!({}))).await?)?;
        if result.version.is_empty()
            || result.chrome_version.is_empty()
            || result.base_profile.is_empty()
        {
            return Err(Error::Invalid(
                "Endpoint is not a valid Mimic runtime".into(),
            ));
        }
        if let Some(expected) = expected_version {
            if result.version.trim_start_matches('v') != expected.trim_start_matches('v') {
                return Err(Error::Invalid(format!(
                    "Runtime version mismatch: expected {expected}, got {}",
                    result.version
                )));
            }
        }
        Ok(result)
    }
}
#[derive(Clone)]
pub struct Experimental {
    client: Client,
    domain: Option<String>,
}
impl Experimental {
    pub fn domain(&self, name: impl Into<String>) -> Self {
        Self {
            client: self.client.clone(),
            domain: Some(name.into()),
        }
    }
    pub async fn call(&self, name: &str, params: Option<Value>) -> Result<Value> {
        let method = match &self.domain {
            Some(domain) => format!("{domain}.{name}"),
            None => name.to_owned(),
        };
        self.client.call(&method, params).await
    }
}
