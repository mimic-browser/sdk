"""Synchronous Playwright integration. All browser objects are Playwright objects."""
from __future__ import annotations

from dataclasses import dataclass
from ..generated import MimicCommands, to_wire
from ..protocol import CDPConnection, Experimental
from ..runtime import RuntimeManager, RuntimeError


def _extensions(sender):
    handle = MimicCommands(sender)
    handle.experimental = Experimental(sender)
    return handle


@dataclass(frozen=True)
class ContextSetup:
    browser_context_id: str
    mimic: MimicCommands


class IntegrationSession:
    def __init__(self, endpoint, *, runtime=None, playwright=None, timeout=30):
        self.runtime, self._driver, self._owned_driver = runtime, playwright, playwright is None
        self._contexts, self._sessions = [], []
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

    def new_context(self, *, media=None, resource_policy=None, profile=None, proxy=None, **options):
        if self._closed:
            raise RuntimeError("Integration session closed")
        managed = profile is not None or proxy is not None
        if managed:
            conflicting = {"viewport", "no_viewport", "screen", "device_scale_factor", "is_mobile", "has_touch", "user_agent",
                           "locale", "timezone_id", "color_scheme", "reduced_motion", "forced_colors", "contrast"}
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
        session = page.context.new_cdp_session(page)
        try:
            target_id = session.send("Target.getTargetInfo")["targetInfo"]["targetId"]
        finally:
            session.detach()
        session_id = self.connection.call("Target.attachToTarget", {"targetId": target_id,
                                                                   "flatten": True})["sessionId"]
        self._sessions.append(session_id)
        return _extensions(lambda method, params: self.connection.call(method, params, session_id))

    def close(self):
        if self._closed:
            return
        self._closed = True
        failures = self.close_errors
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


def launch(*, playwright=None, engine="v8", **runtime_options):
    runtime = RuntimeManager(**runtime_options).launch(engine=engine)
    return IntegrationSession(runtime.endpoint, runtime=runtime, playwright=playwright)


def connect(endpoint, *, playwright=None, timeout=30):
    return IntegrationSession(endpoint, playwright=playwright, timeout=timeout)
