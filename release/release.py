#!/usr/bin/env python3
"""Independent SDK release planning, native packing and resumable registry verification.

Every default operation is local/read-only. Publication is an explicit native
registry handoff; this tool never invents credentials or mutates package versions.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import fnmatch
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile

ROOT = Path(__file__).resolve().parents[1]
EXCLUDED = {"node_modules", "__pycache__", ".venv", "target", "vendor", "bin", "obj", ".git", ".build", ".tools"}
VERSION = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?$")


class ReleaseError(RuntimeError):
    pass


def read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    os.replace(temporary, path)


def sha(data: bytes):
    return hashlib.sha256(data).hexdigest()


def utc():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def execute(command, cwd=ROOT, *, capture=False):
    """No shell evaluation; tools may be overridden by one executable path only."""
    command = [str(part) for part in command]
    tool = command[0]
    command[0] = os.environ.get("MIMIC_RELEASE_" + tool.upper().replace("-", "_"), tool)
    try:
        environment = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "PIP_NO_INPUT": "1"}
        result = subprocess.run(command, cwd=cwd, check=True, text=True, capture_output=capture, stdin=subprocess.DEVNULL, env=environment)
    except (OSError, subprocess.CalledProcessError) as error:
        raise ReleaseError(f"Command failed: {tool} ({error})") from error
    return result.stdout.strip() if capture else None


def git_revision():
    try:
        return execute(["git", "rev-parse", "HEAD"], capture=True)
    except ReleaseError:
        return None


def catalog():
    result = read(ROOT / "release/packages.json")["packages"]
    for key, package in result.items():
        if not VERSION.fullmatch(package["version"]):
            raise ReleaseError(f"Invalid native package version for {key}")
        directory = (ROOT / package["directory"]).resolve()
        if not directory.is_relative_to(ROOT) or not directory.is_dir():
            raise ReleaseError(f"Invalid source directory for {key}")
    return result


def source_files(package):
    paths = set()
    for pattern in package["inputs"]:
        # pathlib's trailing ** selects directories; package catalog means all descendants.
        expanded = pattern + "/*" if pattern.endswith("/**") else pattern
        for path in ROOT.glob(expanded):
            if not path.is_file():
                continue
            relative = path.relative_to(ROOT)
            # bin is authored in Ruby/PHP and generated only in .NET.
            excluded = EXCLUDED - ({"bin"} if relative.parts[0] in ("php", "ruby") else set())
            if any(part in excluded for part in relative.parts):
                continue
            if path.name.endswith(".pyc"):
                continue
            paths.add(path)
    # WindowsPath orders case-insensitively; provenance must have one cross-host order.
    return sorted(paths, key=lambda path: path.relative_to(ROOT).parts)


def input_digest(package):
    """Ignore this package's own version so an empty bump cannot hide no change."""
    digest = hashlib.sha256()
    all_packages = read(ROOT / "release/packages.json")["packages"]
    for dependency in package["requires"]:
        digest.update((dependency + "=" + all_packages[dependency]["version"]).encode() + b"\0")
    for path in source_files(package):
        data = path.read_bytes()
        if path.name in ("package.json", "composer.json", "package-lock.json"):
            document = json.loads(data)
            document.pop("version", None)
            if path.name == "package-lock.json":
                document.get("packages", {}).get("", {}).pop("version", None)
            data = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        elif package["registry"] == "rubygems" and path.name == "mimic_sdk.rb":
            data = re.sub(rf"(VERSION\s*=\s*['\"]){re.escape(package['version'])}(['\"])", r"\1<package-version>\2", data.decode(), count=1).encode()
        elif path.name == "Cargo.lock":
            import tomllib
            document = tomllib.loads(data.decode())
            for dependency in document.get("package", []):
                if dependency.get("name") == package["name"]:
                    dependency["version"] = "<package-version>"
            data = json.dumps(document, sort_keys=True, separators=(",", ":")).encode()
        elif path.suffix in (".csproj", ".gemspec") or path.name in ("pom.xml", "Cargo.toml", "pyproject.toml"):
            # Only remove the declared package version, retaining dependency versions.
            text = data.decode()
            value = re.escape(package["version"])
            patterns = [rf"(<Version>){value}(</Version>)", rf"(spec\.version\s*=\s*['\"]){value}(['\"])"]
            if path.name == "pom.xml":
                component = re.escape(package["name"].split(":", 1)[1])
                patterns.append(rf"(<artifactId>{component}</artifactId>\s*<version>){value}(</version>)")
            if path.suffix == ".toml":
                patterns.append(rf'(?m)^(version\s*=\s*"){value}("\s*)$')
            for pattern in patterns:
                text = re.sub(pattern, r"\1<package-version>\2", text, count=1)
            data = text.encode()
        digest.update(path.relative_to(ROOT).as_posix().encode() + b"\0" + hashlib.sha256(data).digest())
    return digest.hexdigest()


def check_versions(packages):
    for key, item in packages.items():
        directory = ROOT / item["directory"]
        registry = item["registry"]
        if registry in ("npm", "packagist"):
            metadata = read(directory / ("package.json" if registry == "npm" else "composer.json"))
            # Packagist derives versions from Git tags; the release catalog
            # remains authoritative when composer.json omits its version.
            actual = metadata.get("version", item["version"]) if registry == "packagist" else metadata["version"]
            identity = metadata["name"]
        elif registry == "nuget":
            metadata = ET.parse(next(directory.glob("*.csproj")))
            actual, identity = metadata.findtext(".//Version"), metadata.findtext(".//PackageId")
        elif registry == "maven":
            metadata = ET.parse(directory / "pom.xml")
            ns = "{http://maven.apache.org/POM/4.0.0}"
            actual = metadata.findtext(ns + "version")
            identity = metadata.findtext(ns + "groupId") + ":" + metadata.findtext(ns + "artifactId")
        elif registry in ("pypi", "crates"):
            import tomllib
            document = tomllib.loads((directory / ("pyproject.toml" if registry == "pypi" else "Cargo.toml")).read_text())
            metadata = document["project" if registry == "pypi" else "package"]
            actual, identity = metadata["version"], metadata["name"]
        elif registry == "rubygems":
            metadata = next(directory.glob("*.gemspec")).read_text()
            actual = re.search(r"spec\.version\s*=\s*['\"]([^'\"]+)", metadata).group(1)
            identity = re.search(r"spec\.name\s*=\s*['\"]([^'\"]+)", metadata).group(1)
        else:
            actual, identity = item["version"], item["name"]  # Go's version is its module-prefixed tag.
            if not (directory / "go.mod").read_text().startswith("module " + item["name"] + "\n"):
                raise ReleaseError("Go module path disagrees with package catalog")
        if actual != item["version"]:
            raise ReleaseError(f"{key}: catalog {item['version']} differs from native metadata {actual}")
        if identity != item["name"]:
            raise ReleaseError(f"{key}: catalog package name {item['name']} differs from native metadata {identity}")


def make_plan(selected, baseline=None):
    packages = catalog()
    check_versions(packages)
    selected = list(dict.fromkeys(selected))
    if not selected or any(key not in packages for key in selected):
        raise ReleaseError("Select known package IDs explicitly")
    prior = {} if baseline is None else baseline["packages"]
    plan = {"kind":"sdk-release-plan", "createdAt":utc(), "sdkRevision":git_revision(), "schemaSha256":sha((ROOT / "schema/mimic/protocol.json").read_bytes()), "runtimeLockSha256":sha((ROOT / "release/runtime-lock.json").read_bytes()), "packages":{}}
    for key in selected:
        package = packages[key]
        digest = input_digest(package)
        previous = prior.get(key)
        if previous and previous["inputDigest"] == digest:
            raise ReleaseError(f"{key}: packaged inputs unchanged; do not publish an empty version bump")
        if previous and previous["version"] == package["version"]:
            raise ReleaseError(f"{key}: changed package requires an intentional native version update")
        plan["packages"][key] = {"name":package["name"], "version":package["version"], "registry":package["registry"], "inputDigest":digest, "requires":package["requires"]}
    return plan


def validate_plan(plan):
    if plan.get("kind") != "sdk-release-plan" or not plan.get("packages"):
        raise ReleaseError("Expected nonempty explicit SDK release plan")
    packages = catalog()
    check_versions(packages)
    if plan["schemaSha256"] != sha((ROOT / "schema/mimic/protocol.json").read_bytes()) or plan["runtimeLockSha256"] != sha((ROOT / "release/runtime-lock.json").read_bytes()):
        raise ReleaseError("Schema or immutable default runtime lock changed after release planning")
    for key, chosen in plan["packages"].items():
        if key not in packages or any(chosen.get(field) != packages[key][field] for field in ("version", "name", "registry")):
            raise ReleaseError(f"{key}: package selection does not match native catalog")
        if chosen["inputDigest"] != input_digest(packages[key]):
            raise ReleaseError(f"{key}: package inputs changed after planning")
    return packages


def inspect_archive(path: Path, registry: str):
    """Validate package envelope and return a canonical content digest, ignoring archive timestamps."""
    files = {}
    def add(name, data):
        normalized = name.replace("\\", "/")
        if normalized.startswith("/") or ".." in normalized.split("/") or re.match(r"^[A-Za-z]:", normalized):
            raise ReleaseError(f"Unsafe package entry: {name}")
        if any(part in {".build", ".tools", ".git", "internal-notes", ".env", ".npmrc", ".pypirc", "credentials.toml"} for part in normalized.split("/")):
            raise ReleaseError(f"Private file in package: {name}")
        checkout_paths = {str(ROOT), ROOT.as_posix(), str(ROOT).replace("\\", "/")}
        if any(path.encode(encoding) in data for path in checkout_paths for encoding in ("utf-8", "utf-16-le")):
            raise ReleaseError(f"Local checkout path embedded in package: {name}")
        if normalized in files:
            raise ReleaseError(f"Duplicate package entry: {name}")
        files[normalized] = sha(data)
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                if not entry.is_dir():
                    if entry.external_attr >> 16 & 0o170000 == 0o120000:
                        raise ReleaseError("Package contains a symbolic link")
                    add(entry.filename, archive.read(entry))
    elif tarfile.is_tarfile(path):
        with tarfile.open(path) as archive:
            for entry in archive:
                if entry.isfile():
                    add(entry.name, archive.extractfile(entry).read())
                elif not entry.isdir():
                    raise ReleaseError("Package contains a link or special file")
    else:
        # POMs and detached signatures are also independently verified artifacts.
        add(path.name, path.read_bytes())
    if not files:
        raise ReleaseError("Empty package artifact")
    return {"contentSha256":sha(json.dumps(files, sort_keys=True, separators=(",", ":")).encode()), "entries":len(files)}


def artifact(path, registry):
    return {"file":str(path.resolve()), "filename":path.name, "size":path.stat().st_size, "sha256":sha(path.read_bytes()), **inspect_archive(path, registry)}


def pack_package(key, package, output):
    directory = ROOT / package["directory"]
    destination = output / key
    destination.mkdir(parents=True, exist_ok=True)
    if any(destination.iterdir()):
        raise ReleaseError(f"Output must be empty to avoid packaging stale files: {destination}")
    registry = package["registry"]
    if registry == "npm":
        execute(["npm", "run", "build"], directory)
        execute(["npm", "pack", "--pack-destination", destination], directory)
    elif registry == "pypi":
        execute(["python", "-m", "build", "--outdir", destination], directory)
    elif registry == "nuget":
        execute(["dotnet", "pack", next(directory.glob("*.csproj")), "--configuration", "Release", "--output", destination, "--nologo"], directory)
    elif registry == "maven":
        execute(["mvn", "-B", "-ntp", "package"], directory)
        component = package["name"].split(":", 1)[1]
        for suffix in (".jar", "-sources.jar", "-javadoc.jar"):
            shutil.copyfile(directory / "target" / f"{component}-{package['version']}{suffix}", destination / f"{component}-{package['version']}{suffix}")
        shutil.copyfile(directory / "pom.xml", destination / f"{component}-{package['version']}.pom")
    elif registry == "crates":
        execute(["cargo", "package", "--allow-dirty", "--locked"], directory)
        metadata = json.loads(execute(["cargo", "metadata", "--format-version", "1", "--no-deps", "--locked"], directory, capture=True))
        name = f"{package['name']}-{package['version']}.crate"
        shutil.copyfile(Path(metadata["target_directory"]) / "package" / name, destination / name)
    elif registry == "rubygems":
        execute(["gem", "build", next(directory.glob("*.gemspec")), "--output", destination / f"{package['name']}-{package['version']}.gem"], directory)
    elif registry == "packagist":
        execute(["composer", "validate", "--no-interaction"], directory)
        execute(["composer", "archive", "--format=zip", "--dir", destination, "--no-interaction"], directory)
    elif registry == "go":
        execute(["go", "list", "-m"], directory)
        prefix = package["name"] + "@v" + package["version"] + "/"
        with zipfile.ZipFile(destination / f"go-v{package['version']}.zip", "w", zipfile.ZIP_DEFLATED) as archive:
            for source in source_files(package):
                entry = zipfile.ZipInfo(prefix + source.relative_to(directory).as_posix(), (1980, 1, 1, 0, 0, 0))
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, source.read_bytes())
    files = sorted(path for path in destination.iterdir() if path.is_file())
    if not files:
        raise ReleaseError(f"{key}: native builder produced no artifacts")
    return [artifact(path, registry) for path in files]


def build(plan, output):
    packages = validate_plan(plan)
    execute(["python", "generator/generate.py", "--check"])
    if "node" in plan["packages"]:
        execute(["node", "node/scripts/git-package.mjs", "--check"])
    receipt_path = output / "build.json"
    receipt = read(receipt_path) if receipt_path.exists() else {"kind":"sdk-build-receipt", "planSha256":sha(json.dumps(plan, sort_keys=True).encode()), "packages":{}, "createdAt":utc()}
    if receipt["planSha256"] != sha(json.dumps(plan, sort_keys=True).encode()):
        raise ReleaseError("Output belongs to a different release plan")
    for key, chosen in plan["packages"].items():
        if key in receipt["packages"]:
            verify_local(receipt["packages"][key])
            continue
        output.mkdir(parents=True, exist_ok=True)
        final = output / key
        if final.exists():
            raise ReleaseError(f"Unreceipted output requires explicit inspection/repair: {final}")
        with tempfile.TemporaryDirectory(prefix=".staging-", dir=output) as temporary:
            pack_package(key, packages[key], Path(temporary))
            validate_plan(plan)
            os.replace(Path(temporary) / key, final)
        artifacts = [artifact(path, packages[key]["registry"]) for path in sorted(final.iterdir()) if path.is_file()]
        receipt["packages"][key] = {**chosen, "artifacts":artifacts, "packedAt":utc()}
        write(receipt_path, receipt)
    return receipt


def verify_local(package):
    for item in package["artifacts"]:
        path = Path(item["file"])
        if not path.is_file() or path.stat().st_size != item["size"] or sha(path.read_bytes()) != item["sha256"]:
            raise ReleaseError(f"Local artifact changed or missing: {path}")


def registry_urls(package):
    name, version, registry = package["name"], package["version"], package["registry"]
    if registry == "npm":
        metadata = fetch_json(f"https://registry.npmjs.org/{urllib.parse.quote(name, safe='')}/{version}")
        return {package["artifacts"][0]["filename"]:metadata["dist"]["tarball"]}
    if registry == "pypi":
        metadata = fetch_json(f"https://pypi.org/pypi/{name}/{version}/json")
        return {item["filename"]:item["url"] for item in metadata["urls"]}
    if registry == "nuget":
        filename = f"{name.lower()}.{version.lower()}.nupkg"
        return {package["artifacts"][0]["filename"]:f"https://api.nuget.org/v3-flatcontainer/{name.lower()}/{version.lower()}/{filename}"}
    if registry == "maven":
        group, component = name.split(":")
        prefix = f"https://repo.maven.apache.org/maven2/{group.replace('.', '/')}/{component}/{version}/"
        return {item["filename"]:prefix + item["filename"] for item in package["artifacts"]}
    if registry == "crates":
        return {package["artifacts"][0]["filename"]:f"https://crates.io/api/v1/crates/{name}/{version}/download"}
    if registry == "rubygems":
        return {package["artifacts"][0]["filename"]:f"https://rubygems.org/downloads/{name}-{version}.gem"}
    if registry == "go":
        return {package["artifacts"][0]["filename"]:f"https://proxy.golang.org/{name}/@v/v{version}.zip"}
    if registry == "packagist":
        metadata = fetch_json("https://repo.packagist.org/p2/" + name + ".json")
        chosen = next((item for item in metadata["packages"][name] if item["version"].lstrip("v") == version), None)
        if not chosen or not chosen.get("dist"):
            raise ReleaseError("Packagist has not indexed the selected mirror version")
        return {package["artifacts"][0]["filename"]:chosen["dist"]["url"]}
    raise ReleaseError("Unsupported package registry")


def fetch(url):
    if not url.startswith("https://"):
        raise ReleaseError("Registry artifact URLs must use HTTPS")
    request = urllib.request.Request(url, headers={"User-Agent":"Mimic-SDK-Release/0.1"})
    with urllib.request.urlopen(request, timeout=60) as response:
        result = response.read(256 * 1024 * 1024 + 1)
        if len(result) > 256 * 1024 * 1024:
            raise ReleaseError("Registry artifact exceeds 256 MiB")
        return result


def fetch_json(url):
    return json.loads(fetch(url))


def verify_registry(build_receipt, state_path):
    """Retain completion after each package; retries skip only matching verified bytes."""
    state = read(state_path) if state_path.exists() else {"kind":"sdk-registry-receipt", "buildSha256":sha(json.dumps(build_receipt, sort_keys=True).encode()), "packages":{}}
    if state["buildSha256"] != sha(json.dumps(build_receipt, sort_keys=True).encode()):
        raise ReleaseError("Registry state belongs to a different build")
    for key, package in build_receipt["packages"].items():
        verify_local(package)
        if key in state["packages"]:
            continue
        urls = registry_urls(package)
        completed = []
        for item in package["artifacts"]:
            url = urls.get(item["filename"])
            if not url:
                raise ReleaseError(f"Registry is missing selected artifact {item['filename']}")
            data = fetch(url)
            # Go/Packagist construct source archives themselves; compare canonical file contents.
            if package["registry"] in ("go", "packagist"):
                with tempfile.TemporaryDirectory(prefix="mimic-registry-") as temporary:
                    path = Path(temporary) / "archive.zip"
                    path.write_bytes(data)
                    remote = inspect_archive(path, package["registry"])
                    if package["registry"] == "packagist":
                        if normalized_zip(Path(item["file"]), False) != normalized_zip(path, True):
                            raise ReleaseError(f"Registry source contents differ: {key}")
                    elif remote["contentSha256"] != item["contentSha256"]:
                        raise ReleaseError(f"Registry module contents differ: {key}")
            elif sha(data) != item["sha256"]:
                raise ReleaseError(f"Registry bytes differ: {item['filename']}")
            completed.append({"filename":item["filename"], "url":url, "sha256":sha(data), "verifiedAt":utc()})
        state["packages"][key] = {"version":package["version"], "status":"verified", "artifacts":completed}
        write(state_path, state)
    return state


def normalized_zip(path, strip_prefix):
    with zipfile.ZipFile(path) as archive:
        return {(entry.filename.split("/", 1)[1] if strip_prefix else entry.filename):sha(archive.read(entry)) for entry in archive.infolist() if not entry.is_dir()}


def publication_handoff(plan, receipt, qualifications):
    """Generate exact native publication commands. Execute only in separately enabled CI."""
    validate_plan(plan)
    if receipt["planSha256"] != sha(json.dumps(plan, sort_keys=True).encode()):
        raise ReleaseError("Build is not bound to this plan")
    if set(receipt["packages"]) != set(plan["packages"]):
        raise ReleaseError("Build package selection must exactly match the complete frozen release plan")
    qualified = {}
    default_lock = read(ROOT / "release/runtime-lock.json")
    default_hashes = {item["binarySha256"] for item in default_lock["manifest"]["artifacts"]}
    for document in qualifications:
        if document.get("kind") != "sdk-qualification-receipt" or document.get("status") != "passed" or document.get("buildSha256") != sha(json.dumps(receipt, sort_keys=True).encode()):
            raise ReleaseError("Qualification must bind this exact build receipt")
        runtime = document.get("runtime", {})
        if runtime.get("binarySha256") in default_hashes and runtime.get("identity", {}).get("version", "").lstrip("v") == default_lock["release"].lstrip("v"):
            qualified.update(document.get("packages", {}))
    commands = {}
    for key, package in receipt["packages"].items():
        verify_local(package)
        if qualified.get(key, {}).get("status") != "passed":
            raise ReleaseError(f"Selected package lacks full native qualification against its officially released default pin: {key}; a local candidate cannot qualify publication")
        files = [item["file"] for item in package["artifacts"]]
        registry = package["registry"]
        if registry == "npm": command = ["npm", "publish", files[0], "--access", "public", "--provenance"]
        elif registry == "pypi": command = ["python", "-m", "twine", "upload", "--non-interactive", *files]
        elif registry == "nuget": command = ["dotnet", "nuget", "push", files[0], "--source", "https://api.nuget.org/v3/index.json", "--api-key", "${NUGET_API_KEY}"]
        elif registry == "rubygems": command = ["gem", "push", files[0]]
        elif registry == "crates": command = ["cargo", "publish", "--locked", "--manifest-path", str(ROOT / "rust/Cargo.toml")]
        elif registry == "go": command = ["git", "push", "origin", f"{plan['sdkRevision']}:refs/tags/go/v{package['version']}"]
        elif registry == "maven": command = ["python", "release/publish_sources.py", "maven", "--build", "${BUILD_RECEIPT}", "--package", key]
        elif registry == "packagist": command = ["python", "release/publish_sources.py", "php", "--build", "${BUILD_RECEIPT}", "--package", key]
        commands[key] = {"command":command, "registry":registry, "version":package["version"], "status":"pending"}
    return {"kind":"sdk-publication-handoff", "buildSha256":sha(json.dumps(receipt, sort_keys=True).encode()), "sdkRevision":plan["sdkRevision"], "packages":commands}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="operation", required=True)
    plan = sub.add_parser("plan"); plan.add_argument("--select", nargs="+", required=True); plan.add_argument("--baseline", type=Path); plan.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser("validate"); validate.add_argument("--plan", type=Path, required=True)
    pack = sub.add_parser("build"); pack.add_argument("--plan", type=Path, required=True); pack.add_argument("--output", type=Path, required=True)
    verify = sub.add_parser("verify-registry"); verify.add_argument("--build", type=Path, required=True); verify.add_argument("--state", type=Path, required=True)
    handoff = sub.add_parser("handoff"); handoff.add_argument("--plan", type=Path, required=True); handoff.add_argument("--build", type=Path, required=True); handoff.add_argument("--qualification", type=Path, nargs="+", required=True); handoff.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    if args.operation == "plan": write(args.output, make_plan(args.select, read(args.baseline) if args.baseline else None))
    elif args.operation == "validate": validate_plan(read(args.plan)); print("PASS explicit plan, versions, package inputs, immutable default pin")
    elif args.operation == "build": build(read(args.plan), args.output.resolve())
    elif args.operation == "verify-registry": verify_registry(read(args.build), args.state)
    elif args.operation == "handoff": write(args.output, publication_handoff(read(args.plan), read(args.build), [read(path) for path in args.qualification]))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (ReleaseError, OSError, ValueError, KeyError) as error:
        print(f"release: {error}", file=sys.stderr)
        raise SystemExit(1)
