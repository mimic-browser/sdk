import hashlib
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch
import warnings
import zipfile

import nuget_verify
import release


class NuGetSignatureTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.original, self.signed = self.root / "unsigned.nupkg", self.root / "signed.nupkg"
        self.write(self.original, {"LICENSE": b"license", "lib/sdk.dll": b"SDK payload"})
        self.write(self.signed, {"LICENSE": b"license", "lib/sdk.dll": b"SDK payload", ".signature.p7s": b"signature"})
        self.fingerprint = "a" * 64
        self.url = "https://api.nuget.org/v3-index/repository-signatures/5.0.0/index.json"
        self.index = {"resources": [{"@type": "RepositorySignatures/5.0.0", "@id": self.url}]}
        self.metadata = {"allRepositorySigned": True, "signingCertificates": [
            {"fingerprints": {nuget_verify.SHA256_OID: self.fingerprint}},
            {"fingerprints": {nuget_verify.SHA256_OID: "b" * 64}}]}
        self.fetch = Mock(side_effect=lambda url: self.index if url == nuget_verify.SERVICE_INDEX else self.metadata)
        self.success = subprocess.CompletedProcess([], 0,
            "Signature type: Repository\n  Subject Name: NuGet.org\n  SHA256 hash: " + self.fingerprint.upper(), "")

    def write(self, path, entries):
        with zipfile.ZipFile(path, "w") as archive:
            for name, data in entries.items():
                archive.writestr(name, data)

    def verify(self):
        return nuget_verify.verify(self.original, self.signed, fetch_json=self.fetch,
                                   inspect_archive=release.inspect_archive)

    def test_signature_addition_preserves_payload_and_requires_current_trusted_signer(self):
        def run(command, **kwargs):
            self.assertIn("--all", command)
            self.assertEqual(command.count("--certificate-fingerprint"), 2)
            self.assertIn(self.fingerprint, command)
            self.assertIn("b" * 64, command)
            config = Path(command[command.index("--configfile") + 1]).read_text()
            self.assertIn('allowUntrustedRoot="false"', config)
            self.assertIn('signatureValidationMode" value="require"', config)
            self.assertEqual(kwargs["env"]["DOTNET_CLI_UI_LANGUAGE"], "en-US")
            self.assertEqual(kwargs["stdin"], subprocess.DEVNULL)
            return self.success
        with patch.object(nuget_verify.subprocess, "run", side_effect=run):
            proof = self.verify()
        self.assertEqual(proof["unsignedSha256"], hashlib.sha256(self.original.read_bytes()).hexdigest())
        self.assertEqual(proof["downloadSha256"], hashlib.sha256(self.signed.read_bytes()).hexdigest())
        self.assertEqual(proof["payloadSha256"], release.inspect_archive(self.original, "nuget")["contentSha256"])
        self.assertEqual(proof["addedEntries"], [".signature.p7s"])
        self.assertEqual(proof["signerCertificateFingerprint"], self.fingerprint)
        self.assertEqual(self.fetch.call_count, 2)

    def test_changed_removed_or_additional_entries_fail_before_signature_execution(self):
        original = nuget_verify.entries(self.original)
        variants = [
            {**original, "lib/sdk.dll": b"tampered", ".signature.p7s": b"signature"},
            {"LICENSE": b"license", ".signature.p7s": b"signature"},
            {**original, "extra.txt": b"extra", ".signature.p7s": b"signature"},
            {**original, "empty/": b"", ".signature.p7s": b"signature"},
        ]
        for entries in variants:
            with self.subTest(entries=list(entries)), patch.object(nuget_verify.subprocess, "run") as run:
                self.write(self.signed, entries)
                with self.assertRaisesRegex(ValueError, "payload differs"):
                    self.verify()
                run.assert_not_called()

    def test_unsigned_or_already_signed_original_is_not_accepted(self):
        self.write(self.signed, nuget_verify.entries(self.original))
        with self.assertRaisesRegex(ValueError, "no repository signature"):
            self.verify()
        with zipfile.ZipFile(self.original, "a") as archive:
            archive.writestr(".signature.p7s", b"already signed")
        with self.assertRaisesRegex(ValueError, "retained unsigned"):
            self.verify()

    def test_duplicate_signature_and_private_entries_keep_archive_guards(self):
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            with zipfile.ZipFile(self.signed, "a") as archive:
                archive.writestr(".signature.p7s", b"duplicate")
        with self.assertRaisesRegex(release.ReleaseError, "Duplicate"):
            self.verify()
        self.write(self.signed, {**nuget_verify.entries(self.original), ".env": b"private", ".signature.p7s": b"signature"})
        with self.assertRaisesRegex(release.ReleaseError, "Private"):
            self.verify()

    def test_signature_failure_or_wrong_signer_is_never_accepted(self):
        for result in (subprocess.CompletedProcess([], 1, "signature invalid", ""),
                       subprocess.CompletedProcess([], 0, "Signature type: Author\nSHA256 hash: " + self.fingerprint, ""),
                       subprocess.CompletedProcess([], 0, "Signature type: Repository\nSHA256 hash: " + "c" * 64, "")):
            with self.subTest(output=result.stdout), patch.object(nuget_verify.subprocess, "run", return_value=result):
                with self.assertRaises(ValueError):
                    self.verify()

    def test_missing_or_malformed_certificate_policy_fails_closed(self):
        for metadata in ({}, {"allRepositorySigned": False, "signingCertificates": []},
                         {"allRepositorySigned": True, "signingCertificates": []},
                         {"allRepositorySigned": True, "signingCertificates": [{"fingerprints": {nuget_verify.SHA256_OID: "bad"}}]}):
            self.metadata = metadata
            with self.subTest(metadata=metadata), self.assertRaises(ValueError):
                nuget_verify.repository_certificates(self.fetch)

    def test_repository_metadata_cannot_redirect_to_another_origin(self):
        for url in ("http://api.nuget.org/certs", "https://evil.example/certs", "https://user@api.nuget.org/certs"):
            self.index["resources"][0]["@id"] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                nuget_verify.repository_certificates(self.fetch)


if __name__ == "__main__":
    unittest.main()
