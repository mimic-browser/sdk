import urllib.error
import unittest
from unittest.mock import patch

import release


class CratesRegistryTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = "https://crates.io/api/v1/crates/mimic-browser/0.1.0"
        self.artifact = {"filename": "mimic-browser-0.1.0.crate", "sha256": "a" * 64}
        self.package = {"registry": "crates", "name": "mimic-browser", "version": "0.1.0",
                        "artifacts": [self.artifact]}
        self.metadata = {"crate": "mimic-browser", "num": "0.1.0", "checksum": "a" * 64}

    def test_missing_version_uses_metadata_404_without_static_archive_request(self):
        absent = urllib.error.HTTPError(self.endpoint, 404, "Not Found", {}, None)
        with patch.object(release, "fetch_json", side_effect=absent) as fetch:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                release.registry_urls(self.package)
            self.assertEqual(raised.exception.code, 404)
            fetch.assert_called_once_with(self.endpoint)

    def test_metadata_authorization_errors_are_never_treated_as_absence(self):
        denied = urllib.error.HTTPError(self.endpoint, 403, "Forbidden", {}, None)
        with patch.object(release, "fetch_json", side_effect=denied):
            with self.assertRaises(urllib.error.HTTPError) as raised:
                release.registry_urls(self.package)
            self.assertEqual(raised.exception.code, 403)

    def test_published_identity_and_checksum_bind_the_download(self):
        with patch.object(release, "fetch_json", return_value={"version": self.metadata}):
            self.assertEqual(release.registry_urls(self.package), {
                self.artifact["filename"]: self.endpoint + "/download"})
        for change in ({"crate": "other"}, {"num": "0.2.0"}, {"checksum": "b" * 64}):
            with self.subTest(change=change), patch.object(release, "fetch_json", return_value={
                    "version": {**self.metadata, **change}}), self.assertRaises(release.ReleaseError):
                release.registry_urls(self.package)


if __name__ == "__main__":
    unittest.main()
