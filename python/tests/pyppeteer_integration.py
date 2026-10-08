import asyncio
import os
import platform
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading

from mimic.pyppeteer import launch, connect

if platform.system() != "Linux":
    raise RuntimeError("Live tests require WSL/Linux")
ROOT = os.getenv("MIMIC_TEST_CACHE", "/tmp/mimic-sdk-shared-cache")
CANDIDATE = os.getenv("MIMIC_CANDIDATE")


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(b"<title>Pyppeteer fixture</title><input><button onclick=\"document.querySelector('input').value='clicked'\">Save</button>")
    def log_message(self, *_):
        pass


class PyppeteerTests(unittest.IsolatedAsyncioTestCase):
    async def test_real_pyppeteer_and_external_ownership(self):
        fixture = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=fixture.serve_forever, daemon=True).start()
        try:
            async with await launch(runtime_dir=ROOT, executable_path=CANDIDATE, allow_download=False) as session:
                version = (await session.mimic.get_version()).version
                self.assertEqual(version, session.runtime.identity["version"])
                context = await session.new_context(media={"devices": []})
                page = await context.newPage()
                await page.goto(f"http://127.0.0.1:{fixture.server_port}")
                await page.type("input", "pyppeteer")
                await page.click("button")
                self.assertEqual(await page.evaluate("document.querySelector('input').value"), "clicked")
                extension = await session.for_page(page)
                await extension.start_trace()
                await extension.stop_trace()
                other = await connect(session.runtime.endpoint)
                await other.new_context()
                await other.close()
                self.assertEqual((await session.mimic.experimental.getVersion())["version"], version)
        finally:
            fixture.shutdown()
            fixture.server_close()


if __name__ == "__main__":
    unittest.main()
