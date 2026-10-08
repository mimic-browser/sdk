"""Real framework media qualification against deterministic, hardware-free providers."""
import asyncio
import json
import os
import platform
import subprocess
import sys
import time
import unittest
from urllib.request import urlopen

from mimic.generated import CameraFormat, GetMediaSourcesParams, MediaConfiguration, MediaDeviceProfile, MediaProcessing
from mimic.playwright.sync_api import connect as connect_sync
from mimic.playwright.async_api import connect as connect_async
from mimic.protocol import CDPConnection


if platform.system() != "Linux":
    raise RuntimeError("Media qualification requires WSL/Linux")


def setUpModule():
    global child, endpoint, fixture, control
    child = subprocess.Popen([os.getenv("MIMIC_MEDIA_FIXTURE", "/tmp/mimic-sdk-media-fixture"),
                              "--browser-mode", "headless", "--listen", "127.0.0.1:0"],
                             stdout=subprocess.PIPE, text=True)
    endpoint = fixture = None
    while not (endpoint and fixture):
        line = child.stdout.readline()
        if not line:
            raise RuntimeError("Media fixture exited before startup")
        if line.startswith("Mimic listening on "):
            endpoint = line[19:].strip()
        if line.startswith("Fixture listening on "):
            fixture = line[21:].strip()
    control = CDPConnection(endpoint)


def tearDownModule():
    try:
        control.call("Browser.close")
    finally:
        control.close()
        child.terminate()
        child.wait(timeout=10)
        child.stdout.close()


def state():
    with urlopen(fixture + "/state") as response:
        return json.load(response)


def captures_closed():
    for _ in range(100):
        observed = state()
        if all(observed["closes"].get(key) == count for key, count in observed["opens"].items()):
            return
        time.sleep(0.02)
    raise AssertionError("Capture workers retained after context/navigation teardown")


def configuration(sources):
    def private_source(label):
        return {"sourceId": next(source.source_id for source in sources.sources if source.label == label)}
    return MediaConfiguration(seed="public-media-persona", devices=[
        MediaDeviceProfile(key="front", kind="videoinput", label="Studio Camera", group="desk",
                           source=private_source("Private native camera B"),
                           modes=[CameraFormat(width=16, height=8, frame_rate=30)],
                           default_mode=CameraFormat(width=16, height=8, frame_rate=30),
                           processing=MediaProcessing(resize="crop-and-scale")),
        MediaDeviceProfile(key="voice", kind="audioinput", label="Studio Microphone", group="desk",
                           source=private_source("Private native microphone B")),
    ])


CAPTURE = """async () => {
  const devices = (await navigator.mediaDevices.enumerateDevices()).map(device => device.toJSON());
  const camera = devices.find(device => device.kind === 'videoinput');
  const microphone = devices.find(device => device.kind === 'audioinput');
  globalThis.mediaStream = await navigator.mediaDevices.getUserMedia({
    video: {deviceId: {exact: camera.deviceId}}, audio: {deviceId: {exact: microphone.deviceId}}
  });
  return {devices, tracks: mediaStream.getTracks().map(track => ({
    label: track.label, settings: track.getSettings(), capabilities: track.getCapabilities()
  }))};
}"""


class MediaAssertions:
    def assert_identity(self, observed):
        self.assertEqual(sorted(device["label"] for device in observed["devices"]), ["Studio Camera", "Studio Microphone"])
        self.assertNotIn("Private native", json.dumps(observed))
        self.assertEqual(observed["devices"][0]["groupId"], observed["devices"][1]["groupId"])
        for track in observed["tracks"]:
            device = next(device for device in observed["devices"] if device["label"] == track["label"])
            self.assertEqual(track["settings"]["deviceId"], device["deviceId"])
            self.assertEqual(track["settings"]["groupId"], device["groupId"])
            self.assertEqual(track["capabilities"]["deviceId"], device["deviceId"])


class SyncMedia(MediaAssertions, unittest.TestCase):
    def test_typed_factory_source_identity_isolation_and_navigation_teardown(self):
        setup_ids, source_ids = [], []
        before = state()
        def media(setup):
            setup_ids.append(setup.browser_context_id)
            sources = setup.mimic.get_media_sources(GetMediaSourcesParams(browser_context_id=setup.browser_context_id))
            source_ids.append([source.source_id for source in sources.sources])
            return configuration(sources)
        with connect_sync(endpoint) as session:
            first = session.new_context(profile={"generate": {"seed": "python-identity-a"}}, media=media)
            second = session.new_context(profile={"generate": {"seed": "python-identity-b"}}, media=media)
            self.assertEqual(first.pages + second.pages, [])
            self.assertEqual(state(), before)
            self.assertNotEqual(setup_ids[0], setup_ids[1])
            self.assertFalse(set(source_ids[0]) & set(source_ids[1]))
            observed = []
            for context, context_id in zip((first, second), setup_ids):
                after = session.mimic.get_media_sources(GetMediaSourcesParams(browser_context_id=context_id))
                self.assertEqual([source.source_id for source in after.sources], source_ids[len(observed)])
                context.grant_permissions(["camera", "microphone"], origin=fixture)
                page = context.new_page()
                page.goto(fixture)
                result = page.evaluate(CAPTURE)
                self.assert_identity(result)
                observed.append(result)
                page.goto("about:blank")
                captures_closed()
            self.assertNotEqual(observed[0]["devices"][0]["deviceId"], observed[1]["devices"][0]["deviceId"])
            live = state()
            self.assertEqual(live["opens"]["native-camera-b"], before["opens"].get("native-camera-b", 0) + 2)
            self.assertEqual(live["opens"]["native-microphone-b"], before["opens"].get("native-microphone-b", 0) + 2)
            self.assertEqual(live["opens"].get("native-camera-a"), before["opens"].get("native-camera-a"))

    def test_factory_failure_closes_native_context(self):
        def failure(_):
            raise ValueError("factory failure")
        with connect_sync(endpoint) as session:
            before = session.connection.call("Target.getBrowserContexts")
            with self.assertRaisesRegex(ValueError, "factory failure"):
                session.new_context(media=failure)
            self.assertEqual(session.connection.call("Target.getBrowserContexts"), before)

    def test_factory_can_close_its_own_session(self):
        session = connect_sync(endpoint)
        before = control.call("Target.getBrowserContexts")
        def media(_):
            session.close()
            return {"devices": []}
        with self.assertRaisesRegex(Exception, "Integration session closed"):
            session.new_context(media=media)
        self.assertEqual(control.call("Target.getBrowserContexts"), before)


class AsyncMedia(MediaAssertions, unittest.IsolatedAsyncioTestCase):
    async def test_factory_can_await_close_without_deadlock(self):
        session = await connect_async(endpoint)
        before = control.call("Target.getBrowserContexts")
        async def media(_):
            await session.close()
            return {"devices": []}
        with self.assertRaisesRegex(Exception, "Integration session closed"):
            await asyncio.wait_for(session.new_context(media=media), timeout=10)
        self.assertEqual(control.call("Target.getBrowserContexts"), before)

    async def test_async_typed_factory_capture_and_context_teardown(self):
        context_ids = []
        async def media(setup):
            context_ids.append(setup.browser_context_id)
            sources = await setup.mimic.get_media_sources(GetMediaSourcesParams(browser_context_id=setup.browser_context_id))
            return configuration(sources)
        async with await connect_async(endpoint) as session:
            context = await session.new_context(profile={"generate": {"seed": "python-async-media"}}, media=media)
            self.assertEqual(context.pages, [])
            await context.grant_permissions(["camera", "microphone"], origin=fixture)
            page = await context.new_page()
            await page.goto(fixture)
            self.assert_identity(await page.evaluate(CAPTURE))
            await context.close()
            await asyncio.to_thread(captures_closed)
            before = await session.connection.call_async("Target.getBrowserContexts")
            async def failure(_):
                await asyncio.sleep(0)
                raise ValueError("async factory failure")
            with self.assertRaisesRegex(ValueError, "async factory failure"):
                await session.new_context(media=failure)
            self.assertEqual(await session.connection.call_async("Target.getBrowserContexts"), before)


class PyppeteerMedia(MediaAssertions, unittest.IsolatedAsyncioTestCase):
    async def test_private_sources_public_identity_and_context_teardown(self):
        from mimic.pyppeteer import connect
        context_ids = []
        before = state()
        async def media(setup):
            context_ids.append(setup.browser_context_id)
            sources = await setup.mimic.get_media_sources(GetMediaSourcesParams(browser_context_id=setup.browser_context_id))
            return configuration(sources)
        async with await connect(endpoint) as session:
            context = await session.new_context(media=media)
            self.assertEqual(await context.pages(), [])
            self.assertEqual(state(), before)
            await session.connection.call_async("Browser.grantPermissions", {
                "browserContextId": context_ids[0], "origin": fixture,
                "permissions": ["videoCapture", "audioCapture"],
            })
            page = await context.newPage()
            await page.goto(fixture)
            self.assert_identity(await page.evaluate(CAPTURE))
            live = state()
            for source in ("native-camera-b", "native-microphone-b"):
                self.assertEqual(live["opens"][source], before["opens"].get(source, 0) + 1)
            self.assertEqual(live["opens"].get("native-camera-a"), before["opens"].get("native-camera-a"))
            self.assertEqual(live["opens"].get("native-microphone-a"), before["opens"].get("native-microphone-a"))
            await context.close()
            await asyncio.to_thread(captures_closed)


if __name__ == "__main__":
    cases = ("PyppeteerMedia",) if "--pyppeteer" in sys.argv else ("SyncMedia", "AsyncMedia")
    unittest.main(argv=[sys.argv[0]], defaultTest=cases, verbosity=2)
