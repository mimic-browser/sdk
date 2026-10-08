use crate::runtime::{
    file_hash, normalize_version, verify_installation, InstallGuard, Installation, RuntimeManager,
};
use crate::{Error, Result};
use serde_json::Value;
use std::path::Path;

impl RuntimeManager {
    /// Inspect an installed receipt and verify its executable bytes, without
    /// selecting a different release, downloading, or launching a process.
    pub fn verify(&self, directory: &Path) -> Result<Installation> {
        let mut installation: Installation =
            serde_json::from_slice(&std::fs::read(directory.join("installation.json"))?)?;
        let valid_hash = |value: &str| {
            value.len() == 64
                && value
                    .bytes()
                    .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        };
        if normalize_version(&installation.release)? != installation.release
            || !valid_hash(&installation.binary_sha256)
            || !valid_hash(&installation.archive_sha256)
            || !valid_hash(&installation.manifest_sha256)
            || installation.source_revision.len() != 40
            || !installation
                .source_revision
                .bytes()
                .all(|b| b.is_ascii_digit() || (b'a'..=b'f').contains(&b))
        {
            return Err(Error::Invalid(
                "Invalid installation receipt identity".into(),
            ));
        }
        let executable = match installation.platform.as_str() {
            "windows-amd64" => "mimic.exe",
            "linux-amd64" => "mimic",
            _ => return Err(Error::Invalid("Invalid installation platform".into())),
        };
        if installation.executable != executable {
            return Err(Error::Invalid("Invalid installation executable".into()));
        }
        installation.path = directory.join(executable);
        verify_installation(&installation)?;
        Ok(installation)
    }

    pub fn list(&self) -> Result<Vec<Installation>> {
        let root = self.root()?;
        if !root.exists() {
            return Ok(Vec::new());
        }
        let mut installed = Vec::new();
        for release in std::fs::read_dir(&root)? {
            let release = release?;
            if !release.file_type()?.is_dir()
                || !release.file_name().to_string_lossy().starts_with('v')
            {
                continue;
            }
            for platform in std::fs::read_dir(release.path())? {
                let platform = platform?;
                if !platform.file_type()?.is_dir() {
                    continue;
                }
                for artifact in std::fs::read_dir(platform.path())? {
                    let artifact = artifact?;
                    if artifact.file_type()?.is_dir()
                        && artifact.path().join("installation.json").exists()
                    {
                        installed.push(self.verify(&artifact.path())?);
                    }
                }
            }
        }
        installed.sort_by(|a, b| a.path.cmp(&b.path));
        Ok(installed)
    }

    /// Remove exactly one verified installation. Unknown, foreign or live
    /// process leases fail closed. No installed version is pruned implicitly.
    pub async fn prune(&self, installation: &Installation) -> Result<()> {
        let root = self.root()?.canonicalize()?;
        let directory = installation
            .path
            .parent()
            .ok_or_else(|| Error::Invalid("Invalid installation path".into()))?;
        let verified = self.verify(directory)?;
        if serde_json::to_value(&verified)? != serde_json::to_value(installation)? {
            return Err(Error::Invalid(
                "Prune installation identity mismatch".into(),
            ));
        }
        let expected = root
            .join(&verified.release)
            .join(&verified.platform)
            .join(&verified.binary_sha256);
        if directory.canonicalize()? != expected {
            return Err(Error::Invalid(
                "Prune target escapes root or traverses a symbolic link".into(),
            ));
        }
        let _guard = InstallGuard::acquire(
            &root,
            &format!("{}-{}", verified.release, verified.platform),
            self.options.lock_timeout,
        )
        .await?;
        verify_installation(&verified)?;
        let leases = directory.join(".leases");
        if leases.exists() {
            let host = hostname::get()?.to_string_lossy().into_owned();
            for entry in std::fs::read_dir(&leases)? {
                let entry = entry?;
                if !entry.file_type()?.is_file() {
                    return Err(Error::Invalid("Unknown lease entry blocks pruning".into()));
                }
                let lease: Value = serde_json::from_slice(&std::fs::read(entry.path())?)?;
                let pid = lease["runtimePid"]
                    .as_u64()
                    .filter(|&pid| pid > 0 && pid <= i32::MAX as u64)
                    .ok_or_else(|| Error::Invalid("Incomplete lease blocks pruning".into()))?
                    as u32;
                if lease["hostname"] != host {
                    return Err(Error::Invalid("Foreign lease blocks pruning".into()));
                }
                if process_alive(pid)? {
                    return Err(Error::Invalid("Live runtime lease blocks pruning".into()));
                }
            }
        }
        // Recheck bytes after inspecting leases, while still holding launch's lock.
        if file_hash(&verified.path)? != verified.binary_sha256 {
            return Err(Error::Invalid("Installation changed during prune".into()));
        }
        std::fs::remove_dir_all(directory)?;
        Ok(())
    }
}

#[cfg(unix)]
fn process_alive(pid: u32) -> Result<bool> {
    if unsafe { libc::kill(pid as libc::pid_t, 0) } == 0 {
        return Ok(true);
    }
    let error = std::io::Error::last_os_error();
    if error.raw_os_error() == Some(libc::ESRCH) {
        Ok(false)
    } else {
        Err(error.into())
    }
}

#[cfg(windows)]
fn process_alive(pid: u32) -> Result<bool> {
    use windows_sys::Win32::Foundation::{
        CloseHandle, GetLastError, ERROR_INVALID_PARAMETER, STILL_ACTIVE,
    };
    use windows_sys::Win32::System::Threading::{
        GetExitCodeProcess, OpenProcess, PROCESS_QUERY_LIMITED_INFORMATION,
    };
    unsafe {
        let handle = OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid);
        if handle.is_null() {
            return if GetLastError() == ERROR_INVALID_PARAMETER {
                Ok(false)
            } else {
                Err(std::io::Error::last_os_error().into())
            };
        }
        let mut status = 0;
        let success = GetExitCodeProcess(handle, &mut status);
        let error = std::io::Error::last_os_error();
        CloseHandle(handle);
        if success == 0 {
            Err(error.into())
        } else {
            Ok(status == STILL_ACTIVE as u32)
        }
    }
}
