import asyncio
import os
import platform
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from mimic import RuntimeManager, ProtocolError
from mimic.playwright.sync_api import launch, connect
from mimic.playwright.async_api import launch as launch_async, connect as connect_async

if platform.system() != "Linux":
    raise RuntimeError("Live integration tests require WSL/Linux")
ROOT = os.getenv("MIMIC_TEST_CACHE", "/tmp/mimic-sdk-shared-cache")
ARCHIVE = os.getenv("MIMIC_TEST_ARCHIVE")
if not ARCHIVE:
    raise RuntimeError("Set MIMIC_TEST_ARCHIVE to the official pinned Linux release archive")
manager = RuntimeManager(runtime_dir=ROOT, allow_download=False)
manager.install(archive_path=ARCHIVE)
PINNED_RELEASE = manager.resolve_lock()["release"]
HTML = "<title>SDK fixture</title><input><button onclick=\"document.querySelector('h1').textContent=document.querySelector('input').value\">Save</button><h1>before</h1>"
class FixtureHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(HTML.encode())
    def log_message(self, *_):
        pass
fixture = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
threading.Thread(target=fixture.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{fixture.server_port}"

def tearDownModule():
    fixture.shutdown()
    fixture.server_close()


class SyncTests(unittest.TestCase):
    def test_real_framework_extensions_scope_and_teardown(self):
        with launch(runtime_dir=ROOT, allow_download=False) as session:
            process = session.runtime.process
            self.assertEqual(session.mimic.get_version().version, PINNED_RELEASE)
            self.assertEqual(session.mimic.experimental.getVersion()["version"], PINNED_RELEASE)
            with self.assertRaises(ProtocolError) as caught:
                session.mimic.experimental.call("unknownFutureCommand", None)
            self.assertEqual(caught.exception.code, -32601)
            context = session.new_context(media={"devices": []}, resource_policy={"presets": ["noVisualAssets"]})
            page = context.new_page()
            page.goto(URL)
            page.locator("input").fill("Python SDK")
            page.locator("button").click()
            self.assertEqual(page.locator("h1").text_content(), "Python SDK")
            extension = session.for_page(page)
            extension.start_trace()
            self.assertIsNotNone(extension.get_status())
            extension.stop_trace()
        self.assertIsNotNone(process.poll())

    def test_detach_preserves_borrowed_driver_and_runtime(self):
        from playwright.sync_api import sync_playwright
        with RuntimeManager(runtime_dir=ROOT, allow_download=False).launch() as runtime:
            with sync_playwright() as driver:
                retained = runtime.connection.call("Target.createBrowserContext", {})
                with connect(runtime.endpoint, playwright=driver) as session:
                    session.new_context()
                self.assertEqual(runtime.connection.call("Mimic.getVersion")["version"], PINNED_RELEASE)
                self.assertEqual(runtime.connection.call("Target.getBrowserContexts")["browserContextIds"], [retained["browserContextId"]])
                browser = driver.chromium.connect_over_cdp(runtime.endpoint)
                browser.close()


class AsyncTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_cleans_late_context_and_teardown(self):
        session = await launch_async(runtime_dir=ROOT, allow_download=False)
        process = session.runtime.process
        operation = asyncio.create_task(session.new_context(media={"devices": []}))
        await asyncio.sleep(0)
        operation.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertEqual((await session.connection.call_async("Target.getBrowserContexts"))["browserContextIds"], [])
        closing = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        closing.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await closing
        self.assertIsNotNone(process.poll())
        await session.close()

    async def test_real_async_framework(self):
        async with await launch_async(runtime_dir=ROOT, allow_download=False) as session:
            process = session.runtime.process
            self.assertEqual((await session.mimic.get_version()).version, PINNED_RELEASE)
            values = await asyncio.gather(*(session.mimic.experimental.getVersion() for _ in range(8)))
            self.assertTrue(all(value["version"] == PINNED_RELEASE for value in values))
            context = await session.new_context(media={"devices": []})
            page = await context.new_page()
            await page.goto(URL)
            await page.locator("input").fill("Async SDK")
            await page.locator("button").click()
            self.assertEqual(await page.locator("h1").text_content(), "Async SDK")
            extension = await session.for_page(page)
            await extension.start_trace()
            await extension.stop_trace()
        self.assertIsNotNone(process.poll())


if __name__ == "__main__":
    unittest.main()
