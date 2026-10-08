#!/usr/bin/env python3
"""Qualify selected native packages against an explicitly hashed headless runtime.

This is also the runtime-release entry point. It only writes compatibility
evidence: no package version, packaged default pin or publication is changed.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys

import release
import smoke
from ci import pyppeteer_python


def commands(group, runtime, media_fixture=None):
    fixture = str(release.ROOT / "dotnet/Mimic.Tests/transport_fixture.py")
    dotnet = os.getenv("MIMIC_RELEASE_DOTNET", "dotnet")
    maven = os.getenv("MIMIC_RELEASE_MVN", "mvn")
    php = os.getenv("MIMIC_RELEASE_PHP", "php")
    table = {
        "node": ("node", [["npm", "run", "build"], ["npm", "test"], ["node", "--test", "test/transport.mjs", "test/integration.mjs", "test/bridge.mjs", "test/page-lifecycle.mjs"]]),
        "python": ("python", [["python", "-m", "unittest", "discover", "-s", "tests", "-p", "test_*.py"], ["python", "tools/generate_api_typing.py", "--check"], ["python", "tests/transport.py"], ["python", "tests/integration.py"], ["python", "tests/bridge.py"], ["python", "tests/page_lifecycle.py", "Sync", "AsyncPlaywright"], [str(pyppeteer_python()), "tests/pyppeteer_integration.py"], [str(pyppeteer_python()), "tests/page_lifecycle.py", "Pyppeteer"], [str(pyppeteer_python()), "tests/bridge.py", "--pyppeteer"]]),
        "dotnet": (".", [["dotnet", "run", "--project", "dotnet/Mimic.Tests", "--no-launch-profile", "--", "--integration-candidate", runtime], ["python", fixture, dotnet, "run", "--no-build", "--project", "dotnet/Mimic.Tests", "--", "--transport", "{endpoint}"]]),
        "java": ("java", [["mvn", "-B", "-ntp", "test-compile", "exec:java", "-Dexec.mainClass=io.mimicbrowser.sdk.IntegrationCheck", "-Dexec.classpathScope=test", "-Dexec.args=--candidate " + runtime], ["python", fixture, maven, "-B", "-ntp", "exec:java", "-Dexec.mainClass=io.mimicbrowser.sdk.ProtocolCheck", "-Dexec.classpathScope=test", "-Dexec.args={endpoint}"]]),
        "go": ("go", [["go", "test", "-v", "./..."]]),
        "rust": ("rust", [["cargo", "test", "--locked", "--features", "chromiumoxide", "--", "--nocapture"]]),
        "ruby": ("ruby", [["ruby", "-Ilib", "test/sdk_test.rb"], ["ruby", "-Ilib", "test/transport_test.rb"], ["ruby", "-Ilib", "test/ferrum_test.rb"]]),
        "php": ("php", [["php", "tests/integration.php", runtime], ["python", fixture, php, "tests/transport.php", "{endpoint}"]]),
    }
    directory, checks = table[group]
    if media_fixture is not None:
        media_harness = str(release.ROOT / "dotnet/Mimic.Tests/media_fixture.py")
        media_checks = {
            "node": [["node", "test/media.mjs"]],
            "python": [["python", "tests/media.py"], [str(pyppeteer_python()), "tests/media.py", "--pyppeteer"]],
            "dotnet": [["python", media_harness, "--fixture", media_fixture, "--", dotnet, "run", "--no-build", "--project", "dotnet/Mimic.Tests", "--", "--media", "{endpoint}", "{fixture}"]],
            "java": [["python", media_harness, "--fixture", media_fixture, "--", maven, "-B", "-ntp", "exec:java", "-Dexec.mainClass=io.mimicbrowser.sdk.MediaCheck", "-Dexec.classpathScope=test", "-Dexec.args={endpoint} {fixture}"]],
            "php": [["python", media_harness, "--fixture", media_fixture, "--", php, "tests/media.php", "{endpoint}", "{fixture}"]],
            "ruby": [["ruby", "-Ilib", "test/media_test.rb"]],
            # Go and Rust include their opt-in synthetic suites in the commands above.
            "go": [], "rust": [],
        }
        checks.extend(media_checks[group])
    return directory, checks


def verify_media_fixture(path, expected_sha256):
    if (path is None) != (expected_sha256 is None):
        raise release.ReleaseError("Synthetic capture requires both --media-fixture and --media-fixture-sha256")
    if path is not None and release.sha(path.read_bytes()) != expected_sha256:
        raise release.ReleaseError("Synthetic media fixture hash mismatch")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--runtime", type=Path, required=True)
    parser.add_argument("--runtime-sha256", required=True)
    parser.add_argument("--archive", type=Path, required=True, help="Original bundled-pin release archive for installer conformance")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--media-fixture", type=Path, help="Optional synthetic provider executable; no hardware capture")
    parser.add_argument("--media-fixture-sha256", help="Required exact hash when synthetic capture is requested")
    args = parser.parse_args()
    if sys.platform != "linux":
        raise release.ReleaseError("Live qualification runs on Linux only, always headless and loopback")
    runtime = args.runtime.resolve()
    media_fixture = args.media_fixture.resolve() if args.media_fixture else None
    verify_media_fixture(media_fixture, args.media_fixture_sha256)
    if release.sha(runtime.read_bytes()) != args.runtime_sha256:
        raise release.ReleaseError("Explicit qualification runtime hash mismatch")
    lock = release.read(release.ROOT / "release/runtime-lock.json")
    artifact = next(item for item in lock["manifest"]["artifacts"] if item["platform"] == "linux-amd64")
    if release.sha(args.archive.read_bytes()) != artifact["sha256"]:
        raise release.ReleaseError("Installer conformance archive does not match immutable packaged pin")
    build = release.read(args.build)
    packages = release.catalog()
    for key, package in build["packages"].items():
        release.verify_local(package)
        if package["inputDigest"] != release.input_digest(packages[key]):
            raise release.ReleaseError("Package source changed after packing: " + key)
    # Reuse the production native Python transport for exact Mimic identity; no framework is launched.
    sys.path.insert(0, str(release.ROOT / "python"))
    from mimic.runtime import RuntimeManager
    process = RuntimeManager(executable_path=str(runtime), allow_download=False).launch()
    try:
        identity = process.identity
    finally:
        process.close()
    environment = dict(os.environ)
    environment.update({"PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD":"1", "PUPPETEER_SKIP_DOWNLOAD":"1", "MIMIC_CANDIDATE":str(runtime), "MIMIC_SDK_TEST_RUNTIME":str(runtime), "MIMIC_SDK_TEST_CONFIGURE":"1", "MIMIC_SDK_TEST_ARCHIVE":str(args.archive.resolve()), "MIMIC_TEST_ARCHIVE":str(args.archive.resolve()), "PYTHONPATH":str(release.ROOT / "python")})
    if media_fixture:
        environment["MIMIC_MEDIA_FIXTURE"] = str(media_fixture)
    else:
        environment.pop("MIMIC_MEDIA_FIXTURE", None)
    record = {"kind":"sdk-qualification-receipt", "createdAt":release.utc(), "buildSha256":release.sha(json.dumps(build, sort_keys=True).encode()), "sdkRevision":release.git_revision(), "schemaSha256":release.sha((release.ROOT / "schema/mimic/protocol.json").read_bytes()), "runtime":{"identity":identity, "binarySha256":args.runtime_sha256}, "installerArtifact":{"release":lock["release"], "manifestSha256":lock["manifestSha256"], "sourceRevision":lock["manifest"]["sourceRevision"], **artifact}, "packages":{}, "commands":[]}
    record["status"] = "running"
    record["syntheticMedia"] = ({"status":"running", "binarySha256":args.media_fixture_sha256} if media_fixture else {"status":"not-run", "reason":"No explicitly hashed synthetic provider fixture supplied; runtime-only checks do not qualify actual media capture"})
    release.write(args.output, record)
    try:
        qualify(build, packages, runtime, media_fixture, environment, record, args)
    except Exception as error:
        record["status"] = "failed"
        record["error"] = str(error)
        if media_fixture: record["syntheticMedia"]["status"] = "failed"
        release.write(args.output, record)
        raise
    print("PASS selected native package qualification; package versions and runtime locks unchanged")


def qualify(build, packages, runtime, media_fixture, environment, record, args):
    record["packagedConsumers"] = smoke.smoke(build)
    groups = list(dict.fromkeys("dotnet" if key.startswith("dotnet-") else key for key in build["packages"]))
    for group in groups:
        directory, checks = commands(group, str(runtime), str(media_fixture) if media_fixture else None)
        for command in checks:
            command[0] = environment.get("MIMIC_RELEASE_" + command[0].upper(), command[0])
            result = subprocess.run(command, cwd=release.ROOT / directory, env=environment)
            record["commands"].append({"command":command, "directory":directory, "exitCode":result.returncode})
            if result.returncode:
                release.write(args.output, record)
                raise release.ReleaseError("Qualification failed; no package marked passed: " + group)
        for key in build["packages"]:
            if key == group or group == "dotnet" and key.startswith("dotnet-"):
                record["packages"][key] = {"status":"passed", "inputDigest":build["packages"][key]["inputDigest"], "version":build["packages"][key]["version"]}
        release.write(args.output, record)
    if release.sha(runtime.read_bytes()) != args.runtime_sha256:
        raise release.ReleaseError("Runtime changed during qualification; discard this receipt")
    verify_media_fixture(media_fixture, args.media_fixture_sha256)
    for key, package in build["packages"].items():
        if package["inputDigest"] != release.input_digest(packages[key]):
            raise release.ReleaseError("Package source changed during qualification")
    record["status"] = "passed"
    if media_fixture: record["syntheticMedia"]["status"] = "passed"
    release.write(args.output, record)


if __name__ == "__main__":
    try:
        main()
    except (release.ReleaseError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
