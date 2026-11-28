"""Authenticated HTTP inbox/SSE cursor parity; synthetic loopback data only."""

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


class InboxStreamConformance(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.store = Store(self.temp.name)
        self.token = self.store.pair("synthetic-owner")
        self.device = self.store.authenticate(self.token)
        self.other_token = self.store.pair("synthetic-other")
        self.other_device = self.store.authenticate(self.other_token)
        with patch.object(core, "mint", return_value={
            "value": "synthetic-ephemeral", "expires_at": 1,
        }):
            self.sid = self.store.new_session(
                self.device, {"client": "web"})["session_id"]
        self.server = Server(("127.0.0.1", 0), Handler)
        self.server.store = self.store
        self.server.origins = set()
        self.server.stop = threading.Event()
        self.server.sse_lock = threading.Lock()
        self.server.sse_clients = 0
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)

    def tearDown(self):
        self.server.stop.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
        self.temp.cleanup()

    def make_request(self, path, token=None, headers=None):
        merged = dict(headers or {})
        if token is not None:
            merged["Authorization"] = "Bearer " + token
        return urllib.request.Request(self.base + path, headers=merged)

    def json_get(self, path, token=None, headers=None):
        request = self.make_request(path, token, headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.load(response)
        except urllib.error.HTTPError as error:
            try:
                return error.code, json.load(error)
            finally:
                error.close()

    def first_sse(self, path="/v1/stream?after=0", token=None, headers=None):
        # Socket timeout and finite line budget prevent waiting for 15s heartbeats.
        request = self.make_request(path, token or self.token, headers)
        with urllib.request.urlopen(request, timeout=5) as response:
            self.assertEqual(response.status, 200)
            self.assertEqual(response.headers.get_content_type(), "text/event-stream")
            current = {}
            for _ in range(16):
                line = response.readline().decode("utf-8").rstrip("\r\n")
                if line.startswith("id: "):
                    current["id"] = int(line[4:])
                elif line.startswith("event: "):
                    current["event"] = line[7:]
                elif line.startswith("data: "):
                    current["data"] = json.loads(line[6:])
                elif not line and "data" in current:
                    return current
            self.fail("No complete SSE message inside bounded line budget")

    def test_103_messages_page_contract_and_empty_cursor(self):
        status, empty = self.json_get("/v1/inbox?after=0", self.token)
        self.assertEqual(status, 200)
        self.assertEqual(empty, {"messages": [], "after": 0, "has_more": False})
        ids = [self.store.publish("synthetic " + str(i), sid=self.sid,
                                  source="page-" + str(i))
               for i in range(103)]
        status, first = self.json_get("/v1/inbox?after=0", self.token)
        self.assertEqual(status, 200)
        self.assertEqual(set(first), {"messages", "after", "has_more"})
        self.assertEqual([item["id"] for item in first["messages"]], ids[:100])
        self.assertEqual(first["after"], ids[99])
        self.assertIs(first["has_more"], True)
        self.assertEqual(first["messages"][0], {
            "id": ids[0], "say": "synthetic 0", "action": None,
            "params": {}, "session_id": self.sid,
        })
        status, second = self.json_get("/v1/inbox?after=" + str(first["after"]), self.token)
        self.assertEqual(status, 200)
        self.assertEqual([item["id"] for item in second["messages"]], ids[100:])
        self.assertEqual(second["after"], ids[-1])
        self.assertIs(second["has_more"], False)
        status, empty = self.json_get("/v1/inbox?after=" + str(second["after"]), self.token)
        self.assertEqual(status, 200)
        self.assertEqual(empty, {
            "messages": [], "after": ids[-1], "has_more": False,
        })

    def test_broadcast_device_scope_nullable_action_and_metadata(self):
        broadcast = self.store.publish(
            "synthetic broadcast", source="broadcast-synthetic")
        owner = self.store.publish(
            "synthetic owner", sid=self.sid, source="owner-synthetic")
        action = self.store.publish(
            "synthetic optional action", sid=self.sid, action="open_app",
            params={"package": "synthetic.example"}, source="action-synthetic")
        _, owner_page = self.json_get("/v1/inbox?after=0", self.token)
        _, other_page = self.json_get("/v1/inbox?after=0", self.other_token)
        self.assertEqual([m["id"] for m in owner_page["messages"]],
                         [broadcast, owner, action])
        self.assertEqual([m["id"] for m in other_page["messages"]], [broadcast])
        self.assertEqual(owner_page["messages"][0]["action"], None)
        self.assertIsNone(owner_page["messages"][0]["session_id"])
        self.assertEqual(owner_page["messages"][1]["session_id"], self.sid)
        self.assertEqual(owner_page["messages"][2]["action"], "open_app")
        self.assertEqual(owner_page["messages"][2]["params"],
                         {"package": "synthetic.example"})
        self.assertEqual(self.store.inbox(self.device)["messages"],
                         owner_page["messages"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0,
                             "Delivering an action is not executing it")

    def test_sse_id_data_page_parity_and_reconnect_cursor(self):
        first_id = self.store.publish("synthetic first", sid=self.sid, source="stream-1")
        second_id = self.store.publish(
            "synthetic second", sid=self.sid, source="stream-2")
        _, page = self.json_get("/v1/inbox?after=0", self.token)
        first = self.first_sse()
        self.assertEqual(first["event"], "message")
        self.assertEqual(first["id"], first_id)
        self.assertEqual(first["data"], page["messages"][0])
        self.assertEqual(first["data"]["id"], first_id)

        resumed = self.first_sse(
            "/v1/stream", headers={"Last-Event-ID": str(first_id)})
        self.assertEqual(resumed["id"], second_id)
        self.assertEqual(resumed["data"], page["messages"][1])
        explicit = self.first_sse(
            "/v1/stream?after=0", headers={"Last-Event-ID": str(first_id)})
        self.assertEqual(explicit["id"], first_id,
                         "Explicit after query overrides Last-Event-ID header")
        duplicate = self.first_sse("/v1/stream?after=0")
        self.assertEqual(duplicate["id"], first_id,
                         "Transport replay is at-least-once, not action dedupe")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 0)

    def test_auth_origin_revocation_and_query_token_not_auth(self):
        for path in ("/v1/inbox", "/v1/stream"):
            with self.subTest(path=path):
                self.assertEqual(self.json_get(path)[0], 401)
                self.assertEqual(self.json_get(
                    path + "?token=synthetic-not-a-bearer")[0], 401)
                self.assertEqual(self.json_get(
                    path, self.token, {"Origin": "https://not-allowed.invalid"})[0],
                    403)
        self.assertEqual(self.json_get("/v1/inbox", self.token)[0], 200)
        self.store.revoke_token(self.token)
        self.assertEqual(self.json_get("/v1/inbox", self.token)[0], 401)
        self.assertEqual(self.json_get("/v1/stream", self.token)[0], 401)
        self.assertEqual(self.json_get("/v1/inbox", self.other_token)[0], 200)
        # Existing open SSE streams recheck only at a heartbeat; no 15-second
        # assertion, wall-clock wait, or immediate-revocation claim here.


if __name__ == "__main__":
    unittest.main()
