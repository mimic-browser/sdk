import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { IntegrationSession } from "../dist/integration.js";
import { ProtocolError } from "../dist/protocol.js";
import * as puppeteer from "../dist/puppeteer.js";

test("Puppeteer endpoint overrides reject before installation or connection", async () => {
  for (const key of ["browserURL", "browserWSEndpoint", "transport"]) {
    const framework = { [key]: "owned by SDK" };
    await assert.rejects(
      puppeteer.launch({ framework, executablePath: "/missing/never-launch" }),
      /SDK owns/,
    );
    await assert.rejects(
      puppeteer.connect("invalid-endpoint", { framework }),
      /SDK owns/,
    );
  }
});

function fixture({ delayed = false } = {}) {
  const page = new EventEmitter();
  let closed = false;
  page.close = () => {
    closed = true;
    page.emit("close");
  };
  const calls = [];
  let completeAttach;
  const attachment = delayed
    ? new Promise((resolve) => {
        completeAttach = resolve;
      })
    : undefined;
  let sequence = 0;
  const connection = {
    async call(method, params, options) {
      calls.push({ method, params, options });
      if (method === "Target.attachToTarget")
        return attachment ?? { sessionId: `session-${++sequence}` };
      if (method === "Target.detachFromTarget" && closed)
        throw new ProtocolError(-32001, "Session with given id not found.");
      return {};
    },
    close() {
      calls.push({ method: "connection.close" });
    },
  };
  const session = new IntegrationSession(
    {},
    connection,
    {
      targetInfo: async () => ({
        targetId: "page",
        browserContextId: "context",
      }),
      isPageClosed: () => closed,
      onPageClose(page, callback) {
        page.on("close", callback);
        return () => page.off("close", callback);
      },
      disconnect: async () => {},
    },
    undefined,
    {},
    {},
  );
  return { page, session, calls, completeAttach };
}

test("session close waits for an explicit detach already in progress", async () => {
  const { page, session, calls } = fixture();
  const handle = await session.forPage(page);
  let finishDetach;
  const call = session.connection.call.bind(session.connection);
  session.connection.call = (method, ...args) => {
    if (method === "Target.detachFromTarget")
      return new Promise((resolve) => {
        finishDetach = resolve;
      });
    return call(method, ...args);
  };
  const detached = handle.detach();
  const closing = session.close();
  await Promise.resolve();
  assert.equal(
    calls.some((c) => c.method === "connection.close"),
    false,
  );
  finishDetach({});
  await Promise.all([detached, closing]);
  assert.equal(calls.at(-1).method, "connection.close");
});

test("releasing a handle aborts its pending calls without closing the shared transport", async () => {
  const { page, session } = fixture();
  const handle = await session.forPage(page);
  const call = session.connection.call.bind(session.connection);
  session.connection.call = (method, params, options) => {
    if (method === "Mimic.getStatus")
      return new Promise((_, reject) => {
        options.signal.addEventListener(
          "abort",
          () => reject(options.signal.reason),
          { once: true },
        );
      });
    return call(method, params, options);
  };
  const pending = assert.rejects(handle.getStatus(), /handle closed/);
  await handle.close();
  await pending;
  await session.mimic.getVersion();
  await session.close();
});

test("detach does not hide an unrelated protocol failure", async () => {
  const { page, session } = fixture();
  const handle = await session.forPage(page);
  const original = session.connection.call.bind(session.connection);
  const failure = new ProtocolError(-32000, "Permission denied");
  session.connection.call = (method, ...args) =>
    method === "Target.detachFromTarget"
      ? Promise.reject(failure)
      : original(method, ...args);
  await assert.rejects(handle.close(), (error) => error === failure);
  await assert.rejects(handle.close(), (error) => error === failure);
  await session.close();
  assert.deepEqual(session.closeErrors, [failure]);
});

test("page handles deduplicate, detach once and reject calls after release", async () => {
  const { page, session, calls } = fixture();
  const [first, second] = await Promise.all([
    session.forPage(page),
    session.forPage(page),
  ]);
  assert.equal(first, second);
  assert.equal(
    calls.filter((c) => c.method === "Target.attachToTarget").length,
    1,
  );
  await Promise.all([first.close(), first.detach()]);
  assert.equal(first.closed, true);
  assert.equal(
    calls.filter((c) => c.method === "Target.detachFromTarget").length,
    1,
  );
  assert.equal(page.listenerCount("close"), 0);
  await assert.rejects(first.getStatus(), /handle closed/);
  await assert.rejects(first.experimental.getStatus(), /handle closed/);
  const third = await session.forPage(page);
  assert.notEqual(third, first);
  page.close();
  assert.equal(third.closed, true);
  await third.close();
  assert.equal(page.listenerCount("close"), 0);
  assert.equal(
    calls.filter((c) => c.method === "Target.detachFromTarget").length,
    2,
  );
  assert.deepEqual(session.closeErrors, []);
  await session.close();
});

for (const reason of ["page", "session"]) {
  test(`late attachment is disposed when ${reason} closes during acquisition`, async () => {
    const { page, session, calls, completeAttach } = fixture({ delayed: true });
    const attaching = assert.rejects(session.forPage(page), /closed/);
    await Promise.resolve();
    const closing = reason === "session" ? session.close() : page.close();
    completeAttach({ sessionId: "late" });
    await attaching;
    await closing;
    await session.close();
    assert.equal(
      calls.filter((c) => c.method === "Target.detachFromTarget").length,
      1,
    );
    assert.equal(page.listenerCount("close"), 0);
    const detach = calls.findIndex(
      (c) => c.method === "Target.detachFromTarget",
    );
    assert.ok(detach < calls.findIndex((c) => c.method === "connection.close"));
  });
}
