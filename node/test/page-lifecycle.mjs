import test from "node:test";
import assert from "node:assert/strict";
import * as playwright from "../dist/playwright.js";
import * as puppeteer from "../dist/puppeteer.js";
import { ProtocolError } from "../dist/protocol.js";

if (process.platform !== "linux")
  throw new Error("Live tests require WSL/Linux");
for (const [name, adapter] of Object.entries({ playwright, puppeteer })) {
  test(`${name}: native Page ownership releases raw extension attachments`, async () => {
    const session = await adapter.launch({
      executablePath: process.env.MIMIC_CANDIDATE,
      allowDownload: false,
    });
    const ids = [];
    const detaches = [];
    const call = session.connection.call.bind(session.connection);
    session.connection.call = async (method, params, options) => {
      if (method === "Target.detachFromTarget") detaches.push(params.sessionId);
      const result = await call(method, params, options);
      if (method === "Target.attachToTarget") ids.push(result.sessionId);
      return result;
    };
    try {
      const context = await session.newContext();
      const page = await context.newPage();
      const [first, same] = await Promise.all([
        session.forPage(page),
        session.forPage(page),
      ]);
      assert.equal(first, same);
      await first.getStatus();
      await first.detach();
      await first.close();
      assert.equal(page.isClosed(), false);
      await assert.rejects(
        call("Mimic.getStatus", {}, { sessionId: ids[0] }),
        ProtocolError,
      );
      const second = await session.forPage(page);
      assert.notEqual(second, first);
      await page.close();
      assert.equal(second.closed, true);
      await second.close();
      await assert.rejects(second.getStatus(), /handle closed/);
      await assert.rejects(
        call("Mimic.getStatus", {}, { sessionId: ids[1] }),
        ProtocolError,
      );
      assert.deepEqual(detaches, ids);
      assert.deepEqual(session.closeErrors, []);
    } finally {
      await session.close();
    }
    assert.deepEqual(session.closeErrors, []);
  });
}
