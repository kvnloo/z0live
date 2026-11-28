"""topic_sessions: a call's requests land in one chat per project group, not a fresh session each (11-27 12:54,
11-28 12:36). The requests below are real ones from the 11-28 calls."""
import asyncio
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from unittest.mock import patch

from aiohttp import ClientSession, web

sys.path.insert(0, os.path.dirname(__file__))
import topic_sessions as ts  # noqa: E402
import mobile_live as live  # noqa: E402

G = ts.DEFAULT_GROUPS


def key(text, prior="", age=None):
    return ts.classify(text, G, prior, age)[0].key


class ClassifyTests(unittest.TestCase):
    def test_real_requests_from_the_11_28_calls(self):
        cases = {
            "send me grok link for oauth": "grok",
            "Grok OAuth sign-in link please": "grok",
            "Can you keep surfacing blockers for me, like, I don't see any approval cards on my screen": "tv",
            "Like, uh, there it says 56 threads need you. What does that mean": "tv",
            "Are you using the ultra wide camera": "phones",
            "So when I go into the Dash app and I start using the S Pen and annotating": "dash",
            "spin up fable advisor 3 lanes": "z0live",
            "what happened to the multi-session bug for 0 live": "z0live",
            "Optimize voice call setup with advisor lanes": "z0live",
            "the lanes need to start utilizing the semantic router endpoints and evals better": "tokens",
            "Recreate the launch video in fframes": "meetings",
            "What crashed groot": "groot",
            "is delegate_task working again": "hermes",
        }
        for text, want in cases.items():
            self.assertEqual(key(text), want, text)

    def test_a_follow_up_with_no_project_word_stays_with_the_last_group(self):
        self.assertEqual(ts.classify("send me the link plzzz", G, "grok", 40)[1], "sticky")
        self.assertEqual(key("send me the link plzzz", "grok", 40), "grok")
        self.assertEqual(key("send me the link plzzz", "grok", ts.STICKY_SECONDS + 1), "general")
        self.assertEqual(key("send me the link plzzz"), "general")

    def test_a_tie_goes_to_the_calls_last_group(self):
        # "voice" (z0live) and "grok" once each: on a Grok thread it stays Grok, else the earlier (specific) group.
        self.assertEqual(key("is the grok voice working", "z0live", 10), "z0live")
        self.assertEqual(key("is the grok voice working"), "grok")

    def test_words_match_whole_not_inside_other_words(self):
        self.assertEqual(key("I'm in the cartoon store"), "general")  # 'tv' / 'call' / 'card' are not substrings
        self.assertEqual(key("print the recall list"), "general")

    def test_config_file_replaces_the_groups_and_keeps_general(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "g.json"
            p.write_text(json.dumps({"groups": [{"key": "garden", "label": "Garden", "aliases": ["garden", "plants"]}]}))
            groups = ts.load_groups(p)
            self.assertEqual([g.key for g in groups], ["garden", "general"])
            self.assertEqual(ts.classify("water the plants", groups)[0].title, "z0live · Garden")
            p.write_text("{broken")
            self.assertEqual(ts.load_groups(p), ts.DEFAULT_GROUPS)
            self.assertEqual(ts.load_groups(Path(d) / "missing.json"), ts.DEFAULT_GROUPS)


class RouterTests(unittest.TestCase):
    def test_route_remembers_the_last_named_group_and_admit_caps_depth(self):
        r = ts.Router(groups=G)
        self.assertEqual(r.route("send me grok link for oauth", now=0)[0].key, "grok")
        self.assertEqual(r.route("??", now=30)[0].key, "grok")
        self.assertEqual(r.route("what time is it", now=1000)[0].key, "general")
        self.assertEqual(r.last, "grok", "general never becomes the sticky group")
        with patch.object(ts, "DEPTH", 2):
            self.assertTrue(r.admit("grok"))
            self.assertTrue(r.admit("grok"))
            self.assertFalse(r.admit("grok"), "a third concurrent request goes beside the chat")
            r.release("grok")
            self.assertTrue(r.admit("grok"))

    def test_day_session_is_stable_per_group_and_day(self):
        g = G[0]
        self.assertEqual(ts.day_session("z0live-guest", g, 1795896000), ts.day_session("z0live-guest", g, 1795899600))
        self.assertNotEqual(ts.day_session("z0live-guest", g, 1795896000),
                            ts.day_session("z0live-guest", G[-1], 1795896000))
        self.assertTrue(ts.busy_error("Guest Hermes host returned HTTP 409"))
        self.assertFalse(ts.busy_error("Guest Hermes host returned HTTP 500"))


class Dashboard:
    """A fake dashboard /api/ws holding titled chats: session.list {title}, session.create, session.resume,
    prompt.submit (running chats answer queued), and the turn's events."""

    def __init__(self, titled=None, running=False, fail=None):
        self.titled = dict(titled or {})  # title -> stored id
        self.running, self.fail = running, fail
        self.seen, self.created = [], []

    async def handler(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            f = json.loads(msg.data)
            m, p = f["method"], f["params"]
            self.seen.append((m, p))

            async def reply(result=None, error=None):
                await ws.send_str(json.dumps({"jsonrpc": "2.0", "id": f["id"],
                                              **({"error": error} if error else {"result": result})}))
            if self.fail == m:
                await reply(error={"code": 5032, "message": "agent still starting"})
            elif m == "session.list":
                sid = self.titled.get(p["title"])
                await reply({"sessions": [{"id": sid + "-root", "resolved_id": sid}] if sid else []})
            elif m == "session.create":
                sid = f"stored{len(self.created) + 1}"
                self.created.append(p)
                self.titled[p["title"]] = sid
                await reply({"session_id": "live-" + sid, "stored_session_id": sid})
            elif m == "session.resume":
                await reply({"session_id": "live-" + p["session_id"], "running": self.running})
            elif m == "prompt.submit":
                live_id = p["session_id"]
                await reply({"status": "queued" if self.running else "streaming"})
                events = ([("message.complete", "the running turn's answer")] if self.running else [])
                events += [("message.start", ""), ("message.complete", f"answer to: {p['text'][-40:]}")]
                for kind, text in events:
                    await ws.send_str(json.dumps({"jsonrpc": "2.0", "method": "event", "params": {
                        "type": kind, "session_id": live_id, "payload": {"text": text}}}))
        return ws

    async def __aenter__(self):
        app = web.Application()
        app.router.add_get("/api/ws", self.handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        self.url = f"ws://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/api/ws"
        return self

    async def __aexit__(self, *exc):
        await self.runner.cleanup()


GROK = next(g for g in G if g.key == "grok")


class AskTopicTests(unittest.IsolatedAsyncioTestCase):
    async def ask(self, dash, group=GROK, known=""):
        composed = []

        def compose(created):
            composed.append(created)
            return "FULL MEMORY + req" if created else "req"
        async with ClientSession() as http:
            out = await asyncio.wait_for(ts.ask_topic(http, dash.url, "tok", "tek", group, compose, known=known), 5)
        return out, composed

    async def test_missing_chat_is_created_with_its_title_and_cwd_and_gets_the_memory(self):
        async with Dashboard() as dash:
            (text, stored, created), composed = await self.ask(dash)
        self.assertEqual((stored, created, composed), ("stored1", True, [True]))
        self.assertEqual(dash.created[0]["title"], "z0live · Grok and xAI")
        if os.path.isdir(os.path.expanduser(GROK.cwd)):
            self.assertEqual(dash.created[0]["cwd"], os.path.expanduser(GROK.cwd))
        submit = next(p for m, p in dash.seen if m == "prompt.submit")
        self.assertEqual((submit["session_id"], submit["text"], submit["queued"]), ("live-stored1", "FULL MEMORY + req",
                                                                                   True))
        self.assertIn("FULL MEMORY", text)

    async def test_existing_chat_is_found_by_title_resumed_at_its_tip_and_not_recreated(self):
        async with Dashboard({"z0live · Grok and xAI": "tip7"}) as dash:
            (text, stored, created), composed = await self.ask(dash)
        self.assertEqual((stored, created, composed, dash.created), ("tip7", False, [False], []))
        resume = next(p for m, p in dash.seen if m == "session.resume")
        self.assertEqual(resume["session_id"], "tip7")

    async def test_a_known_chat_skips_the_lookup(self):
        async with Dashboard() as dash:
            (_, stored, created), _ = await self.ask(dash, known="tip9")
        self.assertEqual((stored, created), ("tip9", False))
        self.assertNotIn("session.list", [m for m, _ in dash.seen])

    async def test_a_busy_chat_queues_the_request_and_returns_its_own_answer_not_the_running_turns(self):
        async with Dashboard({"z0live · Grok and xAI": "tip7"}, running=True) as dash:
            (text, _, _), _ = await self.ask(dash)
        self.assertEqual(text, "answer to: req")
        self.assertTrue(all(p.get("queued") for m, p in dash.seen if m == "prompt.submit"), "never a steer")

    async def test_a_refusal_before_the_submit_is_not_started(self):
        for method in ("session.list", "session.create", "prompt.submit"):
            async with Dashboard(fail=method) as dash:
                with self.assertRaises(ts.NotStarted, msg=method):
                    await self.ask(dash)
        async with ClientSession() as http:
            with self.assertRaises(ts.NotStarted):
                await ts.ask_topic(http, "ws://127.0.0.1:9/api/ws", "tok", "tek", GROK, lambda c: "x")


class CallTopicTests(unittest.IsolatedAsyncioTestCase):
    """mobile_live: a call's requests go to their group's chat; the group's chat is reused across requests; a burst
    past DEPTH or a refused submit runs beside the chat as before; never a second run after a submit."""

    def setUp(self):
        on = patch.object(live.topic_sessions, 'ENABLED', True)  # off by default under unittest (topic_sessions)
        on.start()
        self.addCleanup(on.stop)

    def test_the_suite_default_is_off_and_the_service_default_is_on(self):
        import subprocess
        code = "import topic_sessions as t; print(t.ENABLED)"
        env = {k: v for k, v in os.environ.items() if k != "Z0LIVE_TOPIC_SESSIONS"}
        out = subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(__file__) or ".", env=env,
                             capture_output=True, text=True, timeout=30).stdout.strip()
        self.assertEqual(out, "True", "the service (no unittest loaded) runs group chats by default")
        off = subprocess.run([sys.executable, "-c", code], cwd=os.path.dirname(__file__) or ".",
                             env={**env, "Z0LIVE_TOPIC_SESSIONS": "0"}, capture_output=True, text=True,
                             timeout=30).stdout.strip()
        self.assertEqual(off, "False")

    async def call(self):
        call = live.Call(live.Manager(None), profile='tek')
        call.token = 'tok'
        call.send = lambda event, to_provider=False, **kw: None
        self.addAsyncCleanup(call.close, 'test')
        return call

    async def test_three_grok_requests_land_in_one_chat_not_three_sessions(self):
        call = await self.call()
        chats, texts = [], []

        async def ask_topic(http, url, token, profile, group, compose, known="", on_submit=None):
            created = not known
            texts.append(compose(created))
            chats.append((group.key, known or "new"))
            return f"{group.key} answer", known or f"stored-{group.key}", created

        background = mock.AsyncMock(side_effect=AssertionError("a bg_* session per request"))
        with patch.object(live.topic_sessions, 'ask_topic', ask_topic), \
                patch.object(live.chat_continuity, 'ask_background', background), \
                patch.object(live.chat_continuity, 'chat_memory', return_value="CHAT MEMORY"):
            for i, req in enumerate(["send me grok link for oauth", "send me the link plzzz", "is grok signed in"]):
                self.assertEqual(await call.ask_chat_slot(f"d{i}", req, "anchor1"), "grok answer")
        self.assertEqual(chats, [("grok", "new"), ("grok", "stored-grok"), ("grok", "stored-grok")])
        self.assertIn("CHAT MEMORY", texts[0], "a new group chat starts with the anchored chat's memory")
        self.assertNotIn("CHAT MEMORY", texts[1], "later turns carry only the request and the call's recent words")
        self.assertEqual((call.counts["topic_turns"], call.counts["topic_chats_created"]), (3, 1))
        self.assertEqual(call.hermes_path["d1"], "topic")

    async def test_past_depth_or_refused_it_runs_beside_the_chat_and_never_twice(self):
        call = await self.call()
        gate = asyncio.Event()
        ran = []

        async def ask_topic(http, url, token, profile, group, compose, known="", on_submit=None):
            ran.append("topic")
            await gate.wait()
            return "topic answer", "stored", True

        async def background(http, token, profile, sid, request, on_receipt=None):
            ran.append("background")
            return "background answer"
        with patch.object(live.topic_sessions, 'ask_topic', ask_topic), patch.object(live.topic_sessions, 'DEPTH', 1), \
                patch.object(live.chat_continuity, 'ask_background', background), \
                patch.object(live.chat_continuity, 'chat_memory', return_value=""):
            first = asyncio.ensure_future(call.ask_chat_slot("d1", "grok link", "anchor1"))
            await asyncio.sleep(0.05)
            self.assertEqual(await call.ask_chat_slot("d2", "grok link again", "anchor1"), "background answer")
            gate.set()
            self.assertEqual(await first, "topic answer")
        self.assertEqual(ran, ["topic", "background"])
        self.assertEqual(call.counts["topic_overflow"], 1)

        async def refused(*a, **k):
            raise ts.NotStarted("session.create: agent still starting")

        async def after_submit(*a, **k):
            raise RuntimeError("chat socket closed before the group chat's turn finished")
        call2 = await self.call()
        with patch.object(live.topic_sessions, 'ask_topic', refused), \
                patch.object(live.chat_continuity, 'ask_background', background), \
                patch.object(live.chat_continuity, 'chat_memory', return_value=""):
            self.assertEqual(await call2.ask_chat_slot("d1", "grok link", "anchor1"), "background answer")
        self.assertEqual(call2.counts["topic_refused"], 1)
        bg = mock.AsyncMock(side_effect=AssertionError("a second run after the submit"))
        with patch.object(live.topic_sessions, 'ask_topic', after_submit), \
                patch.object(live.chat_continuity, 'ask_background', bg):
            with self.assertRaises(RuntimeError):
                await call2.ask_chat_slot("d2", "grok link", "anchor1")

    async def test_off_switch_keeps_one_background_agent_per_request(self):
        call = await self.call()

        async def background(http, token, profile, sid, request, on_receipt=None):
            return "background answer"
        with patch.object(live.topic_sessions, 'ENABLED', False), \
                patch.object(live.topic_sessions, 'ask_topic', side_effect=AssertionError("topic path")), \
                patch.object(live.chat_continuity, 'ask_background', background), \
                patch.object(live.chat_continuity, 'chat_memory', return_value=""):
            self.assertEqual(await call.ask_chat_slot("d1", "grok link", "anchor1"), "background answer")


class ReplayTopicTests(unittest.TestCase):
    """The real adapter over replay.py's fake dashboard (which now keeps titled group chats): requests about one
    project share one chat across the call, a different project gets its own, and every request still gets its own
    answer once. Shape of the 11-28 13:39 call; the words are made up."""

    def test_same_project_requests_share_one_chat_and_each_is_answered_once(self):
        import replay
        from test_replay import delegation, hermes, turn
        rows = [{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}]
        asks = [(5, "d1", "Is the Grok voice signed in?", "Grok is signed in."),
                (20, "d2", "Send me the link again", "Here is the link."),  # no project word: sticky to Grok
                (35, "d3", "The TV app is laggy when I switch screens", "The TV view takes three seconds."),
                (40, "d4", "Can Grok hear the TV app too?", "Grok hears the call, not the TV."),  # tie -> sticky
                (55, "d5", "Why is the TV remote slow", "The remote waits on a full render.")]
        for i, (t, did, text, answer) in enumerate(asks):
            rows += (turn(t, t + 3, "user", text, f"u{i}") + delegation(t + 2.8, did, text)
                     + turn(t + 3, t + 4, "assistant", "Checking.", f"a{i}") + hermes(did, 8, answer))
        with patch.object(live.chat_continuity, "chat_memory", return_value=""):
            r = replay.Replay(rows, topics=True)
            report = asyncio.run(r.run())
        self.assertEqual(report["sessions"], {"background": 0, "chat_turns": 0, "group_chats": 2, "group_turns": 5})
        chats = dict(r.topic_submits)
        self.assertEqual(chats["d1"], chats["d2"])
        self.assertEqual(chats["d3"], chats["d5"])
        self.assertNotEqual(chats["d1"], chats["d3"])
        self.assertEqual((report["unanswered"], report["held"], report["duplicate_finals"]), ([], [], {}))

        off = replay.Replay(rows, topics=False)
        with patch.object(live.chat_continuity, "chat_memory", return_value=""):
            before = asyncio.run(off.run())
        self.assertEqual(before["sessions"]["background"], 5, "before: one bg_* session per request")


if __name__ == "__main__":
    unittest.main()
