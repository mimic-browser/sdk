import urllib.error
import unittest
from unittest.mock import patch

import release


class RubyGemsRegistryTests(unittest.TestCase):
    def setUp(self):
        self.endpoint = "https://rubygems.org/api/v2/rubygems/mimic-browser/versions/0.1.0.json"
        self.artifact = {"filename": "mimic-browser-0.1.0.gem", "sha256": "a" * 64}
        self.package = {"registry": "rubygems", "name": "mimic-browser", "version": "0.1.0",
                        "artifacts": [self.artifact]}
        self.metadata = {"name": "mimic-browser", "version": "0.1.0", "sha": "a" * 64}

    def test_missing_version_uses_metadata_404_without_archive_request(self):
        absent = urllib.error.HTTPError(self.endpoint, 404, "Not Found", {}, None)
        with patch.object(release, "fetch_json", side_effect=absent) as fetch:
            with self.assertRaises(urllib.error.HTTPError) as raised:
                release.registry_urls(self.package)
            self.assertEqual(raised.exception.code, 404)
            fetch.assert_called_once_with(self.endpoint)

    def test_authorization_and_service_errors_remain_failures(self):
        for status in (403, 503):
            error = urllib.error.HTTPError(self.endpoint, status, "Failure", {}, None)
            with self.subTest(status=status), patch.object(release, "fetch_json", side_effect=error):
                with self.assertRaises(urllib.error.HTTPError) as raised:
                    release.registry_urls(self.package)
                self.assertEqual(raised.exception.code, status)

    def test_published_identity_and_checksum_bind_the_download(self):
        with patch.object(release, "fetch_json", return_value=self.metadata) as fetch:
            self.assertEqual(release.registry_urls(self.package), {
                self.artifact["filename"]: "https://rubygems.org/downloads/mimic-browser-0.1.0.gem"})
            fetch.assert_called_once_with(self.endpoint)
        for change in ({"name": "other"}, {"version": "0.2.0"}, {"sha": "b" * 64}, {"sha": None}):
            with self.subTest(change=change), patch.object(release, "fetch_json", return_value={
                    **self.metadata, **change}), self.assertRaises(release.ReleaseError):
                release.registry_urls(self.package)


if __name__ == "__main__":
    unittest.main()
