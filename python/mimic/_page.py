"""Owned Page-scoped CDP attachments shared by the native integrations."""
from __future__ import annotations

import asyncio

from .generated import AsyncMimicCommands, MimicCommands
from .protocol import AsyncExperimental, ConnectionClosed, Experimental, ProtocolError


def _already_detached(error):
    return error.code == -32001 or (error.code == -32000 and error.message == "No session with given id")


async def _finish_owned(task):
    """Repeated caller cancellation must not abandon a state-changing RPC."""
    while True:
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            if task.done():
                return task.result()


class MimicExtensions(MimicCommands):
    def __init__(self, sender):
        super().__init__(sender)
        self.experimental = Experimental(sender)


class AsyncMimicExtensions(AsyncMimicCommands):
    def __init__(self, sender):
        super().__init__(sender)
        self.experimental = AsyncExperimental(sender)


class _Attachment:
    def __init__(self, owner, page, session_id):
        self.owner, self.page, self.session_id = owner, page, session_id
        self.closed = False

    def release(self):
        self.closed = True
        self.owner.connection.cancel_session(self.session_id)
        if self.page is not None:
            self.page.remove_listener("close", self._page_closed)
            self.owner.pages.pop(self.page, None)
            self.page = None
        self.owner.handles.discard(self)

    def check_open(self):
        if self.closed:
            raise ConnectionClosed("Page extension handle closed")


class PageMimic(_Attachment, MimicExtensions):
    """Synchronous Mimic commands bound to one native Page."""
    def __init__(self, owner, page, session_id):
        _Attachment.__init__(self, owner, page, session_id)
        MimicExtensions.__init__(self, self._call)
        self._close_error = None
        page.on("close", self._page_closed)

    def _call(self, method, params):
        self.check_open()
        return self.owner.connection.call(method, params, self.session_id)

    def _page_closed(self, *_):
        try:
            self.close()
        except Exception as error:
            self.owner.errors.append(error)

    def close(self):
        if not self.closed:
            self.release()
            try:
                self.owner.connection.call("Target.detachFromTarget", {"sessionId": self.session_id})
            except ProtocolError as error:
                # Destroying a target already detaches all sessions on the wire.
                if not _already_detached(error):
                    self._close_error = error
            except Exception as error:
                self._close_error = error
        if self._close_error:
            raise self._close_error

    detach = close

    def __enter__(self):
        self.check_open()
        return self

    def __exit__(self, *_):
        self.close()


class AsyncPageMimic(_Attachment, AsyncMimicExtensions):
    """Async Mimic commands bound to one native Page."""
    def __init__(self, owner, page, session_id):
        _Attachment.__init__(self, owner, page, session_id)
        AsyncMimicExtensions.__init__(self, self._call)
        self._close_task = None
        page.on("close", self._page_closed)

    async def _call(self, method, params):
        self.check_open()
        return await self.owner.connection.call_async(method, params, self.session_id)

    def _begin_close(self):
        if self._close_task is None:
            self.release()
            self._close_task = asyncio.create_task(self._detach())
            self.owner.cleanups.add(self._close_task)
            self._close_task.add_done_callback(self.owner.cleanup_done)
        return self._close_task

    def _page_closed(self, *_):
        self._begin_close()

    async def _detach(self):
        try:
            await self.owner.connection.call_async("Target.detachFromTarget", {"sessionId": self.session_id})
        except ProtocolError as error:
            if not _already_detached(error):
                raise

    async def close(self):
        task = self._begin_close()
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            await _finish_owned(task)
            raise

    detach = close

    async def __aenter__(self):
        self.check_open()
        return self

    async def __aexit__(self, *_):
        await self.close()


class PageBindings:
    def __init__(self, session):
        self.session = session
        self.pages, self.handles = {}, set()

    @property
    def connection(self):
        return self.session.connection

    @property
    def errors(self):
        return self.session.close_errors

    def for_page(self, page, target_info, is_closed):
        if self.session._closed or is_closed():
            raise ConnectionClosed("Page or integration session closed")
        if page in self.pages:
            return self.pages[page]
        target_id = target_info()
        if self.session._closed or is_closed():
            raise ConnectionClosed("Page or integration session closed")
        session_id = self.connection.call("Target.attachToTarget", {"targetId": target_id, "flatten": True})["sessionId"]
        handle = PageMimic(self, page, session_id)
        self.pages[page] = handle
        self.handles.add(handle)
        if self.session._closed or is_closed():
            handle.close()
            raise ConnectionClosed("Page or integration session closed")
        return handle

    def close(self):
        for handle in list(self.handles):
            try:
                handle.close()
            except Exception as error:
                self.errors.append(error)


class AsyncPageBindings(PageBindings):
    def __init__(self, session):
        super().__init__(session)
        self.cleanups = set()

    def cleanup_done(self, task):
        self.cleanups.discard(task)
        if not task.cancelled() and task.exception() is not None:
            self.errors.append(task.exception())

    async def for_page(self, page, target_info, is_closed):
        if self.session._closed or is_closed():
            raise ConnectionClosed("Page or integration session closed")
        entry = self.pages.get(page)
        if entry is None:
            entry = {"claimed": False, "waiters": 0}
            entry["task"] = asyncio.create_task(self._attach(page, target_info, is_closed))
            self.pages[page] = entry
            self.session._inflight.add(entry["task"])
            entry["task"].add_done_callback(self.session._inflight.discard)
        entry["waiters"] += 1
        cancelled = False
        try:
            handle = await asyncio.shield(entry["task"])
            entry["claimed"] = True
            return handle
        except asyncio.CancelledError:
            cancelled = True
            entry["waiters"] -= 1
            try:
                handle = await _finish_owned(entry["task"])
                if not entry["claimed"] and entry["waiters"] == 0:
                    await _finish_owned(asyncio.create_task(handle.close()))
            finally:
                raise
        finally:
            # A cancelled waiter was removed before awaiting late acquisition.
            if not cancelled:
                entry["waiters"] -= 1

    async def _attach(self, page, target_info, is_closed):
        try:
            target_id = await target_info()
            if self.session._closed or is_closed():
                raise ConnectionClosed("Page or integration session closed")
            session_id = (await self.connection.call_async("Target.attachToTarget", {
                "targetId": target_id, "flatten": True,
            }))["sessionId"]
            handle = AsyncPageMimic(self, page, session_id)
            self.handles.add(handle)
            if self.session._closed or is_closed():
                await handle.close()
                raise ConnectionClosed("Page or integration session closed")
            return handle
        except BaseException:
            self.pages.pop(page, None)
            raise

    async def close(self):
        for handle in list(self.handles):
            try:
                await handle.close()
            except Exception:
                # The retained cleanup task records its failure exactly once.
                pass
        await asyncio.gather(*self.cleanups, return_exceptions=True)
