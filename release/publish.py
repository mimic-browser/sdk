#!/usr/bin/env python3
"""Explicit, CI-only publication of a previously packed and qualified selected set.

Disabled unless both --execute and MIMIC_SDK_PUBLICATION_ENABLED=true are present.
No runtime release event calls this script. Never use it during local SDK work.
"""
import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import urllib.error

import release
import publish_sources


def check_credentials(packages, environment):
    """Fail before uploading any package when selected registry auth is missing."""
    required = {
        "pypi": ("TWINE_USERNAME", "TWINE_PASSWORD"),
        "nuget": ("NUGET_API_KEY",),
        "crates": ("CARGO_REGISTRY_TOKEN",),
        "rubygems": ("GEM_HOST_API_KEY",),
        "maven": ("MAVEN_CENTRAL_TOKEN", "GNUPGHOME"),
        "packagist": ("PHP_MIRROR_TOKEN",),
    }
    missing = set()
    for package in packages.values():
        registry = package["registry"]
        missing.update(name for name in required.get(registry, ()) if not environment.get(name))
        if registry == "npm" and not environment.get("NODE_AUTH_TOKEN"):
            missing.update(name for name in ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN") if not environment.get(name))
    if missing:
        raise release.ReleaseError("Missing publication configuration: " + ", ".join(sorted(missing)))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--build", type=Path, required=True)
    parser.add_argument("--qualification", type=Path, nargs="+", required=True)
    parser.add_argument("--state", type=Path, required=True)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    if not args.execute or os.getenv("MIMIC_SDK_PUBLICATION_ENABLED") != "true" or os.getenv("CI") != "true":
        raise release.ReleaseError("Publication is disabled; use local plan/build/handoff. Execution requires explicitly enabled CI")
    plan, build = release.read(args.plan), release.read(args.build)
    handoff = release.publication_handoff(plan, build, [release.read(path) for path in args.qualification])
    revision = release.git_revision()
    if not revision or plan["sdkRevision"] != revision:
        raise release.ReleaseError("Publication requires the exact committed SDK source from the release plan")
    if release.execute(["git", "status", "--porcelain", "--untracked-files=all"], capture=True):
        raise release.ReleaseError("Publication requires a clean source tree; keep outputs in ignored .build")
    state = release.read(args.state) if args.state.exists() else {"kind":"sdk-publication-progress", "buildSha256":handoff["buildSha256"], "sdkRevision":revision, "packages":{}}
    if state["buildSha256"] != handoff["buildSha256"]:
        raise release.ReleaseError("Publication state belongs to another build")
    check_credentials({key: package for key, package in build["packages"].items() if state["packages"].get(key, {}).get("state") != "verified"}, os.environ)
    def save(): release.write(args.state, state)
    for key, definition in handoff["packages"].items():
        progress = state["packages"].setdefault(key, {"state":"pending"})
        package = build["packages"][key]
        if progress["state"] == "verified":
            continue
        selected = {**build, "packages":{key:package}}
        registry_state = args.state.with_name(args.state.stem + "-" + key + "-registry.json")
        # Recover an upload whose response was lost by verifying the registry first.
        try:
            release.verify_registry(selected, registry_state)
            progress["state"] = "verified"; save(); continue
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
        except release.ReleaseError as error:
            if "has not indexed" not in str(error):
                raise
        if progress["state"] in ("uploaded", "verifying") and not (package["registry"] == "maven" and progress.get("deploymentId")):
            raise release.ReleaseError(f"{key}: uploaded artifact is not yet visible; retry verification without uploading again")
        registry = package["registry"]
        if registry == "maven":
            publish_sources.maven(package, progress, save)
        elif registry == "packagist":
            publish_sources.php(package, revision, progress, save)
        else:
            if registry == "go":
                origin = release.execute(["git", "remote", "get-url", "origin"], capture=True)
                if origin.removesuffix(".git").rstrip("/") not in ("https://github.com/mimic-browser/sdk", "git@github.com:mimic-browser/sdk"):
                    raise release.ReleaseError("Go module tags must target the SDK repository")
            command = definition["command"]
            expanded = []
            for value in command:
                match = re.fullmatch(r"\$\{([A-Z_]+)\}", value)
                if match:
                    if not os.environ.get(match.group(1)):
                        raise release.ReleaseError("Missing registry credential " + match.group(1))
                    value = os.environ[match.group(1)]
                expanded.append(value)
            expanded[0] = os.getenv("MIMIC_RELEASE_" + expanded[0].upper(), expanded[0])
            progress["state"] = "uploading"; save()
            # Avoid CalledProcessError's argv representation, which can contain the NuGet key.
            result = subprocess.run(expanded, cwd=release.ROOT, stdin=subprocess.DEVNULL, env={**os.environ, "GIT_TERMINAL_PROMPT": "0"})
            if result.returncode:
                raise release.ReleaseError(f"{key}: native publisher failed (exit {result.returncode}); resume using the same receipt")
        progress["state"] = "uploaded"; save()
        release.verify_registry(selected, registry_state)
        progress["state"] = "verified"; save()
    state["status"] = "complete"; save()
    print("All selected package registry artifacts verified")


if __name__ == "__main__":
    try:
        main()
    except (release.ReleaseError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
