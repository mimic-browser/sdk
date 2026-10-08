use crate::transport::{Client, Error, Result, Transport};
use chrono::Utc;
use futures_util::StreamExt;
use regex::Regex;
use serde::{Deserialize, Serialize};
use serde_json::{json, Value};
use sha2::{Digest, Sha256};
use std::collections::HashSet;
use std::io::{Read, Write};
use std::path::{Component, Path, PathBuf};
use std::process::Stdio;
use std::sync::Arc;
use std::time::{Duration, Instant};
use tokio::io::{AsyncBufReadExt, BufReader};
use tokio::process::{Child, Command};

const DEFAULT_LOCK: &str = include_str!("runtime-lock.json");
const MAX_ARCHIVE_SIZE: u64 = 1 << 30;

#[derive(Clone, Debug)]
pub struct RuntimeOptions {
    pub version: Option<String>,
    pub lock_file: Option<PathBuf>,
    pub executable_path: Option<PathBuf>,
    pub runtime_dir: Option<PathBuf>,
    pub archive_path: Option<PathBuf>,
    pub allow_download: Option<bool>,
    pub startup_timeout: Duration,
    pub lock_timeout: Duration,
}
impl Default for RuntimeOptions {
    fn default() -> Self {
        Self {
            version: None,
            lock_file: None,
            executable_path: None,
            runtime_dir: None,
            archive_path: None,
            allow_download: None,
            startup_timeout: Duration::from_secs(30),
            lock_timeout: Duration::from_secs(60),
        }
    }
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct RuntimeLock {
    pub release: String,
    pub manifest_sha256: String,
    pub manifest_json: String,
    pub manifest: Value,
    pub base_url: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Artifact {
    pub platform: String,
    pub archive: String,
    pub sha256: String,
    pub size: u64,
    pub binary_sha256: String,
    pub binary_version: String,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Manifest {
    pub version: String,
    pub source_revision: String,
    pub artifacts: Vec<Artifact>,
}
#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(rename_all = "camelCase")]
pub struct Installation {
    pub release: String,
    pub platform: String,
    pub source_revision: String,
    pub archive_sha256: String,
    pub binary_sha256: String,
    pub executable: String,
    pub manifest_sha256: String,
    #[serde(skip)]
    pub path: PathBuf,
}

pub fn normalize_version(value: &str) -> Result<String> {
    if !Regex::new(r"^v?\d+\.\d+\.\d+(?:-beta\.\d+)?$")
        .unwrap()
        .is_match(value)
    {
        return Err(Error::Invalid(format!(
            "Expected exact runtime release, got {value:?}"
        )));
    }
    Ok(format!("v{}", value.trim_start_matches('v')))
}
fn hash(bytes: &[u8]) -> String {
    format!("{:x}", Sha256::digest(bytes))
}
pub(crate) fn file_hash(path: &Path) -> Result<String> {
    let mut file = std::fs::File::open(path)?;
    let mut digest = Sha256::new();
    let mut buffer = [0; 65536];
    loop {
        let n = file.read(&mut buffer)?;
        if n == 0 {
            break;
        }
        digest.update(&buffer[..n]);
    }
    Ok(format!("{:x}", digest.finalize()))
}
impl RuntimeLock {
    pub fn validate(&self) -> Result<Manifest> {
        if normalize_version(&self.release)? != self.release
            || hash(self.manifest_json.as_bytes()) != self.manifest_sha256
        {
            return Err(Error::Invalid(
                "Runtime manifest digest/release mismatch".into(),
            ));
        }
        if serde_json::from_str::<Value>(&self.manifest_json)? != self.manifest {
            return Err(Error::Invalid(
                "Manifest differs from retained bytes".into(),
            ));
        }
        let manifest: Manifest = serde_json::from_str(&self.manifest_json)?;
        if manifest.version != self.release
            || !Regex::new("^[a-f0-9]{40}$")
                .unwrap()
                .is_match(&manifest.source_revision)
            || self.base_url
                != format!(
                    "https://github.com/mimic-browser/runtime/releases/download/{}",
                    self.release
                )
        {
            return Err(Error::Invalid("Invalid release provenance".into()));
        }
        let digest = Regex::new("^[a-f0-9]{64}$").unwrap();
        let mut platforms = HashSet::new();
        for artifact in &manifest.artifacts {
            let ext = match artifact.platform.as_str() {
                "windows-amd64" => ".zip",
                "linux-amd64" => ".tar.gz",
                _ => return Err(Error::Invalid("Unsupported manifest platform".into())),
            };
            if artifact.archive != format!("mimic-{}-{}{ext}", self.release, artifact.platform)
                || !digest.is_match(&artifact.sha256)
                || !digest.is_match(&artifact.binary_sha256)
                || artifact.size == 0
                || artifact.size > MAX_ARCHIVE_SIZE
                || artifact.binary_version != self.release
                || !platforms.insert(&artifact.platform)
            {
                return Err(Error::Invalid("Invalid artifact provenance".into()));
            }
        }
        if platforms.is_empty() {
            return Err(Error::Invalid("Empty runtime manifest".into()));
        }
        Ok(manifest)
    }
}

#[derive(Clone)]
pub struct RuntimeManager {
    pub options: RuntimeOptions,
    http: reqwest::Client,
}
impl RuntimeManager {
    pub fn new(options: RuntimeOptions) -> Result<Self> {
        Ok(Self {
            options,
            http: reqwest::Client::builder()
                .timeout(Duration::from_secs(120))
                .build()?,
        })
    }
    fn allow_download(&self) -> bool {
        self.options
            .allow_download
            .unwrap_or_else(|| std::env::var("MIMIC_DOWNLOAD").ok().as_deref() != Some("0"))
    }
    pub fn root(&self) -> Result<PathBuf> {
        if let Some(path) = self
            .options
            .runtime_dir
            .clone()
            .or_else(|| std::env::var_os("MIMIC_RUNTIME_DIR").map(PathBuf::from))
        {
            return Ok(path);
        }
        let base = if cfg!(windows) {
            std::env::var_os("LOCALAPPDATA").map(PathBuf::from)
        } else {
            std::env::var_os("XDG_CACHE_HOME")
                .map(PathBuf::from)
                .or_else(|| std::env::var_os("HOME").map(|home| PathBuf::from(home).join(".cache")))
        };
        base.map(|p| p.join("Mimic/runtimes"))
            .ok_or_else(|| Error::Invalid("No OS cache root; set MIMIC_RUNTIME_DIR".into()))
    }
    pub fn platform() -> Result<&'static str> {
        if std::env::consts::ARCH != "x86_64" {
            return Err(Error::Invalid(
                "Only amd64 runtime artifacts are packaged".into(),
            ));
        }
        if cfg!(windows) {
            return Ok("windows-amd64");
        }
        if cfg!(target_os = "linux") {
            #[cfg(all(target_os = "linux", target_env = "gnu"))]
            {
                let version = unsafe { std::ffi::CStr::from_ptr(libc::gnu_get_libc_version()) }
                    .to_string_lossy();
                let parts: Vec<_> = version
                    .split('.')
                    .filter_map(|p| p.parse::<u32>().ok())
                    .collect();
                if parts.first().copied().unwrap_or(0) > 2
                    || parts.first() == Some(&2) && parts.get(1).copied().unwrap_or(0) >= 39
                {
                    return Ok("linux-amd64");
                }
            }
            return Err(Error::Invalid("Linux runtime requires glibc 2.39+".into()));
        }
        Err(Error::Invalid(
            "No runtime artifact for this operating system".into(),
        ))
    }
    async fn download(&self, url: &str, limit: u64) -> Result<Vec<u8>> {
        if !self.allow_download() {
            return Err(Error::Invalid("Runtime downloads are disabled".into()));
        }
        let response = self.http.get(url).send().await?.error_for_status()?;
        if response.content_length().is_some_and(|n| n > limit) {
            return Err(Error::Invalid("Download exceeds declared bound".into()));
        }
        let mut body = Vec::new();
        let mut stream = response.bytes_stream();
        while let Some(chunk) = stream.next().await {
            let chunk = chunk?;
            if body.len() as u64 + chunk.len() as u64 > limit {
                return Err(Error::Invalid("Download exceeds declared bound".into()));
            }
            body.extend_from_slice(&chunk);
        }
        Ok(body)
    }
    pub async fn resolve_lock(&self) -> Result<RuntimeLock> {
        let default: RuntimeLock = serde_json::from_str(DEFAULT_LOCK)?;
        let selected = self.options.version.clone();
        let explicit = self
            .options
            .lock_file
            .as_ref()
            .map(std::fs::read_to_string)
            .transpose()?
            .map(|raw| serde_json::from_str::<RuntimeLock>(&raw))
            .transpose()?;
        let release = if let Some(version) = selected {
            normalize_version(&version)?
        } else if let Some(lock) = &explicit {
            lock.release.clone()
        } else if let Ok(version) = std::env::var("MIMIC_RUNTIME_VERSION") {
            normalize_version(&version)?
        } else {
            default.release.clone()
        };
        if let Some(lock) = explicit {
            lock.validate()?;
            if lock.release != release {
                return Err(Error::Invalid("Version and lock disagree".into()));
            }
            return Ok(lock);
        }
        if release == default.release {
            default.validate()?;
            return Ok(default);
        }
        let cache = self
            .root()?
            .join(".manifests")
            .join(format!("{release}.json"));
        if cache.exists() {
            let lock: RuntimeLock = serde_json::from_slice(&std::fs::read(cache)?)?;
            lock.validate()?;
            if lock.release != release {
                return Err(Error::Invalid("Cached manifest release mismatch".into()));
            }
            return Ok(lock);
        }
        let base_url =
            format!("https://github.com/mimic-browser/runtime/releases/download/{release}");
        let raw = self
            .download(&format!("{base_url}/release-manifest.json"), 4 << 20)
            .await?;
        let sums = self
            .download(&format!("{base_url}/SHA256SUMS"), 1 << 20)
            .await?;
        let manifest_sha256 = hash(&raw);
        let matched = String::from_utf8_lossy(&sums).lines().any(|line| {
            let words: Vec<_> = line.split_whitespace().collect();
            words.len() == 2
                && words[0] == manifest_sha256
                && words[1].trim_start_matches('*') == "release-manifest.json"
        });
        if !matched {
            return Err(Error::Invalid(
                "Manifest SHA256SUMS verification failed".into(),
            ));
        }
        let lock = RuntimeLock {
            release,
            manifest_sha256,
            manifest: serde_json::from_slice(&raw)?,
            manifest_json: String::from_utf8(raw).map_err(|e| Error::Invalid(e.to_string()))?,
            base_url,
        };
        lock.validate()?;
        self.cache_lock(&lock)?;
        Ok(lock)
    }
    fn cache_lock(&self, lock: &RuntimeLock) -> Result<()> {
        lock.validate()?;
        let cache = self
            .root()?
            .join(".manifests")
            .join(format!("{}.json", lock.release));
        std::fs::create_dir_all(cache.parent().unwrap())?;
        let mut temp = tempfile::NamedTempFile::new_in(cache.parent().unwrap())?;
        temp.write_all(&serde_json::to_vec(&lock)?)?;
        if let Err(error) = temp.persist_noclobber(&cache) {
            if error.error.kind() != std::io::ErrorKind::AlreadyExists {
                return Err(error.error.into());
            }
            let existing: RuntimeLock = serde_json::from_slice(&std::fs::read(&cache)?)?;
            existing.validate()?;
            if existing.manifest_sha256 != lock.manifest_sha256 {
                return Err(Error::Invalid(
                    "Conflicting immutable manifest cache".into(),
                ));
            }
        }
        Ok(())
    }
    pub async fn install(&self) -> Result<Installation> {
        let platform = Self::platform()?;
        let lock = self.resolve_lock().await?;
        let manifest = lock.validate()?;
        let artifact = manifest
            .artifacts
            .iter()
            .find(|a| a.platform == platform)
            .ok_or_else(|| Error::Invalid("Release has no artifact for platform".into()))?;
        self.cache_lock(&lock)?;
        let executable = if cfg!(windows) { "mimic.exe" } else { "mimic" };
        let root = self.root()?;
        let destination = root
            .join(&lock.release)
            .join(platform)
            .join(&artifact.binary_sha256);
        let expected = Installation {
            release: lock.release.clone(),
            platform: platform.into(),
            source_revision: manifest.source_revision,
            archive_sha256: artifact.sha256.clone(),
            binary_sha256: artifact.binary_sha256.clone(),
            executable: executable.into(),
            manifest_sha256: lock.manifest_sha256.clone(),
            path: destination.join(executable),
        };
        if destination.exists() {
            verify_installation(&expected)?;
            return Ok(expected);
        }
        let _guard = InstallGuard::acquire(
            &root,
            &format!("{}-{platform}", lock.release),
            self.options.lock_timeout,
        )
        .await?;
        if destination.exists() {
            verify_installation(&expected)?;
            return Ok(expected);
        }
        let archive = if let Some(path) = &self.options.archive_path {
            if std::fs::metadata(path)?.len() != artifact.size {
                return Err(Error::Invalid("Archive size verification failed".into()));
            }
            std::fs::read(path)?
        } else {
            self.download(
                &format!("{}/{}", lock.base_url, artifact.archive),
                artifact.size,
            )
            .await?
        };
        if archive.len() as u64 != artifact.size || hash(&archive) != artifact.sha256 {
            return Err(Error::Invalid(
                "Archive size/SHA256 verification failed".into(),
            ));
        }
        let staging_root = root.join(".staging");
        std::fs::create_dir_all(&staging_root)?;
        let staging = tempfile::Builder::new()
            .prefix("install-")
            .tempdir_in(&staging_root)?;
        extract(
            &archive,
            &artifact.archive,
            &format!("mimic-{}-{platform}", lock.release),
            staging.path(),
        )?;
        let binary = staging.path().join(executable);
        if file_hash(&binary)? != artifact.binary_sha256 {
            return Err(Error::Invalid("Binary SHA256 verification failed".into()));
        }
        #[cfg(unix)]
        {
            use std::os::unix::fs::PermissionsExt;
            std::fs::set_permissions(&binary, std::fs::Permissions::from_mode(0o755))?;
        }
        std::fs::write(
            staging.path().join("installation.json"),
            serde_json::to_vec_pretty(&expected)?,
        )?;
        std::fs::create_dir_all(destination.parent().unwrap())?;
        if let Err(error) = std::fs::rename(staging.path(), &destination) {
            if destination.exists() {
                verify_installation(&expected)?;
            } else {
                return Err(error.into());
            }
        }
        verify_installation(&expected)?;
        Ok(expected)
    }
    pub async fn launch(&self) -> Result<RuntimeProcess> {
        let platform = Self::platform()?;
        let explicit = self
            .options
            .executable_path
            .clone()
            .or_else(|| std::env::var_os("MIMIC_EXECUTABLE_PATH").map(PathBuf::from));
        let (path, version, installation) = if let Some(path) = explicit {
            if !path.is_file() {
                return Err(Error::Invalid(format!(
                    "Explicit runtime executable missing: {}",
                    path.display()
                )));
            }
            let explicit_version = if self.options.lock_file.is_some() {
                let lock = self.resolve_lock().await?;
                let manifest = lock.validate()?;
                let artifact = manifest
                    .artifacts
                    .iter()
                    .find(|artifact| artifact.platform == platform)
                    .ok_or_else(|| {
                        Error::Invalid("Explicit lock has no platform artifact".into())
                    })?;
                if file_hash(&path)? != artifact.binary_sha256 {
                    return Err(Error::Invalid(
                        "Explicit executable does not match lock hash".into(),
                    ));
                }
                Some(lock.release)
            } else if let Some(version) = &self.options.version {
                Some(normalize_version(version)?)
            } else {
                std::env::var("MIMIC_RUNTIME_VERSION")
                    .ok()
                    .map(|v| normalize_version(&v))
                    .transpose()?
            };
            (path, explicit_version, None)
        } else {
            let installed = self.install().await?;
            (
                installed.path.clone(),
                Some(installed.release.clone()),
                Some(installed),
            )
        };
        // Keep the shared prune lock until the process lease is durable.
        let _launch_guard = if let Some(installed) = &installation {
            let guard = InstallGuard::acquire(
                &self.root()?,
                &format!("{}-{}", installed.release, installed.platform),
                self.options.lock_timeout,
            )
            .await?;
            verify_installation(installed)?;
            Some(guard)
        } else {
            None
        };
        let mut command = Command::new(&path);
        command
            .args(["--browser-mode", "headless", "--listen", "127.0.0.1:0"])
            .stdin(Stdio::null())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        #[cfg(windows)]
        {
            command.creation_flags(0x08000000);
        }
        let deadline = tokio::time::Instant::now() + self.options.startup_timeout;
        let mut child = command.spawn()?;
        let pid = child
            .id()
            .ok_or_else(|| Error::Invalid("No child process identity".into()))?;
        let streams: Vec<(Box<dyn tokio::io::AsyncRead + Unpin + Send>, bool)> = vec![
            (Box::new(child.stdout.take().unwrap()), true),
            (Box::new(child.stderr.take().unwrap()), false),
        ];
        let (ready, mut announced) = tokio::sync::mpsc::channel(2);
        let diagnostics = Arc::new(std::sync::Mutex::new(String::new()));
        let mut readers = Vec::new();
        for (stream, startup_output) in streams {
            let tx = ready.clone();
            let diagnostic = diagnostics.clone();
            let mut lines = BufReader::new(stream).lines();
            readers.push(tokio::spawn(async move {
                while let Ok(Some(line)) = lines.next_line().await {
                    if let Some(endpoint) = line
                        .strip_prefix("Mimic listening on ")
                        .filter(|_| startup_output)
                    {
                        if Regex::new(r"^http://127\.0\.0\.1:[1-9][0-9]*$")
                            .unwrap()
                            .is_match(endpoint)
                        {
                            let _ = tx.try_send(endpoint.to_owned());
                        }
                    }
                    let mut text = diagnostic.lock().unwrap();
                    if text.len() < 8192 {
                        text.push_str(&line);
                        text.push('\n');
                    }
                }
            }));
        }
        drop(ready);
        let endpoint = match tokio::time::timeout_at(deadline, announced.recv()).await {
            Ok(Some(endpoint)) => endpoint,
            _ => {
                let _ = child.kill().await;
                for reader in readers {
                    reader.abort();
                }
                return Err(Error::Timeout(format!(
                    "runtime startup: {}",
                    diagnostics.lock().unwrap()
                )));
            }
        };
        let connection = tokio::time::timeout_at(deadline, Transport::connect(&endpoint))
            .await
            .map_err(|_| Error::Timeout("runtime discovery".into()))
            .and_then(|result| result);
        let transport = match connection {
            Ok(t) => t,
            Err(error) => {
                let _ = child.kill().await;
                for reader in readers {
                    reader.abort();
                }
                return Err(error);
            }
        };
        let client = Client::new(transport.clone());
        let identity = tokio::time::timeout_at(deadline, client.identity(version.as_deref()))
            .await
            .map_err(|_| Error::Timeout("runtime identity".into()))
            .and_then(|result| result);
        if let Err(error) = identity {
            transport.close().await;
            let _ = child.kill().await;
            for reader in readers {
                reader.abort();
            }
            return Err(error);
        }
        let lease = if let Some(installed) = &installation {
            let directory = installed.path.parent().unwrap().join(".leases");
            std::fs::create_dir_all(&directory)?;
            let path = directory.join(format!("{}.json", uuid::Uuid::new_v4()));
            std::fs::write(
                &path,
                serde_json::to_vec(
                    &json!({"launcherPid":std::process::id(),"runtimePid":pid,"hostname":hostname::get()?.to_string_lossy(),"createdAt":Utc::now().to_rfc3339()}),
                )?,
            )?;
            Some(path)
        } else {
            None
        };
        Ok(RuntimeProcess {
            child: Some(child),
            endpoint,
            client,
            installation,
            lease,
            readers,
        })
    }
}

pub(crate) fn verify_installation(expected: &Installation) -> Result<()> {
    let parent = expected
        .path
        .parent()
        .ok_or_else(|| Error::Invalid("Invalid installation path".into()))?;
    let actual: Installation =
        serde_json::from_slice(&std::fs::read(parent.join("installation.json"))?)?;
    if !std::fs::symlink_metadata(&expected.path)?
        .file_type()
        .is_file()
        || serde_json::to_value(&actual)? != serde_json::to_value(expected)?
        || file_hash(&expected.path)? != expected.binary_sha256
    {
        return Err(Error::Invalid(
            "Installed runtime receipt/binary integrity mismatch".into(),
        ));
    }
    Ok(())
}
pub(crate) struct InstallGuard {
    path: PathBuf,
    token: String,
}
impl InstallGuard {
    pub(crate) async fn acquire(root: &Path, name: &str, timeout: Duration) -> Result<Self> {
        let locks = root.join(".locks");
        std::fs::create_dir_all(&locks)?;
        let path = locks.join(format!("{name}.lock"));
        let started = Instant::now();
        loop {
            match std::fs::create_dir(&path) {
                Ok(()) => {
                    let guard = Self {
                        path,
                        token: uuid::Uuid::new_v4().to_string(),
                    };
                    let owner = (|| -> Result<()> {
                        let bytes = serde_json::to_vec(
                            &json!({"pid":std::process::id(),"hostname":hostname::get()?.to_string_lossy(),"token":guard.token,"createdAt":Utc::now().to_rfc3339()}),
                        )?;
                        std::fs::write(guard.path.join("owner.json"), bytes)?;
                        Ok(())
                    })();
                    if let Err(error) = owner {
                        let _ = std::fs::remove_file(guard.path.join("owner.json"));
                        let _ = std::fs::remove_dir(&guard.path);
                        return Err(error);
                    }
                    return Ok(guard);
                }
                Err(error) if error.kind() == std::io::ErrorKind::AlreadyExists => {
                    if started.elapsed() >= timeout {
                        return Err(Error::Timeout(
                            "install lock (live/unknown owners are never evicted by age)".into(),
                        ));
                    }
                    tokio::time::sleep(Duration::from_millis(100)).await;
                }
                Err(error) => return Err(error.into()),
            }
        }
    }
}
impl Drop for InstallGuard {
    fn drop(&mut self) {
        if let Ok(raw) = std::fs::read(self.path.join("owner.json")) {
            if let Ok(owner) = serde_json::from_slice::<Value>(&raw) {
                if owner["token"] == self.token {
                    let _ = std::fs::remove_file(self.path.join("owner.json"));
                    let _ = std::fs::remove_dir(&self.path);
                }
            }
        }
    }
}
fn archive_path(raw: &str, prefix: &str) -> Result<PathBuf> {
    if raw.contains('\\') || raw.contains(':') || raw.starts_with('/') {
        return Err(Error::Invalid("Unsafe archive path".into()));
    }
    let raw = raw.strip_prefix(&format!("{prefix}/")).unwrap_or(raw);
    if raw == prefix || raw.is_empty() {
        return Ok(PathBuf::new());
    }
    let path = PathBuf::from(raw);
    if path
        .components()
        .any(|c| !matches!(c, Component::Normal(_)))
    {
        return Err(Error::Invalid("Archive traversal rejected".into()));
    }
    Ok(path)
}
fn extract(bytes: &[u8], name: &str, prefix: &str, directory: &Path) -> Result<()> {
    let mut seen = HashSet::new();
    let mut total = 0u64;
    let mut copy = |raw: &str, is_dir: bool, size: u64, source: &mut dyn Read| -> Result<()> {
        let path = archive_path(raw, prefix)?;
        if !seen.insert(path.clone()) {
            return Err(Error::Invalid("Duplicate archive entry".into()));
        }
        if path.as_os_str().is_empty() {
            if !is_dir {
                return Err(Error::Invalid("Archive root must be a directory".into()));
            }
            return Ok(());
        }
        total = total
            .checked_add(size)
            .ok_or_else(|| Error::Invalid("Archive expansion overflow".into()))?;
        if total > 4 * MAX_ARCHIVE_SIZE {
            return Err(Error::Invalid("Archive expansion exceeds limit".into()));
        }
        let path = directory.join(path);
        if is_dir {
            std::fs::create_dir_all(path)?;
        } else {
            std::fs::create_dir_all(path.parent().unwrap())?;
            let mut file = std::fs::OpenOptions::new()
                .create_new(true)
                .write(true)
                .open(path)?;
            let count = std::io::copy(&mut source.take(size + 1), &mut file)?;
            if count != size {
                return Err(Error::Invalid("Archive entry length mismatch".into()));
            }
        }
        Ok(())
    };
    if name.ends_with(".zip") {
        let mut archive = zip::ZipArchive::new(std::io::Cursor::new(bytes))
            .map_err(|e| Error::Invalid(e.to_string()))?;
        for index in 0..archive.len() {
            let mut entry = archive
                .by_index(index)
                .map_err(|e| Error::Invalid(e.to_string()))?;
            if entry
                .unix_mode()
                .is_some_and(|mode| !matches!(mode & 0o170000, 0 | 0o100000 | 0o040000))
            {
                return Err(Error::Invalid(
                    "Archive links/special entries rejected".into(),
                ));
            }
            let name = entry.name().to_owned();
            copy(&name, entry.is_dir(), entry.size(), &mut entry)?;
        }
    } else {
        let mut archive = tar::Archive::new(flate2::read::GzDecoder::new(bytes));
        for entry in archive.entries()? {
            let mut entry = entry?;
            let kind = entry.header().entry_type();
            if !kind.is_file() && !kind.is_dir() {
                return Err(Error::Invalid(
                    "Archive links/special entries rejected".into(),
                ));
            }
            let name = entry.path()?.to_string_lossy().into_owned();
            copy(&name, kind.is_dir(), entry.size(), &mut entry)?;
        }
    }
    Ok(())
}

pub struct RuntimeProcess {
    child: Option<Child>,
    pub endpoint: String,
    pub client: Client,
    pub installation: Option<Installation>,
    lease: Option<PathBuf>,
    readers: Vec<tokio::task::JoinHandle<()>>,
}
impl RuntimeProcess {
    pub fn id(&self) -> Option<u32> {
        self.child.as_ref().and_then(Child::id)
    }
    pub async fn close(&mut self) -> Result<()> {
        if let Some(mut child) = self.child.take() {
            let _ = self
                .client
                .transport
                .call(
                    "Browser.close",
                    Some(json!({})),
                    None,
                    Duration::from_secs(2),
                )
                .await;
            self.client.transport.close().await;
            match tokio::time::timeout(Duration::from_secs(3), child.wait()).await {
                Ok(result) => {
                    result?;
                }
                Err(_) => {
                    child.kill().await?;
                    child.wait().await?;
                }
            }
            if let Some(path) = self.lease.take() {
                std::fs::remove_file(path)?;
            }
        }
        for reader in self.readers.drain(..) {
            reader.abort();
        }
        Ok(())
    }
}
impl Drop for RuntimeProcess {
    fn drop(&mut self) {
        if let Some(mut child) = self.child.take() {
            if let Ok(runtime) = tokio::runtime::Handle::try_current() {
                let lease = self.lease.take();
                runtime.spawn(async move {
                    if child.kill().await.is_ok() && child.wait().await.is_ok() {
                        if let Some(path) = lease {
                            let _ = std::fs::remove_file(path);
                        }
                    }
                });
            } else {
                let _ = child.start_kill();
                // Without a running executor, preserve the lease because exit
                // cannot be confirmed synchronously from Drop.
            }
        }
        for reader in &self.readers {
            reader.abort();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn default_lock_provenance() {
        let lock: RuntimeLock = serde_json::from_str(DEFAULT_LOCK).unwrap();
        assert_eq!(lock.validate().unwrap().version, lock.release);
        let mut invalid = lock.clone();
        invalid.manifest_json.push(' ');
        assert!(invalid.validate().is_err());
    }
    #[test]
    fn exact_versions_only() {
        for value in ["latest", "main", "^0.2.2", "../v0.2.2"] {
            assert!(normalize_version(value).is_err());
        }
        assert_eq!(normalize_version("0.2.2").unwrap(), "v0.2.2");
    }
    #[test]
    fn traversal_and_links_are_rejected() {
        for value in [
            "../outside",
            "/absolute",
            "C:/outside",
            "safe/../../outside",
            "safe\\outside",
        ] {
            assert!(archive_path(value, "bundle").is_err());
        }
        assert_eq!(
            archive_path("bundle/docs/a.md", "bundle").unwrap(),
            PathBuf::from("docs/a.md")
        );
    }
    #[tokio::test]
    async fn offline_explicit_version_never_fetches() {
        let directory = tempfile::tempdir().unwrap();
        let manager = RuntimeManager::new(RuntimeOptions {
            version: Some("v123.0.0".into()),
            runtime_dir: Some(directory.path().into()),
            allow_download: Some(false),
            ..Default::default()
        })
        .unwrap();
        assert!(manager
            .resolve_lock()
            .await
            .unwrap_err()
            .to_string()
            .contains("disabled"));
    }
}
