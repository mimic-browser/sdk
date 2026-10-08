#![cfg(all(feature = "chromiumoxide", target_os = "linux"))]
use chromiumoxide::cdp::browser_protocol::target::CreateTargetParams;
use mimic_browser::generated::{
    ConfigureContextParams, GetMediaSourcesParams, MediaConfiguration, WireOptional,
};
use mimic_browser::{chromiumoxide::Session, Error};
use serde_json::{json, Value};
use std::process::Stdio;
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, BufReader};

/// The explicitly supplied executable uses synthetic providers only. Never
/// substitute a platform's real camera/microphone provider in this test.
#[tokio::test]
async fn context_media_factory_separates_private_sources_from_public_identity() {
    let Ok(executable) = std::env::var("MIMIC_MEDIA_FIXTURE") else {
        return;
    };
    let mut child = tokio::process::Command::new(executable)
        .args(["--browser-mode", "headless", "--listen", "127.0.0.1:0"])
        .stdout(Stdio::piped())
        .stderr(Stdio::inherit())
        .kill_on_drop(true)
        .spawn()
        .unwrap();
    let result = tokio::time::timeout(Duration::from_secs(30), async {
        let mut lines = BufReader::new(child.stdout.take().unwrap()).lines();
        let mut endpoint = None;
        let mut fixture = None;
        while endpoint.is_none() || fixture.is_none() {
            let line = lines.next_line().await?.ok_or_else(|| {
                Error::Invalid("synthetic media fixture exited before readiness".into())
            })?;
            if let Some(value) = line.strip_prefix("Mimic listening on ") {
                endpoint = Some(value.to_owned());
            }
            if let Some(value) = line.strip_prefix("Fixture listening on ") {
                fixture = Some(value.to_owned());
            }
        }
        let fixture = fixture.unwrap();
        let mut session = Session::connect(&endpoint.unwrap()).await?;
        let before = session.mimic.call("Target.getBrowserContexts", None).await?;
        assert!(matches!(
            session
                .new_context_with_media(ConfigureContextParams::default(), |_| async {
                    Err(Error::Invalid("factory failure".into()))
                })
                .await,
            Err(Error::Invalid(message)) if message == "factory failure"
        ));
        assert_eq!(
            session.mimic.call("Target.getBrowserContexts", None).await?,
            before,
            "failed factory must dispose its new native Context"
        );
        assert!(session
            .new_context_with_media(
                ConfigureContextParams {
                    media: WireOptional::Null,
                    ..Default::default()
                },
                |_| async { panic!("ambiguous factory must not run") },
            )
            .await
            .is_err());
        let context = session
            .new_context_with_media(ConfigureContextParams::default(), |setup| async move {
                let sources = setup
                    .mimic
                    .get_media_sources(GetMediaSourcesParams {
                        browser_context_id: WireOptional::Value(setup.browser_context_id),
                    })
                    .await?;
                let source = |label: &str| {
                    let source = sources.sources.iter().find(|item| item.label == label).unwrap();
                    assert!(!source.source_id.contains("native"));
                    json!({"sourceId": source.source_id})
                };
                Ok(serde_json::from_value::<MediaConfiguration>(json!({
                    "seed": "rust-public-media",
                    "devices": [
                        {
                            "key": "front", "kind": "videoinput", "label": "Studio Camera", "group": "desk",
                            "source": source("Private native camera B"),
                            "modes": [{"width":16,"height":8,"frameRate":30}],
                            "defaultMode": {"width":16,"height":8,"frameRate":30},
                            "processing": {"resize":"crop-and-scale"}
                        },
                        {
                            "key":"voice", "kind":"audioinput", "label":"Studio Microphone", "group":"desk",
                            "source":source("Private native microphone B")
                        }
                    ]
                }))?)
            })
            .await?;
        session
            .mimic
            .call(
                "Browser.grantPermissions",
                Some(json!({
                    "browserContextId":context.as_ref(), "origin":fixture,
                    "permissions":["videoCapture","audioCapture"]
                })),
            )
            .await?;
        let page = session
            .browser
            .new_page(
                CreateTargetParams::builder()
                    .url(fixture.clone())
                    .browser_context_id(context)
                    .build()
                    .unwrap(),
            )
            .await?;
        session
            .for_page(&page)
            .await?
            .call(
                "Emulation.setDeviceMetricsOverride",
                Some(json!({"width":17,"height":19,"deviceScaleFactor":1,"mobile":false})),
            )
            .await?;
        let observed: Value = page
            .evaluate(
                r#"(async () => {
                  const devices = await navigator.mediaDevices.enumerateDevices();
                  const camera = devices.find(item => item.label === 'Studio Camera');
                  const microphone = devices.find(item => item.label === 'Studio Microphone');
                  const stream = await navigator.mediaDevices.getUserMedia({
                    video: {deviceId: {exact: camera.deviceId}},
                    audio: {deviceId: {exact: microphone.deviceId}}
                  });
                  const video = document.createElement('video');
                  video.srcObject = stream;
                  document.body.append(video);
                  await video.play();
                  await new Promise(resolve => video.requestVideoFrameCallback(resolve));
                  const canvas = document.createElement('canvas');
                  canvas.width = 16;
                  canvas.height = 8;
                  const draw = canvas.getContext('2d');
                  draw.drawImage(video, 0, 0, 16, 8);
                  const observation = {
                    devices: devices.map(item => item.toJSON()),
                    pixel: Array.from(draw.getImageData(0, 0, 1, 1).data),
                    tracks: stream.getTracks().map(track => ({
                      label: track.label, settings: track.getSettings()
                    }))
                  };
                  stream.getTracks().forEach(track => track.stop());
                  return observation;
                })()"#,
            )
            .await?
            .into_value()?;
        assert_eq!(observed["pixel"], json!([0, 0, 255, 255]));
        let devices = observed["devices"].as_array().unwrap();
        assert_eq!(devices.len(), 2);
        let camera = devices.iter().find(|item| item["label"] == "Studio Camera").unwrap();
        let microphone = devices.iter().find(|item| item["label"] == "Studio Microphone").unwrap();
        assert_eq!(camera["groupId"], microphone["groupId"]);
        assert_ne!(camera["deviceId"], microphone["deviceId"]);
        assert!(!observed.to_string().contains("Private native"));
        for track in observed["tracks"].as_array().unwrap() {
            let device = devices.iter().find(|device| device["label"] == track["label"]).unwrap();
            assert_eq!(track["settings"]["deviceId"], device["deviceId"]);
            assert_eq!(track["settings"]["groupId"], device["groupId"]);
        }
        page.close().await?;
        session.close().await?;
        Ok::<(), Error>(())
    })
    .await;
    let _ = child.kill().await;
    let _ = child.wait().await;
    result
        .expect("synthetic media integration timed out")
        .unwrap();
}
