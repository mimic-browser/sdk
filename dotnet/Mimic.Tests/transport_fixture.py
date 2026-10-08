"""Linux-only adversarial WebSocket fixture for real native transport clients."""
import asyncio
import json
import sys
import websockets


async def respond(socket):
    async for text in socket:
        request = json.loads(text)
        reply = {"id": request["id"]}
        if "sessionId" in request:
            reply["sessionId"] = request["sessionId"]
        if request["method"] == "Fixture.session":
            await socket.send(json.dumps({"id": request["id"], "sessionId": "foreign", "result": {"owner": "wrong"}}))
            await socket.send(json.dumps({"id": request["id"], "result": {"owner": "wrong-browser"}}))
            reply["result"] = {"owner": request.get("sessionId"), "echo": request["params"]}
        elif request["method"] == "Fixture.error":
            reply["error"] = {"code": -32123, "message": "precise native error", "data": [None, {"future": False}, 9007199254740991]}
        elif request["method"] == "Fixture.timeout":
            continue
        else:
            reply["result"] = {}
        await socket.send(json.dumps(reply))


async def handler(socket):
    try:
        await respond(socket)
    except websockets.exceptions.ConnectionClosed:
        pass  # Native disposal intentionally aborts its transport without Browser.close.


async def main():
    if sys.platform != "linux":
        raise RuntimeError("Live transport fixtures must run inside Linux")
    command = sys.argv[1:]
    if command and command[0] == "--": command = command[1:]
    if not command: raise RuntimeError("Provide a native test command with {endpoint}")
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        endpoint = f"ws://127.0.0.1:{server.sockets[0].getsockname()[1]}/fixture"
        child = await asyncio.create_subprocess_exec(*(part.replace("{endpoint}", endpoint) for part in command))
        return await child.wait()


if __name__ == "__main__": raise SystemExit(asyncio.run(main()))
