"""Reuse the production synthetic provider fixture for any native SDK client."""
import argparse
import asyncio
import sys


async def run(args):
    if sys.platform != "linux": raise RuntimeError("Synthetic media tests require Linux")
    fixture = await asyncio.create_subprocess_exec(args.fixture, "--browser-mode", "headless", "--listen", "127.0.0.1:0", stdout=asyncio.subprocess.PIPE)
    native = None
    try:
        endpoint = page = None
        while endpoint is None or page is None:
            line = (await asyncio.wait_for(fixture.stdout.readline(), 20)).decode().strip()
            if not line: raise RuntimeError("Media fixture exited before readiness")
            if line.startswith("Mimic listening on "): endpoint = line[19:]
            if line.startswith("Fixture listening on "): page = line[21:]
        command = args.command[1:] if args.command[0] == "--" else args.command
        native = await asyncio.create_subprocess_exec(*(part.replace("{endpoint}", endpoint).replace("{fixture}", page) for part in command))
        return await asyncio.wait_for(native.wait(), 90)
    finally:
        for process in (native, fixture):
            if process is not None and process.returncode is None:
                process.terminate()
                try: await asyncio.wait_for(process.wait(), 3)
                except asyncio.TimeoutError: process.kill(); await process.wait()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--fixture", required=True); parser.add_argument("command", nargs=argparse.REMAINDER)
    raise SystemExit(asyncio.run(run(parser.parse_args())))
