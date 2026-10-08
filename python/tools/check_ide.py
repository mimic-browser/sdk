"""Verify installed-wheel type diagnostics and editor services with Pyright.

The selected interpreter must contain the wheel and its Playwright extra. The
Pyright npm package is a development tool only; no browser or editor is started.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import queue
import shutil
import subprocess
import tempfile
import threading
import time


class LanguageServer:
    """Small stdio LSP client for the editor requests exercised by this gate."""

    def __init__(self, script: Path, directory: Path, python: str):
        self.messages: queue.Queue = queue.Queue()
        self.sequence = 0
        self.settings = {"python": {"pythonPath": python, "analysis": {
            "typeCheckingMode": "strict", "autoSearchPaths": False,
            "autoImportCompletions": False,
        }}}
        self.errors = tempfile.TemporaryFile()
        self.process = subprocess.Popen(
            ["node", str(script), "--stdio"], cwd=directory,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=self.errors,
        )
        threading.Thread(target=self._read, daemon=True).start()
        try:
            self.request("initialize", {
                "processId": os.getpid(), "rootUri": directory.as_uri(),
                "capabilities": {"workspace": {"configuration": True},
                                 "textDocument": {"completion": {"contextSupport": True}}},
                "workspaceFolders": [{"uri": directory.as_uri(), "name": "consumer"}],
            })
            self.send("initialized", {})
        except Exception:
            self.process.kill()
            self.process.wait(timeout=10)
            self.errors.close()
            raise

    def _read(self):
        try:
            while True:
                headers = {}
                while True:
                    line = self.process.stdout.readline()
                    if not line:
                        raise EOFError("Pyright language server ended before its reply")
                    if line == b"\r\n":
                        break
                    name, value = line.decode().split(":", 1)
                    headers[name.lower()] = value.strip()
                body = self.process.stdout.read(int(headers["content-length"]))
                self.messages.put(json.loads(body))
        except Exception as error:
            self.messages.put(error)

    def _write(self, message):
        body = json.dumps({"jsonrpc": "2.0", **message}).encode()
        self.process.stdin.write(f"Content-Length: {len(body)}\r\n\r\n".encode() + body)
        self.process.stdin.flush()

    def send(self, method, params):
        self._write({"method": method, "params": params})

    def request(self, method, params):
        self.sequence += 1
        request_id = self.sequence
        self._write({"id": request_id, "method": method, "params": params})
        deadline = time.monotonic() + 30
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError(f"Pyright did not complete {method}")
            message = self.messages.get(timeout=remaining)
            if isinstance(message, Exception):
                raise message
            if "method" in message and "id" in message:
                result = None
                if message["method"] == "workspace/configuration":
                    result = []
                    for item in message["params"]["items"]:
                        value = self.settings
                        for name in item.get("section", "").split("."):
                            value = value.get(name, {}) if isinstance(value, dict) else {}
                        result.append(value)
                self._write({"id": message["id"], "result": result})
            elif message.get("id") == request_id:
                if "error" in message:
                    raise AssertionError(f"{method}: {message['error']}")
                return message.get("result")

    def close(self):
        try:
            if self.process.poll() is None:
                self.request("shutdown", None)
                self.send("exit", None)
                self.process.wait(timeout=10)
        finally:
            if self.process.poll() is None:
                self.process.kill()
                self.process.wait(timeout=10)
            self.process.stdin.close()
            self.process.stdout.close()
            self.errors.close()


def position(text: str, match: str, offset: int = 0):
    start = text.index(match) + offset
    return {"line": text[:start].count("\n"), "character": start - text.rfind("\n", 0, start) - 1}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", required=True)
    parser.add_argument("--pyright", type=Path, required=True, help="Installed npm pyright package directory")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
    pyright = args.pyright.resolve()
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("MYPYPATH", None)
    with tempfile.TemporaryDirectory(prefix="mimic-wheel-ide-") as temporary:
        directory = Path(temporary)
        probe = subprocess.check_output([args.python, "-c", (
            "import inspect,json,mimic; from playwright.sync_api import Browser; "
            "print(json.dumps({'package':mimic.__file__,'options':list(inspect.signature(Browser.new_context).parameters)}))"
        )], cwd=directory, env=environment, text=True)
        installed = json.loads(probe)
        package = Path(installed["package"]).resolve().parent
        if package.is_relative_to(root):
            raise AssertionError("IDE checks require an installed wheel outside the source checkout")
        for name in ("py.typed", "runtime.pyi", "_page.pyi", "pyppeteer.pyi", "playwright/sync_api.pyi", "playwright/async_api.pyi"):
            if not (package / name).is_file():
                raise AssertionError(f"Wheel omitted {name}")
        config = {"typeCheckingMode": "strict"}
        (directory / "pyrightconfig.json").write_text(json.dumps(config))
        for filename in ("positive.py", "negative.py"):
            shutil.copyfile(root / "tests/typing" / filename, directory / filename)
        command = ["node", str(pyright / "index.js"), "--pythonpath", args.python, "--outputjson"]
        positive = subprocess.run([*command, "positive.py"], cwd=directory, env=environment, capture_output=True, text=True, timeout=90)
        if positive.returncode != 0:
            raise AssertionError("Pyright rejected a valid installed-wheel consumer:\n" + positive.stdout + positive.stderr)
        negative = subprocess.run([*command, "negative.py"], cwd=directory, env=environment, capture_output=True, text=True, timeout=90)
        diagnostics = json.loads(negative.stdout)["generalDiagnostics"]
        lines = {item["range"]["start"]["line"] + 1 for item in diagnostics if item["severity"] == "error"}
        if negative.returncode != 1 or not {6, 7, 8, 9, 10, 11, 14, 15, 16, 17}.issubset(lines):
            raise AssertionError("Pyright did not reject every invalid consumer:\n" + negative.stdout)

        source = '''from mimic.playwright.sync_api import launch
from mimic.playwright.async_api import launch as launch_async

with launch() as session:
    context = session.new_context()
    page = context.new_page()
    handle = session.for_page(page)
    version = handle.get_version()

async def example() -> None:
    async with await launch_async() as async_session:
        async_context = await async_session.new_context()
        async_page = await async_context.new_page()
        async_handle = await async_session.for_page(async_page)
'''
        document = directory / "editor.py"
        document.write_text(source)
        uri = document.as_uri()
        server = LanguageServer(pyright / "langserver.index.js", directory, args.python)
        try:
            server.send("textDocument/didOpen", {"textDocument": {
                "uri": uri, "languageId": "python", "version": 1, "text": source,
            }})
            hover_types = {
                "session:": "IntegrationSession", "context =": "BrowserContext",
                "page =": "Page", "handle =": "PageMimic", "version =": "GetVersionResult",
                "async_session:": "IntegrationSession", "async_context =": "BrowserContext",
                "async_page =": "Page", "async_handle =": "AsyncPageMimic",
            }
            for marker, expected in hover_types.items():
                hover = server.request("textDocument/hover", {
                    "textDocument": {"uri": uri}, "position": position(source, marker),
                })
                contents = json.dumps(hover)
                if expected not in contents or "Unknown" in contents or ": Any" in contents:
                    raise AssertionError(f"Wrong hover for {marker}: {contents}")
            options = (set(installed["options"]) - {"self"}) | {"media", "resource_policy", "profile", "framework"}
            for marker in ("session.new_context(", "async_session.new_context("):
                where = position(source, marker, len(marker))
                result = server.request("textDocument/completion", {
                    "textDocument": {"uri": uri}, "position": where, "context": {"triggerKind": 1},
                })
                items = result["items"] if isinstance(result, dict) else result
                labels = {item["label"].rstrip("=") for item in items}
                if not options.issubset(labels):
                    raise AssertionError(f"Missing named completions for {marker}: {sorted(options - labels)}")
                signature = server.request("textDocument/signatureHelp", {
                    "textDocument": {"uri": uri}, "position": where,
                })
                label = signature["signatures"][0]["label"]
                if "BrowserContext" not in label or any(name + ":" not in label for name in options):
                    raise AssertionError(f"Incomplete context signature: {label}")
            for marker, result_type in (("session.for_page(", "PageMimic"), ("async_session.for_page(", "AsyncPageMimic")):
                signature = server.request("textDocument/signatureHelp", {
                    "textDocument": {"uri": uri}, "position": position(source, marker, len(marker)),
                })
                label = signature["signatures"][0]["label"]
                if "page: Page" not in label or result_type not in label:
                    raise AssertionError(f"Native Page signature lost: {label}")
        finally:
            server.close()
        version = json.loads((pyright / "package.json").read_text())["version"]
        print(f"Installed wheel Pyright {version}: positive/negative diagnostics, typed hover, "
              f"{len(options)} named sync/async context completions and Page signatures pass")


if __name__ == "__main__":
    main()
