import asyncio
import concurrent.futures
import threading
from types import SimpleNamespace
import unittest

from mimic._page import AsyncPageBindings, PageBindings
from mimic.protocol import CDPConnection, ConnectionClosed, ProtocolError


class Page:
    def __init__(self):
        self.listeners = []
        self.closed = False

    def on(self, event, callback):
        self.listeners.append(callback)

    def remove_listener(self, event, callback):
        self.listeners.remove(callback)

    def close(self):
        self.closed = True
        for callback in list(self.listeners):
            callback()


class Connection:
    def __init__(self, page):
        self.page, self.calls = page, []
        self.gate = None
        self.started = None

    def cancel_session(self, session_id):
        self.calls.append(("cancel", session_id))

    def call(self, method, params, session_id=None):
        self.calls.append((method, params))
        if method == "Target.attachToTarget":
            return {"sessionId": "owned"}
        if method == "Target.detachFromTarget" and self.page.closed:
            raise ProtocolError(-32001, "Session with given id not found.")
        return {}

    async def call_async(self, method, params, session_id=None):
        if method == "Target.attachToTarget" and self.gate:
            self.started.set()
            await self.gate.wait()
        return self.call(method, params, session_id)


def fixture(async_api=False):
    page = Page()
    connection = Connection(page)
    session = SimpleNamespace(connection=connection, _closed=False, close_errors=[], _inflight=set())
    bindings = (AsyncPageBindings if async_api else PageBindings)(session)
    return page, connection, session, bindings


class SyncPageTests(unittest.TestCase):
    def test_session_release_rejects_only_its_pending_calls(self):
        connection = object.__new__(CDPConnection)
        connection._lock = threading.Lock()
        selected, unrelated = concurrent.futures.Future(), concurrent.futures.Future()
        selected.session_id, unrelated.session_id = "page", "other"
        connection._pending = {1: selected, 2: unrelated}
        connection.cancel_session("page")
        with self.assertRaises(ConnectionClosed):
            selected.result()
        self.assertFalse(unrelated.done())

    def test_detach_preserves_unrelated_failure(self):
        page, connection, _, bindings = fixture()
        handle = bindings.for_page(page, lambda: "page", lambda: page.closed)
        failure = ProtocolError(-32000, "Permission denied")
        def denied(*_):
            raise failure
        connection.call = denied
        for _ in range(2):
            with self.assertRaises(ProtocolError) as caught:
                handle.close()
            self.assertIs(caught.exception, failure)

    def test_framework_bag_preserves_native_keywords_and_detects_conflicts(self):
        from mimic.playwright.sync_api import IntegrationSession
        session = object.__new__(IntegrationSession)
        session._closed, session._contexts, session.close_errors = False, [], []
        observed = []
        def create(**options):
            observed.append(options)
            return object()
        session.browser = SimpleNamespace(new_context=create)
        session.new_context(framework={"locale": "en-US"}, viewport={"width": 800, "height": 600})
        session.new_context(framework={"proxy": {"server": "http://localhost:8080"}})
        self.assertEqual(observed[0], {"locale": "en-US", "viewport": {"width": 800, "height": 600}})
        self.assertEqual(observed[1], {"proxy": {"server": "http://localhost:8080"}})
        with self.assertRaisesRegex(TypeError, "both"):
            session.new_context(framework={"locale": "en"}, locale="fr")
        with self.assertRaisesRegex(Exception, "Managed profile owns"):
            session.new_context(profile={"generate": {}}, framework={"proxy": {"server": "http://localhost:8080"}})
        self.assertEqual(len(observed), 2)

    def test_explicit_detach_and_page_close_own_only_attachment(self):
        page, connection, session, bindings = fixture()
        first = bindings.for_page(page, lambda: "page", lambda: page.closed)
        self.assertIs(first, bindings.for_page(page, lambda: "page", lambda: page.closed))
        first.detach()
        first.close()
        self.assertFalse(page.closed)
        self.assertTrue(first.closed)
        self.assertEqual(page.listeners, [])
        with self.assertRaises(ConnectionClosed):
            first.get_status()
        with self.assertRaises(ConnectionClosed):
            first.experimental.getStatus()
        second = bindings.for_page(page, lambda: "page", lambda: page.closed)
        page.close()
        second.close()
        self.assertTrue(second.closed)
        self.assertEqual(bindings.handles, set())
        self.assertEqual(bindings.pages, {})
        self.assertEqual(session.close_errors, [])
        self.assertEqual(sum(method == "Target.detachFromTarget" for method, _ in connection.calls), 2)


class AsyncPageTests(unittest.IsolatedAsyncioTestCase):
    async def target(self):
        return "page"

    async def test_framework_bag_preserves_native_keywords_and_detects_conflicts(self):
        from mimic.playwright.async_api import IntegrationSession
        session = IntegrationSession()
        observed = []
        async def create(**options):
            observed.append(options)
            return object()
        session.browser = SimpleNamespace(new_context=create)
        await session.new_context(framework={"locale": "en-US"}, viewport={"width": 800, "height": 600})
        await session.new_context(framework={"proxy": {"server": "http://localhost:8080"}})
        with self.assertRaisesRegex(TypeError, "both"):
            await session.new_context(framework={"locale": "en"}, locale="fr")
        with self.assertRaisesRegex(Exception, "Managed profile owns"):
            await session.new_context(profile={"generate": {}}, framework={"proxy": {"server": "http://localhost:8080"}})
        self.assertEqual(observed, [{"locale": "en-US", "viewport": {"width": 800, "height": 600}},
                                    {"proxy": {"server": "http://localhost:8080"}}])

    async def test_dedup_detach_and_native_page_close(self):
        page, connection, session, bindings = fixture(True)
        first, second = await asyncio.gather(*(bindings.for_page(page, self.target, lambda: page.closed) for _ in range(2)))
        self.assertIs(first, second)
        await asyncio.gather(first.close(), first.detach())
        self.assertFalse(page.closed)
        with self.assertRaises(ConnectionClosed):
            await first.get_status()
        third = await bindings.for_page(page, self.target, lambda: page.closed)
        page.close()
        self.assertTrue(third.closed)
        await third.close()
        await bindings.close()
        self.assertEqual(page.listeners, [])
        self.assertEqual(session.close_errors, [])
        self.assertEqual(sum(method == "Target.detachFromTarget" for method, _ in connection.calls), 2)

    async def test_late_attachment_after_page_close_session_close_or_cancellation(self):
        for reason in ("page", "session", "cancel"):
            with self.subTest(reason=reason):
                page, connection, session, bindings = fixture(True)
                connection.gate, connection.started = asyncio.Event(), asyncio.Event()
                operation = asyncio.create_task(bindings.for_page(page, self.target, lambda: page.closed))
                await connection.started.wait()
                if reason == "page":
                    page.close()
                elif reason == "session":
                    session._closed = True
                else:
                    operation.cancel()
                connection.gate.set()
                with self.assertRaises(asyncio.CancelledError if reason == "cancel" else ConnectionClosed):
                    await operation
                await bindings.close()
                self.assertEqual(bindings.handles, set())
                self.assertEqual(bindings.pages, {})
                self.assertEqual(page.listeners, [])
                self.assertEqual(sum(method == "Target.detachFromTarget" for method, _ in connection.calls), 1)

    async def test_cancelling_one_waiter_preserves_another_claim(self):
        page, connection, _, bindings = fixture(True)
        connection.gate, connection.started = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(bindings.for_page(page, self.target, lambda: page.closed))
        second = asyncio.create_task(bindings.for_page(page, self.target, lambda: page.closed))
        await connection.started.wait()
        first.cancel()
        connection.gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await first
        handle = await second
        self.assertFalse(handle.closed)
        await handle.close()

    async def test_repeated_cancellation_cannot_abandon_late_attachment(self):
        page, connection, _, bindings = fixture(True)
        connection.gate, connection.started = asyncio.Event(), asyncio.Event()
        operation = asyncio.create_task(bindings.for_page(page, self.target, lambda: page.closed))
        await connection.started.wait()
        operation.cancel()
        await asyncio.sleep(0)
        operation.cancel()
        await asyncio.sleep(0)
        connection.gate.set()
        with self.assertRaises(asyncio.CancelledError):
            await operation
        self.assertEqual(bindings.handles, set())
        self.assertEqual(sum(method == "Target.detachFromTarget" for method, _ in connection.calls), 1)


if __name__ == "__main__":
    unittest.main()
