"""Language-native installation, exact pins and owned runtime processes."""
from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import queue
import re
import shutil
import socket
import stat
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.request
import uuid
import zipfile
from datetime import datetime, timezone

from .protocol import CDPConnection

BASE = "https://github.com/mimic-browser/runtime/releases/download"
SHA = re.compile(r"[0-9a-f]{64}\Z")


class RuntimeError(Exception):
    """An explicit artifact, platform, install or process ownership failure."""


def normalize_version(value):
    if not isinstance(value, str) or not re.fullmatch(r"v?\d+\.\d+\.\d+(?:-beta\.\d+)?", value):
        raise RuntimeError("Runtime version must be exact (for example v0.2.2)")
    return value if value.startswith("v") else "v" + value


def current_platform():
    system, machine = platform.system(), platform.machine().lower()
    if machine not in ("amd64", "x86_64") or system not in ("Windows", "Linux"):
        raise RuntimeError(f"No Mimic release for {system}/{machine}")
    if system == "Linux":
        libc, version = platform.libc_ver()
        if libc != "glibc" or tuple(map(int, version.split(".")[:2])) < (2, 39):
            raise RuntimeError("Mimic Linux requires glibc 2.39 or newer")
    return system.lower() + "-amd64"


def digest(path):
    with open(path, "rb") as stream:
        result = hashlib.sha256()
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            result.update(block)
        return result.hexdigest()


def stamp():
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def process_alive(pid):
    if not isinstance(pid, int) or pid <= 0:
        return True
    if os.name == "nt":
        import ctypes
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        handle = kernel.OpenProcess(0x1000, False, pid)
        if handle:
            kernel.CloseHandle(ctypes.c_void_p(handle))
            return True
        return ctypes.get_last_error() != 87
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def write_json(path, value):
    Path(path).write_text(json.dumps(value, indent=2) + "\n", encoding="utf-8")


def validate_lock(lock):
    if not isinstance(lock, dict):
        raise RuntimeError("Expected a runtime lock object")
    release = normalize_version(lock.get("release"))
    manifest = lock.get("manifest", {})
    if lock.get("baseUrl") != f"{BASE}/{release}":
        raise RuntimeError("Runtime lock must use its exact official release URL")
    raw = lock.get("manifestJson")
    if not isinstance(raw, str) or json.loads(raw) != manifest:
        raise RuntimeError("Runtime lock must preserve the exact published manifest JSON")
    encoded = raw.encode("utf-8")
    if hashlib.sha256(encoded).hexdigest() != lock.get("manifestSha256"):
        raise RuntimeError("Runtime lock manifest SHA256 mismatch")
    if manifest.get("version") != release:
        raise RuntimeError("Runtime manifest version mismatch")
    for key in ("sourceRevision", "packagingRevision"):
        if not re.fullmatch(r"[0-9a-f]{40}", manifest.get(key, "")):
            raise RuntimeError("Invalid runtime source provenance")
    platforms = set()
    for item in manifest.get("artifacts", []):
        host = item.get("platform")
        if host not in ("windows-amd64", "linux-amd64") or host in platforms:
            raise RuntimeError("Invalid or duplicate manifest platform")
        platforms.add(host)
        suffix = ".zip" if host.startswith("windows") else ".tar.gz"
        if item.get("archive") != f"mimic-{release}-{host}{suffix}":
            raise RuntimeError("Unexpected runtime archive name")
        if item.get("binaryVersion") != release or not isinstance(item.get("size"), int) or item["size"] <= 0:
            raise RuntimeError("Invalid runtime artifact identity")
        if any(not SHA.fullmatch(item.get(key, "")) for key in ("sha256", "binarySha256")):
            raise RuntimeError("Invalid runtime artifact SHA256")
    if not platforms:
        raise RuntimeError("Runtime manifest contains no platform artifacts")
    return lock


def extract_archive(archive, destination, root):
    seen = set()
    total = 0
    def target(name, size):
        nonlocal total
        if "\\" in name or ":" in name or name.startswith("/"):
            raise RuntimeError("Unsafe archive path")
        parts = PurePosixPath(name).parts
        if not parts or parts[0] != root or ".." in parts or name in seen:
            raise RuntimeError("Invalid or duplicate archive entry")
        seen.add(name)
        total += size
        if total > 2 * 1024**3:
            raise RuntimeError("Runtime archive exceeds extraction limit")
        return destination.joinpath(*parts[1:])
    if str(archive).endswith(".zip"):
        with zipfile.ZipFile(archive) as packed:
            for entry in packed.infolist():
                mode = entry.external_attr >> 16
                if stat.S_ISLNK(mode) or (stat.S_IFMT(mode) not in (0, stat.S_IFREG, stat.S_IFDIR)):
                    raise RuntimeError("Archive links and special files are unsupported")
                path = target(entry.filename.rstrip("/"), entry.file_size)
                if entry.is_dir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with packed.open(entry) as source, path.open("xb") as output:
                        shutil.copyfileobj(source, output)
    else:
        with tarfile.open(archive, "r:gz") as packed:
            for entry in packed:
                if not (entry.isfile() or entry.isdir()):
                    raise RuntimeError("Archive links and special files are unsupported")
                path = target(entry.name.rstrip("/"), entry.size)
                if entry.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with packed.extractfile(entry) as source, path.open("xb") as output:
                        shutil.copyfileobj(source, output)


class RuntimeManager:
    def __init__(self, *, runtime_version=None, lock=None, executable_path=None,
                 runtime_dir=None, allow_download=None, timeout=60):
        self.timeout = timeout
        self.allow_download = os.getenv("MIMIC_DOWNLOAD") != "0" if allow_download is None else allow_download
        self.executable_path = executable_path or os.getenv("MIMIC_EXECUTABLE_PATH")
        default_root = (Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
                        if os.name == "nt" else Path(os.getenv("XDG_CACHE_HOME", Path.home() / ".cache")))
        self.root = Path(runtime_dir or os.getenv("MIMIC_RUNTIME_DIR") or default_root / "Mimic/runtimes").absolute()
        if isinstance(lock, (str, Path)):
            lock = json.loads(Path(lock).read_text(encoding="utf-8"))
        self._explicit_lock = validate_lock(lock) if lock is not None else None
        self._explicit_version = runtime_version or (lock["release"] if lock is not None else os.getenv("MIMIC_RUNTIME_VERSION"))
        default = validate_lock(json.loads(Path(__file__).with_name("runtime-lock.json").read_text()))
        selected = runtime_version or (lock["release"] if lock is not None else os.getenv("MIMIC_RUNTIME_VERSION")) or default["release"]
        self.version = normalize_version(selected)
        if lock is not None and self.version != lock["release"]:
            raise RuntimeError("Explicit runtime version conflicts with lock")
        self._lock = lock if lock is not None else default if self.version == default["release"] else None

    def _download(self, url, destination):
        if not self.allow_download:
            raise RuntimeError("Runtime download disabled; preinstall this exact release")
        with urllib.request.urlopen(url, timeout=self.timeout) as source, Path(destination).open("wb") as output:
            shutil.copyfileobj(source, output)

    def resolve_lock(self):
        if self._lock is not None:
            return self._lock
        path = self.root / ".manifests" / (self.version + ".json")
        if path.exists():
            self._lock = validate_lock(json.loads(path.read_text()))
            if self._lock["release"] != self.version:
                raise RuntimeError("Cached manifest version conflict")
            return self._lock
        if not self.allow_download:
            raise RuntimeError("Exact runtime manifest unavailable offline")
        with tempfile.TemporaryDirectory(prefix="mimic-manifest-") as temporary:
            base = f"{BASE}/{self.version}"
            manifest, sums = Path(temporary) / "manifest.json", Path(temporary) / "sums"
            self._download(base + "/release-manifest.json", manifest)
            self._download(base + "/SHA256SUMS", sums)
            expected = [line.split()[0] for line in sums.read_text().splitlines()
                        if len(line.split()) == 2 and line.split()[1] == "release-manifest.json"]
            if expected != [digest(manifest)]:
                raise RuntimeError("Published manifest SHA256 mismatch")
            # read_text() performs universal-newline conversion. The lock must
            # retain the exact original UTF-8 bytes covered by SHA256SUMS.
            manifest_json = manifest.read_bytes().decode("utf-8")
            self._lock = validate_lock({"release": self.version, "manifestSha256": expected[0], "manifestJson": manifest_json,
                                       "manifest": json.loads(manifest_json), "baseUrl": base})
        return self._lock

    @contextlib.contextmanager
    def _install_lock(self, host):
        path = self.root / ".locks" / f"{self.version}-{host}.lock"
        path.parent.mkdir(parents=True, exist_ok=True)
        deadline, token = time.monotonic() + self.timeout, str(uuid.uuid4())
        while True:
            try:
                path.mkdir()
                break
            except FileExistsError:
                if time.monotonic() >= deadline:
                    raise RuntimeError(f"Installation lock timed out: {path}; inspect owner.json before repair")
                time.sleep(.05)
        try:
            write_json(path / "owner.json", {"pid": os.getpid(), "hostname": socket.gethostname(),
                                             "token": token, "createdAt": stamp()})
            yield
        finally:
            try:
                if json.loads((path / "owner.json").read_text())["token"] == token:
                    (path / "owner.json").unlink()
                    path.rmdir()
            except FileNotFoundError:
                pass

    def _identity(self):
        host = current_platform()
        lock = self.resolve_lock()
        matches = [item for item in lock["manifest"]["artifacts"] if item["platform"] == host]
        if len(matches) != 1:
            raise RuntimeError(f"No release artifact for {host}")
        artifact = matches[0]
        destination = self.root / self.version / host / artifact["binarySha256"]
        executable = "mimic.exe" if host.startswith("windows") else "mimic"
        receipt = {"release": self.version, "platform": host, "sourceRevision": lock["manifest"]["sourceRevision"],
                   "archiveSha256": artifact["sha256"], "binarySha256": artifact["binarySha256"],
                   "executable": executable, "manifestSha256": lock["manifestSha256"]}
        return lock, artifact, destination, receipt

    @staticmethod
    def _verify(destination, receipt):
        try:
            actual = json.loads((destination / "installation.json").read_text())
            if any(actual.get(key) != value for key, value in receipt.items()):
                raise RuntimeError("Installed runtime receipt conflicts with exact pin")
            executable = destination / receipt["executable"]
            if executable.is_symlink() or digest(executable) != receipt["binarySha256"]:
                raise RuntimeError("Installed runtime binary SHA256 mismatch")
            return executable
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"Incomplete runtime installation: {destination}") from exc

    def install(self, *, archive_path=None):
        if self.executable_path:
            path = Path(self.executable_path).absolute()
            if not path.is_file():
                raise RuntimeError(f"Explicit runtime executable does not exist: {path}")
            if self._explicit_lock is not None:
                _, artifact, _, _ = self._identity()
                if digest(path) != artifact["binarySha256"]:
                    raise RuntimeError("Explicit runtime executable SHA256 conflicts with lock")
            return path
        lock, artifact, destination, receipt = self._identity()
        if destination.exists():
            return self._verify(destination, receipt)
        with self._install_lock(receipt["platform"]):
            if destination.exists():
                return self._verify(destination, receipt)
            staging_root = self.root / ".staging"
            staging_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix="install-", dir=staging_root) as temporary:
                stage = Path(temporary)
                archive = stage / artifact["archive"]
                if archive_path is None:
                    self._download(lock["baseUrl"] + "/" + artifact["archive"], archive)
                else:
                    shutil.copyfile(archive_path, archive)
                if archive.stat().st_size != artifact["size"] or digest(archive) != artifact["sha256"]:
                    raise RuntimeError("Runtime archive size or SHA256 mismatch")
                tree = stage / "tree"
                tree.mkdir()
                extract_archive(archive, tree, f"mimic-{self.version}-{receipt['platform']}")
                binary = tree / receipt["executable"]
                if not binary.is_file() or digest(binary) != receipt["binarySha256"]:
                    raise RuntimeError("Extracted runtime binary SHA256 mismatch")
                if os.name != "nt":
                    binary.chmod(0o755)
                write_json(tree / "installation.json", receipt)
                destination.parent.mkdir(parents=True, exist_ok=True)
                tree.rename(destination)
            manifests = self.root / ".manifests"
            manifests.mkdir(parents=True, exist_ok=True)
            saved = manifests / (self.version + ".json")
            if saved.exists() and json.loads(saved.read_text()) != lock:
                raise RuntimeError("Cached runtime provenance conflicts with installation")
            if not saved.exists():
                temporary = manifests / (str(uuid.uuid4()) + ".tmp")
                write_json(temporary, lock)
                try:
                    os.link(temporary, saved)
                except FileExistsError:
                    if json.loads(saved.read_text()) != lock:
                        raise RuntimeError("Cached runtime provenance conflicts with installation")
                finally:
                    temporary.unlink()
        return self._verify(destination, receipt)

    def inspect(self):
        return [{"path": str(path.parent), **json.loads(path.read_text())}
                for path in self.root.glob("v*/*/*/installation.json")]

    def verify(self):
        _, _, destination, receipt = self._identity()
        return self._verify(destination, receipt)

    def launch(self, *, engine="v8", timeout=None):
        binary = self.install()
        expected_version = self.version if not self.executable_path or self._explicit_version else None
        with self._install_lock(current_platform()):
            return RuntimeProcess(binary, expected_version, engine=engine, timeout=timeout or self.timeout)

    def prune(self):
        """Deliberately remove only this selected verified install, never live leases."""
        _, _, destination, receipt = self._identity()
        with self._install_lock(receipt["platform"]):
            self._verify(destination, receipt)
            if destination.resolve() != destination or not destination.resolve().is_relative_to(self.root.resolve()):
                raise RuntimeError("Refusing to prune a symlink or path outside the runtime root")
            for path in (destination / ".leases").glob("*.json"):
                lease = json.loads(path.read_text())
                if lease.get("hostname") != socket.gethostname() or process_alive(lease.get("runtimePid")):
                    raise RuntimeError("Cannot prune a runtime with live or unverifiable leases")
            shutil.rmtree(destination)
            return str(destination)


class RuntimeProcess:
    def __init__(self, binary, version, *, engine="v8", timeout=60):
        if engine not in ("v8", "quickjs", "goja"):
            raise RuntimeError("Unknown Mimic JavaScript engine")
        self.version, self.connection, self.lease = version, None, None
        self._closed = False
        self.process = subprocess.Popen([str(binary), "--browser-mode", "headless", "--listen", "127.0.0.1:0",
                                         "--engine", engine], stdin=subprocess.DEVNULL,
                                        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                                        creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        lines = queue.Queue()
        tail = []
        def reader():
            for line in self.process.stdout:
                tail.append(line[-2048:])
                del tail[:-32]
                # Only the endpoint announcement is consumed by startup. Keep
                # draining later logs into the bounded diagnostic tail, without
                # retaining an unbounded queue for a long-lived runtime.
                if line.startswith("Mimic listening on "):
                    lines.put(line)
            lines.put(None)
        self._reader = threading.Thread(target=reader, daemon=True, name="mimic-runtime-log")
        self._reader.start()
        try:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                try:
                    line = lines.get(timeout=max(.001, deadline - time.monotonic()))
                except queue.Empty:
                    break
                if line is None:
                    raise RuntimeError("Runtime exited during startup: " + "".join(tail))
                match = re.fullmatch(r"Mimic listening on (http://127\.0\.0\.1:[1-9]\d*)\s*", line)
                if match:
                    self.endpoint = match[1]
                    break
            else:
                raise RuntimeError("Runtime startup timed out: " + "".join(tail))
            if not hasattr(self, "endpoint"):
                raise RuntimeError("Runtime startup timed out: " + "".join(tail))
            self.connection = CDPConnection(self.endpoint, min(timeout, 30))
            identity = self.connection.call("Mimic.getVersion")
            if not isinstance(identity.get("version"), str) or not identity["version"]:
                raise RuntimeError("Endpoint did not identify a Mimic build")
            if version is not None and identity.get("version") != version:
                raise RuntimeError(f"Runtime identity mismatch: expected {version}, got {identity}")
            self.identity = identity
            if (Path(binary).parent / "installation.json").exists():
                leases = Path(binary).parent / ".leases"
                leases.mkdir(exist_ok=True)
                self.lease = leases / (str(uuid.uuid4()) + ".json")
                write_json(self.lease, {"launcherPid": os.getpid(), "runtimePid": self.process.pid,
                                        "hostname": socket.gethostname(), "createdAt": stamp()})
        except BaseException:
            self.close()
            raise

    def close(self):
        if self._closed:
            return
        self._closed = True
        if self.connection:
            try:
                self.connection.call("Browser.close", timeout=3)
            except Exception:
                pass
            self.connection.close()
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=2)
        if self.lease:
            self.lease.unlink(missing_ok=True)
        self._reader.join(timeout=2)
        self.process.stdout.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def main():
    parser = argparse.ArgumentParser(description="Install and inspect exact Mimic runtime artifacts")
    parser.add_argument("command", choices=["install", "list", "verify", "prune"])
    parser.add_argument("--runtime-version")
    parser.add_argument("--runtime-dir")
    parser.add_argument("--archive")
    parser.add_argument("--offline", action="store_true")
    args = parser.parse_args()
    manager = RuntimeManager(runtime_version=args.runtime_version, runtime_dir=args.runtime_dir,
                             allow_download=not args.offline)
    result = manager.install(archive_path=args.archive) if args.command == "install" else (
        manager.inspect() if args.command == "list" else manager.prune() if args.command == "prune" else manager.verify())
    print(json.dumps(result, default=str, indent=2))


if __name__ == "__main__":
    main()
