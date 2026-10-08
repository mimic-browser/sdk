import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { createServer } from "node:http";
import { RuntimeManager } from "../dist/runtime.js";
import { ProtocolError } from "../dist/protocol.js";
import * as playwright from "../dist/playwright.js";
import * as puppeteer from "../dist/puppeteer.js";

if (process.platform !== "linux")
  throw new Error("Live SDK qualification is restricted to WSL/Linux");
const archivePath = process.env.MIMIC_TEST_ARCHIVE;
if (!archivePath)
  throw new Error(
    "Set MIMIC_TEST_ARCHIVE to the official pinned Linux release archive",
  );
const runtimeDir =
  process.env.MIMIC_TEST_CACHE || "/tmp/mimic-sdk-shared-cache";
const manager = new RuntimeManager({ runtimeDir, allowDownload: false });
const pinnedRelease = (await manager.resolveLock()).release;
await manager.install({ archivePath });
const fixture = createServer((request, response) => {
  response.setHeader("Content-Type", "text/html");
  response.end(
    "<title>SDK fixture</title><label>Name<input id=name></label><button onclick=\"document.querySelector('h1').textContent=document.querySelector('input').value\">Save</button><h1>before</h1>",
  );
});
await new Promise((resolve) => fixture.listen(0, "127.0.0.1", resolve));
const fixtureURL = `http://127.0.0.1:${fixture.address().port}`;
test.after(() => new Promise((resolve) => fixture.close(resolve)));

for (const [name, integration] of Object.entries({ playwright, puppeteer })) {
  test(`${name}: real framework flow, typed/raw extensions, page scope and owned teardown`, async () => {
    const session = await integration.launch({
      runtimeDir,
      allowDownload: false,
    });
    const child = session.runtime.process;
    try {
      assert.equal((await session.mimic.getVersion()).version, pinnedRelease);
      assert.equal(
        (await session.mimic.experimental.getVersion()).version,
        pinnedRelease,
      );
      // Unknown extensions preserve CDP's MethodNotFound error code.
      await assert.rejects(
        session.mimic.experimental.call("doesNotExistYet", { x: null }),
        (error) => error instanceof ProtocolError && error.code === -32601,
      );
      const context = await session.newContext({
        media: { devices: [] },
        resourcePolicy: { presets: ["noVisualAssets"] },
      });
      const page = await context.newPage();
      await page.goto(fixtureURL);
      if (name === "playwright") {
        await page.locator("input").fill("Mimic SDK");
        await page.locator("button").click();
      } else {
        await page.type("input", "Mimic SDK");
        await page.click("button");
      }
      assert.equal(
        await page.evaluate(() => document.querySelector("h1").textContent),
        "Mimic SDK",
      );
      const pageMimic = await session.forPage(page);
      await pageMimic.startTrace();
      assert.ok(await pageMimic.getStatus());
      await pageMimic.stopTrace();
      await assert.rejects(
        session.newContext({ profile: "unsupported" }),
        (error) => error instanceof ProtocolError,
      );
    } finally {
      await session.close();
    }
    assert.ok(child.exitCode !== null || child.signalCode !== null);
    await session.close();
  });

  test(`${name}: connecting and closing preserves external runtime and other client's context`, async () => {
    const runtime = await manager.launch();
    try {
      const retained = await runtime.connection.call(
        "Target.createBrowserContext",
        {},
      );
      const attached = await integration.connect(runtime.endpoint);
      await attached.newContext();
      await attached.close();
      assert.equal(
        (await runtime.connection.call("Mimic.getVersion")).version,
        pinnedRelease,
      );
      const remaining = await runtime.connection.call(
        "Target.getBrowserContexts",
      );
      assert.ok(
        remaining.browserContextIds.includes(retained.browserContextId),
      );
      assert.equal(remaining.browserContextIds.length, 1);
    } finally {
      await runtime.close();
    }
  });
}

test("Node and Python concurrent installers publish one identical receipt; corruption is rejected offline", async () => {
  const directory = await fs.mkdtemp(
    path.join(os.tmpdir(), "mimic-cross-install-"),
  );
  try {
    const installed = new RuntimeManager({
      runtimeDir: directory,
      allowDownload: false,
    }).install({ archivePath });
    const python = spawn(process.env.MIMIC_TEST_PYTHON || "python3", [
      "-c",
      "import sys; from mimic import RuntimeManager; print(RuntimeManager(runtime_dir=sys.argv[1],allow_download=False).install(archive_path=sys.argv[2]))",
      directory,
      archivePath,
    ]);
    let output = "";
    let errors = "";
    python.stdout.on("data", (data) => (output += data));
    python.stderr.on("data", (data) => (errors += data));
    const completed = new Promise((resolve, reject) => {
      python.on("error", reject);
      python.on("exit", (code) =>
        code === 0 ? resolve() : reject(new Error(errors)),
      );
    });
    const binary = await installed;
    await completed;
    assert.equal(output.trim(), binary);
    const isolated = new RuntimeManager({
      runtimeDir: directory,
      allowDownload: false,
    });
    assert.equal((await isolated.inspect()).length, 1);
    const owned = await isolated.launch();
    try {
      await assert.rejects(isolated.prune(), /live leases/);
    } finally {
      await owned.close();
    }
    await fs.appendFile(binary, "corruption");
    await assert.rejects(
      new RuntimeManager({
        runtimeDir: directory,
        allowDownload: false,
      }).install(),
      /SHA256/,
    );
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});

test("invalid explicit executable is never replaced by a cache hit; incompatible pin cleans its child", async () => {
  await assert.rejects(
    new RuntimeManager({
      executablePath: "/does-not-exist/mimic",
      runtimeDir,
    }).launch(),
  );
  const binary = await manager.verify();
  await assert.rejects(
    new RuntimeManager({
      executablePath: binary,
      runtimeVersion: "999.0.0",
      runtimeDir,
    }).launch(),
    /identity mismatch/,
  );
  const records = await fs.readdir(path.join(path.dirname(binary), ".leases"));
  assert.ok(records.every((name) => name.endsWith(".json")));
});

test("installer honors HTTPS_PROXY without falling back to a direct download", async () => {
  const proxy = createServer();
  const requests = [];
  proxy.on("connect", (request, socket) => {
    requests.push(request.url);
    socket.end("HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n");
  });
  await new Promise((resolve) => proxy.listen(0, "127.0.0.1", resolve));
  const keys = ["HTTPS_PROXY", "https_proxy", "NO_PROXY", "no_proxy"];
  const saved = Object.fromEntries(keys.map((key) => [key, process.env[key]]));
  const directory = await fs.mkdtemp(
    path.join(os.tmpdir(), "mimic-proxy-install-"),
  );
  try {
    process.env.HTTPS_PROXY = `http://127.0.0.1:${proxy.address().port}`;
    for (const key of keys.slice(1)) delete process.env[key];
    await assert.rejects(
      new RuntimeManager({ runtimeDir: directory, timeout: 3000 }).install(),
    );
    assert.deepEqual(requests, ["github.com:443"]);
  } finally {
    for (const [key, value] of Object.entries(saved)) {
      if (value === undefined) delete process.env[key];
      else process.env[key] = value;
    }
    await new Promise((resolve) => proxy.close(resolve));
    await fs.rm(directory, { recursive: true, force: true });
  }
});
