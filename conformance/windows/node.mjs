import assert from "node:assert/strict";
import fs from "node:fs/promises";
import path from "node:path";
import { createRequire } from "node:module";
import { RuntimeManager } from "mimic-browser";

assert.equal(process.platform, "win32");
assert.equal(process.env.GITHUB_ACTIONS, "true");
assert.equal(process.env.RUNNER_ENVIRONMENT, "github-hosted");
assert.equal(process.env.RUNNER_OS, "Windows");
const mode = process.argv[2];
const manager = new RuntimeManager();
const cache = path.join(process.env.LOCALAPPDATA, "Mimic", "runtimes");
assert.equal(manager.root, cache);
const lock = await manager.resolveLock();
assert.equal(lock.release, process.env.MIMIC_EXPECTED_RELEASE);
assert.ok(
  lock.manifest.artifacts
    .find((item) => item.platform === "windows-amd64")
    .archive.endsWith(".zip"),
);

if (mode === "core") {
  const require = createRequire(import.meta.url);
  for (const client of ["playwright-core", "puppeteer-core"]) {
    assert.throws(() => require.resolve(client), { code: "MODULE_NOT_FOUND" });
  }
  await assert.rejects(fs.stat(cache), { code: "ENOENT" });
  console.log(JSON.stringify({ mode, inert: true }));
} else if (mode === "install") {
  const binary = await manager.install();
  assert.equal(binary, await manager.verify());
  assert.equal((await manager.inspect()).length, 1);
  console.log(
    JSON.stringify({
      mode,
      binary,
      receipt: JSON.parse(
        await fs.readFile(
          path.join(path.dirname(binary), "installation.json"),
          "utf8",
        ),
      ),
    }),
  );
} else if (mode === "live") {
  await assert.rejects(fs.stat(cache), { code: "ENOENT" });
  const clients = [
    await import("mimic-browser/playwright"),
    await import("mimic-browser/puppeteer"),
  ];
  const results = [];
  for (const [index, client] of clients.entries()) {
    const session = await client.launch();
    const child = session.runtime.process;
    try {
      assert.equal(session.runtime.identity.version, lock.release);
      assert.match(session.runtime.endpoint, /^http:\/\/127\.0\.0\.1:\d+$/);
      assert.deepEqual(child.spawnargs.slice(-6), [
        "--browser-mode",
        "headless",
        "--listen",
        "127.0.0.1:0",
        "--engine",
        "v8",
      ]);
      const context = await session.newContext();
      const page = await context.newPage();
      await page.setContent(
        "<title>Windows SDK</title><input><button onclick=\"document.querySelector('h1').textContent=document.querySelector('input').value\">Save</button><h1>before</h1>",
      );
      assert.equal(await page.title(), "Windows SDK");
      if (index === 0) {
        await page.locator("input").fill("native client");
        await page.locator("button").click();
      } else {
        await page.type("input", "native client");
        await page.click("button");
      }
      assert.equal(
        await page.evaluate(() => document.querySelector("h1").textContent),
        "native client",
      );
      await assert.rejects(manager.prune(), /live leases/);
    } finally {
      await session.close();
    }
    assert.ok(child.exitCode !== null || child.signalCode !== null);
    await session.close();
    process.env.MIMIC_DOWNLOAD = "0";
    const offline = new RuntimeManager();
    const binary = await offline.install();
    assert.equal(binary, await offline.verify());
    assert.deepEqual(
      await fs.readdir(path.join(path.dirname(binary), ".leases")),
      [],
    );
    const external = await offline.launch();
    try {
      const retained = await external.connection.call(
        "Target.createBrowserContext",
      );
      const attached = await client.connect(external.endpoint);
      try {
        const context = await attached.newContext();
        const page = await context.newPage();
        await page.setContent("<title>Attached SDK</title>");
        assert.equal(await page.title(), "Attached SDK");
      } finally {
        await attached.close();
      }
      assert.equal(
        (await external.connection.call("Mimic.getVersion")).version,
        lock.release,
      );
      assert.deepEqual(
        (await external.connection.call("Target.getBrowserContexts"))
          .browserContextIds,
        [retained.browserContextId],
      );
    } finally {
      await external.close();
    }
    assert.ok(
      external.process.exitCode !== null ||
        external.process.signalCode !== null,
    );
    assert.deepEqual(
      await fs.readdir(path.join(path.dirname(binary), ".leases")),
      [],
    );
    results.push(index === 0 ? "playwright" : "puppeteer");
  }
  const offline = new RuntimeManager();
  const verified = await offline.verify();
  await offline.prune();
  await assert.rejects(fs.stat(verified), { code: "ENOENT" });
  await assert.rejects(offline.install(), /disabled|offline/i);
  console.log(
    JSON.stringify({
      mode,
      clients: results,
      release: lock.release,
      coldDownload: true,
      offlineReuse: true,
      nativePage: true,
      attachPreserved: true,
      ownedExit: true,
      leasesCleared: true,
      prune: true,
    }),
  );
} else {
  throw new Error("Unknown qualification mode");
}
