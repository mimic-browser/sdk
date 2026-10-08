#!/usr/bin/env python3
"""Install/import or compile the actual built payloads in isolated consumers."""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tarfile
import tempfile
import xml.etree.ElementTree as ET
import zipfile

import release


def run(command, directory, environment=None):
    environment = dict(os.environ if environment is None else environment)
    environment.pop("PYTHONPATH", None)
    environment.pop("NODE_PATH", None)
    environment["MIMIC_DOWNLOAD"] = "0"
    environment["MIMIC_RUNTIME_DIR"] = str(directory / "never-created-runtime-cache")
    command = [str(value) for value in command]
    command[0] = environment.get("MIMIC_RELEASE_" + command[0].upper(), command[0])
    result = subprocess.run(command, cwd=directory, env=environment)
    if result.returncode:
        raise release.ReleaseError("Packaged consumer failed: " + command[0])
    if (directory / "never-created-runtime-cache").exists():
        raise release.ReleaseError("Import unexpectedly created a runtime cache")


def unpack(path, destination):
    release.inspect_archive(path, "source")
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for entry in archive.infolist():
                if entry.is_dir(): continue
                target = destination / entry.filename; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(entry))
    else:
        with tarfile.open(path) as archive:
            for entry in archive:
                if not entry.isfile(): continue
                target = destination / entry.name; target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.extractfile(entry).read())


def check(key, package, build, directory):
    registry = package["registry"]
    files = [Path(item["file"]) for item in package["artifacts"]]
    if registry == "npm":
        (directory / "package.json").write_text('{"name":"mimic-package-check","private":true,"type":"module"}')
        run(["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund", files[0]], directory)
        run(["node", "--input-type=module", "-e", "import assert from 'node:assert/strict'; import {createRequire} from 'node:module'; const require=createRequire(import.meta.url); const sdk = await import('mimic-browser'); assert.equal(typeof sdk.RuntimeManager,'function'); for(const name of ['playwright-core','puppeteer-core']) assert.throws(()=>require.resolve(name),{code:'MODULE_NOT_FOUND'});"], directory)
        metadata = release.read(directory / "node_modules/mimic-browser/package.json")
        peers = [name + "@" + metadata.get("devDependencies", {}).get(name, version) for name, version in metadata["peerDependencies"].items()]
        run(["npm", "install", "--ignore-scripts", "--no-audit", "--no-fund", *peers], directory)
        run(["node", "--input-type=module", "-e", "await import('mimic-browser/playwright'); await import('mimic-browser/puppeteer');"], directory)
    elif registry == "pypi":
        run(["python", "-m", "venv", directory / "venv"], directory)
        python = directory / "venv" / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
        wheel = next(path for path in files if path.suffix == ".whl")
        run([python, "-m", "pip", "install", wheel], directory)
        run([python, "-c", "import mimic; from mimic import RuntimeManager; from mimic.playwright.sync_api import launch; from mimic.playwright.async_api import launch as launch_async; from mimic.pyppeteer import launch as launch_pyppeteer; import importlib.util; assert all(importlib.util.find_spec(name) is None for name in ('playwright', 'pyppeteer', 'mimic_sdk'))"], directory)
    elif registry == "nuget":
        feed = directory / "feed"; feed.mkdir()
        for selected in build["packages"].values():
            if selected["registry"] == "nuget":
                for item in selected["artifacts"]: shutil.copyfile(item["file"], feed / item["filename"])
        name, version = package["name"], package["version"]
        (directory / "Consumer.csproj").write_text(f'<Project Sdk="Microsoft.NET.Sdk"><PropertyGroup><OutputType>Exe</OutputType><TargetFramework>net8.0</TargetFramework><ImplicitUsings>enable</ImplicitUsings></PropertyGroup><ItemGroup><PackageReference Include="{name}" Version="{version}" /></ItemGroup></Project>')
        reference = "typeof(Mimic.Sdk.RuntimeManager)" if key == "dotnet-core" else "typeof(Mimic.Playwright.PlaywrightSession)" if key == "dotnet-playwright" else "typeof(Mimic.PuppeteerSharp.PuppeteerSession)"
        (directory / "Program.cs").write_text('using Mimic.Sdk; if(RuntimeManager.DefaultLock()["release"] is null) throw new Exception("Missing lock"); Console.WriteLine(' + reference + '.Assembly.GetName());')
        run(["dotnet", "restore", "Consumer.csproj", "--packages", directory / "packages", "--source", feed, "--source", "https://api.nuget.org/v3/index.json"], directory)
        run(["dotnet", "run", "--project", "Consumer.csproj", "--no-restore", "--no-launch-profile"], directory)
    elif registry == "maven":
        ns = "{http://maven.apache.org/POM/4.0.0}"
        pom = ET.parse(next(path for path in files if path.suffix == ".pom")).getroot()
        ET.register_namespace("", ns[1:-1]); project = ET.Element(ns + "project")
        for tag, value in [("modelVersion","4.0.0"),("groupId","local.mimic"),("artifactId","packaged-consumer"),("version","1.0.0")]: ET.SubElement(project, ns + tag).text = value
        properties = ET.SubElement(project, ns + "properties")
        for name in ("maven.compiler.source", "maven.compiler.target"): ET.SubElement(properties, ns + name).text = "17"
        dependencies = ET.SubElement(project, ns + "dependencies")
        sdk = ET.SubElement(dependencies, ns + "dependency")
        jar = next(path for path in files if path.name == f"mimic-sdk-{package['version']}.jar")
        for tag, value in [("groupId","io.mimicbrowser"),("artifactId","mimic-sdk"),("version",package["version"]),("scope","system"),("systemPath",str(jar))]: ET.SubElement(sdk, ns + tag).text = value
        for dependency in pom.find(ns + "dependencies"):
            if dependency.findtext(ns + "optional") != "true" and dependency.findtext(ns + "scope") != "test": dependencies.append(dependency)
        ET.ElementTree(project).write(directory / "pom.xml", encoding="utf-8", xml_declaration=True)
        source = directory / "src/main/java/Consumer.java"; source.parent.mkdir(parents=True)
        source.write_text('import io.mimicbrowser.sdk.RuntimeManager; public class Consumer { public static void main(String[] args) { if (RuntimeManager.defaultLock().get("release") == null) throw new AssertionError(); if (Consumer.class.getResource("/io/mimicbrowser/sdk/playwright/PlaywrightSession.class") == null) throw new AssertionError("Missing optional adapter"); } }')
        run(["mvn", "-B", "-ntp", "compile", "dependency:build-classpath", "-Dmdep.outputFile=classpath.txt"], directory)
        run(["java", "-cp", str(directory / "target/classes") + os.pathsep + (directory / "classpath.txt").read_text().strip(), "Consumer"], directory)
    elif registry == "packagist":
        feed = directory / "feed"; feed.mkdir(); shutil.copyfile(files[0], feed / files[0].name)
        (directory / "composer.json").write_text(json.dumps({"name":"mimic-local/consumer", "repositories":[{"type":"artifact","url":str(feed)},{"packagist.org":False}],"require":{package["name"]:package["version"]},"config":{"allow-plugins":{}}}))
        run(["composer", "install", "--no-dev", "--no-interaction", "--no-progress"], directory)
        run(["php", "-r", "require 'vendor/autoload.php'; if (class_exists('HeadlessChromium\\Browser') || !\\Mimic\\Sdk\\RuntimeManager::defaultLock()->release) throw new Exception('Core package isolation');"], directory)
        run(["php", "vendor/bin/mimic-sdk", "list"], directory)
    elif registry == "rubygems":
        run(["gem", "unpack", files[0], "--target", directory], directory)
        library = directory / f"{package['name']}-{package['version']}" / "lib"
        run(["ruby", "-I" + str(library), "-e", "require 'mimic_sdk'; raise 'Missing core' unless defined?(MimicSDK::RuntimeManager); raise 'Imported unused Ferrum' if $LOADED_FEATURES.any? { |path| path.include?('/ferrum') }"], directory)
    elif registry in ("go", "crates"):
        unpack(files[0], directory)
        if registry == "go":
            source = next(directory.rglob("go.mod")).parent
            run(["go", "test", "-run", "^$", "./..."], source)
        else:
            source = next(directory.rglob("Cargo.toml")).parent
            run(["cargo", "check", "--locked", "--all-features"], source)


def smoke(build):
    result = {}
    for key, package in build["packages"].items():
        release.verify_local(package)
        with tempfile.TemporaryDirectory(prefix="mimic-package-" + key + "-") as temporary:
            check(key, package, build, Path(temporary))
        result[key] = {"status":"passed", "artifacts":[item["sha256"] for item in package["artifacts"]]}
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__); parser.add_argument("--build", type=Path, required=True); parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    release.write(args.output, smoke(release.read(args.build)))
