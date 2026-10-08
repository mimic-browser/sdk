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


def validate_run(document, repository, revision, source_revision=""):
    if document.get("repository", {}).get("full_name", "").lower() != repository.lower():
        raise release.ReleaseError("Resume run belongs to another repository")
    if document.get("head_sha") not in {revision, source_revision or revision}:
        raise release.ReleaseError("Resume run must use the exact current source commit")
    if document.get("path", "").split("@", 1)[0] != ".github/workflows/sdk-release.yml":
        raise release.ReleaseError("Resume run must use the SDK release workflow")
    if document.get("event") != "workflow_dispatch" or document.get("status") != "completed":
        raise release.ReleaseError("Resume requires a completed manually dispatched release run")


def check_source_run(run_id, environment, source_revision="", output=None):
    if not re.fullmatch(r"[1-9][0-9]*", run_id):
        raise release.ReleaseError("Resume run ID must be a positive integer")
    repository, revision = environment.get("GITHUB_REPOSITORY", ""), environment.get("GITHUB_SHA", "")
    token = environment.get("GH_TOKEN", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repository) or not revision or not token:
        raise release.ReleaseError("Resume source validation requires the GitHub Actions repository, commit and token")
    if source_revision and not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise release.ReleaseError("Recovery source revision must be an exact lowercase 40-character commit")
    def fetch(suffix):
        request = urllib.request.Request(
            f"https://api.github.com/repos/{repository}/actions/runs/{run_id}" + suffix,
            headers={"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json",
                     "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "Mimic-SDK-Release/0.1"})
        with urllib.request.urlopen(request, timeout=30) as response:
            return json.load(response)
    document = fetch("")
    validate_run(document, repository, revision, source_revision)
    if str(document.get("id")) != run_id:
        raise release.ReleaseError("GitHub returned a different release run")
    artifacts = [item for item in fetch("/artifacts?per_page=100").get("artifacts", [])
                 if item.get("name") == "selected-sdk-release-receipts" and item.get("expired") is False]
    if len(artifacts) != 1:
        raise release.ReleaseError("Resume requires one retained release artifact")
    artifact = artifacts[0]
    if (artifact.get("workflow_run", {}).get("head_sha") != document["head_sha"]
            or str(artifact.get("workflow_run", {}).get("id")) != run_id
            or not re.fullmatch(r"sha256:[0-9a-f]{64}", artifact.get("digest", ""))):
        raise release.ReleaseError("Retained artifact provenance differs from its release run")
    proof = {"kind": "sdk-resume-source-proof", "repository": repository,
             "sourceRunId": run_id, "sourceRunHeadSha": document["head_sha"],
             "sourceRunAttempt": document["run_attempt"], "sourceArtifactId": artifact["id"],
             "sourceArtifactDigest": artifact["digest"], "sdkRevision": source_revision or revision,
             "toolingRevision": revision, "currentRunId": environment.get("GITHUB_RUN_ID"),
             "currentRunAttempt": environment.get("GITHUB_RUN_ATTEMPT")}
    if output is not None:
        release.write(output, proof)
    print("Verified retained release run " + run_id + " at " + document["head_sha"])
    return proof


def digest(value):
    return release.sha(json.dumps(value, sort_keys=True).encode())


def recovery_proof(path, directory, source_revision, run_id, environment, authenticate=False):
    if path is None or Path(path).resolve().is_relative_to(directory.resolve()):
        raise release.ReleaseError("Recovery source proof must exist outside restored artifacts")
    if not re.fullmatch(r"[0-9a-f]{40}", source_revision):
        raise release.ReleaseError("Recovery source revision must be an exact lowercase 40-character commit")
    proof = release.read(path)
    expected = {"kind": "sdk-resume-source-proof", "repository": environment.get("GITHUB_REPOSITORY"),
                "sdkRevision": source_revision, "toolingRevision": release.git_revision(),
                "sourceRunId": run_id, "currentRunId": environment.get("GITHUB_RUN_ID"),
                "currentRunAttempt": environment.get("GITHUB_RUN_ATTEMPT")}
    if (not run_id or not expected["currentRunId"] or not expected["currentRunAttempt"]
            or expected["toolingRevision"] != environment.get("GITHUB_SHA")
            or any(proof.get(key) != value for key, value in expected.items())
            or proof.get("sourceRunHeadSha") not in {source_revision, expected["toolingRevision"]}):
        raise release.ReleaseError("Recovery source proof does not match this workflow and declared source")
    if authenticate and proof != check_source_run(run_id, environment, source_revision):
        raise release.ReleaseError("Recovery source proof differs from authenticated GitHub run and artifact metadata")
    return proof


def recovery_binding(plan, directory):
    validate_retained(plan, directory)
    build = release.read(directory / "build.json")
    qualification = release.read(directory / "qualification.json")
    # Includes complete selection, unchanged inputs/schema/lock, archive hashes,
    # and successful native qualification against the officially released pin.
    release.publication_handoff(plan, build, [qualification])
    if any(item["registry"] in {"go", "crates", "packagist"} for item in build["packages"].values()):
        raise release.ReleaseError("Tooling recovery supports retained archive uploads, not source-producing publishers")
    return {"sdkRevision": plan["sdkRevision"], "planSha256": digest(plan),
            "buildSha256": digest(build), "qualificationSha256": digest(qualification)}


def prepare_recovery(source, output, source_revision, run_id, proof_path, environment):
    expected = release.read(source)
    release.validate_plan(expected)
    expected["sdkRevision"] = source_revision
    if not output.is_file() or release.read(output) != expected:
        raise release.ReleaseError("Recovery must preserve the exact original frozen source plan")
    directory = output.parent.resolve()
    proof = recovery_proof(proof_path, directory, source_revision, run_id, environment)
    binding = recovery_binding(expected, directory)
    target = directory / "recovery.json"
    if proof["sourceRunHeadSha"] != source_revision:
        previous = release.read(target)
        if (previous.get("kind") != "sdk-tooling-recovery"
                or previous.get("toolingRevision") != proof["sourceRunHeadSha"]
                or previous.get("currentRunId") != run_id
                or any(previous.get(key) != value for key, value in binding.items())):
            raise release.ReleaseError("Prior recovery lineage does not bind the original artifacts and source run")
    attestation = {"kind": "sdk-tooling-recovery", **binding,
                   "toolingRevision": proof["toolingRevision"],
                   "currentRunId": proof["currentRunId"], "currentRunAttempt": proof["currentRunAttempt"],
                   "sourceRunId": run_id, "sourceRunHeadSha": proof["sourceRunHeadSha"],
                   "sourceArtifactId": proof["sourceArtifactId"],
                   "sourceArtifactDigest": proof["sourceArtifactDigest"],
                   "sourceProofSha256": digest(proof)}
    release.write(target, attestation)
    print("Verified tooling recovery; original plan, build, qualification and artifacts are unchanged")
    return attestation


def authorize_recovery(plan_path, build_path, qualifications, attestation_path, proof_path, environment):
    directory = plan_path.parent.resolve()
    if (build_path.resolve() != directory / "build.json"
            or [path.resolve() for path in qualifications] != [directory / "qualification.json"]
            or attestation_path is None or attestation_path.resolve() != directory / "recovery.json"):
        raise release.ReleaseError("Tooling recovery must use the original retained receipt paths")
    source_revision = environment.get("RESUME_SOURCE_REVISION", "")
    proof = recovery_proof(proof_path, directory, source_revision,
                           environment.get("RESUME_RUN_ID", ""), environment, authenticate=True)
    plan = release.read(plan_path)
    binding = recovery_binding(plan, directory)
    attestation = release.read(attestation_path)
    expected = {"kind": "sdk-tooling-recovery", **binding,
                "sdkRevision": source_revision, "toolingRevision": proof["toolingRevision"],
                "currentRunId": proof["currentRunId"], "currentRunAttempt": proof["currentRunAttempt"],
                "sourceRunId": proof["sourceRunId"], "sourceRunHeadSha": proof["sourceRunHeadSha"],
                "sourceArtifactId": proof["sourceArtifactId"],
                "sourceArtifactDigest": proof["sourceArtifactDigest"], "sourceProofSha256": digest(proof)}
    if plan["sdkRevision"] != source_revision or attestation != expected:
        raise release.ReleaseError("Tooling recovery attestation does not bind this verified original build")
    return attestation


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
    source.add_argument("--source-revision", default="")
    source.add_argument("--output", type=Path)
    plan = commands.add_parser("plan")
    plan.add_argument("--plan", type=Path, required=True)
    plan.add_argument("--output", type=Path, required=True)
    plan.add_argument("--resume-run", default="")
    plan.add_argument("--source-revision", default="")
    plan.add_argument("--source-proof", type=Path)
    args = parser.parse_args()
    if args.operation == "source":
        proof = check_source_run(args.run_id, os.environ, args.source_revision, args.output)
        if os.getenv("GITHUB_OUTPUT"):
            with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as output:
                print("artifact_id=" + str(proof["sourceArtifactId"]), file=output)
    else:
        if args.resume_run and not re.fullmatch(r"[1-9][0-9]*", args.resume_run):
            raise release.ReleaseError("Resume run ID must be a positive integer")
        if args.source_revision:
            if not args.resume_run:
                raise release.ReleaseError("Tooling recovery requires an explicit completed resume run")
            prepare_recovery(args.plan, args.output, args.source_revision, args.resume_run, args.source_proof, os.environ)
        else:
            prepare_plan(args.plan, args.output, bool(args.resume_run))


if __name__ == "__main__":
    try:
        main()
    except (release.ReleaseError, OSError, ValueError, KeyError, TypeError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1)
