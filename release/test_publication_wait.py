import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

import publish
import publish_sources
import release


class Clock:
    def __init__(self):
        self.now = 0
        self.sleeps = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def absent(headers=None):
    return urllib.error.HTTPError("https://registry.npmjs.org/example/1.0.0", 404,
                                  "Not Found", headers or {}, io.BytesIO(b"not indexed"))


class PublicationVisibilityTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        artifact = self.root / "example.tgz"
        self.payload = b"immutable selected package"
        artifact.write_bytes(self.payload)
        self.package = {"name": "example", "version": "1.0.0", "registry": "npm",
                        "artifacts": [{"file": str(artifact), "filename": artifact.name,
                                       "size": len(self.payload), "sha256": release.sha(self.payload)}]}
        self.build = {"packages": {"node": self.package}}
        self.registry_state = self.root / "registry.json"
        self.metadata = {"dist": {"tarball": "https://registry.npmjs.org/example/-/example.tgz"}}
        self.clock = Clock()
        for name, replacement in (("monotonic", self.clock.monotonic), ("sleep", self.clock.sleep)):
            mocked = patch.object(publish.time, name, replacement)
            mocked.start()
            self.addCleanup(mocked.stop)

    def test_confirmed_upload_retries_absence_and_verifies_exact_bytes(self):
        progress = {"state": "uploaded"}
        first, second = absent(), absent()
        with patch.object(release, "fetch_json", side_effect=[first, second, self.metadata]) as metadata, \
             patch.object(release, "fetch", return_value=self.payload) as download:
            result = publish.verify_uploaded_registry(self.build, self.registry_state, progress)
        self.assertEqual(metadata.call_count, 3)
        download.assert_called_once()
        self.assertTrue(first.fp.closed)
        self.assertTrue(second.fp.closed)
        self.assertEqual(self.clock.sleeps, [2, 4])
        self.assertEqual(result["packages"]["node"]["status"], "verified")
        self.assertEqual(progress, {"state": "uploaded"})

    def test_pending_or_unknown_upload_never_enters_visibility_wait(self):
        for state in ("pending", "uploading", "pushing", "verified"):
            with self.subTest(state=state), patch.object(release, "verify_registry") as verify:
                with self.assertRaisesRegex(release.ReleaseError, "confirmed upload"):
                    publish.verify_uploaded_registry(self.build, self.registry_state, {"state": state})
                verify.assert_not_called()
        self.assertEqual(self.clock.sleeps, [])

    def test_authorization_and_content_mismatch_fail_immediately(self):
        forbidden = urllib.error.HTTPError("https://registry.npmjs.org/example", 403, "Forbidden",
                                           {"Retry-After": "60"}, io.BytesIO())
        with patch.object(release, "fetch_json", side_effect=forbidden) as metadata:
            with self.assertRaises(urllib.error.HTTPError) as captured:
                publish.verify_uploaded_registry(self.build, self.registry_state, {"state": "uploaded"})
            self.assertEqual(captured.exception.code, 403)
            metadata.assert_called_once()
        forbidden.close()
        with patch.object(release, "fetch_json", return_value=self.metadata), \
             patch.object(release, "fetch", return_value=b"different published bytes") as download:
            with self.assertRaisesRegex(release.ReleaseError, "Registry bytes differ"):
                publish.verify_uploaded_registry(self.build, self.registry_state, {"state": "uploaded"})
            download.assert_called_once()
        self.assertEqual(self.clock.sleeps, [])
        self.assertFalse(self.registry_state.exists())

    def test_partial_file_index_waits_without_ignoring_mismatched_bytes(self):
        self.package["registry"] = "pypi"
        urls = {"urls": [{"filename": "example.tgz", "url": "https://files.pythonhosted.org/example.tgz"}]}
        with patch.object(release, "fetch_json", side_effect=[{"urls": []}, urls]), \
             patch.object(release, "fetch", return_value=self.payload):
            publish.verify_uploaded_registry(self.build, self.registry_state, {"state": "verifying"})
        self.assertEqual(self.clock.sleeps, [2])
        self.assertEqual(release.read(self.registry_state)["packages"]["node"]["status"], "verified")

    def test_retry_headers_respect_remaining_negative_cache_and_server_dates(self):
        # 2026-01-01 00:00:30 UTC; each expected delay is relative to this clock.
        now = 1767225630
        cases = [
            ({"Retry-After": "12"}, 12),
            ({"Retry-After": "Thu, 01 Jan 2026 00:01:00 GMT"}, 30),
            ({"Cache-Control": "max-age=90", "Age": "40"}, 50),
            ({"Cache-Control": 's-maxage="100", max-age=1', "Age": "10"}, 90),
            ({"Cache-Control": "max-age=90", "Date": "Thu, 01 Jan 2026 00:00:00 GMT"}, 60),
            ({"Expires": "Thu, 01 Jan 2026 00:01:00 GMT"}, 30),
            ({"Expires": "Thu, 01 Jan 2026 00:01:00 GMT", "Age": "40"}, 30),
            ({"Cache-Control": "no-cache, max-age=300"}, 2),
            ({"Retry-After": "invalid", "Cache-Control": "max-age=invalid"}, 2),
            ({"Retry-After": "Thu, 01 Jan 2026 00:00:00 GMT"}, 2),
        ]
        with patch.object(publish.time, "time", return_value=now):
            for headers, expected in cases:
                with self.subTest(headers=headers):
                    self.assertEqual(publish.index_retry_delay(headers, 2), expected)

    def test_server_delay_exceeding_deadline_does_not_poll_early(self):
        def unavailable(_):
            raise absent({"Retry-After": "1000"})
        with patch.object(release, "fetch_json", side_effect=unavailable) as metadata:
            with self.assertRaisesRegex(release.ReleaseError, "retain the receipt"):
                publish.verify_uploaded_registry(self.build, self.registry_state, {"state": "uploaded"}, timeout=10)
        self.assertEqual(metadata.call_count, 1)
        self.assertEqual(self.clock.sleeps, [10])
        self.assertFalse(self.registry_state.exists())

    def test_go_proxy_negative_cache_fits_its_longer_default_bound(self):
        self.package["registry"] = "go"
        with patch.object(release, "verify_registry", side_effect=[absent({"Retry-After": "1800"}), {"verified": True}]) as verify:
            result = publish.verify_uploaded_registry(self.build, self.registry_state, {"state": "uploaded"})
        self.assertEqual(result, {"verified": True})
        self.assertEqual(self.clock.sleeps, [1800])
        self.assertEqual(verify.call_count, 2)

    def test_timeout_preserves_uploaded_receipt_and_resume_never_reuploads(self):
        plan_path, build_path = self.root / "plan.json", self.root / "build.json"
        qualification, state_path = self.root / "qualification.json", self.root / "publication.json"
        release.write(plan_path, {"sdkRevision": "frozen-source"})
        release.write(build_path, self.build)
        release.write(qualification, {})
        handoff = {"buildSha256": "exact-build", "packages": {"node": {"command": ["npm", "publish", "example.tgz"]}}}
        arguments = ["publish.py", "--plan", str(plan_path), "--build", str(build_path),
                     "--qualification", str(qualification), "--state", str(state_path), "--execute"]
        def unavailable(_):
            raise absent()
        with patch.dict("os.environ", {"CI": "true", "MIMIC_SDK_PUBLICATION_ENABLED": "true"}, clear=True), \
             patch("sys.argv", arguments), \
             patch.object(release, "publication_handoff", return_value=handoff), \
             patch.object(release, "git_revision", return_value="frozen-source"), \
             patch.object(release, "execute", return_value=""), \
             patch.object(publish, "check_credentials"), \
             patch.object(release, "fetch_json", side_effect=unavailable), \
             patch.object(publish.subprocess, "run") as upload, \
             patch("builtins.print"):
            upload.return_value.returncode = 0
            with self.assertRaisesRegex(release.ReleaseError, "without uploading again"):
                publish.main()
            saved = state_path.read_bytes()
            self.assertEqual(json.loads(saved)["packages"]["node"]["state"], "uploaded")
            self.assertEqual(self.clock.now, 600)
            upload.assert_called_once()
            with self.assertRaisesRegex(release.ReleaseError, "without uploading again"):
                publish.main()
            self.assertEqual(state_path.read_bytes(), saved)
            self.assertEqual(self.clock.now, 1200)
            upload.assert_called_once()

    def test_maven_polls_retained_deployment_without_reupload(self):
        progress = {"state": "uploaded", "deploymentId": "same-deployment"}
        snapshots = []
        states = iter(("PENDING", "VALIDATING", "VALIDATED", "PUBLISHING", "PUBLISHED"))
        def response(request, **kwargs):
            self.assertEqual(request.full_url, "https://central.sonatype.com/api/v1/publisher/status?id=same-deployment")
            self.assertEqual(request.data, b"")
            return io.BytesIO(json.dumps({"deploymentState": next(states)}).encode())
        with patch.object(publish_sources, "maven_token", return_value="private-test-token"), \
             patch.object(publish_sources.urllib.request, "urlopen", side_effect=response) as status, \
             patch.object(publish_sources.subprocess, "run") as native:
            publish_sources.maven({}, progress, lambda: snapshots.append(dict(progress)))
        native.assert_not_called()
        self.assertEqual(status.call_count, 5)
        self.assertEqual(self.clock.sleeps, [5, 10, 20, 30])
        self.assertEqual([item["centralState"] for item in snapshots], ["PENDING", "VALIDATING", "VALIDATED", "PUBLISHING", "PUBLISHED"])
        self.assertEqual(progress["deploymentId"], "same-deployment")

    def test_maven_validation_auth_and_unknown_state_fail_without_retry(self):
        for state in ("FAILED", "UNRECOGNIZED"):
            progress = {"state": "uploaded", "deploymentId": "same-deployment"}
            with self.subTest(state=state), \
                 patch.object(publish_sources, "maven_token", return_value="private-test-token"), \
                 patch.object(publish_sources.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps({"deploymentState": state, "errors": ["validation error"]}).encode())) as status:
                with self.assertRaises(release.ReleaseError):
                    publish_sources.maven({}, progress, lambda: None)
                status.assert_called_once()
            self.assertEqual(progress["centralState"], state)
        forbidden = urllib.error.HTTPError("https://central.sonatype.com/api/v1/publisher/status", 403, "Forbidden", {}, io.BytesIO())
        with patch.object(publish_sources, "maven_token", return_value="private-test-token"), \
             patch.object(publish_sources.urllib.request, "urlopen", side_effect=forbidden) as status:
            with self.assertRaises(urllib.error.HTTPError):
                publish_sources.maven({}, {"state": "uploaded", "deploymentId": "same-deployment"}, lambda: None)
            status.assert_called_once()
        forbidden.close()
        self.assertEqual(self.clock.sleeps, [])

    def test_maven_timeout_retains_deployment_for_status_only_resume(self):
        progress = {"state": "uploaded", "deploymentId": "same-deployment"}
        def processing(*args, **kwargs):
            return io.BytesIO(b'{"deploymentState":"PUBLISHING"}')
        with patch.object(publish_sources, "maven_token", return_value="private-test-token"), \
             patch.object(publish_sources.urllib.request, "urlopen", side_effect=processing), \
             patch.object(publish_sources.subprocess, "run") as native, \
             patch("builtins.print"):
            with self.assertRaisesRegex(release.ReleaseError, "retained deployment receipt"):
                publish_sources.maven({}, progress, lambda: None)
        native.assert_not_called()
        self.assertEqual(self.clock.now, 1800)
        self.assertEqual(progress, {"state": "uploaded", "deploymentId": "same-deployment", "centralState": "PUBLISHING"})


if __name__ == "__main__":
    unittest.main()
