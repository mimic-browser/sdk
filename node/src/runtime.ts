import { createHash, randomUUID } from "node:crypto";
import { createReadStream, createWriteStream } from "node:fs";
import * as fs from "node:fs/promises";
import { homedir, hostname, platform, arch } from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { spawn, type ChildProcess } from "node:child_process";
import { createInterface } from "node:readline";
import { pipeline } from "node:stream/promises";
import { Readable } from "node:stream";
import { createGunzip } from "node:zlib";
import yauzl from "yauzl";
import tar from "tar-stream";
import { EnvHttpProxyAgent, fetch as downloadFetch } from "undici";
import { CDPConnection } from "./protocol.js";

const BASE = "https://github.com/mimic-browser/runtime/releases/download";
const SHA = /^[0-9a-f]{64}$/;
export interface Artifact {
  platform: string;
  archive: string;
  sha256: string;
  size: number;
  binarySha256: string;
  binaryVersion: string;
}
export interface RuntimeLock {
  release: string;
  manifestSha256: string;
  manifestJson: string;
  baseUrl: string;
  manifest: {
    version: string;
    sourceRevision: string;
    packagingRevision: string;
    artifacts: Artifact[];
    [key: string]: unknown;
  };
}
export interface RuntimeOptions {
  runtimeVersion?: string;
  lock?: RuntimeLock;
  executablePath?: string;
  runtimeDir?: string;
  allowDownload?: boolean;
  timeout?: number;
}

export function normalizeVersion(value: string): string {
  if (
    typeof value !== "string" ||
    !/^v?\d+\.\d+\.\d+(?:-beta\.\d+)?$/.test(value)
  )
    throw new Error("Runtime version must be exact, for example v0.2.2");
  return value.startsWith("v") ? value : `v${value}`;
}
export function currentPlatform(): string {
  if (arch() !== "x64" || !["win32", "linux"].includes(platform()))
    throw new Error(`No Mimic release for ${platform()}/${arch()}`);
  if (platform() === "linux") {
    const header = (process.report.getReport() as any).header;
    const version = String(header.glibcVersionRuntime || "0.0")
      .split(".")
      .map(Number);
    if (version[0] < 2 || (version[0] === 2 && version[1] < 39))
      throw new Error("Mimic Linux requires glibc 2.39+");
  }
  return platform() === "win32" ? "windows-amd64" : "linux-amd64";
}
function hash(data: string | Buffer) {
  return createHash("sha256").update(data).digest("hex");
}
async function fileHash(filename: string) {
  const digest = createHash("sha256");
  for await (const chunk of createReadStream(filename)) digest.update(chunk);
  return digest.digest("hex");
}
async function exists(filename: string) {
  try {
    await fs.access(filename);
    return true;
  } catch {
    return false;
  }
}
async function json(filename: string) {
  return JSON.parse(await fs.readFile(filename, "utf8"));
}
async function writeJSON(filename: string, value: unknown) {
  await fs.writeFile(filename, JSON.stringify(value, null, 2) + "\n");
}
function sorted(value: any): any {
  return Array.isArray(value)
    ? value.map(sorted)
    : value && typeof value === "object"
      ? Object.fromEntries(
          Object.keys(value)
            .sort()
            .map((key) => [key, sorted(value[key])]),
        )
      : value;
}
export function validateLock(lock: RuntimeLock): RuntimeLock {
  const release = normalizeVersion(lock.release);
  if (
    lock.baseUrl !== `${BASE}/${release}` ||
    lock.manifest.version !== release
  )
    throw new Error("Runtime lock release/URL mismatch");
  if (
    typeof lock.manifestJson !== "string" ||
    hash(lock.manifestJson) !== lock.manifestSha256 ||
    JSON.stringify(sorted(JSON.parse(lock.manifestJson))) !==
      JSON.stringify(sorted(lock.manifest))
  )
    throw new Error("Runtime manifest SHA256/content mismatch");
  for (const key of ["sourceRevision", "packagingRevision"])
    if (!/^[0-9a-f]{40}$/.test(String(lock.manifest[key])))
      throw new Error("Invalid runtime source provenance");
  const platforms = new Set<string>();
  for (const artifact of lock.manifest.artifacts) {
    const host = artifact.platform;
    const suffix = host === "windows-amd64" ? ".zip" : ".tar.gz";
    if (
      !["windows-amd64", "linux-amd64"].includes(host) ||
      platforms.has(host) ||
      artifact.archive !== `mimic-${release}-${host}${suffix}` ||
      artifact.binaryVersion !== release ||
      !SHA.test(artifact.sha256) ||
      !SHA.test(artifact.binarySha256) ||
      !Number.isSafeInteger(artifact.size) ||
      artifact.size <= 0
    )
      throw new Error("Invalid runtime artifact provenance");
    platforms.add(host);
  }
  if (!platforms.size) throw new Error("No runtime artifacts");
  return lock;
}

export async function extractArchive(
  archive: string,
  destination: string,
  root: string,
) {
  const seen = new Set<string>();
  let total = 0;
  function target(name: string, size: number) {
    name = name.replace(/\/$/, "");
    const parts = name.split("/");
    if (
      name.includes("\\") ||
      name.includes(":") ||
      parts[0] !== root ||
      parts.includes("..") ||
      parts.includes("") ||
      seen.has(name)
    )
      throw new Error("Unsafe or duplicate runtime archive entry");
    seen.add(name);
    total += size;
    if (total > 2 * 1024 ** 3)
      throw new Error("Archive extraction limit exceeded");
    return path.join(destination, ...parts.slice(1));
  }
  if (archive.endsWith(".zip")) {
    await new Promise<void>((resolve, reject) => {
      yauzl.open(archive, { lazyEntries: true }, (error, packed) => {
        if (error || !packed) return reject(error);
        packed.on("error", reject);
        packed.on("end", resolve);
        packed.on("entry", async (entry: yauzl.Entry) => {
          try {
            const mode = (entry.externalFileAttributes >>> 16) & 0o170000;
            if (![0, 0o100000, 0o040000].includes(mode))
              throw new Error("Archive links/special files are unsupported");
            const output = target(entry.fileName, entry.uncompressedSize);
            if (entry.fileName.endsWith("/"))
              await fs.mkdir(output, { recursive: true });
            else {
              await fs.mkdir(path.dirname(output), { recursive: true });
              const stream = await new Promise<Readable>((yes, no) =>
                packed.openReadStream(entry, (error, stream) =>
                  error ? no(error) : yes(stream!),
                ),
              );
              await pipeline(
                stream,
                createWriteStream(output, { flags: "wx" }),
              );
            }
            packed.readEntry();
          } catch (error) {
            packed.close();
            reject(error);
          }
        });
        packed.readEntry();
      });
    });
  } else {
    const unpack = tar.extract();
    unpack.on("entry", (header, stream, next) => {
      (async () => {
        if (header.type !== "file" && header.type !== "directory")
          throw new Error("Archive links/special files are unsupported");
        const output = target(header.name, header.size || 0);
        if (header.type === "directory") {
          await fs.mkdir(output, { recursive: true });
          stream.resume();
        } else {
          await fs.mkdir(path.dirname(output), { recursive: true });
          await pipeline(stream, createWriteStream(output, { flags: "wx" }));
        }
        next();
      })().catch((error) => unpack.destroy(error));
    });
    await pipeline(createReadStream(archive), createGunzip(), unpack);
  }
}

export class RuntimeManager {
  readonly root: string;
  readonly timeout: number;
  readonly allowDownload: boolean;
  private options: RuntimeOptions;
  private selected?: RuntimeLock;
  constructor(options: RuntimeOptions = {}) {
    this.options = options;
    this.timeout = options.timeout ?? 60_000;
    this.allowDownload =
      options.allowDownload ?? process.env.MIMIC_DOWNLOAD !== "0";
    const base =
      platform() === "win32"
        ? process.env.LOCALAPPDATA || path.join(homedir(), "AppData", "Local")
        : process.env.XDG_CACHE_HOME || path.join(homedir(), ".cache");
    this.root = path.resolve(
      options.runtimeDir ||
        process.env.MIMIC_RUNTIME_DIR ||
        path.join(base, "Mimic", "runtimes"),
    );
  }
  async version() {
    const defaults: RuntimeLock = await json(
      fileURLToPath(new URL("./runtime-lock.json", import.meta.url)),
    );
    const version = normalizeVersion(
      this.options.runtimeVersion ||
        this.options.lock?.release ||
        process.env.MIMIC_RUNTIME_VERSION ||
        defaults.release,
    );
    if (
      this.options.lock &&
      normalizeVersion(this.options.lock.release) !== version
    )
      throw new Error("Explicit runtime version conflicts with lock");
    return version;
  }
  private async download(url: string, filename: string, signal?: AbortSignal) {
    if (!this.allowDownload)
      throw new Error(
        "Runtime download disabled; preinstall this exact release",
      );
    const bounded = signal
      ? AbortSignal.any([signal, AbortSignal.timeout(this.timeout)])
      : AbortSignal.timeout(this.timeout);
    // Respect HTTPS_PROXY/HTTP_PROXY/NO_PROXY without changing the caller's
    // global HTTP dispatcher or its browser Context proxy.
    const dispatcher = new EnvHttpProxyAgent();
    try {
      const response = await downloadFetch(url, {
        signal: bounded,
        dispatcher,
      });
      if (!response.ok || !response.body)
        throw new Error(`Runtime download returned HTTP ${response.status}`);
      await pipeline(
        Readable.fromWeb(response.body as any),
        createWriteStream(filename),
        { signal: bounded },
      );
    } finally {
      await dispatcher.destroy();
    }
  }
  async resolveLock(signal?: AbortSignal): Promise<RuntimeLock> {
    if (this.selected) return this.selected;
    const version = await this.version();
    const defaults: RuntimeLock = await json(
      fileURLToPath(new URL("./runtime-lock.json", import.meta.url)),
    );
    if (this.options.lock)
      return (this.selected = validateLock(this.options.lock));
    if (version === defaults.release)
      return (this.selected = validateLock(defaults));
    const cached = path.join(this.root, ".manifests", version + ".json");
    if (await exists(cached)) {
      const lock = validateLock(await json(cached));
      if (lock.release !== version)
        throw new Error("Cached runtime manifest version mismatch");
      return (this.selected = lock);
    }
    if (!this.allowDownload)
      throw new Error("Exact runtime manifest unavailable offline");
    const baseUrl = `${BASE}/${version}`;
    const stage = await fs.mkdtemp(
      path.join(
        await fs.realpath((await import("node:os")).tmpdir()),
        "mimic-manifest-",
      ),
    );
    try {
      await this.download(
        `${baseUrl}/release-manifest.json`,
        path.join(stage, "manifest"),
        signal,
      );
      await this.download(
        `${baseUrl}/SHA256SUMS`,
        path.join(stage, "sums"),
        signal,
      );
      const manifestJson = await fs.readFile(
        path.join(stage, "manifest"),
        "utf8",
      );
      const sums = (await fs.readFile(path.join(stage, "sums"), "utf8"))
        .split(/\r?\n/)
        .map((line) => line.trim().split(/\s+/))
        .filter((row) => row[1] === "release-manifest.json");
      if (sums.length !== 1 || sums[0][0] !== hash(manifestJson))
        throw new Error("Published manifest SHA256 mismatch");
      return (this.selected = validateLock({
        release: version,
        manifestSha256: sums[0][0],
        manifestJson,
        manifest: JSON.parse(manifestJson),
        baseUrl,
      }));
    } finally {
      await fs.rm(stage, { recursive: true, force: true });
    }
  }
  private async identity(signal?: AbortSignal) {
    const host = currentPlatform();
    const lock = await this.resolveLock(signal);
    const artifact = lock.manifest.artifacts.find(
      (item) => item.platform === host,
    );
    if (!artifact) throw new Error(`No runtime artifact for ${host}`);
    const destination = path.join(
      this.root,
      lock.release,
      host,
      artifact.binarySha256,
    );
    const receipt = {
      release: lock.release,
      platform: host,
      sourceRevision: lock.manifest.sourceRevision,
      archiveSha256: artifact.sha256,
      binarySha256: artifact.binarySha256,
      executable: host === "windows-amd64" ? "mimic.exe" : "mimic",
      manifestSha256: lock.manifestSha256,
    };
    return { lock, artifact, destination, receipt };
  }
  private async verifyAt(destination: string, receipt: Record<string, string>) {
    const actual = await json(path.join(destination, "installation.json"));
    if (Object.entries(receipt).some(([key, value]) => actual[key] !== value))
      throw new Error("Installed runtime receipt conflicts with exact pin");
    const binary = path.join(destination, receipt.executable);
    if (
      (await fs.lstat(binary)).isSymbolicLink() ||
      (await fileHash(binary)) !== receipt.binarySha256
    )
      throw new Error("Installed runtime SHA256 mismatch");
    return binary;
  }
  async install({
    archivePath,
    signal,
  }: { archivePath?: string; signal?: AbortSignal } = {}) {
    signal?.throwIfAborted();
    const explicit =
      this.options.executablePath || process.env.MIMIC_EXECUTABLE_PATH;
    if (explicit) {
      await this.version();
      const binary = path.resolve(explicit);
      if (!(await fs.stat(binary)).isFile())
        throw new Error("Explicit runtime executable missing");
      if (this.options.lock) {
        const { artifact } = await this.identity(signal);
        if ((await fileHash(binary)) !== artifact.binarySha256)
          throw new Error(
            "Explicit runtime executable SHA256 conflicts with lock",
          );
      }
      return binary;
    }
    const { lock, artifact, destination, receipt } =
      await this.identity(signal);
    if (await exists(destination)) return this.verifyAt(destination, receipt);
    const mutex = path.join(
      this.root,
      ".locks",
      `${lock.release}-${artifact.platform}.lock`,
    );
    await fs.mkdir(path.dirname(mutex), { recursive: true });
    const deadline = Date.now() + this.timeout;
    while (true) {
      signal?.throwIfAborted();
      try {
        await fs.mkdir(mutex);
        break;
      } catch (error: any) {
        if (error.code !== "EEXIST") throw error;
        if (Date.now() >= deadline)
          throw new Error(
            `Installation lock timed out: ${mutex}; inspect owner.json before repair`,
          );
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    }
    const token = randomUUID();
    let stage: string | undefined;
    try {
      await writeJSON(path.join(mutex, "owner.json"), {
        pid: process.pid,
        hostname: hostname(),
        token,
        createdAt: new Date().toISOString(),
      });
      if (await exists(destination)) return this.verifyAt(destination, receipt);
      const staging = path.join(this.root, ".staging");
      await fs.mkdir(staging, { recursive: true });
      stage = await fs.mkdtemp(path.join(staging, "install-"));
      const archive = path.join(stage, artifact.archive);
      if (archivePath) await fs.copyFile(archivePath, archive);
      else
        await this.download(
          `${lock.baseUrl}/${artifact.archive}`,
          archive,
          signal,
        );
      if (
        (await fs.stat(archive)).size !== artifact.size ||
        (await fileHash(archive)) !== artifact.sha256
      )
        throw new Error("Runtime archive size/SHA256 mismatch");
      const tree = path.join(stage, "tree");
      await fs.mkdir(tree);
      await extractArchive(
        archive,
        tree,
        `mimic-${lock.release}-${artifact.platform}`,
      );
      const binary = path.join(tree, receipt.executable);
      if ((await fileHash(binary)) !== artifact.binarySha256)
        throw new Error("Extracted executable SHA256 mismatch");
      if (platform() !== "win32") await fs.chmod(binary, 0o755);
      await writeJSON(path.join(tree, "installation.json"), receipt);
      signal?.throwIfAborted();
      await fs.mkdir(path.dirname(destination), { recursive: true });
      await fs.rename(tree, destination);
      const manifests = path.join(this.root, ".manifests");
      await fs.mkdir(manifests, { recursive: true });
      const saved = path.join(manifests, lock.release + ".json");
      if (await exists(saved)) {
        if (
          JSON.stringify(sorted(await json(saved))) !==
          JSON.stringify(sorted(lock))
        )
          throw new Error("Cached runtime provenance conflict");
      } else {
        const temporary = path.join(manifests, randomUUID() + ".tmp");
        await writeJSON(temporary, lock);
        try {
          await fs.link(temporary, saved);
        } catch (error: any) {
          if (error.code !== "EEXIST") throw error;
          if (
            JSON.stringify(sorted(await json(saved))) !==
            JSON.stringify(sorted(lock))
          )
            throw new Error("Cached runtime provenance conflict");
        } finally {
          await fs.unlink(temporary);
        }
      }
      return this.verifyAt(destination, receipt);
    } finally {
      if (stage) await fs.rm(stage, { recursive: true, force: true });
      if (
        (await exists(path.join(mutex, "owner.json"))) &&
        (await json(path.join(mutex, "owner.json"))).token === token
      ) {
        await fs.unlink(path.join(mutex, "owner.json"));
        await fs.rmdir(mutex);
      }
    }
  }
  async verify() {
    const { destination, receipt } = await this.identity();
    return this.verifyAt(destination, receipt);
  }
  async inspect() {
    const installs: Record<string, unknown>[] = [];
    if (!(await exists(this.root))) return installs;
    for (const release of await fs.readdir(this.root)) {
      if (!/^v\d+\.\d+\.\d+(?:-beta\.\d+)?$/.test(release)) continue;
      for (const host of ["windows-amd64", "linux-amd64"]) {
        const directory = path.join(this.root, release, host);
        if (!(await exists(directory))) continue;
        for (const digest of await fs.readdir(directory)) {
          if (!SHA.test(digest)) continue;
          const destination = path.join(directory, digest);
          if (await exists(path.join(destination, "installation.json")))
            installs.push({
              path: destination,
              ...(await json(path.join(destination, "installation.json"))),
            });
        }
      }
    }
    return installs;
  }
  private async lifecycleLock<T>(
    action: () => Promise<T>,
    signal?: AbortSignal,
  ): Promise<T> {
    const mutex = path.join(
      this.root,
      ".locks",
      `${await this.version()}-${currentPlatform()}.lock`,
    );
    await fs.mkdir(path.dirname(mutex), { recursive: true });
    const deadline = Date.now() + this.timeout;
    while (true) {
      signal?.throwIfAborted();
      try {
        await fs.mkdir(mutex);
        break;
      } catch (error: any) {
        if (error.code !== "EEXIST") throw error;
        if (Date.now() >= deadline)
          throw new Error(`Lifecycle lock timed out: ${mutex}`);
        await new Promise((resolve) => setTimeout(resolve, 50));
      }
    }
    const token = randomUUID();
    try {
      await writeJSON(path.join(mutex, "owner.json"), {
        pid: process.pid,
        hostname: hostname(),
        token,
        createdAt: new Date().toISOString(),
      });
      return await action();
    } finally {
      if (
        (await exists(path.join(mutex, "owner.json"))) &&
        (await json(path.join(mutex, "owner.json"))).token === token
      ) {
        await fs.unlink(path.join(mutex, "owner.json"));
        await fs.rmdir(mutex);
      }
    }
  }
  async prune() {
    const { destination, receipt } = await this.identity();
    return this.lifecycleLock(async () => {
      await this.verifyAt(destination, receipt);
      if (
        (await fs.realpath(destination)) !== destination ||
        !destination.startsWith((await fs.realpath(this.root)) + path.sep)
      )
        throw new Error(
          "Refusing to prune a symlink or path outside the runtime root",
        );
      const leases = path.join(destination, ".leases");
      if (await exists(leases))
        for (const filename of await fs.readdir(leases)) {
          const lease = await json(path.join(leases, filename));
          if (
            lease.hostname !== hostname() ||
            !Number.isSafeInteger(lease.runtimePid) ||
            lease.runtimePid <= 0
          )
            throw new Error("Cannot prune unverifiable runtime leases");
          try {
            process.kill(lease.runtimePid, 0);
            throw new Error("Cannot prune a runtime with live leases");
          } catch (error: any) {
            if (error.code !== "ESRCH") throw error;
          }
        }
      await fs.rm(destination, { recursive: true });
      return destination;
    });
  }
  async launch({
    engine = "v8",
    signal,
  }: { engine?: "v8" | "quickjs" | "goja"; signal?: AbortSignal } = {}) {
    const binary = await this.install({ signal });
    const version = await this.version();
    const explicitExecutable =
      this.options.executablePath || process.env.MIMIC_EXECUTABLE_PATH;
    const explicitVersion =
      this.options.runtimeVersion ||
      this.options.lock?.release ||
      process.env.MIMIC_RUNTIME_VERSION;
    return this.lifecycleLock(
      () =>
        RuntimeProcess.start(
          binary,
          explicitExecutable && !explicitVersion ? undefined : version,
          engine,
          this.timeout,
          signal,
        ),
      signal,
    );
  }
}

export class RuntimeProcess {
  connection!: CDPConnection;
  endpoint!: string;
  identity: any;
  private lease?: string;
  private closed = false;
  private constructor(readonly process: ChildProcess) {}
  static async start(
    binary: string,
    version: string | undefined,
    engine: string,
    timeout: number,
    signal?: AbortSignal,
  ) {
    const child = spawn(
      binary,
      [
        "--browser-mode",
        "headless",
        "--listen",
        "127.0.0.1:0",
        "--engine",
        engine,
      ],
      { windowsHide: true, stdio: ["ignore", "pipe", "pipe"] },
    );
    const owned = new RuntimeProcess(child);
    const lines = createInterface({ input: child.stdout! });
    const tail: string[] = [];
    child.stderr!.on("data", (data) => {
      tail.push(String(data).slice(-2048));
      if (tail.length > 32) tail.shift();
    });
    try {
      owned.endpoint = await new Promise<string>((resolve, reject) => {
        const timer = setTimeout(
          () =>
            finish(new Error("Runtime startup timed out: " + tail.join(""))),
          timeout,
        );
        const abort = () => finish(signal!.reason);
        const failed = (error: Error) => finish(error);
        const exited = (code: number | null) =>
          finish(new Error(`Runtime exited (${code}): ${tail.join("")}`));
        const cleanup = () => {
          clearTimeout(timer);
          signal?.removeEventListener("abort", abort);
          child.removeListener("error", failed);
          child.removeListener("exit", exited);
          lines.removeListener("line", line);
        };
        const finish = (error: unknown, endpoint?: string) => {
          cleanup();
          error ? reject(error) : resolve(endpoint!);
        };
        const line = (value: string) => {
          const match =
            /^Mimic listening on (http:\/\/127\.0\.0\.1:[1-9]\d*)$/.exec(value);
          if (match) finish(null, match[1]);
        };
        child.once("error", failed);
        child.once("exit", exited);
        lines.on("line", line);
        signal?.addEventListener("abort", abort, { once: true });
        if (signal?.aborted) abort();
      });
      owned.connection = await CDPConnection.connect(owned.endpoint, {
        timeout,
        signal,
      });
      owned.identity = await owned.connection.call(
        "Mimic.getVersion",
        undefined,
        { signal },
      );
      if (typeof owned.identity.version !== "string" || !owned.identity.version)
        throw new Error("Endpoint did not identify a Mimic build");
      if (version !== undefined && owned.identity.version !== version)
        throw new Error(
          `Runtime identity mismatch: expected ${version}, got ${owned.identity.version}`,
        );
      if (await exists(path.join(path.dirname(binary), "installation.json"))) {
        const leases = path.join(path.dirname(binary), ".leases");
        await fs.mkdir(leases, { recursive: true });
        owned.lease = path.join(leases, randomUUID() + ".json");
        await writeJSON(owned.lease, {
          launcherPid: process.pid,
          runtimePid: child.pid,
          hostname: hostname(),
          createdAt: new Date().toISOString(),
        });
      }
      return owned;
    } catch (error) {
      await owned.close();
      throw error;
    }
  }
  async close() {
    if (this.closed) return;
    this.closed = true;
    if (this.connection) {
      try {
        await this.connection.call("Browser.close", undefined, {
          timeout: 3_000,
        });
      } catch {}
      this.connection.close();
    }
    const exited = () =>
      this.process.exitCode !== null || this.process.signalCode !== null;
    const wait = async (duration: number) => {
      const end = Date.now() + duration;
      while (!exited() && Date.now() < end)
        await new Promise((resolve) => setTimeout(resolve, 25));
    };
    await wait(3_000);
    if (!exited()) {
      this.process.kill("SIGTERM");
      await wait(2_000);
    }
    if (!exited()) {
      this.process.kill("SIGKILL");
      await wait(2_000);
    }
    if (!exited() && this.process.pid)
      throw new Error("Owned runtime did not exit; lease retained");
    if (this.lease) await fs.unlink(this.lease);
  }
  async [Symbol.asyncDispose]() {
    await this.close();
  }
}
