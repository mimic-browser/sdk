import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import release


class ReleaseTests(unittest.TestCase):
    def test_recursive_catalog_inputs_bind_nested_production_sources(self):
        packages = release.catalog()
        for key, expected in [("go", "go/runtime.go"), ("node", "node/src/runtime.ts"), ("node", "node/scripts/prepare-git.mjs"), ("node", "package.json"), ("java", "java/src/main/java/io/mimicbrowser/sdk/RuntimeManager.java"), ("ruby", "ruby/lib/mimic_sdk/runtime.rb")]:
            paths = {path.relative_to(release.ROOT).as_posix() for path in release.source_files(packages[key])}
            self.assertIn(expected, paths)
    def test_explicit_selection_and_native_versions(self):
        plan = release.make_plan(["dotnet-core", "php"])
        self.assertEqual(list(plan["packages"]), ["dotnet-core", "php"])
        release.validate_plan(plan)
        with self.assertRaises(release.ReleaseError): release.make_plan(["unknown"])
        with self.assertRaises(release.ReleaseError): release.make_plan([])

    def test_catalog_names_must_match_every_native_distribution(self):
        packages = release.catalog()
        release.check_versions(packages)
        for key in ("node", "python", "dotnet-core", "dotnet-playwright", "dotnet-puppeteer", "java", "rust", "ruby", "php"):
            with self.subTest(package=key):
                changed = {**packages[key], "name": "different-package"}
                with self.assertRaisesRegex(release.ReleaseError, "package name"):
                    release.check_versions({key: changed})

    def test_packagist_uses_tag_version_and_rejects_explicit_mismatch(self):
        package = release.catalog()["php"]
        metadata = release.read(release.ROOT / "php/composer.json")
        self.assertNotIn("version", metadata)
        release.check_versions({"php": package})
        with patch.object(release, "read", return_value={**metadata, "version": "99.0.0"}):
            with self.assertRaises(release.ReleaseError):
                release.check_versions({"php": package})

    def test_empty_bump_and_changed_unversioned_package_are_rejected(self):
        plan = release.make_plan(["php"])
        with self.assertRaisesRegex(release.ReleaseError, "unchanged"):
            release.make_plan(["php"], plan)
        with patch.object(release, "input_digest", return_value="changed"):
            with self.assertRaisesRegex(release.ReleaseError, "version update"):
                release.make_plan(["php"], plan)

    def test_composer_archive_projects_version_only_into_staging(self):
        package = release.catalog()["php"]
        original = (release.ROOT / "php/composer.json").read_bytes()
        def composer(command, directory):
            if command[1] != "archive": return
            self.assertNotEqual(directory, release.ROOT / "php")
            metadata = release.read(directory / "composer.json")
            self.assertEqual(metadata["version"], package["version"])
            output = Path(command[command.index("--dir") + 1]) / "mimic-browser-sdk-0.1.0.zip"
            with zipfile.ZipFile(output, "w") as archive:
                archive.writestr("composer.json", (directory / "composer.json").read_bytes())
        with tempfile.TemporaryDirectory() as directory, patch.object(release, "execute", side_effect=composer):
            release.pack_package("php", package, Path(directory))
        self.assertEqual((release.ROOT / "php/composer.json").read_bytes(), original)
        self.assertNotIn("version", json.loads(original))

    def test_composer_registry_verification_allows_only_selected_version_projection(self):
        with tempfile.TemporaryDirectory() as directory:
            local, remote = Path(directory) / "local.zip", Path(directory) / "remote.zip"
            def archive(path, prefix, metadata, source=b"same"):
                with zipfile.ZipFile(path, "w") as output:
                    output.writestr(prefix + "composer.json", json.dumps(metadata))
                    output.writestr(prefix + "src/Client.php", source)
            metadata = {"name":"mimic-browser/sdk", "require":{"php":"^8.2"}}
            archive(local, "", {**metadata, "version":"0.1.0"})
            archive(remote, "mirror-tag/", metadata)
            expected = release.normalized_zip(local, False, "0.1.0")
            self.assertEqual(expected, release.normalized_zip(remote, True, "0.1.0"))
            archive(remote, "mirror-tag/", {**metadata, "version":"0.2.0"})
            with self.assertRaisesRegex(release.ReleaseError, "version differs"):
                release.normalized_zip(remote, True, "0.1.0")
            archive(remote, "mirror-tag/", metadata, b"changed")
            self.assertNotEqual(expected, release.normalized_zip(remote, True, "0.1.0"))

    def test_changed_inputs_or_lock_invalidate_plan(self):
        plan = release.make_plan(["php"])
        plan["packages"]["php"]["inputDigest"] = "changed"
        with self.assertRaisesRegex(release.ReleaseError, "inputs changed"): release.validate_plan(plan)
        plan = release.make_plan(["php"])
        plan["runtimeLockSha256"] = "changed"
        with self.assertRaisesRegex(release.ReleaseError, "runtime lock changed"): release.validate_plan(plan)

    def test_unsafe_archive_and_timestamp_independent_content(self):
        with tempfile.TemporaryDirectory() as directory:
            one, two = Path(directory) / "one.zip", Path(directory) / "two.zip"
            for path, year in [(one, 2000), (two, 2020)]:
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(zipfile.ZipInfo("LICENSE", (year, 1, 1, 0, 0, 0)), b"same bytes")
            self.assertEqual(release.inspect_archive(one, "nuget"), release.inspect_archive(two, "nuget"))
            with zipfile.ZipFile(two, "w") as archive: archive.writestr("../escape", b"bad")
            with self.assertRaises(release.ReleaseError): release.inspect_archive(two, "nuget")

    def test_private_package_entries_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "package.zip"
            for name in ("package/.build/internal-notes/proof.json", "package/.npmrc", "package/.tools/token", "package/credentials.toml"):
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr(name, "private")
                with self.assertRaisesRegex(release.ReleaseError, "Private file"):
                    release.inspect_archive(path, "npm")

    def test_embedded_checkout_paths_are_rejected_without_stripping_symbols(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "package.nupkg"
            for encoding in ("utf-8", "utf-16-le"):
                with zipfile.ZipFile(path, "w") as archive:
                    archive.writestr("lib/net8.0/Example.dll", b"RSDS" + (str(release.ROOT) + "/obj/Example.pdb").encode(encoding))
                with self.assertRaisesRegex(release.ReleaseError, "Local checkout path"):
                    release.inspect_archive(path, "nuget")
            with zipfile.ZipFile(path, "w") as archive:
                archive.writestr("lib/net8.0/Example.dll", b"RSDS/_/dotnet/Example/obj/Example.pdb")
            release.inspect_archive(path, "nuget")
        packages = release.catalog()
        for key in ("dotnet-core", "dotnet-playwright", "dotnet-puppeteer"):
            self.assertIn(release.ROOT / "dotnet/Directory.Build.props", release.source_files(packages[key]))

    def test_publication_preflights_whole_selected_set_without_secret_values(self):
        import publish
        packages = {"node": {"registry": "npm"}, "python": {"registry": "pypi"}, "core": {"registry": "nuget"}}
        environment = {"NODE_AUTH_TOKEN": "never-include-token-in-errors", "TWINE_USERNAME": "__token__"}
        with self.assertRaises(release.ReleaseError) as captured:
            publish.check_credentials(packages, environment)
        self.assertIn("ACTIONS_ID_TOKEN_REQUEST_TOKEN", str(captured.exception))
        self.assertIn("NUGET_API_KEY", str(captured.exception))
        self.assertNotIn(environment["NODE_AUTH_TOKEN"], str(captured.exception))
        environment.update(TWINE_PASSWORD="secret", NUGET_API_KEY="secret")
        publish.check_credentials(packages, environment)
        publish.check_credentials({"node": packages["node"]}, {"ACTIONS_ID_TOKEN_REQUEST_URL": "url", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "secret"})

    def test_pyppeteer_has_separate_native_qualification(self):
        import qualify
        from ci import pyppeteer_python
        _, commands = qualify.commands("python", "runtime")
        native = [command for command in commands if command[0] == str(pyppeteer_python())]
        self.assertEqual([command[1:] for command in native], [["tests/pyppeteer_integration.py"], ["tests/bridge.py", "--pyppeteer"]])

    def test_registry_resume_retains_verified_receipt_and_rejects_changed_local_bytes(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "package.zip"
            with zipfile.ZipFile(path, "w") as archive: archive.writestr("LICENSE", b"license")
            data = path.read_bytes()
            package = {"name":"Example", "version":"0.1.0", "registry":"nuget", "artifacts":[release.artifact(path, "nuget")]}
            build = {"packages":{"example":package}}
            state = Path(directory) / "state.json"
            with patch.object(release, "fetch", return_value=data) as download, patch(
                    "nuget_verify.verify", return_value={"payloadSha256": package["artifacts"][0]["contentSha256"]}) as signature:
                release.verify_registry(build, state)
                release.verify_registry(build, state)
                self.assertEqual(download.call_count, 1)
                signature.assert_called_once()
                self.assertIn("repositorySignature", release.read(state)["packages"]["example"]["artifacts"][0])
            path.write_bytes(b"changed")
            with self.assertRaises(release.ReleaseError): release.verify_registry(build, state)

    def test_candidate_never_qualifies_default_pin_publication(self):
        plan = release.make_plan(["php"])
        build = {"planSha256":release.sha(json.dumps(plan, sort_keys=True).encode()), "packages":{"php":{**plan["packages"]["php"], "artifacts":[]}}}
        qualification = {"kind":"sdk-qualification-receipt", "status":"passed", "buildSha256":release.sha(json.dumps(build, sort_keys=True).encode()), "runtime":{"binarySha256":"local-candidate", "identity":{"version":"dev"}}, "packages":{"php":{"status":"passed"}}}
        with self.assertRaisesRegex(release.ReleaseError, "officially released default pin"):
            release.publication_handoff(plan, build, [qualification])

    def test_publication_disabled_without_ci_and_explicit_execute(self):
        import publish
        with patch("sys.argv", ["publish.py", "--plan", "unused", "--build", "unused", "--qualification", "unused", "--state", "unused"]):
            with self.assertRaisesRegex(release.ReleaseError, "disabled"): publish.main()

    def test_partial_build_cannot_publish_complete_plan(self):
        plan = release.make_plan(["php", "java"])
        build = {"planSha256":release.sha(json.dumps(plan, sort_keys=True).encode()), "packages":{"php":{}}}
        with self.assertRaisesRegex(release.ReleaseError, "complete frozen release plan"):
            release.publication_handoff(plan, build, [])

    def test_mirror_orphan_removes_obsolete_files(self):
        import publish_sources
        with tempfile.TemporaryDirectory() as temporary:
            workspace = Path(temporary); root = workspace / "mirror"; root.mkdir()
            release.execute(["git", "init", "--quiet", root])
            (root / "obsolete.php").write_text("old release")
            (root / "old-directory").mkdir(); (root / "old-directory/old.txt").write_text("obsolete")
            release.execute(["git", "add", "--all"], root)
            publish_sources.empty_mirror_tree(root, workspace)
            self.assertEqual([path.name for path in root.iterdir()], [".git"])
            self.assertEqual(release.execute(["git", "ls-files"], root, capture=True), "")

    def test_media_qualification_requires_exact_fixture_hash(self):
        import qualify
        with tempfile.TemporaryDirectory() as temporary:
            fixture = Path(temporary) / "synthetic"; fixture.write_bytes(b"synthetic-only provider")
            with self.assertRaisesRegex(release.ReleaseError, "both"):
                qualify.verify_media_fixture(fixture, None)
            with self.assertRaisesRegex(release.ReleaseError, "hash mismatch"):
                qualify.verify_media_fixture(fixture, "wrong")
            qualify.verify_media_fixture(fixture, release.sha(fixture.read_bytes()))
        for group in ("node", "python", "dotnet", "java", "php", "ruby"):
            _, ordinary = qualify.commands(group, "runtime")
            _, synthetic = qualify.commands(group, "runtime", "fixture")
            self.assertGreater(len(synthetic), len(ordinary), group)


if __name__ == "__main__": unittest.main()
