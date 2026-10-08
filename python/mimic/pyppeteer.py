"""Optional Pyppeteer integration. Uses connect only; never downloads Chrome."""
from __future__ import annotations

import asyncio
import inspect

from .playwright.async_api import _extensions, ContextSetup
from .generated import to_wire
from .protocol import CDPConnection, websocket_endpoint
from .runtime import RuntimeManager, RuntimeError


class IntegrationSession:
    def __init__(self):
        self.runtime = self.browser = self.connection = None
        self._contexts = []
        self._inflight = set()
        self._closed = False
        self._close_task = None
        self.close_errors = []

    @classmethod
    async def open(cls, endpoint, *, runtime=None, timeout=30):
        self = cls()
        self.runtime = runtime
        try:
            self.connection = runtime.connection if runtime else await CDPConnection.connect_async(endpoint, timeout)
            self.identity = await self.connection.call_async("Mimic.getVersion")
            if not isinstance(self.identity.get("version"), str):
                raise RuntimeError("Endpoint did not identify itself as Mimic")
            from pyppeteer import connect
            self.browser = await connect(browserWSEndpoint=await asyncio.to_thread(websocket_endpoint, endpoint),
                                         defaultViewport=None)
            self.mimic = _extensions(self.connection.call_async)
            return self
        except BaseException:
            await self.close()
            raise

    async def new_context(self, *, media=None, resource_policy=None, profile=None, proxy=None):
        if self._closed:
            raise RuntimeError("Integration session closed")
        completion = asyncio.get_running_loop().create_future()
        self._inflight.add(completion)
        operation = asyncio.create_task(self._new_context(media=media, resource_policy=resource_policy,
                                                         profile=profile, proxy=proxy, _acquired=completion))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            try:
                context = await operation
                await context.close()
                self._contexts.remove(context)
            finally:
                raise
        finally:
            self._inflight.discard(completion)
            if not completion.done():
                completion.set_result(None)

    async def _new_context(self, *, media=None, resource_policy=None, profile=None, proxy=None, _acquired=None):
        managed = profile is not None or proxy is not None
        context = await self.browser.createIncognitoBrowserContext()
        self._contexts.append(context)
        if _acquired is not None and not _acquired.done():
            _acquired.set_result(None)
        try:
            if self._closed:
                raise RuntimeError("Integration session closed")
            if managed or media is not None or resource_policy is not None:
                probe = await context.newPage()
                protocol = await probe.target.createCDPSession()
                try:
                    context_id = (await protocol.send("Target.getTargetInfo"))["targetInfo"]["browserContextId"]
                finally:
                    await protocol.detach()
                    await probe.close()
                if callable(media):
                    media = media(ContextSetup(context_id, self.mimic))
                    if inspect.isawaitable(media):
                        media = await media
                    if self._closed:
                        raise RuntimeError("Integration session closed")
                    if media is None:
                        raise TypeError("Media factory must return a media configuration")
                media, profile, proxy, resource_policy = (to_wire(item) for item in (media, profile, proxy, resource_policy))
                if managed:
                    params = {"browserContextId": context_id, "profile": profile if profile is not None else {"generate": {}}}
                    for key, value in (("proxy", proxy), ("media", media), ("resourcePolicy", resource_policy)):
                        if value is not None:
                            params[key] = value
                    await self.connection.call_async("Mimic.configureContext", params)
                elif media is not None:
                    await self.connection.call_async("Mimic.setMediaProfile", {**media, "browserContextId": context_id})
                if not managed and resource_policy is not None:
                    await self.connection.call_async("Mimic.updateResourcePolicy", {"browserContextId": context_id, "policy": resource_policy})
            if self._closed:
                raise RuntimeError("Integration session closed")
            return context
        except BaseException:
            if not self._closed:
                try:
                    await context.close()
                    self._contexts.remove(context)
                except Exception as error:
                    self.close_errors.append(error)
            raise

    async def for_page(self, page):
        protocol = await page.target.createCDPSession()
        try:
            target_id = (await protocol.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
        finally:
            await protocol.detach()
        session_id = (await self.connection.call_async("Target.attachToTarget", {"targetId": target_id, "flatten": True}))["sessionId"]
        return _extensions(lambda method, params: self.connection.call_async(method, params, session_id))

    async def close(self):
        if self._close_task is None:
            self._closed = True
            self._close_task = asyncio.create_task(self._close())
        try:
            await asyncio.shield(self._close_task)
        except asyncio.CancelledError:
            await self._close_task
            raise

    async def _close(self):
        await asyncio.gather(*self._inflight, return_exceptions=True)
        for context in reversed(self._contexts):
            try:
                await context.close()
            except Exception as error:
                self.close_errors.append(error)
        if self.browser:
            try:
                await self.browser.disconnect()
            except Exception as error:
                self.close_errors.append(error)
        if self.runtime:
            await asyncio.to_thread(self.runtime.close)
        elif self.connection:
            await asyncio.to_thread(self.connection.close)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()


async def launch(*, engine="v8", **runtime_options):
    task = asyncio.create_task(asyncio.to_thread(RuntimeManager(**runtime_options).launch, engine=engine))
    try:
        runtime = await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            runtime = await task
            await asyncio.to_thread(runtime.close)
        finally:
            raise
    return await IntegrationSession.open(runtime.endpoint, runtime=runtime)


async def connect(endpoint, *, timeout=30):
    return await IntegrationSession.open(endpoint, timeout=timeout)
