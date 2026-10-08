import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { createInterface } from "node:readline";
import * as playwright from "../dist/playwright.js";
import * as puppeteer from "../dist/puppeteer.js";
import { CDPConnection } from "../dist/protocol.js";

if (process.platform !== "linux")
  throw new Error("Fixture tests require WSL/Linux");
const child = spawn(
  process.env.MIMIC_MEDIA_FIXTURE || "/tmp/mimic-sdk-media-fixture",
  ["--browser-mode", "headless", "--listen", "127.0.0.1:0"],
  { stdio: ["ignore", "pipe", "inherit"] },
);
const lines = createInterface({ input: child.stdout });
let endpoint, fixture;
await new Promise((resolve, reject) => {
  const timer = setTimeout(
    () => reject(new Error("Media fixture startup timed out")),
    30_000,
  );
  child.once("error", reject);
  lines.on("line", (line) => {
    if (line.startsWith("Mimic listening on ")) endpoint = line.slice(19);
    if (line.startsWith("Fixture listening on ")) fixture = line.slice(21);
    if (endpoint && fixture) {
      clearTimeout(timer);
      resolve();
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
const state = () =>
  fetch(fixture + "/state").then((response) => response.json());
const configureFixture = (value) =>
  fetch(fixture + "/control", { method: "POST", body: JSON.stringify(value) });
async function capturesClosed() {
  for (let attempt = 0; attempt < 100; attempt++) {
    const value = await state();
    if (
      Object.entries(value.opens).every(
        ([id, count]) => value.closes[id] === count,
      )
    )
      return value;
    await new Promise((resolve) => setTimeout(resolve, 20));
  }
  assert.fail("Capture workers retained after track/context teardown");
}

for (const [name, adapter] of Object.entries({ playwright, puppeteer })) {
  test(
    `${name}: closing inside a media factory releases its native context`,
    { timeout: 10000 },
    async () => {
      const before = await control.call("Target.getBrowserContexts");
      const session = await adapter.connect(endpoint);
      try {
        await assert.rejects(
          session.newContext({
            media: async () => {
              await session.close();
              return { devices: [] };
            },
          }),
          /closed/,
        );
        assert.deepEqual(
          await control.call("Target.getBrowserContexts"),
          before,
        );
      } finally {
        await session.close();
      }
    },
  );
  test(
    `${name}: private media sources bind independently of public identity`,
    { timeout: 25_000 },
    async () => {
      const session = await adapter.connect(endpoint);
      let setupId, sourceId;
      const before = await state();
      async function media(setup) {
        setupId = setup.browserContextId;
        const { sources } = await setup.mimic.getMediaSources({
          browserContextId: setup.browserContextId,
        });
        const selected = (label) => {
          const source = sources.find((item) => item.label === label);
          assert.ok(source);
          assert.ok(!source.sourceId.includes("native"));
          return { sourceId: source.sourceId };
        };
        const camera = (key, native, label, group) => ({
          key,
          kind: "videoinput",
          source: selected(native),
          label,
          group,
          modes: [
            { width: 16, height: 8, frameRate: 30 },
            { width: 32, height: 16, frameRate: 30 },
          ],
          defaultMode: { width: 16, height: 8, frameRate: 30 },
          processing: { resize: "crop-and-scale" },
        });
        sourceId = selected("Private native camera B").sourceId;
        return {
          seed: "public-media-persona",
          devices: [
            camera("front", "Private native camera B", "Studio Camera", "desk"),
            {
              key: "voice",
              kind: "audioinput",
              source: selected("Private native microphone B"),
              label: "Studio Microphone",
              group: "desk",
            },
            camera("rear", "Private native camera A", "Rear Camera", "rear"),
          ],
        };
      }
      try {
        const context = await session.newContext({
          profile: { generate: { seed: "different-environment-seed" } },
          media,
        });
        const contextId = setupId;
        const initialSourceId = sourceId;
        const sourcesAfterProfile = await session.mimic.getMediaSources({
          browserContextId: contextId,
        });
        assert.ok(
          sourcesAfterProfile.sources.some(
            (item) => item.sourceId === initialSourceId,
          ),
        );
        assert.deepEqual(
          await state(),
          before,
          "discovery/configuration must not open capture",
        );
        assert.equal((await context.pages()).length, 0);
        const page = await context.newPage();
        if (process.env.MIMIC_MEDIA_DIAGNOSTIC) {
          page.on("console", (message) =>
            console.error("fixture page:", message.text()),
          );
        }
        await page.goto(fixture);
        const anonymous = await page.evaluate(async () =>
          (await navigator.mediaDevices.enumerateDevices()).map(
            ({ label, deviceId, groupId }) => ({ label, deviceId, groupId }),
          ),
        );
        assert.ok(
          anonymous.length > 0 &&
            anonymous.every(
              (item) => !item.label && !item.deviceId && !item.groupId,
            ),
        );
        await session.connection.call("Browser.grantPermissions", {
          browserContextId: contextId,
          origin: fixture,
          permissions: ["videoCapture", "audioCapture"],
        });
        // Web Audio resume follows normal activation policy. Dispatch actual
        // input through the framework before observing microphone PCM.
        await page.mouse.click(1, 1);
        const observed = await page.evaluate(async () => {
          const devices = (await navigator.mediaDevices.enumerateDevices()).map(
            (device) => device.toJSON(),
          );
          const camera = devices.find(
            (device) => device.label === "Studio Camera",
          );
          const microphone = devices.find(
            (device) => device.label === "Studio Microphone",
          );
          const stream = (globalThis.mediaStream =
            await navigator.mediaDevices.getUserMedia({
              video: { deviceId: { exact: camera.deviceId } },
              audio: { deviceId: { exact: microphone.deviceId } },
            }));
          const video = document.createElement("video");
          video.srcObject = stream;
          document.body.append(video);
          await video.play();
          await new Promise((resolve, reject) => {
            const timer = setTimeout(
              () => reject(new Error("No fixture video frame")),
              5000,
            );
            video.requestVideoFrameCallback(() => {
              clearTimeout(timer);
              resolve();
            });
          });
          const canvas = document.createElement("canvas");
          canvas.width = 16;
          canvas.height = 8;
          const draw = canvas.getContext("2d");
          draw.drawImage(video, 0, 0, 16, 8);
          const audio = new AudioContext({ sampleRate: 48000 });
          const source = audio.createMediaStreamSource(stream);
          const analyser = audio.createAnalyser();
          analyser.fftSize = 2048;
          source.connect(analyser);
          await audio.resume();
          const waveform = new Float32Array(2048);
          for (let attempt = 0; attempt < 25; attempt++) {
            await new Promise((resolve) => setTimeout(resolve, 40));
            analyser.getFloatTimeDomainData(waveform);
            if (waveform.some((value) => Math.abs(value) > 0.04)) break;
          }
          const toneEnergy = (frequency) => {
            let real = 0,
              imaginary = 0;
            for (let i = 0; i < waveform.length; i++) {
              const phase = (2 * Math.PI * frequency * i) / audio.sampleRate;
              real += waveform[i] * Math.cos(phase);
              imaginary += waveform[i] * Math.sin(phase);
            }
            return Math.hypot(real, imaginary);
          };
          const audioEnergy = { a: toneEnergy(440), b: toneEnergy(660) };
          await audio.close();
          return {
            audioEnergy,
            devices,
            tracks: stream.getTracks().map((track) => ({
              kind: track.kind,
              label: track.label,
              settings: track.getSettings(),
              capabilities: track.getCapabilities(),
            })),
            pixel: Array.from(draw.getImageData(0, 0, 1, 1).data),
          };
        });
        assert.deepEqual(observed.devices.map((item) => item.label).sort(), [
          "Rear Camera",
          "Studio Camera",
          "Studio Microphone",
        ]);
        assert.ok(!JSON.stringify(observed).includes("Private native"));
        assert.ok(!JSON.stringify(observed).includes(initialSourceId));
        const camera = observed.devices.find(
          (item) => item.label === "Studio Camera",
        );
        const microphone = observed.devices.find(
          (item) => item.label === "Studio Microphone",
        );
        assert.notEqual(camera.deviceId, microphone.deviceId);
        assert.equal(camera.groupId, microphone.groupId);
        for (const track of observed.tracks) {
          const device = observed.devices.find(
            (item) => item.label === track.label,
          );
          assert.equal(track.settings.deviceId, device.deviceId);
          assert.equal(track.settings.groupId, device.groupId);
          assert.equal(track.capabilities.deviceId, device.deviceId);
        }
        assert.deepEqual(
          observed.pixel,
          [0, 0, 255, 255],
          "native B pixels must feed logical Studio Camera",
        );
        assert.ok(
          observed.audioEnergy.b > 5 * observed.audioEnergy.a &&
            observed.audioEnergy.b > 1,
          "native microphone B's 660 Hz PCM must reach the Web Audio graph",
        );
        const live = await state();
        assert.equal(
          live.opens["native-camera-b"],
          (before.opens["native-camera-b"] || 0) + 1,
        );
        assert.equal(
          live.opens["native-microphone-b"],
          (before.opens["native-microphone-b"] || 0) + 1,
        );
        assert.equal(
          live.opens["native-camera-a"],
          before.opens["native-camera-a"],
        );
        const constraints = await page.evaluate(async () => {
          const track = mediaStream.getVideoTracks()[0];
          const before = track.getSettings();
          let error;
          try {
            await track.applyConstraints({ width: { exact: 100000 } });
          } catch (caught) {
            error = { name: caught.name, constraint: caught.constraint };
          }
          const clone = (globalThis.mediaClone = track.clone());
          await clone.applyConstraints({
            width: { exact: 32 },
            height: { exact: 16 },
          });
          return {
            error,
            before,
            after: track.getSettings(),
            clone: clone.getSettings(),
          };
        });
        assert.equal(constraints.error.name, "OverconstrainedError");
        assert.deepEqual(constraints.after, constraints.before);
        assert.equal(constraints.clone.width, 32);
        assert.equal(constraints.after.width, 16);
        const profile = await session.mimic.getMediaProfile({
          browserContextId: contextId,
        });
        await assert.rejects(
          session.mimic.setMediaProfile({
            browserContextId: contextId,
            devices: [],
          }),
        );
        assert.deepEqual(
          (await session.mimic.getMediaProfile({ browserContextId: contextId }))
            .profile,
          profile.profile,
        );
        await page.evaluate(() => {
          mediaStream.getTracks().forEach((track) => track.stop());
          mediaClone.stop();
        });
        await capturesClosed();
        await configureFixture({ fail: { "native-camera-b": true } });
        const failure = await page.evaluate(async (id) => {
          try {
            await navigator.mediaDevices.getUserMedia({
              video: { deviceId: { exact: id } },
            });
            return null;
          } catch (error) {
            return { name: error.name, message: error.message };
          }
        }, camera.deviceId);
        assert.equal(failure.name, "NotReadableError");
        assert.doesNotMatch(failure.message, /native|fixture|\/dev/);
        const diagnostic = await session.mimic.getMediaProfile({
          browserContextId: contextId,
        });
        assert.match(JSON.stringify(diagnostic.diagnostics), /native-camera-b/);
        await configureFixture({ fail: { "native-microphone-b": true } });
        const audioFailure = await page.evaluate(async (id) => {
          try {
            await navigator.mediaDevices.getUserMedia({
              audio: { deviceId: { exact: id } },
            });
            return null;
          } catch (error) {
            return { name: error.name, message: error.message };
          }
        }, microphone.deviceId);
        assert.equal(audioFailure.name, "NotReadableError");
        assert.doesNotMatch(audioFailure.message, /native|fixture|\/dev/);
        assert.match(
          JSON.stringify(
            (
              await session.mimic.getMediaProfile({
                browserContextId: contextId,
              })
            ).diagnostics,
          ),
          /native-microphone-b/,
        );
        await configureFixture({
          fail: { "native-camera-b": false, "native-microphone-b": false },
          missing: { "native-camera-b": true },
        });
        const available = await page.evaluate(async () =>
          (await navigator.mediaDevices.enumerateDevices()).map(
            (device) => device.label,
          ),
        );
        assert.deepEqual(available.sort(), [
          "Rear Camera",
          "Studio Microphone",
        ]);
        const missingCapture = await page.evaluate(async (id) => {
          try {
            await navigator.mediaDevices.getUserMedia({
              video: { deviceId: { exact: id } },
            });
            return null;
          } catch (error) {
            return { name: error.name, constraint: error.constraint };
          }
        }, camera.deviceId);
        assert.equal(missingCapture.name, "OverconstrainedError");
        assert.equal(missingCapture.constraint, "deviceId");
        assert.equal(
          (await state()).opens["native-camera-a"],
          before.opens["native-camera-a"],
          "missing exact source must not fall back to another native camera",
        );
        await configureFixture({ missing: { "native-camera-b": false } });
        const contexts = await session.connection.call(
          "Target.getBrowserContexts",
        );
        await assert.rejects(
          session.newContext({
            media: async () => {
              throw new Error("factory failure");
            },
          }),
          /factory failure/,
        );
        assert.deepEqual(
          await session.connection.call("Target.getBrowserContexts"),
          contexts,
        );
        await assert.rejects(
          session.newContext({
            media: {
              camera: {
                source: { sourceId: initialSourceId },
                profile: "usb-webcam",
              },
            },
          }),
        );
        assert.deepEqual(
          await session.connection.call("Target.getBrowserContexts"),
          contexts,
        );
        await context.close();
        await capturesClosed();
      } finally {
        await configureFixture({
          fail: { "native-camera-b": false, "native-microphone-b": false },
          missing: { "native-camera-b": false },
        });
        await session.close();
      }
    },
  );
}
