"""Acquire short-lived PyPI credentials from the configured GitHub publisher."""
import json
import urllib.error
import urllib.parse
import urllib.request

import release


def publication_environment(environment, opener=urllib.request.urlopen):
    result = dict(environment)
    if result.get("TWINE_PASSWORD"):
        return result
    url = result.get("ACTIONS_ID_TOKEN_REQUEST_URL")
    request_token = result.get("ACTIONS_ID_TOKEN_REQUEST_TOKEN")
    if not url or not request_token:
        raise release.ReleaseError("PyPI publication requires GitHub OIDC credentials")
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query = [(key, value) for key, value in query if key != "audience"]
    query.append(("audience", "pypi"))
    url = urllib.parse.urlunsplit(parts._replace(query=urllib.parse.urlencode(query)))
    try:
        request = urllib.request.Request(url, headers={"Authorization": "bearer " + request_token})
        with opener(request, timeout=30) as response:
            identity = json.load(response)["value"]
        request = urllib.request.Request(
            "https://pypi.org/_/oidc/mint-token",
            data=json.dumps({"token": identity}).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with opener(request, timeout=30) as response:
            token = json.load(response)["token"]
        if not isinstance(token, str) or not token or "\n" in token or "\r" in token:
            raise ValueError("Invalid credential")
    except (urllib.error.URLError, ValueError, KeyError, TypeError):
        # Never include response bodies, bearer credentials or JWTs in diagnostics.
        raise release.ReleaseError(
            "PyPI trusted publisher authentication failed; check repository, workflow and environment settings"
        ) from None
    print("::add-mask::" + token)
    result.update(TWINE_USERNAME="__token__", TWINE_PASSWORD=token)
    return result
