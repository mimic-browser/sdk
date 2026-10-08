import datetime as dt
import io
import json
import unittest
from unittest.mock import patch
import urllib.error
import urllib.parse

import npm_oidc
import release


class Response(io.BytesIO):
    def __init__(self, value, status=200):
        super().__init__(json.dumps(value).encode())
        self.status = status


class NpmOIDCTests(unittest.TestCase):
    def setUp(self):
        self.environment = {
            "ACTIONS_ID_TOKEN_REQUEST_URL": "https://actions.example.invalid/token?request=1&audience=old",
            "ACTIONS_ID_TOKEN_REQUEST_TOKEN": "secret-github-request",
        }
        self.now = dt.datetime(2026, 10, 8, 10, 0, tzinfo=dt.timezone.utc)
        self.token = {
            "token_type": "oidc", "token": "secret-short-lived-npm",
            "created": "2026-10-08T10:00:00Z", "expires": "2026-10-08T11:00:00Z",
        }

    def opener(self, payload=None, status=201):
        requests = []
        def open_request(request, timeout):
            requests.append(request)
            self.assertEqual(timeout, 30)
            if len(requests) == 1:
                return Response({"value": "secret-identity-jwt"})
            return Response(self.token if payload is None else payload, status)
        return requests, open_request

    def test_real_exchange_shape_and_only_public_metadata_returned(self):
        requests, opener = self.opener()
        with patch("builtins.print") as log:
            result = npm_oidc.verify_oidc(self.environment, opener, self.now)
        self.assertEqual(result, {"package": "mimic-browser", "expires": "2026-10-08T11:00:00+00:00"})
        self.assertEqual(urllib.parse.parse_qs(urllib.parse.urlsplit(requests[0].full_url).query), {
            "request": ["1"], "audience": ["npm:registry.npmjs.org"],
        })
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer secret-github-request")
        self.assertEqual(requests[1].full_url,
                         "https://registry.npmjs.org/-/npm/v1/oidc/token/exchange/package/mimic-browser")
        self.assertEqual(requests[1].get_method(), "POST")
        self.assertEqual(requests[1].data, b"")
        self.assertEqual(requests[1].get_header("Authorization"), "Bearer secret-identity-jwt")
        self.assertNotIn("secret", json.dumps(result))
        log.assert_not_called()

    def test_package_name_comes_from_catalog_and_is_url_encoded(self):
        requests, opener = self.opener()
        with patch.object(release, "catalog", return_value={"node": {"name": "@owner/package", "registry": "npm"}}):
            result = npm_oidc.verify_oidc(self.environment, opener, self.now)
        self.assertTrue(requests[1].full_url.endswith("%40owner%2Fpackage"))
        self.assertEqual(result["package"], "@owner/package")

    def test_legacy_credentials_are_rejected_even_when_empty(self):
        for key in ("NODE_AUTH_TOKEN", "NPM_TOKEN"):
            for value in ("", "secret-legacy-token"):
                with self.subTest(key=key, empty=not value):
                    requests, opener = self.opener()
                    with self.assertRaisesRegex(release.ReleaseError, "must|requires") as error:
                        npm_oidc.verify_oidc({**self.environment, key: value}, opener, self.now)
                    self.assertNotIn("secret", str(error.exception))
                    self.assertEqual(requests, [])

    def test_http_failures_redact_body_url_and_credentials(self):
        for status in (401, 403):
            for phase in (1, 2):
                with self.subTest(status=status, phase=phase):
                    calls = []
                    def opener(request, timeout):
                        calls.append(request)
                        if len(calls) == phase:
                            raise urllib.error.HTTPError(
                                "https://secret.invalid", status, "secret-error-message", {}, io.BytesIO(b"secret-response"),
                            )
                        return Response({"value": "secret-identity-jwt"})
                    with self.assertRaises(release.ReleaseError) as error:
                        npm_oidc.verify_oidc(self.environment, opener, self.now)
                    self.assertNotIn("secret", str(error.exception))
                    self.assertTrue(error.exception.__suppress_context__)
                    self.assertIn(f"HTTP {status}", str(error.exception))
                    self.assertIn("GitHub identity response" if phase == 1 else "npm token exchange",
                                  str(error.exception))

    def test_http_diagnostic_codes_are_allowlisted_and_bodies_redacted(self):
        for body, expected in (({"code": "E403", "message": "secret"}, "; E403"),
                               ({"code": "SECRET_TOKEN", "message": "secret"}, None),
                               ({"code": {"secret": True}}, None)):
            calls = []
            def opener(request, timeout):
                calls.append(request)
                if len(calls) == 1:
                    return Response({"value": "secret-identity-jwt"})
                raise urllib.error.HTTPError("https://secret.invalid", 403, "secret", {},
                                             io.BytesIO(json.dumps(body).encode()))
            with self.assertRaises(release.ReleaseError) as error:
                npm_oidc.verify_oidc(self.environment, opener, self.now)
            self.assertNotIn("secret", str(error.exception).lower())
            self.assertIn("npm token exchange (HTTP 403", str(error.exception))
            if expected:
                self.assertIn(expected, str(error.exception))

    def test_expired_malformed_or_non_oidc_credentials_fail_closed(self):
        payloads = [
            {**self.token, "expires": "2026-10-08T10:00:00Z"},
            {**self.token, "expires": "2026-10-08T09:59:59Z"},
            {**self.token, "expires": "2026-10-08T11:00:00"},
            {**self.token, "expires": "secret-not-a-date"},
            {**self.token, "expires": None},
            {**self.token, "token_type": "legacy"},
            {**self.token, "token": ""},
            {**self.token, "token": "secret\nvalue"},
            {**self.token, "token": "secret\x00value"},
            {**self.token, "token": None},
            {"secret": "missing-credential"},
            ["secret-array-response"],
        ]
        for payload in payloads:
            with self.subTest(payload_shape=type(payload).__name__):
                _, opener = self.opener(payload)
                with self.assertRaises(release.ReleaseError) as error:
                    npm_oidc.verify_oidc(self.environment, opener, self.now)
                self.assertNotIn("secret", str(error.exception))

    def test_exchange_must_return_201(self):
        _, opener = self.opener(status=200)
        with self.assertRaisesRegex(release.ReleaseError, r"npm token exchange \(HTTP 200\)"):
            npm_oidc.verify_oidc(self.environment, opener, self.now)

    def test_expiry_failures_reveal_only_structural_classification(self):
        cases = [({}, "missing expires field"), ({"expires": None}, "expires field is null"),
                 ({"expires": 1234}, "expires field is number"),
                 ({"expires": "secret"}, "expires field is not an ISO datetime"),
                 ({"expires": "2026-10-08T11:00:00"}, "expires datetime has no timezone"),
                 ({"expires": "2026-10-08T09:00:00Z"}, "expires datetime is not in the future")]
        for fields, classification in cases:
            payload = {key: value for key, value in self.token.items() if key != "expires"}
            _, opener = self.opener({**payload, **fields})
            with self.subTest(classification=classification), self.assertRaises(release.ReleaseError) as error:
                npm_oidc.verify_oidc(self.environment, opener, self.now)
            self.assertIn(classification, str(error.exception))
            self.assertNotIn("secret", str(error.exception))

    def test_missing_identity_inputs_do_not_fall_back_to_local_npm_auth(self):
        for environment in ({}, {**self.environment, "ACTIONS_ID_TOKEN_REQUEST_TOKEN": ""},
                            {**self.environment, "ACTIONS_ID_TOKEN_REQUEST_URL": "http://secret.invalid"}):
            requests, opener = self.opener()
            with self.assertRaises(release.ReleaseError) as error:
                npm_oidc.verify_oidc(environment, opener, self.now)
            self.assertEqual(requests, [])
            self.assertNotIn("secret", str(error.exception))

    def test_cli_reports_only_package_and_expiry(self):
        with patch.object(npm_oidc, "verify_oidc", return_value={
            "package": "mimic-browser", "expires": "2026-10-08T11:00:00+00:00",
        }), patch("builtins.print") as log:
            npm_oidc.main()
        log.assert_called_once_with(
            "SUCCESS npm OIDC authentication: mimic-browser; expires 2026-10-08T11:00:00+00:00",
        )


if __name__ == "__main__":
    unittest.main()
