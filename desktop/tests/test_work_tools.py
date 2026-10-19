"""Deterministic voice tool fixtures. No provider, GitHub or user-history calls."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
import json
import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop.core import Store, TOOLS
from z0live_desktop import work_sources as sources
from z0live_desktop.worker import Worker
from z0live_desktop.delivery import Delivery


FIXTURE = {
    "items": [
        {
            "source": "fixture",
            "project": "garden_room",
            "date": "2026-10-18",
            "session": "unit-session",
            "what": "Added a screenshot test.",
            "status": "committed",
            "citation": "fixture:commit=123",
        }
    ],
    "gaps": [],
}


class VoiceToolsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.store = Store(self.tmp.name)
        self.device = self.store.authenticate(self.store.pair("fixture"))
        self.other = self.store.authenticate(self.store.pair("other"))
        with patch(
            "z0live_desktop.core.mint", return_value={"value": "mock", "expires_at": 1}
        ):
            self.sid = self.store.new_session(self.device, {"client": "web"})[
                "session_id"
            ]
        self.work = self.store.work
        self.env = patch.dict(
            os.environ,
            Z0LIVE_SEARCH_SECONDS="0.12",
            Z0LIVE_ASK_SECONDS="0.12",
            Z0LIVE_TELEGRAM_TARGET="",
        )
        self.env.start()

    def tearDown(self):
        self.work.pool.shutdown(wait=True)
        self.env.stop()
        self.tmp.cleanup()

    def call(self, name, args):
        return self.store.tool(
            self.device, name, {"session_id": self.sid, "arguments": args}
        )

    def wait(self, ident):
        for _ in range(100):
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT * FROM work_lookups WHERE id=?", (ident,)
                ).fetchone()
            if row["status"] == "completed":
                return dict(row)
            time.sleep(0.02)
        self.fail("lookup did not complete")

    def test_every_builtin_registered_and_reserved(self):
        names = {t["name"] for t in TOOLS}
        self.assertTrue(
            {"work_search", "ask_hermes", "start_task", "task_status", "what_changed"}
            <= names
        )
        for name in names:
            with self.assertRaises(ValueError):
                self.store.new_session(
                    self.device,
                    {
                        "client": "web",
                        "tools": [
                            {
                                "type": "function",
                                "name": name,
                                "parameters": {"type": "object"},
                            }
                        ],
                    },
                )

    def test_validation_and_ownership(self):
        for name, args in [
            ("work_search", {"query": ""}),
            ("work_search", {"query": "x", "scope": "other"}),
            ("start_task", {"title": "x"}),
            ("task_status", {"extra": "x"}),
            ("what_changed", {"since": "forever"}),
            ("ask_hermes", {"question": 3}),
        ]:
            with self.assertRaises(ValueError):
                self.call(name, args)
        with self.assertRaises(LookupError):
            self.store.tool(
                self.other,
                "ask_hermes",
                {"session_id": self.sid, "arguments": {"question": "x"}},
            )

    def test_work_search_fast_merge_citations_deduplicated(self):
        with patch.multiple(
            sources,
            z0=lambda **kw: FIXTURE,
            hermes=lambda **kw: FIXTURE,
            ctx=lambda **kw: FIXTURE,
        ):
            result = self.call("work_search", {"query": "screenshot"})
            self.assertFalse(result["partial"])
            self.assertEqual(len(result["items"]), 1)
            self.assertEqual(
                set(result["sources"]), {"z0intelligence", "hermes", "ctx"}
            )
            self.assertEqual(result["items"][0]["citation"], "fixture:commit=123")
            self.assertEqual(self.store.inbox(self.device)["messages"], [])

    def test_slow_lookup_returns_partial_then_exact_device_inbox(self):
        def slow(**kw):
            time.sleep(0.25)
            return {
                "items": [
                    {**FIXTURE["items"][0], "citation": "fixture:slow", "source": "ctx"}
                ]
            }

        with patch.multiple(
            sources, z0=lambda **kw: FIXTURE, hermes=lambda **kw: FIXTURE, ctx=slow
        ):
            start = time.monotonic()
            result = self.call("work_search", {"query": "screenshot"})
            self.assertLess(time.monotonic() - start, 0.24)
            self.assertTrue(result["partial"])
            self.assertEqual(len(result["items"]), 1)
            self.wait(result["id"])
            inbox = self.store.inbox(self.device)["messages"]
            self.assertEqual(len(inbox), 1)
            self.assertEqual(inbox[0]["params"]["lookup_id"], result["id"])
            self.assertEqual(len(inbox[0]["params"]["result"]["items"]), 2)
            self.assertEqual(self.store.inbox(self.other)["messages"], [])

    def test_source_failure_is_gap_not_fabricated_success(self):
        def fail(**kw):
            raise RuntimeError("fixture unavailable")

        with patch.multiple(sources, z0=fail, hermes=lambda **kw: FIXTURE, ctx=fail):
            result = self.call("work_search", {"query": "x"})
            self.assertEqual(result["sources"]["ctx"]["status"], "unavailable")
            self.assertTrue(result["gaps"])
            self.assertEqual(len(result["items"]), 1)

    def test_what_changed_uses_digest_sources_and_date(self):
        seen = []

        def reader(**kw):
            seen.append(kw)
            return FIXTURE

        with patch.multiple(sources, z0=reader, hermes=reader, changes=reader):
            result = self.call("what_changed", {"since": "yesterday"})
            self.assertIn("git-github-telemetry", result["sources"])
            self.assertTrue(all(k["since"] == "yesterday" for k in seen))

    def test_task_start_is_real_queue_and_status_distinguishes_request(self):
        result = self.call(
            "start_task",
            {"title": "[test] screenshot", "details": "Create test tracking only"},
        )
        self.assertEqual(result["id"], "voice-1")
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=1").fetchone()
        self.assertEqual(row["kind"], "task")
        self.assertIn("wait for explicit owner confirmation", row["text"])
        self.assertIn("[test] screenshot", row["text"])
        with patch.multiple(
            sources,
            task_sources=lambda **kw: FIXTURE,
            native_tasks=lambda **kw: FIXTURE,
            kanban_tasks=lambda **kw: FIXTURE,
        ):
            status = self.call("task_status", {"id": "voice-1"})
        self.assertEqual(status["local_tasks"][0]["id"], "voice-1")
        self.assertEqual(status["local_tasks"][0]["request_status"], "queued")
        self.assertIn("not delegated task completion", status["local_tasks"][0]["note"])
        self.assertEqual(self.work.local_tasks(self.other), [])

    def test_ask_timeout_later_worker_reply_and_inline_no_duplicate(self):
        result = self.call("ask_hermes", {"question": "fixture question"})
        self.assertIn("still working", result["say"])

        class FakeWorker(Worker):
            def api(inner, path, body=None, idem=None):
                return (
                    {"run_id": "unit-run"}
                    if path == "/v1/runs"
                    else {"status": "completed", "output": "Verified fixture answer"}
                )

        worker = FakeWorker(self.store, threading.Event())
        worker.tick()
        inbox = self.store.inbox(self.device)["messages"]
        self.assertEqual(inbox[0]["say"], "Verified fixture answer")
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT result FROM jobs WHERE id=1").fetchone()[0],
                "Verified fixture answer",
            )
        # A second request completes while its caller is waiting: inline only.
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(
                self.call, "ask_hermes", {"question": "second fixture"}
            )
            time.sleep(0.03)
            worker.tick()
            inline = future.result(timeout=2)
        self.assertEqual(inline["say"], "Verified fixture answer")
        self.assertEqual(len(self.store.inbox(self.device)["messages"]), 1)

    def test_recovery_marks_lost_sync_delivery_for_inbox(self):
        with self.store.connect() as db:
            job = self.store.enqueue(db, "question", "fixture", self.sid)
            db.execute("UPDATE jobs SET delivery='pending' WHERE id=?", (job,))
            db.execute(
                "INSERT INTO work_lookups VALUES('lost',?,'work_search','{}','running','{}','pending',0)",
                (self.sid,),
            )
        self.work.recover()
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT delivery FROM jobs").fetchone()[0], "inbox"
            )
            self.assertEqual(
                tuple(
                    db.execute("SELECT status,delivery FROM work_lookups").fetchone()
                ),
                ("queued", "inbox"),
            )

    def test_delivery_online_suppression_and_offline_batch(self):
        delivery = Delivery(self.store)
        self.store.publish("one", sid=self.sid, source="lookup:one")
        with (
            patch.dict(os.environ, Z0LIVE_TELEGRAM_TARGET="telegram"),
            patch("z0live_desktop.delivery.subprocess.run") as send,
        ):
            delivery.tick()
            send.assert_not_called()
            self.store.end(self.device, self.sid)
            self.store.publish("two", sid=self.sid, source="lookup:two")
            self.store.publish("three", sid=self.sid, source="lookup:three")
            delivery.next_check = 0
            send.return_value.returncode = 0
            send.return_value.stdout = '{"success":true}'
            delivery.tick()
            self.assertEqual(send.call_count, 1)
            self.assertIn("two\n\nthree", send.call_args.kwargs["input"])
            delivery.next_check = 0
            delivery.tick()
            self.assertEqual(send.call_count, 1)

    def test_call_id_deduplicates_task_creation_and_rejects_reuse(self):
        body = {
            "session_id": self.sid,
            "call_id": "same-call",
            "arguments": {"title": "[test] one", "details": "fixture"},
        }
        first = self.store.tool(self.device, "start_task", body)
        again = self.store.tool(self.device, "start_task", body)
        self.assertEqual(first["id"], again["id"])
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        body["arguments"]["title"] = "different"
        with self.assertRaises(ValueError):
            self.store.tool(self.device, "start_task", body)

    def test_completed_pending_answers_recovered_without_reexecution(self):
        with self.store.connect() as db:
            ident = self.store.enqueue(db, "question", "fixture", self.sid)
            db.execute(
                "UPDATE jobs SET status='completed',delivery='pending',run_id='done-run',result='Recovered answer' WHERE id=?",
                (ident,),
            )
        self.work.recover()
        self.work.recover()
        inbox = self.store.inbox(self.device)["messages"]
        self.assertEqual([m["say"] for m in inbox], ["Recovered answer"])

    def test_z0_ignores_search_receipts_and_respects_project(self):
        root = Path(self.tmp.name) / "z0"
        (root / "memory").mkdir(parents=True)
        rows = [
            {
                "event_id": 1,
                "event_type": "context.resolve",
                "ts": time.time() - 1,
                "project": str(sources.ROOT),
                "payload": {"query": "z0live", "evidence": []},
            },
            {
                "event_id": 2,
                "event_type": "work.done",
                "ts": time.time() - 1,
                "project": str(sources.ROOT),
                "payload": "z0live voice tests passed",
            },
            {
                "event_id": 3,
                "event_type": "work.done",
                "ts": time.time() - 1,
                "project": "elsewhere",
                "payload": "z0live mentioned elsewhere",
            },
        ]
        (root / "memory/events.jsonl").write_text(
            "\n".join(json.dumps(r) for r in rows)
        )
        with (
            patch.dict(os.environ, Z0INT_HOME=str(root)),
            patch.object(
                sources, "request_json", return_value={"packet": {"evidence": []}}
            ),
        ):
            result = sources.z0("z0live", project="z0live")
        self.assertEqual(len(result["items"]), 1)
        self.assertEqual(result["items"][0]["status"], "work.done")
        self.assertIn("#event=2", result["items"][0]["citation"])

    def test_kanban_id_filter_and_github_draft_checks(self):
        tasks = [
            {"id": "t_12345678", "title": "one", "status": "blocked"},
            {"id": "t_87654321", "title": "two", "status": "done"},
        ]
        with patch.object(sources, "run", return_value=json.dumps(tasks)):
            result = sources.kanban_tasks(ident="t_12345678")
        self.assertEqual(
            [i["citation"] for i in result["items"]], ["kanban:t_12345678"]
        )
        prs = [
            {
                "number": 14,
                "title": "z0live",
                "state": "OPEN",
                "isDraft": True,
                "url": "https://github.com/fixture/lawnchair/pull/14",
                "statusCheckRollup": [
                    {"name": "ci", "status": "COMPLETED", "conclusion": "SUCCESS"}
                ],
            }
        ]
        with (
            patch.object(sources, "run", return_value=json.dumps(prs)),
            patch.object(
                sources, "projects", return_value={"lawnchair": self.tmp.name}
            ),
        ):
            result = sources.task_sources(project="lawnchair", ident="14")
        self.assertEqual(result["items"][0]["status"], "OPEN draft")
        self.assertEqual(result["items"][0]["checks"][0]["conclusion"], "SUCCESS")

    def test_native_delegation_terminal_updates_are_device_scoped_and_deduplicated(
        self,
    ):
        folder = Path(self.tmp.name) / "native"
        folder.mkdir()
        path = folder / "state.db"
        with closing(sqlite3.connect(path)) as db, db:
            db.executescript("""CREATE TABLE async_delegations(delegation_id TEXT, state TEXT,
                result_json TEXT,task_json TEXT,origin_session TEXT,parent_session_id TEXT,
                origin_session_id TEXT,updated_at REAL);""")
            db.execute(
                "INSERT INTO async_delegations VALUES('unit-child','completed','verified result','{}','z0live','','',1)"
            )
            db.execute(
                "INSERT INTO async_delegations VALUES('other-child','failed','not ours','{}','other-session','','',1)"
            )
        with self.store.connect() as db:
            self.store.enqueue(db, "task", "fixture", self.sid)
        with patch.dict(os.environ, Z0LIVE_HERMES_HOME=str(folder)):
            self.work.monitor()
            self.work.next_monitor = 0
            self.work.monitor()
        inbox = self.store.inbox(self.device)["messages"]
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox[0]["params"]["delegation_id"], "unit-child")
        self.assertIn("completed", inbox[0]["say"])
        self.assertEqual(self.store.inbox(self.other)["messages"], [])
        with closing(sqlite3.connect(path)) as db, db:
            db.execute(
                "UPDATE async_delegations SET state='failed' WHERE delegation_id='unit-child'"
            )
        with patch.dict(os.environ, Z0LIVE_HERMES_HOME=str(folder)):
            self.work.next_monitor = 0
            self.work.monitor()
        self.assertIn("failed", self.store.inbox(self.device)["messages"][-1]["say"])

    def test_fts_fixture_profiles_read_only_and_date_window(self):
        root = Path(self.tmp.name) / "history"
        for profile in ["a", "b"]:
            folder = root / "profiles" / profile
            folder.mkdir(parents=True)
            with closing(sqlite3.connect(folder / "state.db")) as db, db:
                db.executescript("""CREATE TABLE sessions(id TEXT,title TEXT,cwd TEXT);
                    CREATE TABLE messages(id INTEGER PRIMARY KEY,session_id TEXT,timestamp REAL,content TEXT,role TEXT);
                    CREATE VIRTUAL TABLE messages_fts USING fts5(content);""")
                db.execute(
                    "INSERT INTO sessions VALUES('unit-session','Fixture','garden_room')"
                )
                db.execute(
                    "INSERT INTO messages VALUES(1,'unit-session',?,'garden_room screenshot verified','assistant')",
                    (time.time() - 10,),
                )
                db.execute(
                    "INSERT INTO messages_fts(rowid,content) VALUES(1,'garden_room screenshot verified')"
                )
        with patch.dict(os.environ, Z0LIVE_HISTORY_ROOTS=str(root)):
            result = sources.hermes("screenshot")
        self.assertEqual(len(result["items"]), 2)
        self.assertTrue(
            all(i["citation"].startswith("hermes:") for i in result["items"])
        )
        with sources.ro(root / "profiles/a/state.db") as db:
            with self.assertRaises(sqlite3.OperationalError):
                db.execute("DELETE FROM messages")


if __name__ == "__main__":
    unittest.main()
