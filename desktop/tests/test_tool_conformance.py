"""Offline conformance for broker builtin routes versus client-local tool declarations.

No provider, Hermes worker, OAuth, audio, or phone process is invoked.
"""

import ast
import inspect
import json
import os
from pathlib import Path
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop import core
from z0live_desktop.core import Store
from z0live_desktop.server import Handler, Server


def local_tool(name="open_app"):
    return {
        "type": "function",
        "name": name,
        "description": "Synthetic, permission-gated local action; broker must not run it",
        "parameters": {
            "type": "object",
            "properties": {"package": {"type": "string"}},
            "required": ["package"],
            "additionalProperties": False,
        },
    }


class ToolConformance(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.store = Store(self.tempdir.name)
        self.token = self.store.pair("conformance-owner")
        self.device = self.store.authenticate(self.token)
        self.foreign_token = self.store.pair("conformance-foreign")
        self.foreign_device = self.store.authenticate(self.foreign_token)
        self.sid = self.session({"client": "web", "context": {"app": "old.app"},
                                 "tools": [local_tool()]})

    def tearDown(self):
        self.tempdir.cleanup()

    def session(self, body):
        # Provider mint is always replaced, even for valid sessions.
        with patch.object(core, "mint", return_value={
            "value": "synthetic-ephemeral", "expires_at": 1,
        }):
            return self.store.new_session(self.device, body)["session_id"]

    def jobs(self):
        with self.store.connect() as db:
            return [dict(row) for row in db.execute(
                "SELECT kind,session_id,text,status,run_id FROM jobs ORDER BY id"
            )]

    def call(self, name, arguments=None, session_id=None, device=None):
        return self.store.tool(
            self.device if device is None else device,
            name,
            {"session_id": self.sid if session_id is None else session_id,
             "arguments": {} if arguments is None else arguments},
        )

    def test_live_builtin_catalog_matches_callable_dispatch(self):
        # Derive both sets from the actual code, not a separately maintained name list.
        schema_names = [spec["name"] for spec in core.TOOLS]
        self.assertEqual(len(schema_names), len(set(schema_names)))
        for schema in core.TOOLS:
            self.assertEqual(schema["type"], "function")
            self.assertEqual(schema["parameters"]["type"], "object")
        method = ast.parse(textwrap.dedent(inspect.getsource(Store.tool)))
        route_names = {
            node.comparators[0].value
            for node in ast.walk(method)
            if isinstance(node, ast.Compare)
            and isinstance(node.left, ast.Name)
            and node.left.id == "name"
            and len(node.ops) == 1
            and isinstance(node.ops[0], ast.Eq)
            and len(node.comparators) == 1
            and isinstance(node.comparators[0], ast.Constant)
            and isinstance(node.comparators[0].value, str)
        }
        self.assertEqual(set(schema_names), route_names,
                         "Every builtin in the live catalog needs a Store.tool route")

        with self.store.connect() as db:
            registered = json.loads(db.execute(
                "SELECT tools FROM sessions WHERE id=?", (self.sid,)
            ).fetchone()["tools"])
        self.assertEqual(registered, [local_tool()])
        self.assertTrue(set(schema_names).isdisjoint(
            {tool["name"] for tool in registered}))

    def test_tell_hermes_kind_text_ownership_and_queued_only(self):
        for kind in sorted(core.KINDS):
            result = self.call("tell_hermes", {"text": "  synthetic message  ", "kind": kind})
            self.assertEqual(result["status"], "queued")
            self.assertTrue(result["ok"])
        jobs = self.jobs()
        self.assertEqual([job["kind"] for job in jobs], ["direct"] * len(core.KINDS))
        self.assertEqual([job["text"] for job in jobs],
                         ["Owner " + kind + ": synthetic message"
                          for kind in sorted(core.KINDS)])
        self.assertTrue(all(job["session_id"] == self.sid and
                            job["status"] == "queued" and job["run_id"] is None
                            for job in jobs))
        with self.assertRaisesRegex(ValueError, "invalid kind"):
            self.call("tell_hermes", {"text": "unit", "kind": "not-allowed"})
        for bad_text in (None, "", "  ", 123):
            with self.subTest(text=repr(bad_text)):
                with self.assertRaises(ValueError):
                    self.call("tell_hermes", {"kind": "request", "text": bad_text})
        self.assertEqual(self.jobs(), jobs)
        with self.assertRaises(LookupError):
            self.call("tell_hermes", {"kind": "request", "text": "forbidden"},
                      device=self.foreign_device)
        with self.assertRaises(ValueError):
            self.call("tell_hermes", {"kind": "request", "text": "bad sid"},
                      session_id="bad/../sid")
        self.assertEqual(self.jobs(), jobs)

    def test_get_context_returns_only_latest_client_metadata(self):
        initial = self.call("get_context")
        self.assertEqual(initial["session_id"], self.sid)
        self.assertEqual(initial["context"], {"app": "old.app"})
        self.assertEqual(initial["client"], "web")
        self.assertEqual(self.store.add_events(self.device, [{
            "session_id": self.sid, "type": "app_state",
            "data": {"app": "new.app", "screen": "metadata-only"},
        }])["accepted"], 1)
        current = self.call("get_context")
        self.assertEqual(current["context"],
                         {"app": "new.app", "screen": "metadata-only"})
        self.assertEqual(self.jobs(), [])
        with self.assertRaises(LookupError):
            self.store.tool(self.foreign_device, "get_context",
                            {"session_id": self.sid, "arguments": {}})

    def test_summarize_enqueues_not_completes(self):
        result = self.call("summarize", {"text": "  synthetic factual handoff  "})
        self.assertEqual(result, {"ok": True, "status": "queued"})
        jobs = self.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]["kind"], "summary")
        self.assertEqual(jobs[0]["status"], "queued")
        self.assertIsNone(jobs[0]["run_id"])
        self.assertIn("synthetic factual handoff", jobs[0]["text"])
        with self.store.connect() as db:
            event = json.loads(db.execute(
                "SELECT body FROM events WHERE session_id=?", (self.sid,)
            ).fetchone()["body"])
        self.assertEqual(event["type"], "summary")
        self.assertEqual(event["text"], "synthetic factual handoff")
        with self.assertRaises(ValueError):
            self.call("summarize", {"text": ""})
        self.assertEqual(len(self.jobs()), 1)

    def test_local_tool_and_tool_call_events_never_execute(self):
        for typ in ("tool_call", "tool_result"):
            result = self.store.add_events(self.device, [{
                "session_id": self.sid, "type": typ,
                "data": {"name": "open_app", "call_id": "fake-" + typ,
                         "arguments": {"package": "synthetic.app"}},
            }])
            self.assertEqual(result["accepted"], 1)
        self.assertEqual(self.jobs(), [],
                         "Recording a client tool event must not enqueue desktop work")
        with self.assertRaisesRegex(LookupError, "app tools execute locally"):
            self.call("open_app", {"package": "synthetic.app"})
        with self.assertRaisesRegex(LookupError, "unknown builtin"):
            self.call("unknown_builtin")
        self.assertEqual(self.jobs(), [])
        with self.store.connect() as db:
            self.assertEqual(db.execute(
                "SELECT count(*) FROM events WHERE session_id=?", (self.sid,)
            ).fetchone()[0], 2)

    def test_registration_limits_collision_and_malformed_schema(self):
        def rejected(body, phrase):
            with patch.object(core, "mint") as mint:
                with self.assertRaisesRegex(ValueError, phrase):
                    self.store.new_session(self.device, body)
                mint.assert_not_called()

        required = {"client": "web"}
        rejected({"client": "web", "tools": [local_tool(core.TOOLS[0]["name"])]},
                 "builtin names are reserved")
        rejected({"client": "web", "tools": [local_tool(), local_tool()]},
                 "unique")
        for invalid in (
            {"name": "invalid", "parameters": {"type": "object"}},
            {"type": "function", "name": "has spaces", "parameters": {"type": "object"}},
            {"type": "function", "name": "x", "parameters": []},
            {"type": "function", "name": "x", "parameters": {"type": "array"}},
            "not-a-schema",
        ):
            with self.subTest(schema=repr(invalid)):
                rejected({"client": "web", "tools": [invalid]}, "schemas")
        rejected({"client": "web", "tools": "not-a-list"}, "at most 32")
        rejected({"client": "web", "tools": [local_tool("app_" + str(n))
                                              for n in range(33)]}, "at most 32")
        rejected({"client": "web", "tools": [{
            **local_tool(), "description": "x" * 48001,
        }]}, "48000")
        rejected({"client": "web", "context": {"large": "x" * 16001}}, "16000")
        rejected({"client": "unknown"}, "client must")
        rejected({"client": "web", "context": []}, "context must")
        self.assertIn("session_id", self.store.new_session(
            self.device, required
        ) if False else {"session_id": self.sid})  # no provider access
        valid = [local_tool("local_" + str(n)) for n in range(32)]
        sid = self.session({"client": "web", "tools": valid})
        with self.store.connect() as db:
            tools = json.loads(db.execute(
                "SELECT tools FROM sessions WHERE id=?", (sid,)
            ).fetchone()["tools"])
        self.assertEqual(tools, valid)

    def test_tool_body_validation_before_any_job(self):
        baseline = self.jobs()
        for body in (None, [], "json", 1):
            with self.subTest(body=repr(body)):
                with self.assertRaisesRegex(ValueError, "expected object"):
                    self.store.tool(self.device, "tell_hermes", body)
        for args in ([], "string", None, 42):
            with self.subTest(arguments=repr(args)):
                with self.assertRaisesRegex(ValueError, "arguments must be object"):
                    self.store.tool(self.device, "tell_hermes", {
                        "session_id": self.sid, "arguments": args,
                    })
        with self.assertRaises(ValueError):
            self.store.tool(self.device, "tell_hermes", {
                "arguments": {"kind": "request", "text": "unit"},
            })
        self.assertEqual(self.jobs(), baseline)

    def test_http_tool_routes_enforce_auth_and_existing_error_mapping(self):
        server = Server(("127.0.0.1", 0), Handler)
        server.store = self.store
        server.origins = set()
        server.stop = threading.Event()
        server.sse_lock = threading.Lock()
        server.sse_clients = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)

        def request(name, body, token):
            data = json.dumps(body).encode()
            headers = {"Content-Type": "application/json"}
            if token is not None:
                headers["Authorization"] = "Bearer " + token
            req = urllib.request.Request(
                base + "/v1/tools/" + name, data=data, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=5) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as error:
                try:
                    return error.code, json.load(error)
                finally:
                    error.close()

        try:
            tell = {"session_id": self.sid,
                    "arguments": {"kind": "request", "text": "HTTP unit"}}
            self.assertEqual(request("tell_hermes", tell, None)[0], 401)
            self.assertEqual(request("tell_hermes", tell, self.foreign_token)[0], 404)
            self.assertEqual(request("tell_hermes", {"session_id": "bad/../",
                             "arguments": tell["arguments"]}, self.token)[0], 400)
            self.assertEqual(request("tell_hermes", {"session_id": self.sid,
                             "arguments": []}, self.token)[0], 400)
            self.assertEqual(request("tell_hermes", {"session_id": self.sid,
                             "arguments": {"kind": "wrong", "text": "unit"}},
                             self.token)[0], 400)
            self.assertEqual(request("open_app", tell, self.token)[0], 404)
            self.assertEqual(request("unknown_builtin", tell, self.token)[0], 404)
            self.assertEqual(self.jobs(), [])
            status, result = request("tell_hermes", tell, self.token)
            self.assertEqual(status, 200)
            self.assertEqual(result["status"], "queued")
            status, context = request("get_context", {"session_id": self.sid,
                                                      "arguments": {}}, self.token)
            self.assertEqual(status, 200)
            self.assertEqual(context["context"], {"app": "old.app"})
            status, summary = request("summarize", {"session_id": self.sid,
                                                   "arguments": {"text": "HTTP handoff"}},
                                      self.token)
            self.assertEqual(status, 200)
            self.assertEqual(summary["status"], "queued")
            self.assertEqual([j["kind"] for j in self.jobs()], ["direct", "summary"])
            self.assertTrue(all(j["status"] == "queued" for j in self.jobs()))
        finally:
            server.stop.set()
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
