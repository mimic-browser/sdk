import asyncio
import json
import platform
import threading
import time
import unittest

from websockets.sync.server import serve
from mimic.protocol import CDPConnection, Experimental, AsyncExperimental, ProtocolError

if platform.system() != "Linux":
    raise RuntimeError("Listener tests require WSL/Linux")


class TransportTests(unittest.IsolatedAsyncioTestCase):
    async def test_raw_contract_without_schema(self):
        requests = []
        def handler(socket):
            def reply(request):
                time.sleep((request.get("params") or {}).get("delay", 0))
                response = {"id": request["id"], "result": {"present": "params" in request, "params": request.get("params"), "sessionId": request.get("sessionId")}}
                if request["method"] == "Mimic.fail":
                    response = {"id": request["id"], "error": {"code": -32601, "message": "Absent", "data": {"nested": [None, True]}}}
                if "sessionId" in request:
                    response["sessionId"] = request["sessionId"]
                try:
                    socket.send(json.dumps({"id": request["id"], "sessionId": "foreign-session", "result": {"wrong": True}}))
                    socket.send(json.dumps(response))
                except Exception:
                    pass
            for raw in socket:
                request = json.loads(raw)
                requests.append(request)
                threading.Thread(target=reply, args=(request,), daemon=True).start()
        server = serve(handler, "127.0.0.1", 0)
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        connection = CDPConnection(f"ws://127.0.0.1:{server.socket.getsockname()[1]}")
        proxy = AsyncExperimental(lambda method, params: connection.call_async(method, params, "page-explicit"))
        try:
            self.assertFalse((await proxy.newUnknownCommand())["present"])
            null = await proxy.call("newUnknownCommand", None)
            self.assertTrue(null["present"])
            self.assertIsNone(null["params"])
            self.assertEqual(null["sessionId"], "page-explicit")
            with self.assertRaises(ProtocolError) as caught:
                await proxy.fail()
            self.assertEqual((caught.exception.code, caught.exception.message, caught.exception.data), (-32601, "Absent", {"nested": [None, True]}))
            result = await asyncio.gather(*(proxy.newUnknownCommand({"delay": value}) for value in (.03, .001, .02)))
            self.assertEqual([item["params"]["delay"] for item in result], [.03, .001, .02])
            pending = asyncio.create_task(proxy.late({"delay": .2}))
            await asyncio.sleep(.01)
            pending.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await pending
            with self.assertRaises(TimeoutError):
                await connection.call_async("Mimic.late", {"delay": .2}, timeout=.001)
            self.assertFalse((await proxy.newUnknownCommand())["present"])
            self.assertEqual(len([item for item in requests if item["method"] == "Mimic.late"]), 2)
            self.assertTrue(all(item["method"].startswith("Mimic.") for item in requests))
        finally:
            connection.close()
            server.shutdown()
            worker.join(timeout=2)


if __name__ == "__main__":
    unittest.main()
