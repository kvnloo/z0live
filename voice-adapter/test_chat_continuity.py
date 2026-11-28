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
        # 114034: the chat's own turn ran (Kevin typed in it); the submit folded the call's request into it.
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


class ChatMemory(unittest.TestCase):
    """11-27 13:00: Kevin found the call "dumb, 0 context on any of our work". Since f62dc9a every request runs as a
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
                    ("c", "user", "lets add sol lanes"),
                    ("c", "assistant", "Done: 10 sol lanes."),
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
        self.assertLess(text.index("z0live voice calls"), text.index("lets add sol lanes"))
        self.assertIn("Kevin: lets add sol lanes", text)
        self.assertIn("Assistant: Done: 10 sol lanes.", text)

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
        self.assertIn("lets add sol lanes", cc.chat_memory("", "c", self.db))
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
        self.assertIn("[13:50] Kevin: the TV now points at the Studio", text)
        self.assertLess(text.index("[09:05]"), text.index("[13:50]"))


if __name__ == "__main__":
    unittest.main()
