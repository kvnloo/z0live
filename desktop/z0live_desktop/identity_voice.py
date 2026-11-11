"""Opt-in z0bridge voice access verification; no second device-token store.

Introspection is authoritative on every request/heartbeat. Failure is closed.
The service credential must be a local z0bridge:introspect enrollment, never owner auth.
"""
import json
import os
from pathlib import Path
import re
import stat
import time
import urllib.request
from urllib.parse import urlsplit


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def voice_identity(token, *, env=None, request=None, now=None):
    env = os.environ if env is None else env
    if env.get("Z0LIVE_IDENTITY") != "1" or not isinstance(token, str):
        return None
    if len(token) > 4096 or not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
        return None
    try:
        url = env.get("Z0LIVE_IDENTITY_URL", "http://127.0.0.1:4789")
        parsed = urlsplit(url)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"} or parsed.username or parsed.password or parsed.path or parsed.query or parsed.fragment:
            return None
        path = Path(env["Z0LIVE_IDENTITY_CREDENTIAL_FILE"])
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_uid != os.getuid():
            return None
        credential = json.loads(path.read_text())
        if "z0bridge:introspect" not in credential.get("scopes", []) or not re.fullmatch(r"z0bc\.cred_[0-9a-f]{16}\.[A-Za-z0-9_-]{43}", credential.get("credential", "")):
            return None
        req = urllib.request.Request(url + "/v1/introspect", data=json.dumps({"token": token}).encode(), headers={"Authorization": "Bearer " + credential["credential"], "Content-Type": "application/json"}, method="POST")
        opener = request or urllib.request.build_opener(NoRedirect()).open
        with opener(req, timeout=3) as response:
            result = json.loads(response.read(16385))
        if not isinstance(result, dict) or result.get("active") is not True or result.get("token_type") != "access" or result.get("aud") != "z0live" or result.get("scope") != "z0live:voice":
            return None
        exp = result.get("exp")
        if not isinstance(exp, (int, float)) or not (now or time.time)() < exp <= (now or time.time)() + 300:
            return None
        sub, dev = result.get("sub", ""), result.get("dev", "")
        if not re.fullmatch(r"cred_[0-9a-f]{16}", sub) or not re.fullmatch(r"dev_[0-9a-f]{16}", dev):
            return None
        return "z0bridge:" + dev + ":" + sub
    except (OSError, ValueError, KeyError, TypeError):
        return None


def voice_route(method, path):
    """Voice enrollment never authorizes gesture/device administration routes."""
    if method == "GET":
        return path in {"/v1/health", "/v1/inbox", "/v1/stream", "/v1/context", "/v1/trace", "/v1/live/trace", "/v1/sessions", "/v1/devices/self"}
    return method == "POST" and (path in {"/v1/realtime/session", "/v1/live/calls", "/v1/events", "/v1/cancel"} or bool(re.fullmatch(r"/v1/sessions/[A-Za-z0-9_-]+/end", path)) or path in {"/v1/tools/ask_z0", "/v1/tools/cancel_work", "/v1/tools/tell_hermes", "/v1/tools/get_context", "/v1/tools/summarize", "/v1/tools/relay_status"})
