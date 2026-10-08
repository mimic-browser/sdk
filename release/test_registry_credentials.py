import base64
import unittest

import publish
import publish_sources
import release


class RegistryCredentialsTests(unittest.TestCase):
    def test_portal_pair_encodes_exact_utf8_credentials(self):
        environment = {"MAVEN_CENTRAL_USERNAME": "portal-user", "MAVEN_CENTRAL_PASSWORD": "portal:password"}
        token = publish_sources.maven_token(environment)
        self.assertEqual(base64.b64decode(token), b"portal-user:portal:password")
        publish.check_credentials({"java": {"registry": "maven"}}, {**environment, "GNUPGHOME": "/private/keyring"})
        self.assertNotIn("MAVEN_CENTRAL_TOKEN", environment)

    def test_incomplete_pair_fails_without_exposing_credentials(self):
        for environment in ({}, {"MAVEN_CENTRAL_USERNAME": "private-username"}, {"MAVEN_CENTRAL_PASSWORD": "private-password"}):
            with self.assertRaises(release.ReleaseError) as error:
                publish_sources.maven_token(environment)
            self.assertNotIn("private-", str(error.exception))

    def test_credentials_remain_scoped_to_selected_registry(self):
        publish.check_credentials({"rust": {"registry": "crates"}}, {"CARGO_REGISTRY_TOKEN": "cargo-token"})
        with self.assertRaisesRegex(release.ReleaseError, "GNUPGHOME"):
            publish.check_credentials({"java": {"registry": "maven"}}, {"MAVEN_CENTRAL_USERNAME": "user", "MAVEN_CENTRAL_PASSWORD": "password"})


if __name__ == "__main__":
    unittest.main()
