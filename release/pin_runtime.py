#!/usr/bin/env python3
"""Pin the SDK to an officially published, checksum-verified runtime release."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "python"))
from mimic.runtime import BASE, normalize_version, validate_lock

COPIES = (
    "release/runtime-lock.json", "node/runtime-lock.json",
    "python/mimic/runtime-lock.json", "go/runtime-lock.json",
    "rust/src/runtime-lock.json", "ruby/lib/mimic_sdk/runtime-lock.json",
    "php/resources/runtime-lock.json",
)


def verified_lock(version, manifest_bytes, sums_bytes):
    version = normalize_version(version)
    sums = {}
    for line in sums_bytes.decode("utf-8").splitlines():
        fields = line.split()
        if len(fields) != 2 or fields[1] in sums:
            raise ValueError("Malformed or duplicate release checksum")
        sums[fields[1]] = fields[0]
    digest = hashlib.sha256(manifest_bytes).hexdigest()
    if sums.get("release-manifest.json") != digest:
        raise ValueError("Published manifest checksum mismatch")
    raw = manifest_bytes.decode("utf-8")
    lock = validate_lock({"release": version, "manifestSha256": digest,
                          "manifestJson": raw, "manifest": json.loads(raw),
                          "baseUrl": f"{BASE}/{version}"})
    if {item["platform"] for item in lock["manifest"]["artifacts"]} != {"windows-amd64", "linux-amd64"}:
        raise ValueError("A public SDK pin requires both supported platforms")
    for item in lock["manifest"]["artifacts"]:
        if sums.get(item["archive"]) != item["sha256"]:
            raise ValueError("Archive manifest/checksum disagreement")
    return lock


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version")
    args = parser.parse_args()
    version = normalize_version(args.version)
    base = f"{BASE}/{version}"
    def fetch(name):
        with urllib.request.urlopen(f"{base}/{name}", timeout=60) as response:
            return response.read()
    manifest, sums = fetch("release-manifest.json"), fetch("SHA256SUMS")
    lock = verified_lock(version, manifest, sums)
    encoded = (json.dumps(lock, indent=2) + "\n").encode("utf-8")
    # Validate all public evidence before changing any package-local copy.
    for name in COPIES:
        (ROOT / name).write_bytes(encoded)
    (ROOT / "release/runtime-manifest.json").write_bytes(manifest)
    (ROOT / "release/runtime-SHA256SUMS").write_bytes(sums)
    print(f"Pinned all SDK packages to {version}; qualification is still required")


if __name__ == "__main__":
    main()
