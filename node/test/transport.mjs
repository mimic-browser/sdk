import test from "node:test";
import assert from "node:assert/strict";
import { WebSocketServer } from "ws";
import {
  CDPConnection,
  ProtocolError,
  experimental,
  OMITTED,
} from "../dist/protocol.js";
if (process.platform !== "linux")
  throw new Error("Listener tests require WSL/Linux");

test("schema-independent raw dispatch preserves omission, null, errors, scope and concurrent routing", async () => {
  const server = new WebSocketServer({ host: "127.0.0.1", port: 0 });
  await new Promise((resolve) => server.once("listening", resolve));
  const requests = [];
  server.on("connection", (socket) =>
    socket.on("message", (raw) => {
      const request = JSON.parse(raw);
      requests.push(request);
      const response =
        request.method === "Mimic.fail"
          ? {
              id: request.id,
              error: {
                code: -32601,
                message: "Future command is absent",
                data: { nested: [null, true] },
              },
            }
          : {
              id: request.id,
              result: {
                present: Object.hasOwn(request, "params"),
                params: request.params,
                sessionId: request.sessionId,
              },
            };
      if (request.sessionId !== undefined)
        response.sessionId = request.sessionId;
      socket.send(
        JSON.stringify({
          id: request.id,
          sessionId: "foreign-session",
          result: { wrong: true },
        }),
      );
      setTimeout(() => {
        if (socket.readyState === 1) socket.send(JSON.stringify(response));
      }, request.params?.delay || 0);
    }),
  );
  const connection = await CDPConnection.connect(
    `ws://127.0.0.1:${server.address().port}`,
  );
  try {
    const proxy = experimental((method, params) =>
      connection.call(method, params, { sessionId: "page-explicit" }),
    );
    assert.equal((await proxy.newUnknownCommand()).present, false);
    assert.deepEqual(await proxy.call("newUnknownCommand", null), {
      present: true,
      params: null,
      sessionId: "page-explicit",
    });
    await assert.rejects(
      proxy.fail(),
      (error) =>
        error instanceof ProtocolError &&
        error.code === -32601 &&
        error.message === "Future command is absent" &&
        error.data.nested[0] === null,
    );
    const values = await Promise.all(
      [30, 1, 20, 2].map((delay) => proxy.newUnknownCommand({ delay })),
    );
    assert.deepEqual(
      values.map((value) => value.params.delay),
      [30, 1, 20, 2],
    );
    const cancellation = new AbortController();
    const pending = connection.call(
      "Mimic.late",
      { delay: 50 },
      { signal: cancellation.signal },
    );
    cancellation.abort(new Error("cancelled"));
    await assert.rejects(pending, /cancelled/);
    await assert.rejects(
      connection.call("Mimic.late", { delay: 50 }, { timeout: 1 }),
      /timed out/,
    );
    assert.equal((await proxy.newUnknownCommand()).present, false);
    assert.ok(requests.every((request) => request.method.startsWith("Mimic.")));
    assert.equal(
      requests.filter((request) => request.method === "Mimic.late").length,
      2,
    );
    const disconnected = connection.call("Mimic.late", { delay: 500 });
    connection.close();
    await assert.rejects(disconnected, /closed/);
  } finally {
    connection.close();
    for (const socket of server.clients) socket.terminate();
    await new Promise((resolve) => server.close(resolve));
  }
});
