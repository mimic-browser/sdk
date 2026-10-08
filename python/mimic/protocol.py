"""One raw CDP dispatcher shared by stable bindings and experimental calls."""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import math
import threading
import urllib.request
from typing import Any
from urllib.parse import urlparse

OMITTED = object()


class ProtocolError(Exception):
    def __init__(self, code: int, message: str, data: Any = OMITTED):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data


class ConnectionClosed(Exception):
    pass


def json_value(value: Any) -> None:
    """Reject silent lossy Python-to-JSON conversions, including non-string keys."""
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float) and math.isfinite(value):
        return
    if isinstance(value, list):
        for item in value:
            json_value(item)
        return
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        for item in value.values():
            json_value(item)
        return
    raise TypeError("Expected a JSON value with string object keys and finite numbers")


def websocket_endpoint(endpoint: str, timeout: float = 30) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme in ("ws", "wss") and parsed.hostname:
        return endpoint
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("Expected an explicit HTTP(S) or WS(S) endpoint")
    with urllib.request.urlopen(endpoint.rstrip("/") + "/json/version", timeout=timeout) as response:
        value = json.load(response)
    result = value.get("webSocketDebuggerUrl", "")
    if urlparse(result).scheme not in ("ws", "wss"):
        raise ValueError("Discovery did not return a browser websocket endpoint")
    return result


class CDPConnection:
    @classmethod
    async def connect_async(cls, endpoint: str, timeout: float = 30):
        task = asyncio.create_task(asyncio.to_thread(cls, endpoint, timeout))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            try:
                connection = await task
                await asyncio.to_thread(connection.close)
            finally:
                raise

    def __init__(self, endpoint: str, timeout: float = 30):
        from websocket import create_connection
        self.timeout = timeout
        self._lock = threading.Lock()
        self._send_lock = threading.Lock()
        self._pending: dict[int, concurrent.futures.Future] = {}
        self._sequence = 0
        self._closed = False
        self._socket = create_connection(websocket_endpoint(endpoint, timeout), timeout=timeout,
                                          enable_multithread=True)
        self._socket.settimeout(None)
        self._reader = threading.Thread(target=self._read, name="mimic-cdp", daemon=True)
        self._reader.start()

    def _read(self):
        failure = ConnectionClosed("CDP connection closed")
        try:
            while True:
                raw = self._socket.recv()
                if not raw:
                    break
                message = json.loads(raw)
                with self._lock:
                    future = self._pending.get(message.get("id"))
                    if future is not None and message.get("sessionId") != future.session_id:
                        continue
                    self._pending.pop(message.get("id"), None)
                if future is None or future.done():
                    continue
                try:
                    if "error" in message:
                        error = message["error"]
                        future.set_exception(ProtocolError(error["code"], error["message"],
                                                           error.get("data", OMITTED)))
                    else:
                        future.set_result(message.get("result"))
                except concurrent.futures.InvalidStateError:
                    # A timeout/cancellation can win after response routing.
                    pass
        except Exception as exc:
            failure = ConnectionClosed(str(exc))
        finally:
            with self._lock:
                self._closed = True
                pending, self._pending = self._pending, {}
            for future in pending.values():
                if not future.done():
                    try:
                        future.set_exception(failure)
                    except concurrent.futures.InvalidStateError:
                        pass

    def submit(self, method: str, params: Any = OMITTED, session_id: str | None = None):
        if not isinstance(method, str) or not method or any(ord(c) < 32 for c in method):
            raise ValueError("Expected an exact nonempty CDP method name")
        if params is not OMITTED:
            json_value(params)
        with self._lock:
            if self._closed:
                raise ConnectionClosed("CDP connection closed")
            self._sequence += 1
            request_id = self._sequence
            future = concurrent.futures.Future()
            future.session_id = session_id
            self._pending[request_id] = future
        message: dict[str, Any] = {"id": request_id, "method": method}
        if params is not OMITTED:
            message["params"] = params
        if session_id is not None:
            message["sessionId"] = session_id
        try:
            with self._send_lock:
                self._socket.send(json.dumps(message, allow_nan=False))
        except Exception as exc:
            with self._lock:
                self._pending.pop(request_id, None)
            future.set_exception(exc)
        def forget(_):
            with self._lock:
                self._pending.pop(request_id, None)
        future.add_done_callback(forget)
        return future

    def call(self, method: str, params: Any = OMITTED, session_id: str | None = None,
             timeout: float | None = None):
        future = self.submit(method, params, session_id)
        try:
            return future.result(timeout=self.timeout if timeout is None else timeout)
        except concurrent.futures.TimeoutError:
            future.cancel()
            raise TimeoutError(f"{method} timed out; a dispatched operation may have completed") from None

    async def call_async(self, method: str, params: Any = OMITTED, session_id: str | None = None,
                         timeout: float | None = None):
        future = self.submit(method, params, session_id)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future),
                                          self.timeout if timeout is None else timeout)
        finally:
            if not future.done():
                future.cancel()

    def cancel_session(self, session_id):
        """Reject Page calls promptly when their owned attachment is released."""
        with self._lock:
            pending = [future for future in self._pending.values()
                       if future.session_id == session_id]
        for future in pending:
            try:
                future.set_exception(ConnectionClosed("Page extension handle closed"))
            except concurrent.futures.InvalidStateError:
                pass

    def close(self):
        self._socket.close()
        if threading.current_thread() is not self._reader:
            self._reader.join(timeout=5)


def experimental_method(name: str) -> str:
    if not isinstance(name, str) or not name or "." in name or any(ord(c) < 32 for c in name):
        raise ValueError("Expected an exact Mimic command leaf without a namespace")
    return "Mimic." + name


class Experimental:
    def __init__(self, sender):
        self._sender = sender

    def call(self, name: str, params: Any = OMITTED):
        return self._sender(experimental_method(name), params)

    def __getattr__(self, name: str):
        if name.startswith("_"):
            raise AttributeError(name)
        return lambda params=OMITTED: self.call(name, params)


class AsyncExperimental(Experimental):
    async def call(self, name: str, params: Any = OMITTED):
        return await self._sender(experimental_method(name), params)
