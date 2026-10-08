# Shared runtime manager contract

This document is normative for all eight native implementations. It describes
artifact provenance and process ownership, not selectable versions of the RPC API.

## Selectors and trust

- Accept exact `vMAJOR.MINOR.PATCH[-beta.N]` or the same without `v`; normalize to `v`.
- Explicit version and explicit lock must agree. Otherwise select explicit lock,
  explicit version, `MIMIC_RUNTIME_VERSION`, then the packaged default lock.
- Binary location is explicit executable, `MIMIC_EXECUTABLE_PATH`, then verified
  cache. An invalid explicit path never falls through. Explicit selectors still
  apply to explicit executables; inspect runtime identity on connection.
- Default lock is `release/runtime-lock.json`, embedding the unchanged official
  manifest, its SHA256, official base URL and Chrome source provenance.
- Lock JSON: `{"release":"v0.2.2","manifestSha256":"...","manifest":{...},
  "manifestJson":"exact original UTF-8 manifest text",
  "baseUrl":"https://github.com/mimic-browser/runtime/releases/download/v0.2.2"}`.
  Hash manifestJson's UTF-8 bytes and require its parsed value equals manifest;
  do not hash a language-dependent reserialization of the nested manifest.
  Keep sourceRevision, packagingRevision, binaryVersion, archive/binary hashes,
  requirements and client references distinct; never rewrite a dev banner.
- An explicit other version resolves release-manifest.json and SHA256SUMS from
  its exact official GitHub release URL. Verify manifest digest against sums,
  manifest version, artifact name/size/hashes and source revision. No latest API.
- Preserve a resolved manifest lock at `<root>/.manifests/<release>.json` for
  offline explicit-version selection; reject conflicting cached provenance.
- `allowDownload=false` / `MIMIC_DOWNLOAD=0` forbids all installer network calls.
  Imports, object construction and connect never install or spawn.

## Platforms and storage

Only windows-amd64 and linux-amd64 (glibc >=2.39) are currently packaged. Reject
unsupported OS/architecture/libc before downloading. Use LOCALAPPDATA/Mimic/runtimes
on Windows, XDG_CACHE_HOME/Mimic/runtimes or ~/.cache/Mimic/runtimes on Linux;
`MIMIC_RUNTIME_DIR` or explicit runtime directory overrides that root.

Installation destination: `<root>/<release>/<platform>/<binarySha256>/`.
The executable is at destination root (`mimic.exe` or `mimic`); preserve the full
release tree and licenses. Flatten only the archive's single declared top-level
`mimic-<release>-<platform>` directory, if present. Reject absolute/traversal paths,
symlinks/hardlinks, duplicate entries and extracted paths escaping staging.

## Cross-language installation

1. Verify an existing destination's receipt and binary hash before reuse.
2. Acquire `<root>/.locks/<release>-<platform>.lock` using atomic directory creation.
   Write `owner.json` with `pid`, `hostname`, `token` (UUID), `createdAt` (UTC ISO8601).
   This is an install lock, never an execution/global browser-state lock.
3. Wait with bounded timeout/cancellation. Never remove an unknown/live lock merely
   because it is old. A crash-stale lock may be reclaimed only after proving the
   same-host owner process is absent; an ownerless lock requires explicit repair.
4. Recheck destination, then download to a unique sibling staging directory
   `<root>/.staging/<uuid>/`; verify archive length and SHA256 before extraction.
5. Verify executable SHA256, preserve license files, set executable permission on
   Linux, and write `installation.json` last, containing exactly the shared keys:
   `release`, `platform`, `sourceRevision`, `archiveSha256`, `binarySha256`,
   `executable` (basename), `manifestSha256`. Extra receipt fields are allowed.
6. Atomically rename the complete staged tree to destination. If a concurrent
   complete destination exists, verify it and discard staging. Never overwrite
   a binary in use. Clean only this install's staging and owned lock in finally.

Never launch partial installs. Bounded download timeouts, normal HTTPS/proxy/CA
settings, explicit integrity/platform/offline/permission/lock errors and no retries
of state-changing browser RPCs are required. Installer proxy is separate from
browser Context proxy. Lock/manifest input is strictly validated, not executable.

Cached release manifests are immutable too. Publish fully written temporary
bytes atomically without replacing an existing destination (for example a
same-filesystem hard link). If another installer won, validate and compare its
release and manifest digest; reuse identical provenance and reject conflicts.
Never expose partially written metadata or overwrite it via an unconditional
rename. Lock cleanup rechecks the original owner's random token before deleting
the owner record or directory. Cache filesystems must support atomic publication.

## Ownership and launch

Launch with redirected pipes, `--browser-mode headless --listen 127.0.0.1:0`.
Never enable shell execution, detached/background daemon mode or a visible Windows
console. The Windows runtime watches parent death. Published v0.2.2 lacks the
Unix parent-death watcher; qualify that lifecycle explicitly before selecting a
runtime that provides it rather than inferring Unix behavior from Windows.
Parse only the redirected startup
line `Mimic listening on http://127.0.0.1:<port>`; do not reserve/free ports.
Fetch `/json/version` to discover the browser websocket, then inspect
`Mimic.getVersion` and required command support. Report bounded output/exit details
on startup failure and clean up the owned child on timeout, cancellation, attach
failure or disposal. Gracefully request Browser.close for owned launches only,
then bounded termination. A started native process owns a lease record under
`<destination>/.leases/<uuid>.json` with launcher PID, runtime PID, hostname and
UTC createdAt; remove it after confirmed child exit. Never prune live leases.

Connect accepts explicit HTTP/WS endpoint, checks Mimic identity, and never sends
Browser.close on disposal. Preserve other clients. Framework Contexts created by
the SDK helper are owned and closed; borrowed framework drivers are not stopped.
Framework objects remain genuine, with framework errors/types/semantics intact.

Each integration establishes one explicit raw extension CDP connection/session
when needed: framework send APIs can discard protocol error code/data. Stable
typed and experimental calls share this transport, pending-request routing,
cancellation, timeout and closure. Experimental does not create another transport,
probe methods or gate commands on schema membership. Browser/page session scope
is explicit. Preserve raw JSON error code, message and data.

## Integration test execution

Run runtime/browser integration tests inside Linux or WSL, headless and loopback
only. Windows checks may compile and run
non-listening unit tests. Do not start a Windows server/test executable that might
trigger a firewall prompt, change firewall settings, or install system-wide tools.
Use the existing exact release archives for integration; reuse test binaries when
their source has not changed. Retain run-specific receipts and diagnostics in
ignored build output or CI artifacts.
