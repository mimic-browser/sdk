import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import release
import resume


class ResumeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source-plan.json"
        self.output = self.root / "release/plan.json"
        self.plan = release.make_plan(["php", "java"])
        release.write(self.source, self.plan)
        self.revision = patch.object(release, "git_revision", return_value="exact-source")
        self.revision.start()
        self.addCleanup(self.revision.stop)

    def freeze(self):
        resume.prepare_plan(self.source, self.output)
        self.plan = release.read(self.output)

    def build(self, selected=("php", "java")):
        packages = {}
        for key in selected:
            path = self.output.parent / key / "artifact.bin"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(("payload-" + key).encode())
            packages[key] = {**self.plan["packages"][key], "artifacts": [{
                "file": str(path), "filename": path.name, "size": path.stat().st_size,
                "sha256": release.sha(path.read_bytes()),
            }]}
        build = {"kind": "sdk-build-receipt", "planSha256": resume.digest(self.plan), "packages": packages}
        release.write(self.output.parent / "build.json", build)
        return build

    def test_prior_run_requires_repository_workflow_commit_and_completion(self):
        run = {"repository": {"full_name": "mimic-browser/sdk"}, "head_sha": "exact-source",
               "path": ".github/workflows/sdk-release.yml", "event": "workflow_dispatch", "status": "completed"}
        resume.validate_run(run, "mimic-browser/sdk", "exact-source")
        for key, value in [("head_sha", "another-source"), ("path", ".github/workflows/other.yml"),
                           ("status", "in_progress"), ("event", "pull_request"),
                           ("repository", {"full_name": "other/sdk"})]:
            with self.subTest(field=key), self.assertRaises(release.ReleaseError):
                resume.validate_run({**run, key: value}, "mimic-browser/sdk", "exact-source")

    def test_invalid_run_id_never_makes_authenticated_request(self):
        with patch.object(resume.urllib.request, "urlopen") as request:
            for run_id in ("", "0", "-1", "12/path", "$(command)"):
                with self.assertRaises(release.ReleaseError):
                    resume.check_source_run(run_id, {"GH_TOKEN": "never-log-this"})
            request.assert_not_called()

    def test_partial_build_can_resume_without_changing_frozen_receipts(self):
        self.freeze()
        self.build(("php",))
        before = {path: path.read_bytes() for path in (self.output, self.output.parent / "build.json")}
        resume.prepare_plan(self.source, self.output, True)
        self.assertEqual(before, {path: path.read_bytes() for path in before})

    def test_fresh_run_cannot_overwrite_retained_state(self):
        self.freeze()
        with self.assertRaisesRegex(release.ReleaseError, "Fresh release output"):
            resume.prepare_plan(self.source, self.output)

    def test_source_plan_or_revision_changes_reject_resume(self):
        self.freeze()
        with patch.object(release, "input_digest", return_value="changed"):
            with self.assertRaisesRegex(release.ReleaseError, "inputs changed"):
                resume.prepare_plan(self.source, self.output, True)
        with patch.object(release, "git_revision", return_value="other-source"):
            with self.assertRaisesRegex(release.ReleaseError, "frozen plan differs"):
                resume.prepare_plan(self.source, self.output, True)
        changed = release.read(self.source)
        changed["packages"].pop("java")
        release.write(self.source, changed)
        with self.assertRaisesRegex(release.ReleaseError, "frozen plan differs"):
            resume.prepare_plan(self.source, self.output, True)

    def test_changed_artifact_or_package_metadata_rejects_resume(self):
        self.freeze()
        build = self.build()
        artifact = Path(build["packages"]["php"]["artifacts"][0]["file"])
        artifact.write_bytes(b"tampered")
        with self.assertRaisesRegex(release.ReleaseError, "artifact changed"):
            resume.prepare_plan(self.source, self.output, True)
        build = self.build()
        build["packages"]["php"]["version"] = "999.0.0"
        release.write(self.output.parent / "build.json", build)
        with self.assertRaisesRegex(release.ReleaseError, "package differs"):
            resume.prepare_plan(self.source, self.output, True)

    def test_artifact_path_cannot_escape_original_package_directory(self):
        self.freeze()
        build = self.build()
        moved = self.root / "outside.bin"
        moved.write_bytes(b"payload-php")
        build["packages"]["php"]["artifacts"][0].update(file=str(moved), filename=moved.name)
        release.write(self.output.parent / "build.json", build)
        with self.assertRaisesRegex(release.ReleaseError, "original package path"):
            resume.prepare_plan(self.source, self.output, True)

    def test_publication_qualification_and_registry_state_bind_exact_build(self):
        self.freeze()
        build = self.build()
        state = {"kind": "sdk-publication-progress", "sdkRevision": "exact-source",
                 "buildSha256": resume.digest(build), "packages": {"php": {"state": "uploaded"}}}
        qualification = {"kind": "sdk-qualification-receipt", "sdkRevision": "exact-source",
                         "buildSha256": resume.digest(build), "status": "passed"}
        registry = {"kind": "sdk-registry-receipt",
                    "buildSha256": resume.digest({**build, "packages": {"php": build["packages"]["php"]}})}
        documents = {"publication.json": state, "qualification.json": qualification,
                     "publication-php-registry.json": registry}
        for name, record in documents.items(): release.write(self.output.parent / name, record)
        resume.prepare_plan(self.source, self.output, True)
        for name, record in documents.items():
            with self.subTest(file=name):
                altered = copy.deepcopy(record)
                altered["buildSha256"] = "different-build"
                release.write(self.output.parent / name, altered)
                with self.assertRaises(release.ReleaseError):
                    resume.prepare_plan(self.source, self.output, True)
                release.write(self.output.parent / name, record)

    def test_publication_state_requires_complete_build(self):
        self.freeze()
        build = self.build(("php",))
        release.write(self.output.parent / "publication.json", {
            "kind": "sdk-publication-progress", "sdkRevision": "exact-source",
            "buildSha256": resume.digest(build), "packages": {},
        })
        with self.assertRaisesRegex(release.ReleaseError, "complete exact build"):
            resume.prepare_plan(self.source, self.output, True)

    def test_mirror_push_response_loss_preserves_recoverable_provenance(self):
        self.freeze()
        build = self.build()
        progress = {"state": "pushing", "mirrorCommit": "a" * 40, "sourceRevision": "exact-source",
                    "archiveSha256": build["packages"]["php"]["artifacts"][0]["sha256"]}
        state = {"kind": "sdk-publication-progress", "sdkRevision": "exact-source",
                 "buildSha256": resume.digest(build), "packages": {"php": progress}}
        path = self.output.parent / "publication.json"
        release.write(path, state)
        before = path.read_bytes()
        resume.prepare_plan(self.source, self.output, True)
        self.assertEqual(path.read_bytes(), before)
        progress["archiveSha256"] = "different-artifact"
        release.write(path, state)
        with self.assertRaisesRegex(release.ReleaseError, "mirror push"):
            resume.prepare_plan(self.source, self.output, True)


if __name__ == "__main__":
    unittest.main()
