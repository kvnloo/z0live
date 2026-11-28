import os
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

import call_brief


def msg(role, text):
    return {"role": role, "content": [{"type": "text", "text": text}]}


class Brief(unittest.TestCase):
    def test_digest_is_framed_background_not_conversation(self):
        hist = [msg("user", "pls monitor thru adb"), msg("assistant", "Watching now. Details follow.")]
        with tempfile.TemporaryDirectory() as d:
            call_brief.BRIEF_DIR = Path(d)
            text = call_brief.build(hist)
        self.assertIn("Do not reply to it", text)
        self.assertIn("pls monitor thru adb", text)
        self.assertIn("approved it watching his screen", text)
        self.assertNotIn("assistant:", text)  # no transcript roles the model could continue

    def test_people_on_the_call_come_from_the_chat(self):
        self.assertEqual(call_brief.people([msg("user", "status on the build?")]), 1)
        self.assertEqual(call_brief.people([msg("assistant", "This call has two people at the desk, Kevin and his "
                                                             "friend Jules, and two bots.")]), 2)
        self.assertEqual(call_brief.people([msg("user", "3 people are here")]), 3)
        self.assertEqual(call_brief.people("not a list"), 1)

    def test_written_brief_wins_while_fresh(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            call_brief.save("Kevin is testing the z0live switch.", "", root)
            self.assertIn("z0live switch", call_brief.written("", root=root))
            old = time.time() - call_brief.FRESH_S - 10
            os.utime(root / "latest.md", (old, old))
            self.assertEqual(call_brief.written("", root=root), "")

    def test_chat_brief_beats_global(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            call_brief.save("global", "", root)
            call_brief.save("this chat", "c1", root)
            self.assertEqual(call_brief.written("c1", root=root), "this chat")


T = 1795882517.0  # 2026-11-28 10:15:17 CST, the 101517 call


def stores(d: Path):
    """A profile store with FTS (as Hermes builds it: content rowid = messages.id) and a kanban board."""
    import sqlite3
    state, board = d / "state.db", d / "kanban.db"
    with sqlite3.connect(state) as db:
        db.executescript("""
        CREATE TABLE sessions(id TEXT, source TEXT, parent_session_id TEXT, started_at REAL, message_count INTEGER,
                              title TEXT, last_activity_at REAL);
        CREATE TABLE messages(id INTEGER PRIMARY KEY, session_id TEXT, role TEXT, content TEXT, timestamp REAL);
        CREATE VIRTUAL TABLE messages_fts USING fts5(content, content='messages', content_rowid='id');""")
        db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?,?,?)", [
            ("cos", "gui", None, T - 3 * 86400, 559, "Resume z0live canvas mode on TV", T),
            ("side", "gui", None, T - 57, 22, "Auto-request permission approvals", T),
            ("part", "gui", None, T - 538, 32, "Prevent untitled partition", T),
            ("old", "gui", None, T - 9 * 86400, 80, "Last week's chat", T - 3 * 86400),
            ("z0live-ask-1", "api_server", None, T - 100, 4, "probe", None),
            ("k1", "kanban", None, T - 100, 9, "a worker", T)])
        rows = [("cos", "user", "I'm still so confused, bro. So what happened to all of my asks?", T - 3600),
                ("cos", "assistant", "That looks wrong to me too", T - 3500),
                ("cos", "user", "ok so where does each ask stand", T - 117),
                ("cos", "user", "[IMPORTANT: Background process proc_x completed]", T - 30),
                ("side", "user", "auto-request approvals via @pc", T - 57),
                ("side", "user", "\u23f8 [default] @cfg Kanban t_2b200d6c blocked: sandbox can't write", T - 10),
                ("part", "user", "do u never fucking listen to a single fucking thing i ask for??", T - 538),
                ("old", "user", "this is broken", T - 3 * 86400),
                ("z0live-ask-1", "user", "the call is broken", T - 100),
                ("k1", "user", "work kanban task: everything is broken", T - 90)]
        db.executemany("INSERT INTO messages(session_id, role, content, timestamp) VALUES (?,?,?,?)", rows)
        db.execute("INSERT INTO messages_fts(rowid, content) SELECT id, content FROM messages")
    with sqlite3.connect(board) as kb:
        kb.executescript("""CREATE TABLE tasks(id TEXT, title TEXT, assignee TEXT, status TEXT, block_kind TEXT,
                            created_at REAL, started_at REAL, last_heartbeat_at REAL);""")
        kb.executemany("INSERT INTO tasks VALUES (?,?,?,?,?,?,?,?)", [
            ("t1", "z0live fix F4: call starts with the right memory", "tek", "running", None, T - 600, T - 500, T),
            ("t2", "pvlib #2956 epw shift", "tek", "blocked", "needs_input", T - 3600, T - 3500, None),
            ("t3", "SuperSplat data panel", "default", "blocked", None, T - 1800, T - 1700, None),
            ("t4", "ancient blocked card", "tek", "blocked", None, T - 30 * 86400, None, None),
            ("t5", "finished", "tek", "done", None, T - 600, T - 500, None)])
    return state, board


class CrossThread(unittest.TestCase):
    """11-27 121336 @981s: "So what happened to all of my asks... does that mean everything is done?" The call knew one
    chat. It now opens with a short read-only picture of Kevin's other threads."""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.state, self.board = stores(Path(self.dir.name))

    def brief(self, **kw):
        return call_brief.cross_thread("", now=T, db_path=self.state, kanban=self.board, **kw)

    def test_asks_cards_and_last_complaint_from_kevins_chats_only(self):
        text = self.brief()
        self.assertTrue(text.startswith("Across Kevin's threads"))
        self.assertLessEqual(len(text), 1500)
        # latest ask per chat, newest first; never an envelope, a worker prompt or z0live's own run
        self.assertIn('"auto-request approvals via @pc"', text)
        self.assertIn('"ok so where does each ask stand"', text)
        self.assertLess(text.index("auto-request approvals"), text.index("where does each ask stand"))
        for absent in ("Background process", "work kanban task", "the call is broken", "Last week's chat", "\u23f8"):
            self.assertNotIn(absent, text)
        self.assertIn("Cards running: z0live fix F4: call starts with the right memory (@tek)", text)
        self.assertIn("pvlib #2956 epw shift (@tek, waiting on Kevin)", text)
        self.assertLess(text.index("pvlib"), text.index("SuperSplat"), "waiting-on-Kevin cards first")
        self.assertNotIn("ancient blocked card", text)
        self.assertNotIn("finished", text)
        # Kevin's newest complaint (11-28 10:06), not the assistant's "looks wrong" or last week's "broken"
        self.assertIn("Kevin's last complaint (8 min ago, in Prevent untitled partition): \"do u never fucking listen",
                      text)
        with sqlite3.connect(self.state) as db:
            db.execute("DELETE FROM messages WHERE session_id='part'")
        self.assertIn("Kevin's last complaint (60 min ago, in Resume z0live canvas mode on TV): \"I'm still so "
                      "confused, bro.", self.brief())

    def test_capped_and_goes_into_the_opening_brief(self):
        text = self.brief(cap=300)
        self.assertLessEqual(len(text), 300)
        with tempfile.TemporaryDirectory() as d:
            call_brief.BRIEF_DIR = Path(d)
            opening = call_brief.build([msg("user", "hi")], cross=self.brief())
        self.assertIn("Cards running:", opening)
        self.assertTrue(opening.endswith("</call_brief>"))

    def test_missing_stores_give_nothing_and_never_raise(self):
        missing = Path(self.dir.name) / "none.db"
        self.assertEqual(call_brief.cross_thread("", now=T, db_path=missing, kanban=missing), "")
        only_cards = call_brief.cross_thread("", now=T, db_path=missing, kanban=self.board)
        self.assertIn("Cards running:", only_cards)
        self.assertNotIn("latest ask", only_cards)


if __name__ == "__main__":
    unittest.main()
