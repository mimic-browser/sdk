import io
import json
import unittest
from unittest.mock import patch
import urllib.error

import publish
import pypi_auth
import release


class PyPITrustedPublisherTests(unittest.TestCase):
    def test_exchange_is_scoped_and_preserves_other_registry_credentials(self):
        environment = {
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.invalid/token?request=1&audience=old",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "github-credential",
            "NODE_AUTH_TOKEN": "npm-credential",
        }
        requests = []
        def opener(request, timeout):
            requests.append(request)
            value = {"value": "identity-jwt"} if len(requests) == 1 else {"token": "short-lived-token"}
            return io.BytesIO(json.dumps(value).encode())
        with patch("builtins.print") as log:
            result = pypi_auth.publication_environment(environment, opener)
        self.assertIn("audience=pypi", requests[0].full_url)
        self.assertNotIn("audience=old", requests[0].full_url)
        self.assertEqual(requests[1].full_url, "https://pypi.org/_/oidc/mint-token")
        self.assertEqual(json.loads(requests[1].data), {"token": "identity-jwt"})
        self.assertEqual(result["TWINE_PASSWORD"], "short-lived-token")
        self.assertEqual(result["NODE_AUTH_TOKEN"], environment["NODE_AUTH_TOKEN"])
        self.assertNotIn("TWINE_PASSWORD", environment)
        log.assert_called_once_with("::add-mask::short-lived-token")

    def test_oidc_satisfies_only_pypi_credentials(self):
        environment = {"ACTIONS_ID_TOKEN_REQUEST_URL": "url", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "token"}
        publish.check_credentials({"python": {"registry": "pypi"}}, environment)
        with self.assertRaisesRegex(release.ReleaseError, "NUGET_API_KEY"):
            publish.check_credentials({"dotnet": {"registry": "nuget"}}, environment)

    def test_auth_failure_does_not_expose_credentials(self):
        environment = {"ACTIONS_ID_TOKEN_REQUEST_URL": "https://example.invalid", "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "secret"}
        def fail(request, timeout):
            raise urllib.error.URLError("secret server response")
        with self.assertRaises(release.ReleaseError) as error:
            pypi_auth.publication_environment(environment, fail)
        self.assertNotIn("secret", str(error.exception))


if __name__ == "__main__":
    unittest.main()
