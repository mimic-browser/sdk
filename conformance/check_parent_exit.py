"""Verify owned headless runtime shutdown after an abruptly exiting parent.

Run this POSIX-only integration probe with an explicit candidate executable.
It never installs software, changes firewall policy, or opens a browser window.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("executable", type=Path)
    parser.add_argument("--receipt", type=Path)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Run this listener integration check in Linux/WSL")
    executable = args.executable.resolve(strict=True)
    with tempfile.TemporaryDirectory(prefix="mimic-parent-proof-") as directory:
        proof = Path(directory, "started.json")
        parent = subprocess.Popen(
            [sys.executable, "-c", """
import json, os, subprocess, sys
from pathlib import Path
child = subprocess.Popen([sys.argv[1], '--browser-mode', 'headless', '--listen', '127.0.0.1:0'], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
for line in child.stdout:
    if line.startswith('Mimic listening on http://127.0.0.1:'):
        Path(sys.argv[2]).write_text(json.dumps({'pid': child.pid, 'endpoint': line.strip().split(' on ', 1)[1]}))
        os._exit(0)
raise RuntimeError('Runtime never became ready')
""", str(executable), str(proof)],
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
        )
        exited = False
        try:
            _, errors = parent.communicate(timeout=30)
            if parent.returncode != 0:
                raise RuntimeError(f"Parent failed: {errors}")
            started = json.loads(proof.read_text())
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                status = Path(f"/proc/{started['pid']}/stat")
                if not status.exists() or status.read_text().split(") ", 1)[1].startswith("Z "):
                    exited = True
                    result = {"case": "abrupt-parent-exit", "passed": True, "executableSha256": hashlib.file_digest(executable.open("rb"), "sha256").hexdigest()}
                    print(json.dumps(result))
                    if args.receipt:
                        args.receipt.write_text(json.dumps(result, indent=2) + "\n")
                    return
                time.sleep(0.05)
            raise RuntimeError("Runtime remained alive after parent exited")
        finally:
            if parent.poll() is None:
                parent.kill()
                parent.wait()
            if not exited and proof.exists():
                pid = json.loads(proof.read_text())["pid"]
                try:
                    os.kill(pid, 15)
                except ProcessLookupError:
                    pass


if __name__ == "__main__":
    main()
