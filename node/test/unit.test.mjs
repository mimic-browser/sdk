import test from "node:test";
import assert from "node:assert/strict";
import fs from "node:fs/promises";
import { inspect } from "node:util";
import { tmpdir } from "node:os";
import path from "node:path";
import { IntegrationSession } from "../dist/integration.js";
import { experimental, OMITTED, assertJSON } from "../dist/protocol.js";
import {
  normalizeVersion,
  validateLock,
  RuntimeManager,
} from "../dist/runtime.js";

test("all close callers await ownership acquisition and shared teardown", async () => {
  let finishCreation;
  let finishCleanup;
  const created = new Promise((resolve) => {
    finishCreation = resolve;
  });
  const cleaned = new Promise((resolve) => {
    finishCleanup = resolve;
  });
  const events = [];
  const adapter = {
    newContext: () => created,
    closeContext: async () => {
      events.push("context");
      await cleaned;
    },
    disconnect: async () => {
      events.push("disconnect");
    },
  };
  const connection = {
    call: async () => ({}),
    close: () => events.push("transport"),
  };
  const session = new IntegrationSession(
    {},
    connection,
    adapter,
    undefined,
    {},
    {},
  );
  const pending = assert.rejects(session.newContext(), /closed/);
  let firstDone = false,
    secondDone = false;
  const first = session.close().then(() => {
    firstDone = true;
  });
  const second = session.close().then(() => {
    secondDone = true;
  });
  await Promise.resolve();
  assert.equal(firstDone || secondDone, false);
  finishCreation({});
  await pending;
  await Promise.resolve();
  assert.equal(firstDone || secondDone, false);
  finishCleanup();
  await Promise.all([first, second]);
  assert.deepEqual(events, ["context", "disconnect", "transport"]);
  await assert.rejects(session.newContext(), /closed/);
});

test(
  "a media factory may await its own session close without retaining a context",
  { timeout: 2000 },
  async () => {
    const events = [];
    const adapter = {
      newContext: async () => ({}),
      newPage: async () => ({}),
      targetInfo: async () => ({
        targetId: "page",
        browserContextId: "context",
      }),
      closePage: async () => events.push("probe"),
      closeContext: async () => events.push("context"),
      disconnect: async () => events.push("disconnect"),
    };
    const connection = {
      call: async () => {
        throw new Error("configuration must not run after close");
      },
      close: () => events.push("transport"),
    };
    const session = new IntegrationSession(
      {},
      connection,
      adapter,
      undefined,
      {},
      {},
    );
    await assert.rejects(
      session.newContext({
        media: async () => {
          await session.close();
          return { devices: [] };
        },
      }),
      /closed/,
    );
    await session.close();
    assert.deepEqual(events, ["probe", "context", "disconnect", "transport"]);
  },
);

test("experimental lookup, await, inspection and JSON are inert; reserved fallback works", async () => {
  const calls = [];
  const proxy = experimental(async (...args) => {
    calls.push(args);
    return { ok: true };
  });
  const method = proxy.newFutureCommand;
  assert.equal(await proxy, proxy);
  JSON.stringify(proxy);
  inspect(proxy);
  assert.equal(calls.length, 0);
  await method({ x: null });
  await proxy.call("newFutureCommand", { x: null });
  assert.deepEqual(calls[0], calls[1]);
  await proxy.call("then", null);
  await proxy.call("constructor");
  assert.deepEqual(calls[2], ["Mimic.then", null]);
  assert.equal(calls[3][1], OMITTED);
  assert.throws(() => proxy.call("Runtime.evaluate"));
});

test("transport rejects lossy JSON conversions instead of silently changing user values", () => {
  for (const value of [
    undefined,
    NaN,
    Infinity,
    1n,
    new Date(),
    { a: undefined },
    [, 1],
  ])
    assert.throws(() => assertJSON(value));
  const circular = {};
  circular.self = circular;
  assert.throws(() => assertJSON(circular));
  assertJSON({ values: [null, false, -0, 1, "text"] });
});

test("manifest locks retain exact provenance; selectors reject ranges and conflicts", async () => {
  const lock = JSON.parse(
    await fs.readFile(new URL("../runtime-lock.json", import.meta.url), "utf8"),
  );
  assert.equal(validateLock(lock), lock);
  assert.equal(normalizeVersion("0.2.2"), "v0.2.2");
  for (const value of ["latest", "^0.2", "../../0.2.2", "0.2"])
    assert.throws(() => normalizeVersion(value));
  assert.throws(() =>
    validateLock({ ...lock, manifestJson: lock.manifestJson + " " }),
  );
  await assert.rejects(
    new RuntimeManager({ lock, runtimeVersion: "999.0.0" }).version(),
    /conflicts/,
  );
});

test("an explicit executable cannot bypass a supplied lock or its binary hash", async () => {
  const lock = JSON.parse(
    await fs.readFile(new URL("../runtime-lock.json", import.meta.url), "utf8"),
  );
  const directory = await fs.mkdtemp(
    path.join(tmpdir(), "mimic-explicit-pin-"),
  );
  const executablePath = path.join(directory, "mimic");
  await fs.writeFile(executablePath, "never execute this unverified file");
  try {
    await assert.rejects(
      new RuntimeManager({
        executablePath,
        lock: { ...lock, manifestJson: lock.manifestJson + " " },
      }).install(),
      /SHA256|integrity/,
    );
    await assert.rejects(
      new RuntimeManager({ executablePath, lock }).install(),
      /SHA256/,
    );
    assert.equal(
      await new RuntimeManager({
        executablePath,
        allowDownload: false,
      }).install(),
      executablePath,
    );
  } finally {
    await fs.rm(directory, { recursive: true, force: true });
  }
});
