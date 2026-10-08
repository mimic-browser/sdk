#!/usr/bin/env python3
"""Verify the dedicated Maven signing authority and its public key distribution."""
import argparse
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import release

KEYSERVER = "hkps://keyserver.ubuntu.com"


def run(arguments, message, input=None):
    result = subprocess.run(["gpg", *map(str, arguments)], input=input, capture_output=True)
    if result.returncode:
        # GPG diagnostics can contain key identities. Keep the CI log generic.
        raise release.ReleaseError(message)
    return result.stdout


def key_groups(data):
    groups = []
    current = None
    for line in data.decode().splitlines():
        fields = line.split(":")
        if fields[0] in {"sec", "pub"}:
            current = {"fingerprint": None, "keys": [fields]}
            groups.append(current)
        elif fields[0] in {"ssb", "sub"} and current:
            current["keys"].append(fields)
        elif fields[0] == "fpr" and current and current["fingerprint"] is None:
            current["fingerprint"] = fields[9]
    return groups


def valid_key(fields, now):
    return (len(fields) > 11 and fields[1] not in {"r", "e", "d", "i"}
            and (not fields[6] or int(fields[6]) > now) and "D" not in fields[11])


def signing_fingerprint(data, now=None):
    groups = key_groups(data)
    now = time.time() if now is None else now
    if len(groups) != 1:
        raise release.ReleaseError("Maven signing requires exactly one dedicated primary key")
    group = groups[0]
    fingerprint = group["fingerprint"] or ""
    if not re.fullmatch(r"[0-9A-F]{40}|[0-9A-F]{64}", fingerprint) or not valid_key(group["keys"][0], now):
        raise release.ReleaseError("The dedicated Maven primary key is invalid or expired")
    if not any(valid_key(key, now) and "s" in key[11] and (len(key) < 15 or key[14] != "#")
               for key in group["keys"]):
        raise release.ReleaseError("The dedicated Maven key has no usable signing key")
    return fingerprint


def public_key(fingerprint):
    url = "https://keyserver.ubuntu.com/pks/lookup?op=get&search=0x" + fingerprint
    request = urllib.request.Request(url, headers={"User-Agent": "Mimic-SDK-Release/0.1"})
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            data = response.read(1024 * 1024 + 1)
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return None
        raise release.ReleaseError("Cannot query the Maven public key server") from None
    except OSError:
        raise release.ReleaseError("Cannot query the Maven public key server") from None
    if len(data) > 1024 * 1024 or b"BEGIN PGP PUBLIC KEY BLOCK" not in data or b"PRIVATE KEY" in data:
        raise release.ReleaseError("The key server did not return a bounded public key")
    return data


def import_public(home, data, fingerprint):
    if b"BEGIN PGP PUBLIC KEY BLOCK" not in data or b"PRIVATE KEY" in data:
        raise release.ReleaseError("Only an exported public key may enter the verification keyring")
    run(["--homedir", home, "--batch", "--no-tty", "--import"], "Cannot import Maven public verification key", data)
    groups = key_groups(run(["--homedir", home, "--batch", "--no-tty", "--with-colons", "--fingerprint", "--list-keys"],
                            "Cannot inspect Maven public verification key"))
    if len(groups) != 1 or groups[0]["fingerprint"] != fingerprint:
        raise release.ReleaseError("Published Maven key does not match the dedicated signing authority")


def verify(home, passphrase, work):
    secret = run(["--homedir", home, "--batch", "--no-tty", "--with-colons", "--fingerprint", "--list-secret-keys"],
                 "Cannot inspect the dedicated Maven signing key")
    fingerprint = signing_fingerprint(secret)
    probe, signature = work / "probe.txt", work / "probe.txt.asc"
    probe.write_bytes(b"Mimic Maven signing preflight\n")
    run(["--homedir", home, "--batch", "--yes", "--no-tty", "--pinentry-mode", "loopback", "--passphrase-fd", "0",
         "--local-user", fingerprint, "--armor", "--detach-sign", "--output", signature, probe],
        "Cannot sign with the dedicated Maven key", (passphrase + "\n").encode())
    run(["--homedir", home, "--batch", "--no-tty", "--verify", signature, probe], "Local Maven signature verification failed")
    published = public_key(fingerprint)
    provisioned = published is None
    if provisioned:
        # Only public material enters the keyring used for keyserver submission.
        public_home = work / "public-export"
        public_home.mkdir(mode=0o700)
        exported = run(["--homedir", home, "--batch", "--no-tty", "--armor", "--export", fingerprint],
                       "Cannot export the Maven public key")
        import_public(public_home, exported, fingerprint)
        run(["--homedir", public_home, "--batch", "--no-tty", "--keyserver", KEYSERVER,
             "--keyserver-options", "timeout=30", "--send-keys", fingerprint], "Cannot distribute the Maven public key")
        for attempt in range(4):
            published = public_key(fingerprint)
            if published is not None:
                break
            if attempt < 3:
                time.sleep(2)
        if published is None:
            raise release.ReleaseError("Maven public key submission is not yet visible; retry the retained release run")
    verification_home = work / "public-verification"
    verification_home.mkdir(mode=0o700)
    import_public(verification_home, published, fingerprint)
    run(["--homedir", verification_home, "--batch", "--no-tty", "--verify", signature, probe],
        "Published Maven public key cannot verify the signing probe")
    return provisioned


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build", type=Path, required=True)
    args = parser.parse_args()
    if not any(package["registry"] == "maven" for package in release.read(args.build)["packages"].values()):
        return
    if os.getenv("CI") != "true" or os.getenv("MIMIC_SDK_PUBLICATION_ENABLED") != "true":
        raise release.ReleaseError("Maven key distribution requires explicitly enabled publication CI")
    home = os.environ.get("GNUPGHOME")
    if not home or not Path(home).is_dir():
        raise release.ReleaseError("The dedicated Maven signing keyring has not been provisioned")
    with tempfile.TemporaryDirectory(prefix="mimic-maven-signing-") as temporary:
        provisioned = verify(Path(home), os.getenv("MAVEN_SIGNING_PASSPHRASE", ""), Path(temporary))
    print("PASS Maven signing authority and independently fetched public-key signature" + ("; public key provisioned" if provisioned else ""))


if __name__ == "__main__":
    try:
        main()
    except (release.ReleaseError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
