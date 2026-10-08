//! Mimic capabilities beside the actual chromiumoxide browser and page types.
use crate::generated::{
    ConfigureContextParams, MediaConfiguration, SetMediaProfileParams, UpdateResourcePolicyParams,
    WireOptional,
};
use crate::{Client, Error, Result, RuntimeManager, RuntimeOptions, RuntimeProcess, Transport};
use ::chromiumoxide::browser::Browser;
use ::chromiumoxide::cdp::browser_protocol::browser::BrowserContextId;
use ::chromiumoxide::cdp::browser_protocol::target::CreateBrowserContextParams;
use ::chromiumoxide::handler::HandlerConfig;
use ::chromiumoxide::Page;
use futures_util::StreamExt;
use serde_json::json;
use std::future::Future;
use std::time::Duration;
use tokio::task::JoinHandle;

pub struct Session {
    pub browser: Browser,
    pub mimic: Client,
    runtime: Option<RuntimeProcess>,
    handler: Option<JoinHandle<Result<()>>>,
    contexts: Vec<BrowserContextId>,
}

/// Access to the newly created Context before any user Page exists. Source IDs
/// returned by `get_media_sources` must be requested for this Context's ID.
pub struct ContextSetup {
    pub browser_context_id: String,
    pub mimic: Client,
}

impl Session {
    pub async fn launch(options: RuntimeOptions) -> Result<Self> {
        let mut runtime = RuntimeManager::new(options)?.launch().await?;
        match Self::attach(runtime.client.clone()).await {
            Ok(mut session) => {
                session.runtime = Some(runtime);
                Ok(session)
            }
            Err(error) => {
                let _ = runtime.close().await;
                Err(error)
            }
        }
    }

    /// Connect to a caller-owned runtime. Closing this session never sends Browser.close.
    pub async fn connect(endpoint: &str) -> Result<Self> {
        let client = Client::new(Transport::connect(endpoint).await?);
        Self::attach(client).await
    }

    async fn attach(client: Client) -> Result<Self> {
        client.identity(None).await?;
        let (browser, mut handler) = tokio::time::timeout(
            Duration::from_secs(30),
            Browser::connect_with_config(
                client.transport.endpoint.clone(),
                HandlerConfig {
                    ignore_https_errors: false,
                    ..Default::default()
                },
            ),
        )
        .await
        .map_err(|_| Error::Timeout("chromiumoxide connect".into()))??;
        let handler = tokio::spawn(async move {
            while let Some(event) = handler.next().await {
                event?;
            }
            Ok(())
        });
        Ok(Self {
            browser,
            mimic: client,
            runtime: None,
            handler: Some(handler),
            contexts: Vec::new(),
        })
    }

    /// Create a native browser context, then configure its Mimic capabilities
    /// before the first user Page. The returned ID is chromiumoxide's own type.
    pub async fn new_context(
        &mut self,
        options: ConfigureContextParams,
    ) -> Result<BrowserContextId> {
        self.create_configured_context(options, |_| async { Ok(None) })
            .await
    }

    /// Select private capture sources inside their owning Context, independently
    /// of the public device identities declared in the returned media profile.
    pub async fn new_context_with_media<F, Fut>(
        &mut self,
        options: ConfigureContextParams,
        factory: F,
    ) -> Result<BrowserContextId>
    where
        F: FnOnce(ContextSetup) -> Fut,
        Fut: Future<Output = Result<MediaConfiguration>>,
    {
        if !options.media.is_missing() {
            return Err(Error::Invalid(
                "set media through either options or the factory, not both".into(),
            ));
        }
        self.create_configured_context(
            options,
            |setup| async move { factory(setup).await.map(Some) },
        )
        .await
    }

    async fn create_configured_context<F, Fut>(
        &mut self,
        mut options: ConfigureContextParams,
        factory: F,
    ) -> Result<BrowserContextId>
    where
        F: FnOnce(ContextSetup) -> Fut,
        Fut: Future<Output = Result<Option<MediaConfiguration>>>,
    {
        if !options.browser_context_id.is_empty() {
            return Err(Error::Invalid(
                "new_context assigns browser_context_id; leave it empty".into(),
            ));
        }
        let context = self
            .browser
            .create_browser_context(CreateBrowserContextParams {
                dispose_on_detach: Some(true),
                ..Default::default()
            })
            .await?;
        // Record ownership before user code can await or cancel. Session close
        // still disposes this Context if the factory future is interrupted.
        self.contexts.push(context.clone());
        options.browser_context_id = context.as_ref().to_owned();
        let configured = async {
            if let Some(media) = factory(ContextSetup {
                browser_context_id: options.browser_context_id.clone(),
                mimic: self.mimic.clone(),
            })
            .await?
            {
                options.media = WireOptional::Value(media);
            }
            if !options.profile.is_missing() || !options.proxy.is_missing() {
                self.mimic.configure_context(options).await?;
            } else {
                // Media/source configuration alone must not opt the Context
                // into a managed environment profile or freeze CDP emulation.
                match options.media {
                    WireOptional::Value(media) => {
                        self.mimic
                            .set_media_profile(SetMediaProfileParams {
                                browser_context_id: WireOptional::Value(
                                    options.browser_context_id.clone(),
                                ),
                                camera: media.camera,
                                devices: media.devices,
                                microphone: media.microphone,
                                seed: media.seed,
                            })
                            .await?;
                    }
                    WireOptional::Null => {
                        return Err(Error::Invalid(
                            "media must be a configuration object".into(),
                        ));
                    }
                    WireOptional::Missing => {}
                }
                match options.resource_policy {
                    WireOptional::Value(policy) => {
                        self.mimic
                            .update_resource_policy(UpdateResourcePolicyParams {
                                browser_context_id: options.browser_context_id,
                                policy,
                            })
                            .await?;
                    }
                    WireOptional::Null => {
                        return Err(Error::Invalid("resource policy must be an object".into()));
                    }
                    WireOptional::Missing => {}
                }
            }
            Ok(())
        }
        .await;
        if let Err(error) = configured {
            if self
                .browser
                .dispose_browser_context(context.clone())
                .await
                .is_ok()
            {
                self.contexts.retain(|owned| owned != &context);
            }
            return Err(error);
        }
        Ok(context)
    }

    /// Attach a separate, explicit CDP session to this native Page. Framework
    /// session IDs are connection-local and cannot be reused on the SDK socket.
    pub async fn for_page(&self, page: &Page) -> Result<Client> {
        let response = self
            .mimic
            .call(
                "Target.attachToTarget",
                Some(json!({
                    "targetId": page.target_id().as_ref(), "flatten": true
                })),
            )
            .await?;
        let id = response["sessionId"]
            .as_str()
            .ok_or_else(|| Error::Invalid("Target.attachToTarget omitted sessionId".into()))?;
        Ok(self.mimic.session(id))
    }

    pub async fn close(&mut self) -> Result<()> {
        let mut failure = None;
        for context in self.contexts.drain(..) {
            if let Err(error) = self.browser.dispose_browser_context(context).await {
                failure.get_or_insert(Error::from(error));
            }
        }
        if let Some(handler) = self.handler.take() {
            if handler.is_finished() {
                match handler.await {
                    Ok(Err(error)) => {
                        failure.get_or_insert(error);
                    }
                    Err(error) => {
                        failure.get_or_insert(Error::Invalid(format!(
                            "chromiumoxide handler: {error}"
                        )));
                    }
                    _ => {}
                }
            } else {
                handler.abort();
                let _ = handler.await;
            }
        }
        if let Some(mut runtime) = self.runtime.take() {
            if let Err(error) = runtime.close().await {
                failure.get_or_insert(error);
            }
        } else {
            self.mimic.transport.close().await;
        }
        failure.map_or(Ok(()), Err)
    }
}

impl Drop for Session {
    fn drop(&mut self) {
        if let Some(handler) = self.handler.take() {
            handler.abort();
        }
        // RuntimeProcess terminates only SDK-owned processes. Native Browser
        // was connected, never launched, and its Drop cannot kill a shared host.
    }
}
