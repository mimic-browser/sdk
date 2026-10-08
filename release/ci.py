#!/usr/bin/env python3
"""Small native CI entry points; never launch Chromium or invoke publication."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import release


def pyppeteer_python():
    name = "Scripts/python.exe" if os.name == "nt" else "bin/python"
    return release.ROOT / ".build/pyppeteer-venv" / name


def prepare(groups):
    release.execute(["python", "-m", "pip", "install", "websocket-client>=1.8,<2", "websockets>=14,<16", "build", "twine"])
    for group in groups:
        directory = release.ROOT / group
        if group == "node": release.execute(["npm", "ci"], directory)
        elif group == "python":
            # Type check the installed wheel: setuptools editable import hooks
            # are executed by Python but are not discoverable by type checkers.
            release.execute(["python", "-m", "pip", "install", ".[playwright,test]", "mypy==1.18.2"], directory)
            # Pyppeteer requires older pyee/websockets than the modern client and
            # transport fixture. Qualify it independently instead of resolving
            # incompatible extras into one environment.
            interpreter = pyppeteer_python()
            release.execute(["python", "-m", "venv", interpreter.parent.parent])
            release.execute([interpreter, "-m", "pip", "install", "-e", ".[pyppeteer]"], directory)
        elif group == "ruby": release.execute(["bundle", "install"], directory)
        elif group == "php": release.execute(["composer", "install", "--no-interaction", "--no-progress"], directory)


def unit(group):
    prepare([group])
    commands = {
        "node": [["node", "scripts/git-package.mjs", "--check"], ["npm", "run", "build"], ["npm", "test"], ["node", "--test", "test/transport.mjs"]],
        "python": [["python", "tools/generate_api_typing.py", "--check"], ["python", "tools/check_typing.py", "--python", sys.executable], ["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"], ["python", "tests/transport.py"]],
        "dotnet": [["dotnet", "run", "--project", "Mimic.Tests", "--no-launch-profile"]],
        "java": [["mvn", "-B", "-ntp", "test"]],
        "go": [["go", "test", "./..."]],
        "rust": [["cargo", "test", "--locked", "--all-features"]],
        "ruby": [["bundle", "exec", "ruby", "-Ilib", "test/sdk_test.rb"], ["bundle", "exec", "ruby", "-Ilib", "test/transport_test.rb"]],
        "php": [["php", "tests/unit.php"]],
    }
    for command in commands[group]: release.execute(command, release.ROOT / group)


def runtime(url, archive_sha, binary_sha):
    match = re.fullmatch(r"https://github\.com/mimic-browser/runtime/releases/download/(v\d+\.\d+\.\d+(?:-beta\.\d+)?)/(mimic-\1-linux-amd64\.tar\.gz)", url)
    if not match or not re.fullmatch(r"[a-f0-9]{64}", archive_sha) or not re.fullmatch(r"[a-f0-9]{64}", binary_sha):
        raise release.ReleaseError("Require an exact official Linux release URL and lowercase SHA256 digests")
    sys.path.insert(0, str(release.ROOT / "python"))
    from mimic.runtime import RuntimeManager
    manager = RuntimeManager(runtime_version=match[1], runtime_dir=release.ROOT / ".build/ci-runtime-cache")
    lock = manager.resolve_lock()
    artifact = next(item for item in lock["manifest"]["artifacts"] if item["platform"] == "linux-amd64")
    if artifact["sha256"] != archive_sha or artifact["binarySha256"] != binary_sha or artifact["archive"] != match[2]:
        raise release.ReleaseError("Requested qualification identity differs from verified official manifest")
    executable = Path(manager.install())
    target = release.ROOT / ".build/qualification-runtime"
    if target.exists(): raise release.ReleaseError("Qualification directory already exists; use a fresh CI checkout")
    shutil.copytree(executable.parent, target)
    release.write(release.ROOT / ".build/qualification-runtime-lock.json", lock)
    default = release.read(release.ROOT / "release/runtime-lock.json")
    baseline = next(item for item in default["manifest"]["artifacts"] if item["platform"] == "linux-amd64")
    data = release.fetch(default["baseUrl"] + "/" + baseline["archive"])
    if len(data) != baseline["size"] or release.sha(data) != baseline["sha256"]:
        raise release.ReleaseError("Default installer artifact integrity failure")
    (release.ROOT / ".build/default-runtime.tar.gz").write_bytes(data)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    one = sub.add_parser("unit"); one.add_argument("group", choices=["node", "python", "dotnet", "java", "go", "rust", "ruby", "php"])
    selected = sub.add_parser("prepare"); selected.add_argument("--plan", type=Path, required=True)
    frozen = sub.add_parser("freeze-plan"); frozen.add_argument("--plan", type=Path, required=True); frozen.add_argument("--output", type=Path, required=True)
    signing = sub.add_parser("signing-key"); signing.add_argument("--build", type=Path, required=True)
    runtime_parser = sub.add_parser("runtime"); runtime_parser.add_argument("--url", required=True); runtime_parser.add_argument("--archive-sha256", required=True); runtime_parser.add_argument("--binary-sha256", required=True)
    args = parser.parse_args()
    if args.operation == "unit": unit(args.group)
    elif args.operation == "signing-key":
        if not any(package["registry"] == "maven" for package in release.read(args.build)["packages"].values()): return
        key = os.environ.get("MAVEN_SIGNING_KEY")
        if not key: raise release.ReleaseError("Selected Maven publication requires the dedicated MAVEN_SIGNING_KEY secret")
        home = release.ROOT / ".build/sdk-gnupg"; home.mkdir(mode=0o700, parents=True, exist_ok=True)
        result = subprocess.run(["gpg", "--homedir", str(home), "--batch", "--no-tty", "--import"], input=key, text=True, capture_output=True)
        if result.returncode: raise release.ReleaseError("Cannot import dedicated noninteractive signing key")
        with open(os.environ["GITHUB_ENV"], "a", encoding="utf-8") as environment: environment.write("GNUPGHOME=" + str(home) + "\n")
    elif args.operation == "runtime": runtime(args.url, args.archive_sha256, args.binary_sha256)
    elif args.operation == "freeze-plan":
        plan = release.read(args.plan); release.validate_plan(plan)
        # A committed plan cannot contain its own commit hash. Input hashes above
        # remain immutable; bind the reviewed plan to the actual CI source commit.
        plan["sdkRevision"] = release.git_revision(); release.write(args.output, plan)
    else:
        plan = release.read(args.plan); release.validate_plan(plan)
        groups = {"dotnet" if key.startswith("dotnet-") else key for key in plan["packages"]}
        prepare(sorted(groups))


if __name__ == "__main__":
    try: main()
    except (release.ReleaseError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr); raise SystemExit(1)
