#![cfg(target_os = "linux")]
use mimic_sdk::runtime::RuntimeLock;
use mimic_sdk::{RuntimeManager, RuntimeOptions};
use serde_json::json;
use sha2::{Digest, Sha256};
use std::io::Write;
use std::time::Duration;

fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}

fn fixture(directory: &std::path::Path) -> RuntimeOptions {
    let binary = b"fixture executable bytes";
    let mut archive = tar::Builder::new(Vec::new());
    for (name, body) in [
        ("mimic", binary.as_slice()),
        ("LICENSE", b"retained license".as_slice()),
    ] {
        let mut header = tar::Header::new_gnu();
        header.set_size(body.len() as u64);
        header.set_mode(0o644);
        header.set_cksum();
        archive
            .append_data(
                &mut header,
                format!("mimic-v999.0.0-linux-amd64/{name}"),
                body,
            )
            .unwrap();
    }
    let mut gzip = flate2::write::GzEncoder::new(Vec::new(), flate2::Compression::default());
    gzip.write_all(&archive.into_inner().unwrap()).unwrap();
    let bytes = gzip.finish().unwrap();
    let manifest = json!({"version":"v999.0.0","sourceRevision":"0123456789012345678901234567890123456789","artifacts":[{
        "platform":"linux-amd64","archive":"mimic-v999.0.0-linux-amd64.tar.gz","sha256":hash(&bytes),"size":bytes.len(),"binarySha256":hash(binary),"binaryVersion":"v999.0.0"
    }]});
    let raw = manifest.to_string();
    let lock = RuntimeLock {
        release: "v999.0.0".into(),
        manifest_sha256: hash(raw.as_bytes()),
        manifest_json: raw,
        manifest,
        base_url: "https://github.com/mimic-browser/runtime/releases/download/v999.0.0".into(),
    };
    let lock_path = directory.join("runtime-lock.json");
    let archive_path = directory.join("archive.tar.gz");
    std::fs::write(&lock_path, serde_json::to_vec(&lock).unwrap()).unwrap();
    std::fs::write(&archive_path, bytes).unwrap();
    RuntimeOptions {
        lock_file: Some(lock_path),
        archive_path: Some(archive_path),
        runtime_dir: Some(directory.join("runtimes")),
        allow_download: Some(false),
        lock_timeout: Duration::from_millis(40),
        ..Default::default()
    }
}

#[tokio::test]
async fn verified_offline_install_reuse_receipt_and_integrity() {
    let directory = tempfile::tempdir().unwrap();
    let options = fixture(directory.path());
    let manager = RuntimeManager::new(options.clone()).unwrap();
    let (first, second) = tokio::join!(manager.install(), manager.install());
    let first = first.unwrap();
    assert_eq!(first.path, second.unwrap().path);
    assert_eq!(
        std::fs::read_to_string(first.path.parent().unwrap().join("LICENSE")).unwrap(),
        "retained license"
    );
    let receipt: serde_json::Value = serde_json::from_slice(
        &std::fs::read(first.path.parent().unwrap().join("installation.json")).unwrap(),
    )
    .unwrap();
    assert_eq!(receipt["executable"], "mimic");
    assert_eq!(receipt["release"], "v999.0.0");
    assert_eq!(receipt.as_object().unwrap().len(), 7);
    let offline = RuntimeManager::new(RuntimeOptions {
        version: Some("v999.0.0".into()),
        runtime_dir: options.runtime_dir.clone(),
        allow_download: Some(false),
        ..Default::default()
    })
    .unwrap();
    assert_eq!(offline.resolve_lock().await.unwrap().release, "v999.0.0");
    std::fs::remove_file(options.archive_path.unwrap()).unwrap();
    assert_eq!(
        manager.install().await.unwrap().path,
        first.path,
        "offline cache does not need original archive"
    );
    std::fs::write(first.path, b"tampered").unwrap();
    assert!(manager
        .install()
        .await
        .unwrap_err()
        .to_string()
        .contains("integrity"));
}

#[tokio::test]
async fn selectors_corruption_and_unknown_locks_fail_closed() {
    let directory = tempfile::tempdir().unwrap();
    let mut options = fixture(directory.path());
    options.version = Some("v999.0.1".into());
    assert!(RuntimeManager::new(options.clone())
        .unwrap()
        .resolve_lock()
        .await
        .unwrap_err()
        .to_string()
        .contains("disagree"));
    options.version = None;
    let path = options
        .runtime_dir
        .as_ref()
        .unwrap()
        .join(".locks/v999.0.0-linux-amd64.lock");
    std::fs::create_dir_all(&path).unwrap();
    assert!(RuntimeManager::new(options.clone())
        .unwrap()
        .install()
        .await
        .unwrap_err()
        .to_string()
        .contains("lock"));
    assert!(path.exists(), "unknown lock is never evicted by age");
    std::fs::remove_dir(&path).unwrap();
    let archive = options.archive_path.as_ref().unwrap();
    let mut bytes = std::fs::read(archive).unwrap();
    bytes[0] ^= 1;
    std::fs::write(archive, bytes).unwrap();
    assert!(RuntimeManager::new(options.clone())
        .unwrap()
        .install()
        .await
        .unwrap_err()
        .to_string()
        .contains("SHA256"));
    assert!(
        !path.exists(),
        "only the failed install's lock is cleaned up"
    );
    options.executable_path = Some(directory.path().join("absent"));
    assert!(RuntimeManager::new(options)
        .unwrap()
        .launch()
        .await
        .err()
        .unwrap()
        .to_string()
        .contains("Explicit runtime executable missing"));
}

#[tokio::test]
async fn inspect_and_prune_reject_live_leases_and_escaped_targets() {
    let directory = tempfile::tempdir().unwrap();
    let manager = RuntimeManager::new(fixture(directory.path())).unwrap();
    let installed = manager.install().await.unwrap();
    assert_eq!(manager.list().unwrap().len(), 1);
    assert_eq!(
        manager
            .verify(installed.path.parent().unwrap())
            .unwrap()
            .binary_sha256,
        installed.binary_sha256
    );
    let leases = installed.path.parent().unwrap().join(".leases");
    std::fs::create_dir(&leases).unwrap();
    let lease = leases.join("owner.json");
    std::fs::write(&lease,serde_json::to_vec(&json!({"hostname":hostname::get().unwrap().to_string_lossy(),"runtimePid":std::process::id()})).unwrap()).unwrap();
    assert!(manager
        .prune(&installed)
        .await
        .unwrap_err()
        .to_string()
        .contains("Live"));
    std::fs::write(&lease, b"{}").unwrap();
    assert!(manager
        .prune(&installed)
        .await
        .unwrap_err()
        .to_string()
        .contains("Incomplete"));
    std::fs::remove_file(&lease).unwrap();
    let outside = tempfile::tempdir().unwrap();
    let mut escaped = installed.clone();
    std::fs::copy(&installed.path, outside.path().join("mimic")).unwrap();
    std::fs::copy(
        installed.path.parent().unwrap().join("installation.json"),
        outside.path().join("installation.json"),
    )
    .unwrap();
    escaped.path = outside.path().join("mimic");
    assert!(manager
        .prune(&escaped)
        .await
        .unwrap_err()
        .to_string()
        .contains("escapes"));
    manager.prune(&installed).await.unwrap();
    assert!(manager.list().unwrap().is_empty());
    assert!(escaped.path.exists());
}

#[tokio::test]
async fn explicit_lock_verifies_executable_before_spawn() {
    let directory = tempfile::tempdir().unwrap();
    let mut options = fixture(directory.path());
    let binary = directory.path().join("must-not-start");
    std::fs::write(&binary, b"different binary bytes").unwrap();
    options.executable_path = Some(binary);
    assert!(RuntimeManager::new(options)
        .unwrap()
        .launch()
        .await
        .err()
        .unwrap()
        .to_string()
        .contains("lock hash"));
}
