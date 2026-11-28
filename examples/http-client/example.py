"""Dependency-free, offline-safe client seam example. No credentials shipped.

This sample constructs requests and delegates transport; no HTTP is issued by default.
"""

import json
from urllib.parse import urlsplit

ORIGIN = "https://broker.example.invalid"  # documentation-only reserved domain


def prepare(method, path, body, bearer):
    parsed = urlsplit(ORIGIN)
    if parsed.scheme != "https" or parsed.netloc != "broker.example.invalid":
        raise ValueError("exact approved HTTPS origin required")
    if not path.startswith("/v1/") or "token=" in path or "#" in path:
        raise ValueError("invalid path")
    if not bearer or any(c in bearer for c in "\r\n"):
        raise ValueError("invalid injected bearer")
    if method not in ("GET", "POST"):
        raise ValueError("unsupported method")
    if method == "GET" and body is not None:
        raise ValueError("GET must not carry JSON body")
    headers = {"Authorization": "Bearer " + bearer}
    if method == "POST":
        headers["Content-Type"] = "application/json"
    return {
        "method": method, "url": ORIGIN + path,
        "headers": headers,
        "body": json.dumps(body, separators=(",", ":")) if method == "POST" else None,
        "allow_redirects": False,
    }


def dispatch(transport, method, path, body, token_provider):
    """Transport supplied by caller. No automatic retry of POST mutations."""
    request = prepare(method, path, body, token_provider())
    status, data = transport(request)
    if status == 401:
        raise PermissionError("paired device token rejected")
    if not 200 <= status < 300:
        raise ConnectionError("broker request unsuccessful")
    if not isinstance(data, dict):
        raise ValueError("response must be an object")
    return data


class FakeTransport:
    """Synthetic seam for tests; deliberately never sends HTTP."""
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, request):
        self.calls.append(request)
        return self.responses[request["url"].removeprefix(ORIGIN)]


if __name__ == "__main__":
    print("Offline example only: inject a trusted transport and private bearer.")
