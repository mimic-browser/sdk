import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import * as playwright from "../dist/playwright.js";
import * as puppeteer from "../dist/puppeteer.js";
import { CDPConnection } from "../dist/protocol.js";
import { RuntimeManager } from "../dist/runtime.js";

if (process.platform !== "linux")
  throw new Error("Live tests require WSL/Linux");
const child = spawn(
  process.env.MIMIC_CANDIDATE || "/tmp/mimic-sdk-runtime-candidate/mimic",
  ["--browser-mode", "headless", "--listen", "127.0.0.1:0"],
  { stdio: ["ignore", "pipe", "inherit"] },
);
let stage = "runtime startup";
if (process.env.MIMIC_BRIDGE_DUMP_STACKS) {
  const watchdog = setTimeout(() => {
    console.error("Bridge stalled at", stage);
    child.kill("SIGQUIT");
  }, 10_000);
  watchdog.unref();
}
const lines = createInterface({ input: child.stdout });
const endpoint = await new Promise((resolve, reject) => {
  const timer = setTimeout(
    () => reject(new Error("Candidate startup timeout")),
    30_000,
  );
  child.once("error", reject);
  lines.on("line", (line) => {
    const match = /Mimic listening on (http:\/\/127\.0\.0\.1:\d+)/.exec(line);
    if (match) {
      clearTimeout(timer);
      resolve(match[1]);
    }
  });
});
const control = await CDPConnection.connect(endpoint);
test.after(async () => {
  try {
    await control.call("Browser.close");
  } finally {
    control.close();
    child.kill();
  }
});

test("an unpinned explicit development executable uses its actual identity", async () => {
  const owned = await new RuntimeManager({
    executablePath:
      process.env.MIMIC_CANDIDATE || "/tmp/mimic-sdk-runtime-candidate/mimic",
    runtimeDir: "/tmp/mimic-sdk-candidate-cache",
    allowDownload: false,
  }).launch();
  try {
    assert.equal(typeof owned.identity.version, "string");
    assert.equal(
      (await owned.connection.call("Mimic.getVersion")).version,
      owned.identity.version,
    );
  } finally {
    await owned.close();
  }
});

test("Puppeteer managed contexts reject an explicit viewport before creating state", async () => {
  const session = await puppeteer.connect(endpoint, {
    framework: { defaultViewport: { width: 800, height: 600 } },
  });
  try {
    const before = await session.connection.call("Target.getBrowserContexts");
    await assert.rejects(
      session.newContext({ profile: { generate: {} } }),
      /defaultViewport/,
    );
    assert.deepEqual(
      await session.connection.call("Target.getBrowserContexts"),
      before,
    );
  } finally {
    await session.close();
  }
});

for (const [name, adapter] of Object.entries({ playwright, puppeteer })) {
  test(`${name}: managed profiles use real contexts, reject emulation changes and clean failed configuration`, async () => {
    stage = `${name}: connect`;
    const session = await adapter.connect(endpoint);
    try {
      for (const seed of ["sdk-0", "sdk-1", "sdk-2", "sdk-3"]) {
        stage = `${name} ${seed}: newContext`;
        const context = await session.newContext({
          profile: { generate: { seed } },
          media: { devices: [] },
        });
        if (process.env.MIMIC_PROFILE_DIAGNOSTIC) {
          const contexts = await session.connection.call(
            "Target.getBrowserContexts",
          );
          for (const browserContextId of contexts.browserContextIds) {
            const observed = await session.connection.call("Mimic.getProfile", {
              browserContextId,
            });
            console.log(
              "profile font defaults",
              seed,
              JSON.stringify(observed.profile.fonts).slice(0, 1400),
            );
          }
        }
        stage = `${name} ${seed}: newPage`;
        const page = await context.newPage();
        stage = `${name} ${seed}: evaluate`;
        assert.equal(await page.evaluate(() => navigator.webdriver), false);
        assert.ok(
          (await page.evaluate(() => navigator.hardwareConcurrency)) > 0,
        );
        stage = `${name} ${seed}: protocol`;
        const protocol =
          name === "playwright"
            ? await context.newCDPSession(page)
            : await page.createCDPSession();
        const info = (await protocol.send("Target.getTargetInfo")).targetInfo;
        const profile = await session.mimic.getProfile({
          browserContextId: info.browserContextId,
        });
        assert.ok(profile.profile);
        await assert.rejects(
          protocol.send("Emulation.setDeviceMetricsOverride", {
            width: 999,
            height: 999,
            deviceScaleFactor: 1,
            mobile: false,
          }),
          /profile|context/i,
        );
        stage = `${name} ${seed}: detach`;
        await protocol.detach();
        stage = `${name} ${seed}: close page`;
        await page.close();
        stage = `${name} ${seed}: close context`;
        await context.close();
      }
      stage = `${name}: invalid profile`;
      const before = await session.connection.call("Target.getBrowserContexts");
      await assert.rejects(session.newContext({ profile: "invalid-token" }));
      const after = await session.connection.call("Target.getBrowserContexts");
      assert.deepEqual(after, before);
    } finally {
      stage = `${name}: close session`;
      await session.close();
    }
  });
}
