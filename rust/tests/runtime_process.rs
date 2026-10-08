#![cfg(target_os = "linux")]
use mimic_browser::{Client, RuntimeManager, RuntimeOptions, Transport};

#[tokio::test]
async fn startup_timeout_and_future_cancellation_reap_owned_children() {
    use std::os::unix::fs::PermissionsExt;
    let directory = tempfile::tempdir().unwrap();
    let path = directory.path().join("pending-runtime");
    let pid_file = directory.path().join("pid");
    let source = format!("#!/usr/bin/python3\nimport os,time\nwith open({},'w') as output: output.write(str(os.getpid()))\ntime.sleep(60)\n",serde_json::to_string(pid_file.to_str().unwrap()).unwrap());
    std::fs::write(&path, source).unwrap();
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o700)).unwrap();
    let options = RuntimeOptions {
        executable_path: Some(path),
        allow_download: Some(false),
        startup_timeout: std::time::Duration::from_millis(250),
        ..Default::default()
    };
    let manager = RuntimeManager::new(options.clone()).unwrap();
    assert!(matches!(
        manager.launch().await,
        Err(mimic_browser::Error::Timeout(_))
    ));
    let pid: u32 = std::fs::read_to_string(&pid_file).unwrap().parse().unwrap();
    assert_eq!(unsafe { libc::kill(pid as libc::pid_t, 0) }, -1);
    std::fs::remove_file(&pid_file).unwrap();
    let task = tokio::spawn(async move {
        RuntimeManager::new(RuntimeOptions {
            startup_timeout: std::time::Duration::from_secs(30),
            ..options
        })
        .unwrap()
        .launch()
        .await
    });
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        while !pid_file.exists() {
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    })
    .await
    .unwrap();
    let pid: u32 = std::fs::read_to_string(&pid_file).unwrap().parse().unwrap();
    task.abort();
    let _ = task.await;
    tokio::time::timeout(std::time::Duration::from_secs(3), async {
        while unsafe { libc::kill(pid as libc::pid_t, 0) } == 0 {
            tokio::time::sleep(std::time::Duration::from_millis(10)).await;
        }
    })
    .await
    .expect("cancelled owned child was not reaped");
}

#[tokio::test]
async fn released_archive_offline_cache_and_owned_process_lease() {
    let Ok(archive) = std::env::var("MIMIC_SDK_TEST_ARCHIVE") else {
        return;
    };
    let temporary = tempfile::tempdir().unwrap();
    let cache = std::env::var_os("MIMIC_SDK_TEST_CACHE")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| temporary.path().join("runtimes"));
    let manager = RuntimeManager::new(RuntimeOptions {
        archive_path: Some(archive.into()),
        runtime_dir: Some(cache),
        allow_download: Some(false),
        ..Default::default()
    })
    .unwrap();
    let lock = manager.resolve_lock().await.unwrap();
    let expected = lock.validate().unwrap();
    let artifact = expected
        .artifacts
        .iter()
        .find(|item| item.platform == "linux-amd64")
        .unwrap();
    let installed = manager.install().await.unwrap();
    assert_eq!(installed.binary_sha256, artifact.binary_sha256);
    assert_eq!(manager.install().await.unwrap().path, installed.path);
    let mut process = manager.launch().await.unwrap();
    let pid = process.id().unwrap();
    let leases = installed.path.parent().unwrap().join(".leases");
    let owned_lease = std::fs::read_dir(&leases)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .find(|path| {
            let lease: serde_json::Value =
                serde_json::from_slice(&std::fs::read(path).unwrap()).unwrap();
            lease["runtimePid"] == pid
        })
        .expect("owned process lease");
    let identity = process.client.identity(Some(&lock.release)).await.unwrap();
    assert_eq!(identity.version, lock.release);
    let borrowed = Client::new(Transport::connect(&process.endpoint).await.unwrap());
    borrowed.transport.close().await;
    process.client.identity(Some(&lock.release)).await.unwrap();
    process.close().await.unwrap();
    assert!(
        !owned_lease.exists(),
        "lease removed only after confirmed process exit"
    );
    assert_eq!(
        unsafe { libc::kill(pid as libc::pid_t, 0) },
        -1,
        "owned process reaped"
    );
}
