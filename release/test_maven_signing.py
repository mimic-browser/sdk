from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import maven_signing as signing
import publish_sources
import release


FINGERPRINT = "A" * 40
PUBLIC = b"-----BEGIN PGP PUBLIC KEY BLOCK-----\npublic-only\n-----END PGP PUBLIC KEY BLOCK-----"


def record(kind="sec", capability="sc", expiry="", marker="+"):
    fields = [kind, "u", "255", "22", "public-id", "1", expiry, "", "", "", "", capability, "", "", marker]
    return ":".join(fields) + "\n"


def keyring(primary="sc", subkey=None, expiry=""):
    text = record(capability=primary, expiry=expiry) + "fpr:::::::::" + FINGERPRINT + ":\n"
    if subkey:
        text += record("ssb", subkey) + "fpr:::::::::" + "B" * 40 + ":\n"
    return text.encode()


class MavenSigningTests(unittest.TestCase):
    def test_one_valid_primary_and_signing_subkey_are_supported(self):
        self.assertEqual(signing.signing_fingerprint(keyring(), now=10), FINGERPRINT)
        self.assertEqual(signing.signing_fingerprint(keyring("c", "s"), now=10), FINGERPRINT)
        for data in (b"", keyring() + keyring(), keyring("c"), keyring(expiry="5")):
            with self.subTest(data=data), self.assertRaises(release.ReleaseError):
                signing.signing_fingerprint(data, now=10)

    def exercise(self, missing=False, incorrect=False, verify_failure=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            home = root / "dedicated"
            home.mkdir()
            work = root / "work"
            work.mkdir()
            calls = []
            def gpg(arguments, message, input=None):
                arguments = list(map(str, arguments))
                calls.append((arguments, input))
                if "--list-secret-keys" in arguments:
                    return keyring("c", "s")
                if "--export" in arguments:
                    return PUBLIC
                if "--list-keys" in arguments:
                    fingerprint = "C" * 40 if incorrect else FINGERPRINT
                    return (record("pub") + "fpr:::::::::" + fingerprint + ":\n").encode()
                if "--verify" in arguments and "public-verification" in arguments[1] and verify_failure:
                    raise release.ReleaseError(message)
                return b""
            with patch.object(signing, "run", side_effect=gpg), patch.object(signing, "public_key", side_effect=[None, PUBLIC] if missing else [PUBLIC]):
                provisioned = signing.verify(home, "test-passphrase", work)
            self.assertEqual(provisioned, missing)
            sends = [args for args, _ in calls if "--send-keys" in args]
            self.assertEqual(len(sends), int(missing))
            if sends:
                self.assertEqual(sends[0][1], str(work / "public-export"))
                self.assertNotEqual(sends[0][1], str(home))
            sign = next((args, value) for args, value in calls if "--detach-sign" in args)
            self.assertIn("--local-user", sign[0])
            self.assertEqual(sign[0][sign[0].index("--local-user") + 1], FINGERPRINT)
            self.assertTrue(any("--verify" in args and args[1] == str(work / "public-verification") for args, _ in calls))
            self.assertFalse(any("--export-secret-keys" in args for args, _ in calls))

    def test_existing_public_key_is_independently_verified_without_submission(self):
        self.exercise()

    def test_missing_public_key_is_submitted_from_public_only_keyring_and_refetched(self):
        self.exercise(missing=True)

    def test_foreign_public_key_or_invalid_signature_stops_publication(self):
        for options in ({"incorrect": True}, {"verify_failure": True}):
            with self.subTest(options=options), self.assertRaises(release.ReleaseError):
                self.exercise(**options)

    def test_private_key_material_cannot_enter_public_keyring(self):
        with patch.object(signing, "run") as gpg:
            with self.assertRaises(release.ReleaseError):
                signing.import_public(Path("unused"), b"-----BEGIN PGP PRIVATE KEY BLOCK-----", FINGERPRINT)
            gpg.assert_not_called()

    def test_publisher_uses_validated_authority_and_keeps_gpg_diagnostics_private(self):
        with tempfile.TemporaryDirectory() as directory:
            artifact = Path(directory) / "package.jar"
            artifact.write_bytes(b"artifact")
            package = {"name": "io.github.mimic-browser:mimic-browser", "version": "0.1.0",
                       "artifacts": [{"file": str(artifact)}]}
            calls = []
            def subprocess_run(arguments, **options):
                calls.append((arguments, options))
                Path(arguments[arguments.index("--output") + 1]).write_bytes(b"signature")
                return type("Result", (), {"returncode": 0})()
            class Response:
                def __init__(self, value): self.value = value
                def __enter__(self): return self
                def __exit__(self, *args): pass
                def read(self): return self.value
            progress = {}
            with patch.object(publish_sources, "maven_token", return_value="temporary-test-token"), \
                    patch.object(signing, "run", return_value=keyring("c", "s")), \
                    patch.object(publish_sources.subprocess, "run", side_effect=subprocess_run), \
                    patch.object(publish_sources.urllib.request, "urlopen", side_effect=[
                        Response(b"deployment-id"), Response(b'{"deploymentState":"PUBLISHED"}')]):
                publish_sources.maven(package, progress, lambda: None)
            arguments, options = calls[0]
            self.assertEqual(arguments[arguments.index("--local-user") + 1], FINGERPRINT)
            self.assertTrue(options["capture_output"])
            self.assertEqual(progress["centralState"], "PUBLISHED")


if __name__ == "__main__":
    unittest.main()
