# Private capture sources and public media devices

A camera or microphone has two separate roles. `source` selects a private capture
backend. `key`, `kind`, `label`, `group`, camera `profile`/`modes` and the media
`seed` define the device observed by a page. Choosing OBS or a USB camera does not
require exposing its native name or native identifier to the website.

| Private capture selection                                        | Public device identity                                                    |
| ---------------------------------------------------------------- | ------------------------------------------------------------------------- |
| Context-bound `sourceId`, exact native label, `default` or `obs` | Explicit logical key, label and group                                     |
| Never a Web `deviceId`                                           | Origin/context-scoped Web `deviceId` and `groupId` derived by the runtime |
| Camera pixels or microphone PCM supplied by the backend          | Camera modes, settings and capabilities follow the configured device      |
| Backend errors remain available in CDP diagnostics               | Web errors omit private device paths and backend details                  |

Web IDs are derived coherently; arbitrary public `deviceId` strings are not an
override API. Use the public ID returned by `enumerateDevices()` as an exact
`getUserMedia` constraint. It resolves back to the selected private binding.
Devices sharing a logical `group` expose a shared public group without copying
the backend's physical identifiers.

## Discover in the context being configured

Native source IDs belong to one BrowserContext. Discovering in the default
context and reusing that ID in a new context is invalid. The SDK therefore accepts
a media factory, called after creating the real framework context and closing its
owned blank ID probe, before applying configuration or returning any user Page.

```typescript
const context = await session.newContext({
  profile: { generate: { seed: "desktop-persona" } },
  media: async ({ browserContextId, mimic }) => {
    const { sources } = await mimic.getMediaSources({ browserContextId });
    const camera = sources.find(
      (source) => source.label === "OBS Virtual Camera",
    );
    if (!camera) throw new Error("OBS Virtual Camera is unavailable");
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
      ],
    };
  },
});
```

The factory receives `ContextSetup.browserContextId` and the usual browser-scoped
typed `mimic` client. Pass the ID explicitly to media commands. Omitting it from
`getMediaSources`, `getMediaProfile`, `setMediaProfile`, `validateMediaProfile` or
`getMediaPresets` selects the runtime's default context, including through a
page-attached `forPage` client. Attaching a protocol session does not infer a media
context. Generated Python methods use the corresponding snake_case names.

An explicit camera in `devices` must include output `modes` and a `defaultMode`
from that list. The optional `profile` constrains the recipe; it does not populate
it. Use the `camera` shorthand when you want a complete preset.

Python's synchronous helper accepts a regular callable; its async helper accepts
a callable or async callable. Both accept generated dataclasses as configuration:

```python
from mimic.generated import GetMediaSourcesParams, MediaConfiguration, MediaDeviceProfile

async def media(setup):
    result = await setup.mimic.get_media_sources(
        GetMediaSourcesParams(browser_context_id=setup.browser_context_id)
    )
    camera = next(source for source in result.sources if source.label == "OBS Virtual Camera")
    return MediaConfiguration(devices=[MediaDeviceProfile(
        key="camera", kind="videoinput", source={"sourceId": camera.source_id},
        label="Studio Camera", group="desk",
        modes=[{"width": 640, "height": 480, "frameRate": 30}],
        default_mode={"width": 640, "height": 480, "frameRate": 30},
        processing={"resize": "crop-and-scale"},
    )])

context = await session.new_context(media=media)
```

The source binding remains valid when the helper subsequently configures a new
environment profile or seed. A failed factory/configuration closes its native
context. The helper returns the framework's original BrowserContext object.
Full camera-and-microphone examples are in `examples/node/media.mjs` and
`examples/python/media.py`.

## Permission and lifetime

Discovery and configuration do not open capture or grant site permission. Until
permission is granted, Web enumeration retains its normal redaction. Grant
permission separately through the framework or CDP when your application intends
to allow capture. `getUserMedia` opens only the selected sources.

An explicit `devices` list exposes only that logical catalog. A missing source is
omitted; an exact public ID cannot silently select another camera. Camera
constraints select supported modes; failed constraints leave existing settings
unchanged. Clones share the private capture but retain their own output settings.
Changing the catalog while capture is active is rejected. Stopping the last
track, navigation or context teardown releases capture workers.

The advanced microphone contract currently controls source and public identity.
It does not fabricate arbitrary microphone DSP capabilities or implement an
audio-output device catalog. Runtime errors and diagnostics remain authoritative.

## Runtime support and testing

The immutable v0.2.3 runtime is the SDK's default pin and includes the
configure-context bridge used by managed environment profiles. See
[supported integrations](../compatibility/README.md) for client-specific limits.

`node/test/media.mjs` and `python/tests/media.py` exercise real framework clients
against `mimic/tools/sdk-media-fixture`, a separate test executable with two
deterministic cameras and microphones. It never enumerates or opens hardware.
Camera B's blue pixels, microphone B's 660 Hz waveform observed through Web Audio
and native open counters check source routing; public
enumeration, track labels, settings and capabilities prove identity projection.
Tests also cover source/context isolation, factory rollback, missing backends,
sanitized errors, live-catalog rejection, clone constraints and teardown.

Run these checks headless on ephemeral loopback ports. Synthetic providers test
observable media behavior independently of physical drivers; hardware capture
requires separate platform-specific verification. Preserve compatibility
evidence and oracle provenance in ignored build output or CI artifacts.
