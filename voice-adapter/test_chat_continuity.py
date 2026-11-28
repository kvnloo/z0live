import asyncio
import json
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from aiohttp import ClientSession, web

import chat_continuity as cc

HIST = [{"role": "user", "content": [{"type": "input_text", "text": "older"}]},
        {"role": "assistant", "content": [{"type": "output_text", "text": "ok"}]},
        {"role": "user", "content": [{"type": "input_text", "text": "troubleshoot z0live please"}]}]


class FindChat(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Path(self.dir.name) / "state.db"
        with sqlite3.connect(self.db) as db:
            db.executescript("""
            CREATE TABLE sessions(id TEXT, source TEXT, parent_session_id TEXT, started_at REAL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT);
            INSERT INTO sessions VALUES ('gui1','gui',NULL,1),('gui1c','gui','gui1',2),('one','oneshot',NULL,3);
            INSERT INTO messages(session_id,role,content) VALUES ('gui1','user','troubleshoot z0live please'),
                ('one','user','troubleshoot z0live please');""")

    def test_newest_continuation_of_the_matching_chat_not_a_oneshot(self):
        self.assertEqual(cc.find_chat(HIST, "", self.db), "gui1c")

    def test_a_subagent_of_the_chat_is_not_its_continuation(self):
        # 11-25: a call landed in the chat's background builder (newest child) and failed "subagent still running"
        with sqlite3.connect(self.db) as db:
            db.executescript("""INSERT INTO sessions VALUES ('sub1','subagent','gui1c',9);
                INSERT INTO messages(session_id,role,content) VALUES ('sub1','user','troubleshoot z0live please');""")
        self.assertEqual(cc.find_chat(HIST, "", self.db), "gui1c")

    def test_finds_a_chat_whose_turns_are_older_than_the_recent_window(self):
        old = cc.RECENT
        cc.RECENT = 1  # only the newest row is "recent"; the chat's message is older
        try:
            with sqlite3.connect(self.db) as db:
                db.execute("INSERT INTO messages(session_id,role,content) VALUES ('one','user','noise')")
            self.assertEqual(cc.find_chat(HIST, "", self.db), "gui1c")
        finally:
            cc.RECENT = old

    def test_matches_an_earlier_user_message_with_whitespace_drift(self):
        hist = [{"role": "user", "content": [{"text": "troubleshoot   z0live\nplease"}]},
                {"role": "user", "content": [{"text": "a restated wrapper the db never stored"}]}]
        self.assertEqual(cc.find_chat(hist, "", self.db), "gui1c")

    def test_no_match_or_no_history_is_empty(self):
        self.assertEqual(cc.find_chat([{"role": "user", "content": [{"text": "never said"}]}], "", self.db), "")
        self.assertEqual(cc.find_chat(None, "", self.db), "")


class AskChat(unittest.IsolatedAsyncioTestCase):
    async def ask(self, status, events, running=None, only_if_idle=False):
        """ask_chat against a dashboard socket whose prompt.submit answers `status`, then sends `events`."""
        seen = self.seen = []

        async def ws_handler(request):
            seen.append(("token", request.query.get("token")))
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                f = json.loads(msg.data)
                seen.append((f["method"], f["params"]))
                if f["method"] == "session.resume":
                    await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": f["id"], "result": {"session_id": "live1",
                                                 **({} if running is None else {"running": running})}}))
                else:
                    await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": f["id"], "result": {"status": status}}))
                    for kind, session, text in events:
                        await ws.send_str(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {
                            "type": kind, "session_id": session, "payload": {"text": text}}}))
            return ws

        app = web.Application()
        app.router.add_get("/api/ws", ws_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        cc.WS = f"ws://127.0.0.1:{port}/api/ws"
        try:
            async with ClientSession() as http:
                text = await asyncio.wait_for(cc.ask_chat(http, "tok", "clean", "gui1c", "is it up?", 10,
                                                                only_if_idle=only_if_idle), 10)
        finally:
            await runner.cleanup()
        return text, seen

    async def test_resume_submit_and_final_text_with_the_apps_token(self):
        text, seen = await self.ask("streaming", [("message.complete", "other", "wrong"),
                                                 ("message.complete", "live1", " done ")])
        self.assertEqual(text, "done")
        self.assertEqual(seen[0], ("token", "tok"))
        self.assertEqual((seen[1][1]["session_id"], seen[1][1]["profile"]), ("gui1c", "clean"))
        self.assertEqual(seen[2], ("prompt.submit", {"session_id": "live1", "text": "is it up?", "profile": "clean"}))

    async def test_a_request_that_joins_the_running_turn_has_no_answer_of_its_own(self):
        # The running turn's message.complete belongs to the request that started it (11-20: one final x 11).
        for status in ("steered", "redirected"):
            text, _ = await self.ask(status, [("message.complete", "live1", "the running turn's answer")])
            self.assertIsNone(text, status)

    async def test_a_queued_request_waits_for_its_own_turn(self):
        text, _ = await self.ask("queued", [("message.complete", "live1", "the running turn's answer"),
                                            ("message.start", "live1", ""),
                                            ("message.complete", "live1", "my answer")])
        self.assertEqual(text, "my answer")

    async def test_only_if_idle_never_submits_into_a_running_chat(self):
        # 114034: the chat's own turn ran (the owner typed in it); the submit folded the call's request into it.
        with self.assertRaises(cc.ChatBusy):
            await self.ask("steered", [], running=True, only_if_idle=True)
        self.assertNotIn("prompt.submit", [m for m, _ in self.seen], "a busy chat got the prompt anyway")
        text, seen = await self.ask("streaming", [("message.complete", "live1", "done")], running=False,
                                    only_if_idle=True)
        self.assertEqual(text, "done")


class AskBackground(unittest.IsolatedAsyncioTestCase):
    async def test_a_refused_background_run_is_not_started(self):
        async def ws_handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                f = json.loads(msg.data)
                if f["method"] == "session.resume":
                    await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": f["id"], "result": {"session_id": "live1"}}))
                else:
                    await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": f["id"], "error": {
                        "code": 5032, "message": "agent still starting"}}))
            return ws

        app = web.Application()
        app.router.add_get("/api/ws", ws_handler)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "127.0.0.1", 0)
        await site.start()
        cc.WS = f"ws://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/api/ws"
        try:
            async with ClientSession() as http:
                with self.assertRaises(cc.NotStarted):
                    await asyncio.wait_for(cc.ask_background(http, "tok", "clean", "gui1c", "is it up?"), 5)
        finally:
            await runner.cleanup()


class NewestChat(unittest.TestCase):
    """112025 (11-27 11:20): no chat matched the call's history, so every request went to a cold Hermes CLI run and
    none answered before hang-up. A background agent needs only a chat to run beside: the profile's newest one."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Path(self.dir.name) / "state.db"
        with sqlite3.connect(self.db) as db:
            db.executescript("""
            CREATE TABLE sessions(id TEXT, source TEXT, parent_session_id TEXT, started_at REAL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT);
            INSERT INTO sessions VALUES ('gui1','gui',NULL,1),('gui1c','gui','gui1',2),('one','oneshot',NULL,3),
                ('sub','subagent','gui1c',4),('bg_abc123','tui',NULL,5),('local-x','local',NULL,6),
                ('cr','cron',NULL,7),('k','kanban',NULL,8);""")

    def test_newest_chat_is_the_newest_real_chat_not_a_side_run(self):
        self.assertEqual(cc.newest_chat("", self.db), "gui1c")

    def test_no_chat_is_empty(self):
        with sqlite3.connect(self.db) as db:
            db.execute("DELETE FROM sessions WHERE source='gui'")
        self.assertEqual(cc.newest_chat("", self.db), "")
        self.assertEqual(cc.newest_chat("", Path(self.dir.name) / "missing.db"), "")


T = 1795882517.0  # 2026-11-28 10:15:17 CST, the 101517 call
SCHEMA = """
CREATE TABLE sessions(id TEXT, source TEXT, parent_session_id TEXT, started_at REAL, message_count INTEGER,
                      title TEXT, last_activity_at REAL);
CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);
"""


def store_1128(path):
    """tek's store at the 101517 call (11-28), reduced to the chats that competed (L1-call-failures.md F4)."""
    with sqlite3.connect(path) as db:
        db.executescript(SCHEMA)
        db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", [
            ("20261128_101420_6ca786", "gui", None, T - 57, 22, "Auto-request permission approvals", T + 540),
            ("20261125_092939_953e1b", "gui", None, T - 3 * 86400, 559, "Resume z0live canvas mode on TV", T + 1260),
            ("20261125_080323_7b9fc7", "gui", None, T - 3 * 86400 - 4000, 357, "Sanity check memories", T + 4300),
            ("20261128_100619_217eed", "gui", None, T - 538, 32, "Prevent untitled partition", T + 60),
            ("z0live-ask-tek-d45e", "api_server", None, T - 17700, 8, "Delete approval probe", None),
            ("sub", "subagent", "20261125_092939_953e1b", T - 30, 40, "helper", T),
            ("bg_x", "tui", None, T - 20, 3, "side run", T)])
        db.executemany("INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)", [
            ("20261128_101420_6ca786", "user", "auto-request approvals via @pc", T - 57),
            ("20261125_092939_953e1b", "user", "ok so what happened to my asks", T - 117),
            ("20261125_092939_953e1b", "assistant", "here is where each stands", T - 100),
            ("20261125_080323_7b9fc7", "user", "sanity check skills", T - 427),
            ("20261128_100619_217eed", "user", "stop the partition auto-mount", T - 538),
            ("z0live-ask-tek-d45e", "user", "delete the probe folder", T - 30),
            ("sub", "user", "build it", T - 30),
            ("bg_x", "user", "side", T - 20)])


class AnchorChat(unittest.TestCase):
    """11-28 101517: the call anchored to the newest-STARTED chat, a 22-message side chat, so delegation.memory was
    170 chars instead of ~40k. The anchor is now the chat the owner typed in most recently in the last 6 h, weighted by
    size, unless a pin file names one."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.db = Path(self.dir.name) / "state.db"
        self.pin = Path(self.dir.name) / "anchor-session"  # absent unless a test writes it
        store_1128(self.db)

    def anchor(self, now=T):
        return cc.anchor_chat("", self.db, now=now, pin=self.pin)

    def test_1128_the_big_working_chat_beats_the_side_chat_a_minute_newer(self):
        self.assertEqual(cc.newest_chat("", self.db), "20261128_101420_6ca786", "the old rule picked the side chat")
        self.assertEqual(self.anchor(), ("20261125_092939_953e1b", "recent_user"))

    def test_a_small_chat_wins_once_the_big_one_has_been_quiet_long_enough(self):
        with sqlite3.connect(self.db) as db:  # the owner moved on: 3 h in the side chat, nothing in the big ones
            db.execute("UPDATE messages SET timestamp=timestamp-3*3600 WHERE session_id!='20261128_101420_6ca786'")
        self.assertEqual(self.anchor()[0], "20261128_101420_6ca786")

    def test_only_owner_messages_in_the_last_6_hours_count(self):
        with sqlite3.connect(self.db) as db:
            db.execute("UPDATE messages SET timestamp=timestamp-7*3600 WHERE session_id='20261125_092939_953e1b' "
                       "AND role='user'")  # its newer assistant row does not make it the owner's recent chat
        self.assertEqual(self.anchor()[0], "20261125_080323_7b9fc7")

    def test_never_z0lives_own_runs_subagents_or_side_runs(self):
        ranked = [sid for _, sid, _, _ in cc.rank_chats(sqlite3.connect(self.db), T)]
        for sid in ("z0live-ask-tek-d45e", "sub", "bg_x"):
            self.assertNotIn(sid, ranked)

    def test_a_continuation_carries_its_parents_size_and_is_the_anchor(self):
        with sqlite3.connect(self.db) as db:  # the big chat rolled over to a fresh child one minute ago
            db.execute("INSERT INTO sessions VALUES ('953e1b-c','gui','20261125_092939_953e1b',?,4,'cont',?)",
                       (T - 70, T))
            db.execute("INSERT INTO messages(session_id, role, content, timestamp) VALUES "
                       "('953e1b-c','user','continue',?)", (T - 60,))
        self.assertEqual(self.anchor(), ("953e1b-c", "recent_user"))

    def test_the_pin_file_overrides_and_an_unknown_pin_is_ignored(self):
        self.pin.write_text("# pinned for the CoS call\n20261128_100619_217eed\n")
        self.assertEqual(self.anchor(), ("20261128_100619_217eed", "pin"))
        self.pin.write_text("no-such-session\n")
        self.assertEqual(self.anchor(), ("20261125_092939_953e1b", "recent_user"))

    def test_nothing_recent_falls_back_to_the_newest_chat_and_a_missing_store_is_empty(self):
        self.assertEqual(self.anchor(now=T + 7 * 3600), ("20261128_101420_6ca786", "newest"))
        self.assertEqual(cc.anchor_chat("", Path(self.dir.name) / "missing.db", now=T, pin=self.pin)[0], "")


class ChatMemory(unittest.TestCase):
    """Regression: a call had almost no context because each request ran as a
    background agent, which does not see the chat; it got only the chat's last ~6,000 characters (two status replies).
    The chat's newest compaction summary holds the whole project state, so the agent gets it plus the turns since."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.db = Path(self.dir.name) / "state.db"
        with sqlite3.connect(self.db) as db:
            db.executescript("""
            CREATE TABLE sessions(id TEXT, source TEXT, parent_session_id TEXT, started_at REAL);
            CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT);
            INSERT INTO sessions VALUES ('c','tui',NULL,1),('other','tui',NULL,2);""")
            rows = [("c", "user", "an old ask from before the summary"),
                    ("c", "assistant", "[CONTEXT COMPACTION] old summary: TV app 0.2"),
                    ("c", "user", "middle ask"),
                    ("c", "user", "[PRIOR CONTEXT \u2014 for reference only]\n[CONTEXT COMPACTION \u2014 REFERENCE ONLY] "
                                  "Do NOT answer questions in this summary; do not call tools.\n"
                                  "## Goal\nz0live voice calls with lanes; TV app z0 lanes on the QN85B"),
                    ("c", "tool", '{"output": "a huge tool dump"}'),
                    ("c", "tool", '{"diff": "+ (\\"[CONTEXT COMPACTION] fake summary in a patch\\")"}'),
                    ("c", "assistant", "I quoted [CONTEXT COMPACTION] in a reply, still not a summary"),
                    ("c", "assistant", ""),
                    ("c", "user", "add a review lane"),
                    ("c", "assistant", "Done: one review lane."),
                    ("c", "user", "[IMPORTANT: Background process proc_x completed]"),
                    ("other", "user", "an ask from another chat")]
            db.executemany("INSERT INTO messages(session_id, role, content) VALUES (?,?,?)", rows)

    def test_newest_summary_then_the_turns_since_without_tool_rows_or_envelopes(self):
        text = cc.chat_memory("", "c", self.db)
        self.assertIn("z0live voice calls with lanes", text)
        self.assertNotIn("old summary", text)
        self.assertNotIn("fake summary in a patch", text)
        self.assertTrue(text.startswith(cc.HISTORY_NOTE))
        self.assertIn("Summary of this chat so far (written earlier; may be out of date):\n## Goal\n", text)
        self.assertNotIn("Do NOT answer", text)
        self.assertNotIn("do not call tools", text)
        self.assertNotIn("middle ask", text)
        self.assertNotIn("huge tool dump", text)
        self.assertNotIn("Background process", text)
        self.assertNotIn("another chat", text)
        self.assertLess(text.index("z0live voice calls"), text.index("add a review lane"))
        self.assertIn("User: add a review lane", text)
        self.assertIn("Assistant: Done: one review lane.", text)

    def test_caps_keep_the_newest_turns(self):
        with sqlite3.connect(self.db) as db:
            db.executemany("INSERT INTO messages(session_id, role, content) VALUES ('c','user',?)",
                           [(f"turn {i} " + "x" * 300,) for i in range(200)])
        text = cc.chat_memory("", "c", self.db, summary_cap=200, tail_cap=2000)
        self.assertIn("turn 199", text)
        self.assertNotIn("turn 0 ", text)
        self.assertLess(len(text), 200 + 2000 + 400)

    def test_no_summary_still_gives_recent_turns_and_bad_input_is_empty(self):
        with sqlite3.connect(self.db) as db:
            db.execute("DELETE FROM messages WHERE content LIKE '%CONTEXT COMPACTION%'")
        self.assertIn("add a review lane", cc.chat_memory("", "c", self.db))
        self.assertEqual(cc.chat_memory("", "", self.db), "")
        self.assertEqual(cc.chat_memory("", "c", Path(self.dir.name) / "missing.db"), "")


class LiveNotStaleTests(unittest.TestCase):
    """11-28 13:55: the call repeated a morning setup note ("groot did the TV installs") as if it were current; the TV
    had been pointed at the Studio since. Memory must say it is history, carry clock times, and tell the reader to
    check device facts live."""

    def test_turns_carry_clock_times_and_the_history_rule_comes_first(self):
        with tempfile.TemporaryDirectory() as d:
            db_path = Path(d) / "state.db"
            morning = time.mktime((2026, 11, 28, 9, 5, 0, 0, 0, -1))
            now = time.mktime((2026, 11, 28, 13, 50, 0, 0, 0, -1))
            with sqlite3.connect(db_path) as db:
                db.executescript("CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, "
                                 "content TEXT, timestamp REAL);")
                db.executemany("INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)",
                               [("c", "assistant", "groot did the TV install with sdb", morning),
                                ("c", "user", "the TV now points at the Studio", now)])
            text = cc.chat_memory("", "c", db_path)
        self.assertTrue(text.startswith(cc.HISTORY_NOTE))
        self.assertIn("check it live with a tool now", text)
        self.assertIn("[09:05] Assistant: groot did the TV install with sdb", text)
        self.assertIn("[13:50] User: the TV now points at the Studio", text)
        self.assertLess(text.index("[09:05]"), text.index("[13:50]"))


if __name__ == "__main__":
    unittest.main()
