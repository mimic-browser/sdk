import { launch } from "mimic-browser/playwright";

// The current development runtime contains the managed-context bridge.
const session = await launch({
  executablePath: process.env.MIMIC_EXECUTABLE_PATH,
});
try {
  const context = await session.newContext({
    profile: { generate: { seed: "desktop-persona" } },
    media: async ({ browserContextId, mimic }) => {
      const { sources } = await mimic.getMediaSources({ browserContextId });
      const camera = sources.find(
        (source) => source.label === "OBS Virtual Camera",
      );
      const microphone = sources.find(
        (source) => source.kind === "audioinput" && source.default,
      );
      if (!camera || !microphone)
        throw new Error("Requested capture sources are unavailable");
      return {
        seed: "meeting-devices",
        devices: [
          {
            key: "camera",
            kind: "videoinput",
            source: { sourceId: camera.sourceId },
            label: "Studio Camera",
            group: "desk",
            modes: [{ width: 640, height: 480, frameRate: 30 }],
            defaultMode: { width: 640, height: 480, frameRate: 30 },
            processing: { resize: "crop-and-scale" },
          },
          {
            key: "microphone",
            kind: "audioinput",
            source: { sourceId: microphone.sourceId },
            label: "Studio Microphone",
            group: "desk",
          },
        ],
      };
    },
  });
  // Source discovery/configuration does not open capture or grant permission.
  await context.grantPermissions(["camera", "microphone"], {
    origin: "https://example.com",
  });
  const page = await context.newPage();
  await page.goto("https://example.com");
  console.log(
    await page.evaluate(async () =>
      (await navigator.mediaDevices.enumerateDevices()).map((device) =>
        device.toJSON(),
      ),
    ),
  );
} finally {
  await session.close();
}
