"""Async Playwright integration using native Python runtime installation."""
from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass

from ..generated import AsyncMimicCommands, to_wire
from ..protocol import AsyncExperimental, CDPConnection
from ..runtime import RuntimeManager, RuntimeError
from .._page import AsyncPageBindings, AsyncMimicExtensions


def _extensions(sender):
    return AsyncMimicExtensions(sender)


@dataclass(frozen=True)
class ContextSetup:
    browser_context_id: str
    mimic: AsyncMimicCommands


class IntegrationSession:
    def __init__(self):
        self.runtime = self.browser = self.connection = self._driver = None
        self._owned_driver = False
        self._contexts = []
        self._pages = AsyncPageBindings(self)
        self._inflight = set()
        self._closed = False
        self._close_task = None
        self.close_errors = []

    @classmethod
    async def open(cls, endpoint, *, runtime=None, playwright=None, timeout=30):
        self = cls()
        self.runtime, self._driver, self._owned_driver = runtime, playwright, playwright is None
        try:
            self.connection = runtime.connection if runtime else await CDPConnection.connect_async(endpoint, timeout)
            self.identity = await self.connection.call_async("Mimic.getVersion")
            if not isinstance(self.identity.get("version"), str):
                raise RuntimeError("Endpoint did not identify itself as Mimic")
            if self._owned_driver:
                from playwright.async_api import async_playwright
                self._driver = await async_playwright().start()
            self.browser = await self._driver.chromium.connect_over_cdp(endpoint, timeout=timeout * 1000)
            self.mimic = _extensions(self.connection.call_async)
            return self
        except BaseException:
            await self.close()
            raise

    async def new_context(self, *, media=None, resource_policy=None, profile=None, proxy=None, framework=None, **options):
        if self._closed:
            raise RuntimeError("Integration session closed")
        if framework is not None:
            if options.keys() & framework.keys():
                raise TypeError("Native context option supplied both as keyword and in framework")
            options = {**framework, **options}
        completion = asyncio.get_running_loop().create_future()
        self._inflight.add(completion)
        operation = asyncio.create_task(self._new_context(media=media, resource_policy=resource_policy,
                                                         profile=profile, proxy=proxy, _acquired=completion, _options=options))
        try:
            return await asyncio.shield(operation)
        except asyncio.CancelledError:
            # Context creation is a state-changing RPC. Finish ownership
            # acquisition so cancellation cannot orphan a late context.
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

    async def _new_context(self, *, media=None, resource_policy=None, profile=None, proxy=None, _acquired=None, _options=None):
        options = {} if _options is None else _options
        managed = profile is not None or proxy is not None
        if managed:
            conflicting = {"viewport", "no_viewport", "screen", "device_scale_factor", "is_mobile", "has_touch", "user_agent",
                           "locale", "timezone_id", "color_scheme", "reduced_motion", "forced_colors", "contrast", "proxy"}
            if any(key in options for key in conflicting):
                raise RuntimeError("Managed profile owns emulation; configure it in the profile")
            options.update(no_viewport=True, color_scheme="null", reduced_motion="null", forced_colors="null", contrast="null")
        context = await self.browser.new_context(**options)
        self._contexts.append(context)
        # Teardown waits for ownership acquisition, not user callback code that
        # may itself await close(). Cancellation still shields the full operation.
        if _acquired is not None and not _acquired.done():
            _acquired.set_result(None)
        try:
            if self._closed:
                raise RuntimeError("Integration session closed")
            if managed or media is not None or resource_policy is not None:
                probe = await context.new_page()
                session = await context.new_cdp_session(probe)
                try:
                    info = (await session.send("Target.getTargetInfo"))["targetInfo"]
                    context_id = info["browserContextId"]
                finally:
                    await session.detach()
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
                    await self.connection.call_async("Mimic.updateResourcePolicy", {"browserContextId": context_id,
                                                                                    "policy": resource_policy})
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
        async def target_info():
            session = await page.context.new_cdp_session(page)
            try:
                return (await session.send("Target.getTargetInfo"))["targetInfo"]["targetId"]
            finally:
                await session.detach()
        return await self._pages.for_page(page, target_info, page.is_closed)

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
        await self._pages.close()
        failures = self.close_errors
        for context in reversed(self._contexts):
            try:
                await context.close()
            except Exception as error:
                failures.append(error)
        if self.browser:
            try:
                await self.browser.close()
            except Exception as error:
                failures.append(error)
        if self.runtime:
            await asyncio.to_thread(self.runtime.close)
        elif self.connection:
            await asyncio.to_thread(self.connection.close)
        if self._owned_driver and self._driver:
            await self._driver.stop()
        self.close_errors = failures

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        await self.close()


async def launch(*, playwright=None, engine="v8", runtime_version=None, lock=None,
                 executable_path=None, runtime_dir=None, allow_download=None, timeout=60):
    # Shield ownership acquisition: cancellation waits for a started process so it
    # can always be closed, rather than abandoning a still-running worker thread.
    manager = RuntimeManager(runtime_version=runtime_version, lock=lock, executable_path=executable_path,
                             runtime_dir=runtime_dir, allow_download=allow_download, timeout=timeout)
    task = asyncio.create_task(asyncio.to_thread(manager.launch, engine=engine))
    try:
        runtime = await asyncio.shield(task)
    except asyncio.CancelledError:
        try:
            runtime = await task
            await asyncio.to_thread(runtime.close)
        finally:
            raise
    return await IntegrationSession.open(runtime.endpoint, runtime=runtime, playwright=playwright)


async def connect(endpoint, *, playwright=None, timeout=30):
    return await IntegrationSession.open(endpoint, playwright=playwright, timeout=timeout)
