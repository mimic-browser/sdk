#!/usr/bin/env python3
"""Verify npm trusted-publisher authentication without uploading a package."""
from __future__ import annotations

import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request

import release


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, response, code, message, headers, new_url):
        # Authentication endpoints must not forward bearer credentials elsewhere.
        return None


class _UnexpectedStatus(ValueError):
    def __init__(self, status):
        self.status = status


class _InvalidExpiry(ValueError):
    def __init__(self, reason):
        self.reason = reason


def _expiry(response, now):
    if "expires" not in response:
        raise _InvalidExpiry("missing expires field")
    value = response["expires"]
    if not isinstance(value, str):
        kind = "null" if value is None else "number" if isinstance(value, (int, float)) else "non-string"
        raise _InvalidExpiry(f"expires field is {kind}")
    try:
        expires = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise _InvalidExpiry("expires field is not an ISO datetime") from None
    if expires.tzinfo is None:
        raise _InvalidExpiry("expires datetime has no timezone")
    if expires <= now:
        raise _InvalidExpiry("expires datetime is not in the future")
    return expires


def _failure_detail(error):
    """Expose only transport status and a fixed allowlist of public error codes."""
    if isinstance(error, _InvalidExpiry):
        return error.reason
    status = error.code if isinstance(error, urllib.error.HTTPError) else getattr(error, "status", None)
    detail = f"HTTP {status}" if type(status) is int and 100 <= status <= 599 else "invalid response"
    if isinstance(error, urllib.error.HTTPError):
        try:
            data = error.read(65_537)
            body = json.loads(data) if len(data) <= 65_536 else None
            code = body.get("code") if isinstance(body, dict) else None
            if code in ("E400", "E401", "E403", "E404", "E500", "EOTP", "ENEEDAUTH",
                        "EUNAUTHORIZED", "EFORBIDDEN", "EINVALIDOIDC", "EINVALIDTOKEN"):
                detail += f"; {code}"
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        finally:
            error.close()
    return detail


def _json_response(opener, request, expected_status):
    with opener(request, timeout=30) as response:
        if response.status != expected_status:
            raise _UnexpectedStatus(response.status)
        data = response.read(65_537)
        if len(data) > 65_536:
            raise ValueError("Oversized authentication response")
        result = json.loads(data)
        if not isinstance(result, dict):
            raise ValueError("Invalid authentication response")
        return result


def _credential(value):
    if not isinstance(value, str) or not value or any(not 32 < ord(character) < 127 for character in value):
        raise ValueError("Invalid authentication credential")
    return value


def verify_oidc(environment, opener=None, now=None):
    """Return only public metadata after a real, package-scoped token exchange.

    This intentionally has no token or npm configuration fallback. Neither the
    GitHub JWT nor the exchanged npm credential leaves this function.
    """
    if any(name in environment for name in ("NODE_AUTH_TOKEN", "NPM_TOKEN")):
        raise release.ReleaseError("npm OIDC proof requires NODE_AUTH_TOKEN and NPM_TOKEN to be absent")
    package = release.catalog()["node"]
    if package["registry"] != "npm":
        raise release.ReleaseError("The Node package must target the npm registry")
    package_name = package["name"]
    if not isinstance(package_name, str) or not package_name or any(character.isspace() for character in package_name):
        raise release.ReleaseError("Invalid Node package name in the release catalog")
    opener = opener or urllib.request.build_opener(_NoRedirect()).open
    phase = "GitHub identity configuration"
    try:
        request_token = _credential(environment.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN"))
        parts = urllib.parse.urlsplit(environment.get("ACTIONS_ID_TOKEN_REQUEST_URL", ""))
        if parts.scheme != "https" or not parts.hostname or parts.username or parts.password or parts.fragment:
            raise ValueError("Invalid GitHub identity endpoint")
        query = [(key, value) for key, value in urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
                 if key != "audience"]
        query.append(("audience", "npm:registry.npmjs.org"))
        identity_url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
        identity_request = urllib.request.Request(
            identity_url,
            headers={"Authorization": "Bearer " + request_token, "Accept": "application/json"},
        )
        phase = "GitHub identity response"
        identity = _credential(_json_response(opener, identity_request, 200)["value"])
        exchange_request = urllib.request.Request(
            "https://registry.npmjs.org/-/npm/v1/oidc/token/exchange/package/"
            + urllib.parse.quote(package_name, safe=""),
            data=b"",
            headers={"Authorization": "Bearer " + identity, "Accept": "application/json"},
            method="POST",
        )
        phase = "npm token exchange"
        exchanged = _json_response(opener, exchange_request, 201)
        phase = "npm token validation"
        if exchanged.get("token_type") != "oidc":
            raise ValueError("Unexpected npm credential type")
        _credential(exchanged["token"])
        phase = "npm expiry validation"
        expires = _expiry(exchanged, now or dt.datetime.now(dt.timezone.utc))
    except (urllib.error.URLError, OSError, ValueError, KeyError, TypeError, AttributeError, OverflowError) as error:
        detail = _failure_detail(error)
        # Never include URLs, exception text, response messages or credentials.
        raise release.ReleaseError(
            f"npm trusted publisher authentication failed during {phase} ({detail}); "
            "check the package, repository, workflow and environment"
        ) from None
    return {"package": package_name, "expires": expires.astimezone(dt.timezone.utc).isoformat()}


def main():
    verified = verify_oidc(os.environ)
    print(f"SUCCESS npm OIDC authentication: {verified['package']}; expires {verified['expires']}")


if __name__ == "__main__":
    try:
        main()
    except release.ReleaseError as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(1) from None
