"""Real native Page teardown with the shared raw extension transport."""
import asyncio
import os
import platform
import unittest

from mimic.protocol import ConnectionClosed, ProtocolError

if platform.system() != "Linux":
    raise RuntimeError("Live tests require WSL/Linux")
OPTIONS = {"executable_path": os.environ.get("MIMIC_CANDIDATE"), "allow_download": False}


class Sync(unittest.TestCase):
    def test_page_handle_lifecycle(self):
        from mimic.playwright.sync_api import launch
        with launch(**OPTIONS) as session:
            ids, detaches = [], []
            call = session.connection.call
            def traced(method, params=None, session_id=None, **kwargs):
                if method == "Target.detachFromTarget":
                    detaches.append(params["sessionId"])
                result = call(method, params, session_id, **kwargs)
                if method == "Target.attachToTarget":
                    ids.append(result["sessionId"])
                return result
            session.connection.call = traced
            context = session.new_context()
            page = context.new_page()
            first = session.for_page(page)
            self.assertIs(first, session.for_page(page))
            first.get_status()
            first.detach()
            first.close()
            self.assertFalse(page.is_closed())
            with self.assertRaises(ProtocolError):
                call("Mimic.getStatus", {}, ids[0])
            second = session.for_page(page)
            page.close()
            second.close()
            self.assertTrue(second.closed)
            with self.assertRaises(ConnectionClosed):
                second.get_status()
            with self.assertRaises(ProtocolError):
                call("Mimic.getStatus", {}, ids[1])
            self.assertEqual(detaches, ids)
            self.assertEqual(session.close_errors, [])


class AsyncPlaywright(unittest.IsolatedAsyncioTestCase):
    legacy = False

    async def test_page_handle_lifecycle(self):
        if self.legacy:
            from mimic.pyppeteer import launch
        else:
            from mimic.playwright.async_api import launch
        async with await launch(**OPTIONS) as session:
            ids, detaches = [], []
            call = session.connection.call_async
            async def traced(method, params=None, session_id=None, **kwargs):
                if method == "Target.detachFromTarget":
                    detaches.append(params["sessionId"])
                result = await call(method, params, session_id, **kwargs)
                if method == "Target.attachToTarget":
                    ids.append(result["sessionId"])
                return result
            session.connection.call_async = traced
            context = await session.new_context()
            page = await (context.newPage() if self.legacy else context.new_page())
            first, same = await asyncio.gather(session.for_page(page), session.for_page(page))
            self.assertIs(first, same)
            await first.get_status()
            await asyncio.gather(first.detach(), first.close())
            self.assertFalse(page.isClosed() if self.legacy else page.is_closed())
            with self.assertRaises(ProtocolError):
                await call("Mimic.getStatus", {}, ids[0])
            second = await session.for_page(page)
            await page.close()
            await second.close()
            self.assertTrue(second.closed)
            with self.assertRaises(ConnectionClosed):
                await second.get_status()
            with self.assertRaises(ProtocolError):
                await call("Mimic.getStatus", {}, ids[1])
            self.assertEqual(detaches, ids)
            self.assertEqual(session.close_errors, [])


class Pyppeteer(AsyncPlaywright):
    legacy = True


if __name__ == "__main__":
    unittest.main()
