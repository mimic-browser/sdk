"""Run language-native installer commands concurrently against one fresh cache.

Command JSON contains language -> argv. Arguments may contain {cache} and
{archive}; no shell is used. The second phase removes the archive input and
requires offline cache reuse by every language. Keep the resulting receipt.
"""

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time


def run_phase(commands, cache, archive):
    results = {}
    environment = dict(os.environ, MIMIC_DOWNLOAD="0", MIMIC_RUNTIME_DIR=str(cache), MIMIC_RUNTIME_VERSION="v0.2.2")
    environment.pop("MIMIC_EXECUTABLE_PATH", None)

    def run(language, command):
        argv = [item.replace("{cache}", str(cache)).replace("{archive}", archive) for item in command]
        started = time.monotonic()
        completed = subprocess.run(argv, capture_output=True, text=True, env=environment, timeout=240)
        result = {"exitCode": completed.returncode, "seconds": round(time.monotonic() - started, 3), "output": (completed.stdout + completed.stderr)[-16000:]}
        print(f"{language}: exit {completed.returncode}", flush=True)
        return result

    with ThreadPoolExecutor(max_workers=8) as pool:
        pending = {pool.submit(run, language, argv): language for language, argv in commands.items()}
        for future in as_completed(pending):
            results[pending[future]] = future.result()
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("commands", type=Path)
    parser.add_argument("archive", type=Path)
    parser.add_argument("--receipt", type=Path, required=True)
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Run native cross-language integration inside Linux/WSL")
    commands = json.loads(args.commands.read_text())
    required = {"node", "python", "dotnet", "java", "go", "rust", "ruby", "php"}
    if set(commands) != required or any(not isinstance(argv, list) or not argv for argv in commands.values()):
        parser.error("Provide argv arrays for all eight native languages")
    pin = json.loads(Path(__file__).resolve().parents[1].joinpath("release/runtime-lock.json").read_text())
    artifact = next(item for item in pin["manifest"]["artifacts"] if item["platform"] == "linux-amd64")
    with args.archive.open("rb") as source:
        if hashlib.file_digest(source, "sha256").hexdigest() != artifact["sha256"]:
            parser.error("The supplied archive does not match the immutable release pin")
    with tempfile.TemporaryDirectory(prefix="mimic-eight-language-cache-") as directory:
        cache = Path(directory)
        cold = run_phase(commands, cache, str(args.archive.resolve()))
        warm = run_phase(commands, cache, "") if all(item["exitCode"] == 0 for item in cold.values()) else {}
        receipts = list(cache.glob("v*/*/*/installation.json"))
        binary_hash = None
        if len(receipts) == 1:
            installed = json.loads(receipts[0].read_text())
            with receipts[0].parent.joinpath(installed["executable"]).open("rb") as binary:
                binary_hash = hashlib.file_digest(binary, "sha256").hexdigest()
        locks = list(cache.joinpath(".locks").glob("*.lock"))
        staging = list(cache.joinpath(".staging").glob("*"))
        passed = bool(len(warm) == 8 and all(item["exitCode"] == 0 for item in warm.values()) and len(receipts) == 1 and binary_hash == artifact["binarySha256"] and not locks and not staging)
        result = {"case": "eight-language-concurrent-cold-install-and-offline-reuse", "passed": passed, "runtimeRelease": pin["release"], "archiveSha256": artifact["sha256"], "binarySha256": binary_hash, "installationCount": len(receipts), "remainingLocks": len(locks), "remainingStaging": len(staging), "cold": cold, "offlineReuse": warm}
        args.receipt.write_text(json.dumps(result, indent=2) + "\n")
        if not passed:
            raise SystemExit("Cross-language cache qualification failed; inspect the receipt")
        print("All eight installers shared one verified artifact and reused it offline.")


if __name__ == "__main__":
    main()
