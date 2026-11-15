"""Loopback HTTP boundary controls; no deployment, provider or phone."""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop import core
from z0live_desktop.core import Store
from z0live_desktop.server import Handler, Server

ALLOWED_ORIGIN = "https://approved.example.invalid"
OTHER_ORIGIN = "https://unapproved.example.invalid"


class HTTPSecurityConformance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.store = Store(self.temp.name)
        self.token = self.store.pair("synthetic trusted owner")
        self.device = self.store.authenticate(self.token)
        self.other_token = self.store.pair("synthetic second device")
        self.other_device = self.store.authenticate(self.other_token)
        with patch.object(core, "mint", return_value={
            "value": "synthetic-ephemeral", "expires_at": 1,
        }):
            self.sid = self.store.new_session(self.device, {"client": "web"})["session_id"]
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.store = self.store
        self.server.stop = threading.Event()
        self.server.origins = {ALLOWED_ORIGIN}
        self.server.sse_lock = threading.Lock()
        self.server.sse_clients = 0
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def request(self, path, *, method="GET", body=None, token=None, origin=None,
                content_type="application/json", extra_headers=None):
        headers = dict(extra_headers or {})
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        if origin is not None:
            headers["Origin"] = origin
        if content_type is not None and method == "POST":
            headers["Content-Type"] = content_type
        if body is not None and not isinstance(body, bytes):
            body = json.dumps(body).encode()
        if method == "POST" and body is None:
            body = b""
        req = urllib.request.Request(
            self.base + path, data=body, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=5) as response:
                text = response.read()
                payload = json.loads(text) if text else None
                return response.status, payload, response.headers
        except urllib.error.HTTPError as error:
            try:
                text = error.read()
                payload = json.loads(text) if text else None
                return error.code, payload, error.headers
            finally:
                error.close()

    def counts(self):
        with self.store.connect() as db:
            return tuple(db.execute(
                "SELECT count(*) FROM " + table
            ).fetchone()[0] for table in ("sessions", "events", "jobs"))

    def assert_denied_no_side_effect(self, status, path, **kwargs):
        before = self.counts()
        code, body, headers = self.request(path, **kwargs)
        self.assertEqual(code, status)
        self.assertEqual(self.counts(), before)
        self.assertEqual(headers.get("Cache-Control"), "no-store")
        self.assertEqual(headers.get("X-Content-Type-Options"), "nosniff")
        self.assertEqual(headers.get_content_type(), "application/json")
        self.assertIsInstance(body.get("error"), str)
        self.assertNotIn(self.token, json.dumps(body))
        self.assertNotIn(self.other_token, json.dumps(body))
        return headers

    def test_bearer_required_even_on_loopback_health_and_opaque_query(self):
        for path in (
            "/v1/health", "/v1/inbox?after=0", "/v1/sessions",
            "/v1/health?token=synthetic-query-only",
        ):
            with self.subTest(path=path):
                self.assert_denied_no_side_effect(401, path)
                self.assert_denied_no_side_effect(401, path, token="wrong")
        status, payload, _ = self.request("/v1/health", token=self.token)
        self.assertEqual(status, 200)
        self.assertTrue(payload["ok"])
        self.assertEqual(self.counts(), (1, 0, 0))

    def test_revocation_blocks_fresh_requests_and_origin_not_auth(self):
        self.assertEqual(self.request("/v1/health", token=self.token)[0], 200)
        self.store.revoke_token(self.token)
        self.assert_denied_no_side_effect(401, "/v1/health", token=self.token)
        self.assert_denied_no_side_effect(
            401, "/v1/inbox?after=0", token=self.token,
            origin=ALLOWED_ORIGIN)
        self.assertEqual(self.request("/v1/health", token=self.other_token)[0], 200)

    def test_origin_exact_match_preflight_and_no_wildcard(self):
        # Allowed browser origin is a separate control from bearer authorization.
        code, _, headers = self.request(
            "/v1/health", token=self.token, origin=ALLOWED_ORIGIN)
        self.assertEqual(code, 200)
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), ALLOWED_ORIGIN)
        self.assertEqual(headers.get("Vary"), "Origin")
        for origin in (OTHER_ORIGIN, "null", ALLOWED_ORIGIN + ".attacker.invalid"):
            with self.subTest(origin=origin):
                headers = self.assert_denied_no_side_effect(
                    403, "/v1/health", token=self.token, origin=origin)
                self.assertIsNone(headers.get("Access-Control-Allow-Origin"))
                self.assert_denied_no_side_effect(
                    403, "/v1/health", origin=origin)
                self.assertEqual(self.request(
                    "/v1/health", method="OPTIONS", origin=origin)[0], 403)
        status, data, headers = self.request(
            "/v1/health", method="OPTIONS", origin=ALLOWED_ORIGIN)
        self.assertEqual((status, data), (204, None))
        self.assertEqual(headers.get("Access-Control-Allow-Origin"), ALLOWED_ORIGIN)
        self.assertNotIn("*", headers.get("Access-Control-Allow-Origin"))
        self.assertIn("Authorization", headers.get("Access-Control-Allow-Headers"))
        self.assert_denied_no_side_effect(
            401, "/v1/health", origin=ALLOWED_ORIGIN)

    def test_cross_device_session_and_tools_not_found(self):
        self.assert_denied_no_side_effect(
            404, "/v1/context?session_id=" + self.sid,
            token=self.other_token)
        self.assert_denied_no_side_effect(
            404, "/v1/tools/tell_hermes", method="POST",
            body={"session_id": self.sid, "arguments": {
                "text": "should never enqueue", "kind": "request",
            }}, token=self.other_token)
        status, payload, _ = self.request("/v1/sessions", token=self.other_token)
        self.assertEqual(status, 200)
        self.assertEqual(payload, {"sessions": []})
        self.assertEqual(self.counts(), (1, 0, 0))

    def test_body_content_type_json_and_length_preconditions(self):
        path = "/v1/events"
        for body, ctype in (
            (b"", "application/json"),
            (b"not JSON", "application/json"),
            (b"{", "application/json"),
            ({"events": []}, "application/json"),  # legacy envelope invalid
            ({"anything": "wrong"}, "application/json"),
            ([{"session_id": self.sid, "type": "invalid"}], "application/json"),
            ([], "text/plain"),
            ([], "application/octet-stream"),
            ([{"session_id": self.sid, "type": "user_transcript",
               "text": "synthetic"}], None),
        ):
            with self.subTest(body=repr(body)[:80], ctype=ctype):
                self.assert_denied_no_side_effect(
                    400, path, method="POST", body=body, token=self.token,
                    content_type=ctype)
        too_big = b" " * 1_000_001
        self.assert_denied_no_side_effect(
            400, path, method="POST", body=too_big, token=self.token)

    def test_mint_tool_count_and_text_limit_preflight_no_side_effect(self):
        cases = [
            ("/v1/realtime/session", {"client": "bad"}),
            ("/v1/realtime/session", {"client": "web", "context": {
                "synthetic": "x" * 16001,
            }}),
            ("/v1/realtime/session", {"client": "web", "tools": [
                {"type": "function", "name": "local_" + str(i),
                 "parameters": {"type": "object"}} for i in range(33)
            ]}),
            ("/v1/events", [{
                "session_id": self.sid, "type": "user_transcript",
                "text": "synthetic " + str(i),
            } for i in range(101)]),
            ("/v1/tools/tell_hermes", {"session_id": self.sid,
                "arguments": {"text": "x" * 16001, "kind": "request"}}),
            ("/v1/tools/tell_hermes", {"session_id": self.sid,
                "arguments": "not-object"}),
        ]
        for path, body in cases:
            with self.subTest(path=path, detail=repr(body)[:50]):
                self.assert_denied_no_side_effect(
                    400, path, method="POST", body=body, token=self.token)
        self.assertEqual(self.counts(), (1, 0, 0))

    def test_sse_limit_and_log_redaction_of_query_path(self):
        self.server.sse_clients = 16
        self.assert_denied_no_side_effect(
            429, "/v1/stream?after=0", token=self.token)
        self.server.sse_clients = 0
        with patch("z0live_desktop.server.LOG.info") as sink:
            self.assertEqual(self.request(
                "/v1/health?token=synthetic-query-secret", token=self.token)[0],
                200)
            for call in sink.call_args_list:
                joined = repr(call)
                self.assertNotIn("synthetic-query-secret", joined)
                self.assertNotIn(self.token, joined)


if __name__ == "__main__":
    unittest.main()
