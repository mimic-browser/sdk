#!/usr/bin/env python3
"""Source-registry and Maven bundle publishers, called only by guarded publish.py."""
import base64
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import urllib.parse
import urllib.request
import uuid
import zipfile

import release
import mirror_auth
import maven_signing


def empty_mirror_tree(root, workspace):
    """Clear only the new private clone, including files deleted since the previous release."""
    root, workspace = root.resolve(), workspace.resolve()
    if root == workspace or not root.is_relative_to(workspace) or not (root / ".git").is_dir():
        raise release.ReleaseError("Mirror cleanup must remain inside the newly created private workspace")
    release.execute(["git", "read-tree", "--empty"], root)
    for path in root.iterdir():
        if path.name == ".git": continue
        if path.is_symlink() or path.is_file(): path.unlink()
        else: shutil.rmtree(path)


def maven_token(environment):
    """Central Portal expects the base64-encoded Portal token pair as bearer auth."""
    if environment.get("MAVEN_CENTRAL_TOKEN"):
        return environment["MAVEN_CENTRAL_TOKEN"]
    username = environment.get("MAVEN_CENTRAL_USERNAME")
    password = environment.get("MAVEN_CENTRAL_PASSWORD")
    if not username or not password:
        raise release.ReleaseError("Maven publication requires Central Portal username and password tokens")
    return base64.b64encode((username + ":" + password).encode()).decode()


def maven(package, progress, save, timeout=1800):
    """Use Central's supported bundle API; retain deployment ID before status checks."""
    token = maven_token(os.environ)
    headers = {"Authorization":"Bearer " + token, "User-Agent":"Mimic-SDK-Release/0.1"}
    if not progress.get("deploymentId"):
        if progress.get("state") == "uploading":
            raise release.ReleaseError("Prior upload outcome is unknown: recover its Central deployment ID into the receipt before retrying")
        with tempfile.TemporaryDirectory(prefix="mimic-central-") as temporary:
            root = Path(temporary)
            authority = maven_signing.signing_fingerprint(maven_signing.run(
                ["--batch", "--no-tty", "--with-colons", "--fingerprint", "--list-secret-keys"],
                "Cannot inspect the dedicated Maven signing key"))
            group, name = package["name"].split(":")
            prefix = group.replace(".", "/") + "/" + name + "/" + package["version"] + "/"
            payload = io.BytesIO()
            with zipfile.ZipFile(payload, "w", zipfile.ZIP_DEFLATED) as bundle:
                for item in package["artifacts"]:
                    path = Path(item["file"])
                    signature = root / (path.name + ".asc")
                    command = ["gpg", "--batch", "--yes", "--no-tty", "--pinentry-mode", "loopback", "--passphrase-fd", "0", "--local-user", authority, "--armor", "--detach-sign", "--output", str(signature), str(path)]
                    result = subprocess.run(command, input=os.getenv("MAVEN_SIGNING_PASSPHRASE", "") + "\n", text=True, capture_output=True)
                    if result.returncode:
                        raise release.ReleaseError("Noninteractive Maven artifact signing failed")
                    data = path.read_bytes()
                    bundle.writestr(prefix + path.name, data)
                    bundle.writestr(prefix + signature.name, signature.read_bytes())
                    for algorithm in ("md5", "sha1", "sha256", "sha512"):
                        bundle.writestr(prefix + path.name + "." + algorithm, hashlib.new(algorithm, data).hexdigest())
            boundary = "mimic-" + uuid.uuid4().hex
            body = ("--" + boundary + '\r\nContent-Disposition: form-data; name="bundle"; filename="bundle.zip"\r\nContent-Type: application/octet-stream\r\n\r\n').encode() + payload.getvalue() + ("\r\n--" + boundary + "--\r\n").encode()
            headers["Content-Type"] = "multipart/form-data; boundary=" + boundary
            progress["state"] = "uploading"; save()
            request = urllib.request.Request("https://central.sonatype.com/api/v1/publisher/upload?publishingType=AUTOMATIC", data=body, headers=headers, method="POST")
            with urllib.request.urlopen(request, timeout=120) as response:
                progress["deploymentId"] = response.read().decode().strip()
            progress["state"] = "uploaded"; save()
    # Query only the retained deployment; a slow validation queue never causes
    # another upload. Central's documented nonterminal states are explicit.
    deadline = time.monotonic() + timeout
    delay = 5
    while time.monotonic() < deadline:
        request = urllib.request.Request("https://central.sonatype.com/api/v1/publisher/status?id=" + urllib.parse.quote(progress["deploymentId"], safe=""), data=b"", headers=headers, method="POST")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        with urllib.request.urlopen(request, timeout=min(60, remaining)) as response:
            status = json.loads(response.read())
        progress["centralState"] = status["deploymentState"]; save()
        if status["deploymentState"] == "FAILED":
            raise release.ReleaseError("Central validation failed: " + json.dumps(status.get("errors")))
        if status["deploymentState"] == "PUBLISHED":
            return
        if status["deploymentState"] not in ("PENDING", "VALIDATING", "VALIDATED", "PUBLISHING"):
            raise release.ReleaseError("Central returned an unknown deployment state")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        pause = min(delay, remaining)
        print(f"Central deployment {status['deploymentState']}; checking the retained deployment after {pause:g}s", flush=True)
        time.sleep(pause)
        delay = min(delay * 2, 30)
    raise release.ReleaseError("Central deployment is still processing after the bounded wait; resume with the retained deployment receipt")


def php(package, source_revision, progress, save):
    """Publish an exact SDK/php tree to the designated CI-only mirror, never an edited fork."""
    with mirror_auth.credentials(os.environ) as (mirror, environment):
        _php(package, source_revision, progress, save, mirror, environment)


def _php(package, source_revision, progress, save, mirror, environment):
    def git(arguments, cwd=release.ROOT, capture=False):
        result = subprocess.run(["git", *map(str, arguments)], cwd=cwd, env=environment, text=True, capture_output=capture)
        if result.returncode:
            raise release.ReleaseError("PHP mirror Git operation failed; retain the same release receipt for recovery")
        return result.stdout.strip() if capture else None
    tag = "v" + package["version"]
    reference = "refs/tags/" + tag
    existing = git(["ls-remote", "--tags", mirror, reference], capture=True)
    if existing:
        commit = existing.split()[0]
        if progress.get("mirrorCommit") != commit:
            raise release.ReleaseError("PHP mirror tag exists without matching retained provenance; inspect it rather than overwrite")
        return
    with tempfile.TemporaryDirectory(prefix="mimic-php-mirror-") as temporary:
        root = Path(temporary) / "repository"
        git(["clone", "--no-checkout", mirror, root])
        git(["checkout", "--orphan", "sdk-release-" + package["version"]], root)
        empty_mirror_tree(root, Path(temporary))
        # The orphan now has an empty index/worktree; no obsolete previous-mirror file can survive.
        archive = Path(package["artifacts"][0]["file"])
        release.inspect_archive(archive, "packagist")
        with zipfile.ZipFile(archive) as source:
            for entry in source.infolist():
                if not entry.is_dir():
                    path = root / entry.filename
                    path.parent.mkdir(parents=True, exist_ok=True)
                    path.write_bytes(source.read(entry))
        # The standalone ZIP needs a version, whereas the mirrored source gets
        # its version from the immutable tag. Preserve the exact source metadata.
        canonical = (release.ROOT / "php/composer.json").read_bytes()
        packaged = (root / "composer.json").read_bytes()
        if release.composer_source_metadata(packaged, package["version"]) != release.composer_source_metadata(canonical, package["version"]):
            raise release.ReleaseError("Composer artifact metadata differs from selected source")
        (root / "composer.json").write_bytes(canonical)
        git(["add", "--all"], root)
        git(["-c", "user.name=Mimic SDK Release", "-c", "user.email=releases@mimic.boo", "commit", "-m", f"release: PHP {package['version']} from SDK {source_revision}"], root)
        progress["mirrorCommit"] = git(["rev-parse", "HEAD"], root, capture=True)
        progress["sourceRevision"] = source_revision
        progress["archiveSha256"] = package["artifacts"][0]["sha256"]
        progress["state"] = "pushing"; save()
        git(["push", "origin", "HEAD:" + reference], root)
        progress["state"] = "uploaded"; save()
    # Packagist is configured once with a GitHub webhook for this mirror; no source changes here.
