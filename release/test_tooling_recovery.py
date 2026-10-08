import copy
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import zipfile

import release
import resume


class ToolingRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source_revision, self.tooling_revision = "a" * 40, "b" * 40
        self.environment = {"GITHUB_REPOSITORY": "mimic-browser/sdk", "GITHUB_SHA": self.tooling_revision,
                            "GITHUB_RUN_ID": "200", "GITHUB_RUN_ATTEMPT": "1", "GH_TOKEN": "not-printed",
                            "RESUME_RUN_ID": "100", "RESUME_SOURCE_REVISION": self.source_revision}
        self.source = self.root / "committed-plan.json"
        self.directory = self.root / "release"
        self.output = self.directory / "plan.json"
        self.proof_path = self.root / "authenticated-source.json"
        self.attestation_path = self.directory / "recovery.json"
        self.plan = release.make_plan(["dotnet-core"])
        self.plan["sdkRevision"] = self.source_revision
        release.write(self.source, self.plan)
        release.write(self.output, self.plan)
        artifact = self.directory / "dotnet-core" / "mimic-browser.0.1.0.nupkg"
        artifact.parent.mkdir(parents=True)
        with zipfile.ZipFile(artifact, "w") as archive:
            archive.writestr("LICENSE", "license")
        self.build = {"kind": "sdk-build-receipt", "planSha256": resume.digest(self.plan),
                      "packages": {"dotnet-core": {**self.plan["packages"]["dotnet-core"],
                                   "artifacts": [release.artifact(artifact, "nuget")]}}}
        release.write(self.directory / "build.json", self.build)
        runtime = release.read(release.ROOT / "release/runtime-lock.json")
        self.qualification = {"kind": "sdk-qualification-receipt", "sdkRevision": self.source_revision,
                              "status": "passed", "buildSha256": resume.digest(self.build),
                              "runtime": {"binarySha256": runtime["manifest"]["artifacts"][0]["binarySha256"],
                                          "identity": {"version": runtime["release"]}},
                              "packages": {"dotnet-core": {"status": "passed"}}}
        release.write(self.directory / "qualification.json", self.qualification)
        self.proof = {"kind": "sdk-resume-source-proof", "repository": "mimic-browser/sdk",
                      "sourceRunId": "100", "sourceRunHeadSha": self.source_revision,
                      "sourceRunAttempt": 1, "sourceArtifactId": 500, "sourceArtifactDigest": "sha256:" + "c" * 64,
                      "sdkRevision": self.source_revision, "toolingRevision": self.tooling_revision,
                      "currentRunId": "200", "currentRunAttempt": "1"}
        release.write(self.proof_path, self.proof)
        self.git = patch.object(release, "git_revision", return_value=self.tooling_revision)
        self.git.start()
        self.addCleanup(self.git.stop)

    def prepare(self):
        return resume.prepare_recovery(self.source, self.output, self.source_revision,
                                       self.environment["RESUME_RUN_ID"], self.proof_path, self.environment)

    def authorize(self):
        return resume.authorize_recovery(self.output, self.directory / "build.json",
                                         [self.directory / "qualification.json"], self.attestation_path,
                                         self.proof_path, self.environment)

    def test_preserves_original_plan_build_qualification_and_payload(self):
        before = {path: path.read_bytes() for path in self.directory.rglob("*") if path.is_file()}
        result = self.prepare()
        with patch.object(resume, "check_source_run", return_value=self.proof) as authenticate:
            self.assertEqual(self.authorize(), result)
            authenticate.assert_called_once_with("100", self.environment, self.source_revision)
        self.assertEqual(before, {path: path.read_bytes() for path in before})
        self.assertEqual(result["sdkRevision"], self.source_revision)
        self.assertEqual(result["toolingRevision"], self.tooling_revision)

    def test_missing_forged_or_inside_artifact_proof_is_rejected(self):
        self.prepare()
        for field, value in (("toolingRevision", "d" * 40), ("currentRunId", "999"),
                             ("sourceArtifactId", 777), ("sourceRunId", "900")):
            release.write(self.proof_path, {**self.proof, field: value})
            with self.subTest(field=field), patch.object(resume, "check_source_run", return_value=self.proof):
                with self.assertRaises(release.ReleaseError):
                    self.authorize()
        release.write(self.proof_path, self.proof)
        inside = self.directory / "source-proof.json"
        release.write(inside, self.proof)
        with self.assertRaisesRegex(release.ReleaseError, "outside restored"):
            resume.recovery_proof(inside, self.directory, self.source_revision, "100", self.environment)
        self.proof_path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.prepare()

    def test_attestation_cannot_rebind_original_source_build_or_tooling(self):
        attestation = self.prepare()
        for field in ("sdkRevision", "toolingRevision", "buildSha256", "planSha256", "qualificationSha256", "sourceProofSha256"):
            release.write(self.attestation_path, {**attestation, field: "forged"})
            with self.subTest(field=field), patch.object(resume, "check_source_run", return_value=self.proof):
                with self.assertRaisesRegex(release.ReleaseError, "attestation"):
                    self.authorize()

    def test_current_input_schema_and_runtime_changes_reject_recovery(self):
        with patch.object(release, "input_digest", return_value="changed"):
            with self.assertRaisesRegex(release.ReleaseError, "inputs changed"):
                self.prepare()
        for field in ("schemaSha256", "runtimeLockSha256"):
            changed = {**self.plan, field: "changed"}
            release.write(self.source, changed)
            with self.subTest(field=field), self.assertRaisesRegex(release.ReleaseError, "Schema or immutable"):
                self.prepare()
        release.write(self.source, {**self.plan, "createdAt": "changed"})
        with self.assertRaisesRegex(release.ReleaseError, "exact original"):
            self.prepare()

    def test_partial_build_missing_or_failed_qualification_and_corruption_fail(self):
        altered = {**self.build, "packages": {}}
        release.write(self.directory / "build.json", altered)
        with self.assertRaises(release.ReleaseError):
            self.prepare()
        release.write(self.directory / "build.json", self.build)
        release.write(self.directory / "qualification.json", {**self.qualification, "status": "failed"})
        with self.assertRaises(release.ReleaseError):
            self.prepare()
        (self.directory / "qualification.json").unlink()
        with self.assertRaises(FileNotFoundError):
            self.prepare()
        release.write(self.directory / "qualification.json", self.qualification)
        Path(self.build["packages"]["dotnet-core"]["artifacts"][0]["file"]).write_bytes(b"tampered")
        with self.assertRaisesRegex(release.ReleaseError, "artifact changed"):
            self.prepare()

    def test_subsequent_recovery_requires_retained_original_lineage(self):
        prior = self.prepare()
        self.environment["RESUME_RUN_ID"] = "200"
        self.environment["GITHUB_RUN_ID"] = "300"
        self.proof.update(sourceRunId="200", sourceRunHeadSha=self.tooling_revision, currentRunId="300")
        release.write(self.proof_path, self.proof)
        result = self.prepare()
        self.assertEqual(result["sdkRevision"], self.source_revision)
        for field in ("sdkRevision", "toolingRevision", "currentRunId", "buildSha256"):
            release.write(self.attestation_path, {**prior, field: "forged"})
            with self.subTest(field=field), self.assertRaisesRegex(release.ReleaseError, "lineage"):
                self.prepare()
        self.attestation_path.unlink()
        with self.assertRaises(FileNotFoundError):
            self.prepare()

    def test_authenticated_run_and_exact_artifact_are_bound_to_proof(self):
        run = {"id": 100, "run_attempt": 1, "repository": {"full_name": "mimic-browser/sdk"},
               "head_sha": self.source_revision, "path": ".github/workflows/sdk-release.yml",
               "event": "workflow_dispatch", "status": "completed"}
        artifact = {"id": 500, "name": "selected-sdk-release-receipts", "expired": False,
                    "digest": "sha256:" + "c" * 64, "workflow_run": {"id": 100, "head_sha": self.source_revision}}
        def response(document):
            return io.BytesIO(json.dumps(document).encode())
        with patch.object(resume.urllib.request, "urlopen", side_effect=[response(run), response({"artifacts": [artifact]})]):
            self.assertEqual(resume.check_source_run("100", self.environment, self.source_revision), self.proof)
        for change in ({"expired": True}, {"digest": "bad"}, {"workflow_run": {"id": 100, "head_sha": "wrong"}}):
            with self.subTest(change=change), patch.object(resume.urllib.request, "urlopen", side_effect=[
                    response(run), response({"artifacts": [{**artifact, **change}]})]):
                with self.assertRaises(release.ReleaseError):
                    resume.check_source_run("100", self.environment, self.source_revision)


if __name__ == "__main__":
    unittest.main()
