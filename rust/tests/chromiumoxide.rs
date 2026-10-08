#![cfg(all(feature = "chromiumoxide", target_os = "linux"))]
use chromiumoxide::cdp::browser_protocol::target::CreateTargetParams;
use mimic_browser::generated::{
    ConfigureContextParams, GenerateProfileParams, GetMediaProfileParams, GetProfileParams,
    GetResourcePolicyParams, GetStatusParams, MediaConfiguration, ResourcePolicy, WireOptional,
};
use mimic_browser::{chromiumoxide::Session, RuntimeOptions};
use serde_json::json;
use tokio::io::{AsyncReadExt, AsyncWriteExt};

#[tokio::test]
async fn genuine_framework_context_page_and_borrowed_ownership() {
    let Ok(executable) = std::env::var("MIMIC_SDK_TEST_RUNTIME") else {
        return;
    };
    let listener = tokio::net::TcpListener::bind("127.0.0.1:0").await.unwrap();
    let url = format!("http://{}", listener.local_addr().unwrap());
    let fixture = tokio::spawn(async move {
        loop {
            let Ok((mut stream, _)) = listener.accept().await else {
                break;
            };
            tokio::spawn(async move {
                let mut request = [0; 4096];
                let _ = stream.read(&mut request).await;
                let body="<!doctype html><title>Rust native integration</title><button id='button'>Click</button><script>document.querySelector('button').addEventListener('click',()=>document.title='Clicked')</script>";
                let response=format!("HTTP/1.1 200 OK\r\nContent-Type: text/html\r\nContent-Length: {}\r\nConnection: close\r\n\r\n{body}",body.len());
                let _ = stream.write_all(response.as_bytes()).await;
            });
        }
    });
    let result = tokio::time::timeout(std::time::Duration::from_secs(60), async {
        let mut owned = Session::launch(RuntimeOptions {
            executable_path: Some(executable.into()),
            allow_download: Some(false),
            ..Default::default()
        })
        .await?;
        let endpoint = owned.mimic.transport.endpoint.clone();
        let profile = owned
            .mimic
            .generate_profile(GenerateProfileParams {
                seed: WireOptional::Value("rust-native".into()),
                ..Default::default()
            })
            .await?;
        let context = owned
            .new_context(ConfigureContextParams {
                profile: WireOptional::Value(json!(profile.profile)),
                media: WireOptional::Value(MediaConfiguration {
                    devices: WireOptional::Value(vec![]),
                    ..Default::default()
                }),
                resource_policy: WireOptional::Value(ResourcePolicy {
                    report_only: WireOptional::Value(true),
                    ..Default::default()
                }),
                ..Default::default()
            })
            .await?;
        let context_id = context.as_ref().to_owned();
        let media = owned
            .mimic
            .get_media_profile(GetMediaProfileParams {
                browser_context_id: WireOptional::Value(context_id.clone()),
            })
            .await?;
        assert!(media.profile.unwrap().devices.is_empty());
        let policy = owned
            .mimic
            .get_resource_policy(GetResourcePolicyParams {
                browser_context_id: context_id,
            })
            .await?;
        assert_eq!(policy.policy.report_only, WireOptional::Value(true));
        let params = CreateTargetParams::builder()
            .url(url)
            .browser_context_id(context)
            .build()
            .unwrap();
        let page = owned.browser.new_page(params).await?;
        let title: String = page.evaluate("document.title").await?.into_value()?;
        assert_eq!(title, "Rust native integration");
        page.find_element("#button").await?.click().await?;
        let title: String = page.evaluate("document.title").await?.into_value()?;
        assert_eq!(title, "Clicked");
        let extension = owned.for_page(&page).await?;
        let repeated =
            futures_util::future::try_join_all((0..8).map(|_| owned.for_page(&page))).await?;
        assert!(repeated
            .iter()
            .all(|client| client.session_id == extension.session_id));
        extension.get_status(GetStatusParams::default()).await?;
        let effective = serde_json::to_value(
            extension
                .get_profile(GetProfileParams {
                    target_id: WireOptional::Value(page.target_id().as_ref().to_owned()),
                    ..Default::default()
                })
                .await?
                .profile,
        )?;
        assert_eq!(
            page.evaluate("navigator.userAgent")
                .await?
                .into_value::<String>()?,
            effective["identity"]["userAgent"]
        );
        owned.detach_page(&page).await?;
        assert!(matches!(
            extension.get_status(GetStatusParams::default()).await,
            Err(mimic_browser::Error::Closed(_))
        ));
        assert!(matches!(
            extension
                .experimental()
                .call("Mimic.getStatus", Some(json!({})))
                .await,
            Err(mimic_browser::Error::Closed(_))
        ));
        assert_eq!(
            page.evaluate("1+2").await?.into_value::<i64>()?,
            3,
            "SDK detach preserves the native session"
        );
        let extension = owned.for_page(&page).await?;
        assert_ne!(extension.session_id, repeated[0].session_id);
        let mut attached = Session::connect(&endpoint).await?;
        attached.close().await?;
        let dropped = Session::connect(&endpoint).await?;
        let dropped_extension = dropped.for_page(&page).await?;
        drop(dropped);
        assert!(matches!(
            dropped_extension
                .get_status(GetStatusParams::default())
                .await,
            Err(mimic_browser::Error::Closed(_))
        ));
        assert_eq!(
            page.evaluate("1+2").await?.into_value::<i64>()?,
            3,
            "attached disposal preserves shared runtime"
        );
        page.clone().close().await?;
        tokio::time::timeout(std::time::Duration::from_secs(2), async {
            loop {
                if matches!(
                    extension.get_status(GetStatusParams::default()).await,
                    Err(mimic_browser::Error::Closed(_))
                ) {
                    break;
                }
                tokio::task::yield_now().await;
            }
        })
        .await
        .expect("native Page close did not invalidate extension handle");
        owned.close().await?;
        assert!(matches!(
            owned.for_page(&page).await,
            Err(mimic_browser::Error::Closed(_))
        ));
        Ok::<(), mimic_browser::Error>(())
    })
    .await;
    fixture.abort();
    result.expect("native integration timed out").unwrap();
}
