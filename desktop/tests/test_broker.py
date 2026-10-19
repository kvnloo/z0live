"""Deterministic unit/HTTP tests (provider mint mocked explicitly, no live claims)."""

import concurrent.futures
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop.core import Store
from z0live_desktop.server import Server, Handler


class BrokerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.store = Store(self.tmp.name)
        self.token = self.store.pair("unit device")
        self.device = self.store.authenticate(self.token)
        self.other = self.store.authenticate(self.store.pair("other device"))
        with patch(
            "z0live_desktop.core.mint",
            return_value={"value": "explicitly-mocked-secret", "expires_at": 1},
        ):
            self.sid = self.store.new_session(
                self.device,
                {
                    "client": "web",
                    "tools": [
                        {
                            "type": "function",
                            "name": "open_app",
                            "parameters": {"type": "object"},
                        }
                    ],
                },
            )["session_id"]

    def tearDown(self):
        self.tmp.cleanup()

    def event(self, **extra):
        return {
            "session_id": self.sid,
            "type": "user_transcript",
            "text": "unit transcript",
            "ts": time.time(),
            **extra,
        }

    def test_pair_hash_only_private_and_ownership(self):
        devices = Path(self.tmp.name) / "devices.json"
        self.assertEqual(devices.stat().st_mode & 0o777, 0o600)
        self.assertNotIn(self.token, devices.read_text())
        self.assertIsNone(self.store.authenticate("wrong"))
        self.assertEqual(self.store.sessions(self.other), {"sessions": []})
        with self.assertRaises(LookupError):
            self.store.context(self.other, self.sid)
        with self.assertRaises(ValueError):
            self.store.context(self.device, "../../bad")

    def test_app_tool_registration_validation(self):
        with self.assertRaises(ValueError):
            self.store.new_session(
                self.device,
                {
                    "client": "web",
                    "tools": [
                        {
                            "type": "function",
                            "name": "tell_hermes",
                            "parameters": {"type": "object"},
                        }
                    ],
                },
            )
        with self.assertRaises(ValueError):
            self.store.new_session(self.device, {"client": "invalid"})

    def test_batch_atomic_dedupe_and_jsonl(self):
        event = self.event(data={"event_id": "retry-id"})
        self.assertEqual(self.store.add_events(self.device, [event])["accepted"], 1)
        self.assertEqual(self.store.add_events(self.device, [event])["accepted"], 0)
        with self.assertRaises(ValueError):
            self.store.add_events(
                self.device, [self.event(), self.event(type="invalid")]
            )
        path = Path(self.tmp.name) / "sessions" / (self.sid + ".jsonl")
        self.assertEqual(len(path.read_text().splitlines()), 1)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        # Simulate append completed but SQLite checkpoint not committed.
        with self.store.connect() as db:
            db.execute("UPDATE events SET exported=0")
        self.store.export_events()
        self.assertEqual(len(path.read_text().splitlines()), 1)
        with path.open("ab") as f:
            f.write(b'{"incomplete":')
        self.store.add_events(self.device, [self.event()])
        self.assertEqual(
            len([json.loads(line) for line in path.read_text().splitlines()]), 2
        )

    def test_tool_dispatch_feedback_summary_and_end(self):
        self.store.add_events(
            self.device,
            [
                self.event(),
                self.event(type="feedback", text="unit feedback"),
                self.event(type="app_state", data={"app": "unit"}),
            ],
        )
        result = self.store.tool(
            self.device, "get_context", {"session_id": self.sid, "arguments": {}}
        )
        self.assertEqual(result["context"]["app"], "unit")
        result = self.store.tool(
            self.device,
            "tell_hermes",
            {
                "session_id": self.sid,
                "arguments": {"text": "unit delegation", "kind": "praise"},
            },
        )
        self.assertEqual(result["status"], "queued")
        self.store.end(self.device, self.sid)
        self.store.end(self.device, self.sid)
        with self.store.connect() as db:
            rows = db.execute("SELECT kind,text FROM jobs ORDER BY id").fetchall()
        self.assertEqual(
            [r["kind"] for r in rows], ["feedback", "feedback_note", "summary"]
        )
        self.assertIn("unit transcript", rows[-1]["text"])
        self.assertIn("unit feedback", rows[-1]["text"])
        with self.assertRaises(ValueError):
            self.store.add_events(self.device, [self.event()])

    def test_summary_active_and_idle(self):
        self.store.add_events(self.device, [self.event()])
        with self.store.connect() as db:
            db.execute("UPDATE sessions SET active=301 WHERE id=?", (self.sid,))
        self.store.tick_summaries()
        self.store.tick_summaries()
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM jobs WHERE kind='summary'").fetchone()[
                    0
                ],
                1,
            )
        self.store.add_events(self.device, [self.event(text="after first summary")])
        with self.store.connect() as db:
            db.execute(
                "UPDATE sessions SET updated=? WHERE id=?",
                (time.time() - 121, self.sid),
            )
        self.store.tick_summaries()
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM jobs WHERE kind='summary'").fetchone()[
                    0
                ],
                2,
            )

    def test_inbox_order_dedupe_device_scope(self):
        def publish(i):
            return self.store.publish(str(i), sid=self.sid, source="test:" + str(i))

        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(publish, range(12)))
        self.store.publish("duplicate", sid=self.sid, source="test:0")
        inbox = self.store.inbox(self.device)
        self.assertEqual(len(inbox["messages"]), 12)
        self.assertEqual(self.store.inbox(self.other)["messages"], [])
        self.assertEqual(self.store.inbox(self.device, inbox["after"])["messages"], [])

    def test_inbox_contract_pagination_and_empty_cursor(self):
        self.assertEqual(
            self.store.inbox(self.device),
            {"messages": [], "after": 0, "has_more": False},
        )
        ids = [self.store.publish(str(i), sid=self.sid) for i in range(101)]
        first = self.store.inbox(self.device)
        self.assertEqual(set(first), {"messages", "after", "has_more"})
        self.assertEqual([m["id"] for m in first["messages"]], ids[:100])
        self.assertEqual(first["after"], ids[99])
        self.assertIs(first["has_more"], True)
        self.assertEqual(
            first["messages"][0],
            {
                "id": ids[0],
                "say": "0",
                "action": None,
                "params": {},
                "session_id": self.sid,
            },
        )
        second = self.store.inbox(self.device, first["after"])
        self.assertEqual([m["id"] for m in second["messages"]], ids[100:])
        self.assertEqual(second["after"], ids[-1])
        self.assertIs(second["has_more"], False)
        self.assertEqual(
            self.store.inbox(self.device, second["after"]),
            {"messages": [], "after": ids[-1], "has_more": False},
        )
        with self.assertRaises(ValueError):
            self.store.publish("invalid params", params=[])

    def test_native_worker_restart_idempotency(self):
        from z0live_desktop.worker import Worker

        self.store.send(self.device, {"session_id": self.sid, "text": "unit direct"})
        calls = []

        class FakeWorker(Worker):
            def api(inner, path, body=None, idem=None):
                calls.append((path, body, idem))
                if path == "/v1/runs":
                    return {"run_id": "unit-run"}
                return {
                    "status": "completed",
                    "output": "unit reply",
                    "session_id": "z0live",
                }

        FakeWorker(self.store, threading.Event()).tick()
        FakeWorker(self.store, threading.Event()).tick()
        self.assertEqual(sum(path == "/v1/runs" for path, _, _ in calls), 1)
        with self.store.connect() as db:
            persisted_key = db.execute(
                "SELECT native_key FROM jobs WHERE id=1"
            ).fetchone()[0]
        self.assertEqual(calls[0][2], persisted_key)
        self.assertTrue(persisted_key.startswith("z0live-"))
        self.assertTrue(persisted_key.endswith("-job-1"))
        self.assertEqual(
            self.store.inbox(self.device)["messages"][0]["say"], "unit reply"
        )

    def test_http_auth_health_events_history_stream(self):
        server = Server(("127.0.0.1", 0), Handler)
        server.store = self.store
        server.origins = set()
        server.stop = threading.Event()
        server.sse_lock = threading.Lock()
        server.sse_clients = 0
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = "http://127.0.0.1:" + str(server.server_port)

        def req(path, body=None, auth=True, headers=None):
            request = urllib.request.Request(
                base + path,
                data=json.dumps(body).encode() if body is not None else None,
                headers={
                    "Content-Type": "application/json",
                    **({"Authorization": "Bearer " + self.token} if auth else {}),
                    **(headers or {}),
                },
            )
            return urllib.request.urlopen(request, timeout=5)

        try:
            with self.assertRaises(urllib.error.HTTPError) as err:
                req("/v1/health", auth=False)
            self.assertEqual(err.exception.code, 401)
            err.exception.close()
            with req("/v1/health") as r:
                self.assertTrue(json.load(r)["ok"])
            with req("/v1/inbox?after=0") as r:
                self.assertEqual(r.headers.get_content_type(), "application/json")
                self.assertEqual(
                    json.load(r), {"messages": [], "after": 0, "has_more": False}
                )
            with req("/v1/events", [self.event()]) as r:
                self.assertEqual(json.load(r)["accepted"], 1)
            with req("/v1/sessions") as r:
                self.assertEqual(len(json.load(r)["sessions"]), 1)
            message_id = self.store.publish("unit SSE", sid=self.sid)
            with req("/v1/inbox?after=0") as r:
                self.assertEqual(
                    json.load(r),
                    {
                        "messages": [
                            {
                                "id": message_id,
                                "say": "unit SSE",
                                "action": None,
                                "params": {},
                                "session_id": self.sid,
                            }
                        ],
                        "after": message_id,
                        "has_more": False,
                    },
                )
            with req("/v1/stream") as r:
                lines = [r.readline().decode() for _ in range(5)]
                self.assertTrue(any("unit SSE" in line for line in lines))
            with self.assertRaises(urllib.error.HTTPError) as err:
                req("/v1/health", headers={"Origin": "https://evil.example"})
            self.assertEqual(err.exception.code, 403)
            err.exception.close()
        finally:
            server.stop.set()
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == "__main__":
    unittest.main()
