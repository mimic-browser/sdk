#!/usr/bin/env python3
"""Verify a retained release run before resuming its immutable build receipts."""
import argparse
import json
import os
from pathlib import Path
import re
import sys
import urllib.request

import release


def validate_run(document, repository, revision):
    if document.get("repository", {}).get("full_name", "").lower() != repository.lower():
        raise release.ReleaseError("Resume run belongs to another repository")
    if document.get("head_sha") != revision:
        raise release.ReleaseError("Resume run must use the exact current source commit")
    if document.get("path", "").split("@", 1)[0] != ".github/workflows/sdk-release.yml":
        raise release.ReleaseError("Resume run must use the SDK release workflow")
    if document.get("event") != "workflow_dispatch" or document.get("status") != "completed":
        raise release.ReleaseError("Resume requires a completed manually dispatched release run")


def check_source_run(run_id, environment):
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise release.ReleaseError("Resume run ID must be a positive integer")
    repository, revision = environment.get("GITHUB_REPOSITORY", ""), environment.get("GITHUB_SHA", "")
    token = environment.get("GH_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not revision or not token:
        raise release.ReleaseError("Resume source validation requires the GitHub Actions repository, commit and token")
    request = urllib.request.Request(
        f"https://api.github.com/repos/{repository}/actions/runs/{run_id}",
        headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Mimic-SDK-Release/0.1"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        document = json.load(response)
    validate_run(document, repository, revision)
    print("Verified retained release run " + run_id + " at " + revision)


def digest(value):
    return release.sha(json.dumps(value, sort_keys=True).encode())


def validate_retained(plan, directory):
    build_path = directory / "build.json"
    if not build_path.exists():
        if ((directory / "publication.json").exists() or (directory / "qualification.json").exists()
                or any(directory.glob("publication-*-registry.json"))):
            raise release.ReleaseError("Retained publication or qualification state requires its build receipt")
        return
    build = release.read(build_path)
    if build.get("kind") != "sdk-build-receipt" or build.get("planSha256") != digest(plan):
        raise release.ReleaseError("Retained build must bind the exact frozen release plan")
    packages = build.get("packages", {})
    if not isinstance(packages, dict) or not set(packages).issubset(plan["packages"]):
        raise release.ReleaseError("Retained build has an unexpected package selection")
    for key, package in packages.items():
        if any(package.get(field) != value for field, value in plan["packages"][key].items()):
            raise release.ReleaseError("Retained package differs from frozen plan: " + key)
        artifacts = package.get("artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise release.ReleaseError("Retained package has no verified artifacts: " + key)
        for artifact in artifacts:
            path = Path(artifact["file"]).resolve()
            if not path.is_relative_to((directory / key).resolve()) or path.name != artifact["filename"]:
                raise release.ReleaseError("Retained artifact must remain at its original package path")
        release.verify_local(package)
    publication = directory / "publication.json"
    if publication.exists():
        state = release.read(publication)
        if (state.get("kind") != "sdk-publication-progress" or state.get("sdkRevision") != plan["sdkRevision"]
                or state.get("buildSha256") != digest(build) or set(packages) != set(plan["packages"])):
            raise release.ReleaseError("Retained publication state must bind the complete exact build and source")
        progress = state.get("packages", {})
        if not isinstance(progress, dict) or not set(progress).issubset(packages):
            raise release.ReleaseError("Retained publication has an unexpected package selection")
        for key, item in progress.items():
            if item.get("state") not in {"pending", "uploading", "uploaded", "verifying", "verified", "pushing"}:
                raise release.ReleaseError("Retained publication has an unknown state")
            if item.get("state") == "pushing":
                if (packages[key]["registry"] != "packagist" or item.get("sourceRevision") != plan["sdkRevision"]
                        or item.get("archiveSha256") != packages[key]["artifacts"][0]["sha256"]
                        or not re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", item.get("mirrorCommit", ""))):
                    raise release.ReleaseError("Retained mirror push requires its exact source, archive and commit provenance")
    qualification = directory / "qualification.json"
    if qualification.exists():
        record = release.read(qualification)
        if (record.get("kind") != "sdk-qualification-receipt" or record.get("sdkRevision") != plan["sdkRevision"]
                or record.get("buildSha256") != digest(build)):
            raise release.ReleaseError("Retained qualification must bind the exact build and source")
    for path in directory.glob("publication-*-registry.json"):
        key = path.name.removeprefix("publication-").removesuffix("-registry.json")
        if key not in packages:
            raise release.ReleaseError("Retained registry receipt has an unexpected package")
        record = release.read(path)
        selected = {**build, "packages": {key: packages[key]}}
        if record.get("kind") != "sdk-registry-receipt" or record.get("buildSha256") != digest(selected):
            raise release.ReleaseError("Retained registry receipt must bind its exact selected build")


def prepare_plan(source, output, resume=False):
    expected = release.read(source)
    release.validate_plan(expected)
    revision = release.git_revision()
    if not revision:
        raise release.ReleaseError("Release planning requires a committed checkout")
    expected["sdkRevision"] = revision
    if resume:
        if not output.is_file() or release.read(output) != expected:
            raise release.ReleaseError("Retained frozen plan differs from the selected plan or current source")
        validate_retained(expected, output.parent.resolve())
        print("Verified retained release plan and immutable receipts")
    else:
        if output.parent.exists() and any(output.parent.iterdir()):
            raise release.ReleaseError("Fresh release output must be empty; use an explicit verified resume run")
        release.write(output, expected)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    source = commands.add_parser("source")
    source.add_argument("--run-id", required=True)
    plan = commands.add_parser("plan")
    plan.add_argument("--plan", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--resume-run", default="")
    args = parser.parse_args()
    if args.operation == "source":
        check_source_run(args.run_id, os.environ)
    else:
        if args.resume_run and not re.fullmatch(r"[1-9][0-9]*", args.resume_run):
            raise release.ReleaseError("Resume run ID must be a positive integer")
        prepare_plan(args.plan, args.output, bool(args.resume_run))


if __name__ == "__main__":
    try:
        main()
    except (release.ReleaseError, OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
