import asyncio
import json
import sqlite3
import tempfile
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

    def test_matches_an_earlier_user_message_with_whitespace_drift(self):
        hist = [{"role": "user", "content": [{"text": "troubleshoot   z0live\nplease"}]},
                {"role": "user", "content": [{"text": "a restated wrapper the db never stored"}]}]
        self.assertEqual(cc.find_chat(hist, "", self.db), "gui1c")

    def test_no_match_or_no_history_is_empty(self):
        self.assertEqual(cc.find_chat([{"role": "user", "content": [{"text": "never said"}]}], "", self.db), "")
        self.assertEqual(cc.find_chat(None, "", self.db), "")


class AskChat(unittest.IsolatedAsyncioTestCase):
    async def ask(self, status, events):
        """ask_chat against a dashboard socket whose prompt.submit answers `status`, then sends `events`."""
        seen = []

        async def ws_handler(request):
            seen.append(("token", request.query.get("token")))
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                f = json.loads(msg.data)
                seen.append((f["method"], f["params"]))
                if f["method"] == "session.resume":
                    await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": f["id"], "result": {"session_id": "live1"}}))
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
                text = await asyncio.wait_for(cc.ask_chat(http, "tok", "clean", "gui1c", "is it up?", 10), 10)
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


if __name__ == "__main__":
    unittest.main()
