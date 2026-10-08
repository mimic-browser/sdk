import asyncio
import os
import platform
import sys
import unittest

from mimic.playwright.sync_api import launch as launch_sync
from mimic.playwright.async_api import launch as launch_async
from mimic.pyppeteer import launch as launch_pyppeteer

if platform.system() != "Linux":
    raise RuntimeError("Live tests require WSL/Linux")
CANDIDATE = os.getenv("MIMIC_CANDIDATE", "/tmp/mimic-sdk-runtime-candidate/mimic")
OPTIONS = {"executable_path": CANDIDATE, "allow_download": False, "runtime_dir": "/tmp/mimic-sdk-candidate-cache"}


class SyncBridge(unittest.TestCase):
    def test_managed_profile_real_context(self):
        with launch_sync(**OPTIONS) as session:
            for seed in ("python-0", "python-1", "python-2"):
                context = session.new_context(profile={"generate": {"seed": seed}}, media={"devices": []})
                self.assertEqual(context.pages, [])
                page = context.new_page()
                self.assertGreater(page.evaluate("navigator.hardwareConcurrency"), 0)
                protocol = context.new_cdp_session(page)
                with self.assertRaisesRegex(Exception, "profileLocked"):
                    protocol.send("Emulation.setDeviceMetricsOverride", {"width": 999, "height": 999, "deviceScaleFactor": 1, "mobile": False})
                protocol.detach()
                context.close()
            with self.assertRaisesRegex(Exception, "Managed profile owns emulation"):
                session.new_context(profile={"generate": {}}, viewport={"width": 1, "height": 1})


class AsyncBridge(unittest.IsolatedAsyncioTestCase):
    async def test_managed_profile_real_context(self):
        async with await launch_async(**OPTIONS) as session:
            context = await session.new_context(profile={"generate": {"seed": "python-async"}})
            self.assertEqual(context.pages, [])
            page = await context.new_page()
            self.assertGreater(await page.evaluate("navigator.hardwareConcurrency"), 0)
            extension = await session.for_page(page)
            await extension.start_trace()
            await extension.stop_trace()


class PyppeteerBridge(unittest.IsolatedAsyncioTestCase):
    async def test_factory_can_close_owned_session(self):
        session = await launch_pyppeteer(**OPTIONS)
        async def media(_):
            await session.close()
            return {"devices": []}
        with self.assertRaisesRegex(Exception, "Integration session closed"):
            await asyncio.wait_for(session.new_context(media=media), timeout=10)

    async def test_managed_profile_real_context(self):
        async with await launch_pyppeteer(**OPTIONS) as session:
            context = await session.new_context(profile={"generate": {"seed": "python-pyppeteer"}})
            self.assertEqual(await context.pages(), [])
            page = await context.newPage()
            self.assertGreater(await page.evaluate("navigator.hardwareConcurrency"), 0)
            extension = await session.for_page(page)
            await extension.start_trace()
            await extension.stop_trace()


if __name__ == "__main__":
    cases = [PyppeteerBridge] if "--pyppeteer" in sys.argv else [SyncBridge, AsyncBridge]
    suite = unittest.TestSuite(unittest.defaultTestLoader.loadTestsFromTestCase(case) for case in cases)
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
