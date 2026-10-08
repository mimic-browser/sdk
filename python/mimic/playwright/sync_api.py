"""Synchronous Playwright integration. All browser objects are Playwright objects."""
from __future__ import annotations

from dataclasses import dataclass
from ..generated import MimicCommands, to_wire
from ..protocol import CDPConnection, Experimental
from ..runtime import RuntimeManager, RuntimeError
from .._page import PageBindings, MimicExtensions


def _extensions(sender):
    return MimicExtensions(sender)


@dataclass(frozen=True)
class ContextSetup:
    browser_context_id: str
    mimic: MimicCommands


class IntegrationSession:
    def __init__(self, endpoint, *, runtime=None, playwright=None, timeout=30):
        self.runtime, self._driver, self._owned_driver = runtime, playwright, playwright is None
        self._contexts = []
        self._pages = PageBindings(self)
        self._closed = False
        self.close_errors = []
        self.browser = None
        self.connection = None
        try:
            self.connection = runtime.connection if runtime else CDPConnection(endpoint, timeout)
            self.identity = self.connection.call("Mimic.getVersion")
            if not isinstance(self.identity.get("version"), str):
                raise RuntimeError("Endpoint did not identify itself as Mimic")
            if self._owned_driver:
                from playwright.sync_api import sync_playwright
                self._driver = sync_playwright().start()
            self.browser = self._driver.chromium.connect_over_cdp(endpoint, timeout=timeout * 1000)
            self.mimic = _extensions(self.connection.call)
        except BaseException:
            self.close()
            raise

    def new_context(self, *, media=None, resource_policy=None, profile=None, proxy=None, framework=None, **options):
        if self._closed:
            raise RuntimeError("Integration session closed")
        if framework is not None:
            if options.keys() & framework.keys():
                raise TypeError("Native context option supplied both as keyword and in framework")
            options = {**framework, **options}
        managed = profile is not None or proxy is not None
        if managed:
            conflicting = {"viewport", "no_viewport", "screen", "device_scale_factor", "is_mobile", "has_touch", "user_agent",
                           "locale", "timezone_id", "color_scheme", "reduced_motion", "forced_colors", "contrast", "proxy"}
            if any(key in options for key in conflicting):
                raise RuntimeError("Managed profile owns emulation; configure it in the profile")
            options.update(no_viewport=True, color_scheme="null", reduced_motion="null", forced_colors="null", contrast="null")
        context = self.browser.new_context(**options)
        self._contexts.append(context)
        try:
            if managed or media is not None or resource_policy is not None:
                probe = context.new_page()
                session = context.new_cdp_session(probe)
                try:
                    info = session.send("Target.getTargetInfo")["targetInfo"]
                    context_id = info["browserContextId"]
                finally:
                    session.detach()
                    probe.close()
                if callable(media):
                    media = media(ContextSetup(context_id, self.mimic))
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
                    self.connection.call("Mimic.configureContext", params)
                elif media is not None:
                    self.connection.call("Mimic.setMediaProfile", {**media, "browserContextId": context_id})
                if not managed and resource_policy is not None:
                    self.connection.call("Mimic.updateResourcePolicy", {"browserContextId": context_id,
                                                                       "policy": resource_policy})
            if self._closed:
                raise RuntimeError("Integration session closed")
            return context
        except BaseException:
            if not self._closed:
                try:
                    context.close()
                    self._contexts.remove(context)
                except Exception as error:
                    self.close_errors.append(error)
            raise

    def for_page(self, page):
        def target_info():
            session = page.context.new_cdp_session(page)
            try:
                return session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
            finally:
                session.detach()
        return self._pages.for_page(page, target_info, page.is_closed)

    def close(self):
        if self._closed:
            return
        self._closed = True
        failures = self.close_errors
        self._pages.close()
        for context in reversed(self._contexts):
            try:
                context.close()
            except Exception as error:
                failures.append(error)
        if self.browser:
            try:
                self.browser.close()
            except Exception as error:
                failures.append(error)
        if self.runtime:
            self.runtime.close()
        elif self.connection:
            self.connection.close()
        if self._owned_driver and self._driver:
            self._driver.stop()
        self.close_errors = failures

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def launch(*, playwright=None, engine="v8", runtime_version=None, lock=None,
           executable_path=None, runtime_dir=None, allow_download=None, timeout=60):
    runtime = RuntimeManager(runtime_version=runtime_version, lock=lock, executable_path=executable_path,
                             runtime_dir=runtime_dir, allow_download=allow_download, timeout=timeout).launch(engine=engine)
    return IntegrationSession(runtime.endpoint, runtime=runtime, playwright=playwright)


def connect(endpoint, *, playwright=None, timeout=30):
    return IntegrationSession(endpoint, playwright=playwright, timeout=timeout)
