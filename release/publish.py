#!/usr/bin/env python3
"""Explicit, CI-only publication of a previously packed and qualified selected set.

Disabled unless both --execute and MIMIC_SDK_PUBLICATION_ENABLED=true are present.
No runtime release event calls this script. Never use it during local SDK work.
"""
import argparse
import datetime as dt
import email.utils
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time
import urllib.error

import release
import publish_sources
import pypi_auth
import resume


def index_retry_delay(headers, fallback):
    """Respect server retry dates and the remaining freshness of a cached 404."""
    headers = {key.lower(): value for key, value in (headers.items() if headers else [])}
    now = time.time()

    def date(value):
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=dt.timezone.utc)
            return parsed.timestamp()
        except (TypeError, ValueError, OverflowError):
            return None

    retry = headers.get("retry-after", "").strip()
    retry_date = date(retry)
    requested = int(retry) if retry.isdecimal() else max(0, retry_date - now) if retry_date is not None else 0
    directives = {}
    for value in headers.get("cache-control", "").split(","):
        name, _, value = value.strip().partition("=")
        directives[name.lower()] = value.strip(' "')
    freshness = 0
    if not {"no-cache", "no-store"}.intersection(directives):
        response_date = date(headers.get("date"))
        age = headers.get("age", "0").strip()
        age = max(int(age) if age.isdecimal() else 0, max(0, now - response_date) if response_date is not None else 0)
        maximum = directives.get("s-maxage", directives.get("max-age", ""))
        if maximum.isdecimal():
            freshness = max(0, int(maximum) - age)
        else:
            expires = date(headers.get("expires"))
            if expires is not None:
                freshness = max(0, expires - response_date - age if response_date is not None else expires - now)
    return max(fallback, requested, freshness)


def verify_uploaded_registry(selected, registry_state, progress, timeout=None):
    """Wait only after a confirmed upload; never publish, change bytes or reset state."""
    if progress.get("state") not in ("uploaded", "verifying"):
        raise release.ReleaseError("Registry visibility waiting requires a confirmed upload")
    if timeout is None:
        # The public Go proxy negatively caches a missing initial module tag for
        # up to thirty minutes. Other registries use the shorter indexing bound.
        timeout = max(2100 if package["registry"] == "go" else 600
                      for package in selected["packages"].values())
    deadline = time.monotonic() + timeout
    delay = 2
    while True:
        if time.monotonic() >= deadline:
            raise release.ReleaseError("Uploaded registry artifacts are not yet visible; retain the receipt and resume verification without uploading again")
        headers = None
        try:
            return release.verify_registry(selected, registry_state)
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            headers = error.headers
            error.close()
        except release.RegistryNotIndexed:
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            continue
        pause = min(index_retry_delay(headers, delay), remaining)
        print(f"Uploaded package not yet indexed; verifying again after {pause:g}s", flush=True)
        time.sleep(pause)
        delay = min(delay * 2, 30)


def check_credentials(packages, environment):
    """Fail before uploading any package when selected registry auth is missing."""
    required = {
        "nuget": ("NUGET_API_KEY",),
        "crates": ("CARGO_REGISTRY_TOKEN",),
        "rubygems": ("GEM_HOST_API_KEY",),
        "maven": ("GNUPGHOME",),
        "packagist": ("PHP_MIRROR_SSH_KEY",),
    }
    missing = set()
    for package in packages.values():
        registry = package["registry"]
        missing.update(name for name in required.get(registry, ()) if not environment.get(name))
        if registry == "maven":
            publish_sources.maven_token(environment)
        if registry == "pypi":
            names = (("TWINE_USERNAME", "TWINE_PASSWORD") if environment.get("TWINE_PASSWORD")
                     else ("ACTIONS_ID_TOKEN_REQUEST_URL", "ACTIONS_ID_TOKEN_REQUEST_TOKEN"))
            missing.update(name for name in names if not environment.get(name))
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
    parser.add_argument("--recovery", type=Path)
    parser.add_argument("--source-proof", type=Path)
    args = parser.parse_args()
    if not args.execute or os.getenv("MIMIC_SDK_PUBLICATION_ENABLED") != "true" or os.getenv("CI") != "true":
        raise release.ReleaseError("Publication is disabled; use local plan/build/handoff. Execution requires explicitly enabled CI")
    plan, build = release.read(args.plan), release.read(args.build)
    handoff = release.publication_handoff(plan, build, [release.read(path) for path in args.qualification])
    revision = release.git_revision()
    if os.getenv("RESUME_SOURCE_REVISION"):
        resume.authorize_recovery(args.plan, args.build, args.qualification,
                                  args.recovery, args.source_proof, os.environ)
    elif not revision or plan["sdkRevision"] != revision:
        raise release.ReleaseError("Publication requires the exact committed SDK source from the release plan")
    if release.execute(["git", "status", "--porcelain", "--untracked-files=all"], capture=True):
        raise release.ReleaseError("Publication requires a clean source tree; keep outputs in ignored .build")
    state = release.read(args.state) if args.state.exists() else {"kind":"sdk-publication-progress", "buildSha256":handoff["buildSha256"], "sdkRevision":plan["sdkRevision"], "packages":{}}
    if state["buildSha256"] != handoff["buildSha256"] or state["sdkRevision"] != plan["sdkRevision"]:
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
        if progress["state"] in ("uploaded", "verifying") and not (package["registry"] == "maven" and progress.get("deploymentId")):
            verify_uploaded_registry(selected, registry_state, progress)
            progress["state"] = "verified"; save(); continue
        # Recover an upload whose response was lost by verifying the registry first.
        try:
            release.verify_registry(selected, registry_state)
            progress["state"] = "verified"; save(); continue
        except urllib.error.HTTPError as error:
            if error.code != 404:
                raise
            error.close()
        except release.RegistryNotIndexed:
            pass
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
            environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}
            if registry == "pypi":
                environment = pypi_auth.publication_environment(environment)
            result = subprocess.run(expanded, cwd=release.ROOT, stdin=subprocess.DEVNULL, env=environment)
            if result.returncode:
                raise release.ReleaseError(f"{key}: native publisher failed (exit {result.returncode}); resume using the same receipt")
        progress["state"] = "uploaded"; save()
        verify_uploaded_registry(selected, registry_state, progress)
        progress["state"] = "verified"; save()
    state["status"] = "complete"; save()
    print("All selected package registry artifacts verified")


if __name__ == "__main__":
    try:
        main()
    except (release.ReleaseError, OSError, ValueError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
