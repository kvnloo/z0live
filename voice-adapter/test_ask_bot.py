"""ask_bot: parse the spoken command, refuse guest/secrets, deliver to a fake host, prove it on the ledger."""
import asyncio
import http.server
import json
import os
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

import ask_bot


class FakeHost(http.server.BaseHTTPRequestHandler):
    seen: list = []

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        FakeHost.seen.append({"auth": self.headers.get("Authorization"), "session": self.headers.get("X-Hermes-Session-Id"),
                              "body": body})
        out = json.dumps({"id": "chatcmpl-test1", "choices": [{"message": {"content": "Drive has 3 folders. token: hunter22"}}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Hermes-Session-Id", self.headers.get("X-Hermes-Session-Id"))
        self.end_headers()
        self.wfile.write(out.encode())

    def log_message(self, *_):
        pass


class AskBotTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.server = http.server.HTTPServer(("127.0.0.1", 0), FakeHost)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        port = self.server.server_address[1]
        (root / ".config/z0live/guest-hosts").mkdir(parents=True)
        (root / ".config/z0live/guest-hosts/pc.key").write_text("k-pc\n")
        self.env = mock.patch.dict(os.environ, {"Z0LIVE_ASK_HOSTS": json.dumps({"pc": port, "guest": port}),
                                                "Z0LIVE_LEDGER": str(root / "ledger"),
                                                "HERMES_HOME": str(root / ".hermes/profiles/tek")})
        self.env.start()
        self.home = mock.patch.object(ask_bot, "HOME", root)
        self.home.start()
        FakeHost.seen = []

    def tearDown(self):
        self.home.stop()
        self.env.stop()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def test_parse_forms(self):
        cases = {
            "ask @pc to list the drive": ("pc", "list the drive"),
            "Ask the PC bot: list the drive": ("pc", "list the drive"),
            "@pc list the drive": ("pc", "list the drive"),
            "at pc, list the drive": ("pc", "list the drive"),
            "okay can you ask p c to list the drive": ("pc", "list the drive"),
            "tell pc to check the disk": ("pc", "check the disk"),
            "ask P.C. whether the drive is mounted": ("pc", "whether the drive is mounted"),
            # Ported from feat/voice-bot-dm aa9cc2b: gpt-live wrote "PCBot" in the 11-27 real-voice probe.
            "At PCBot, list the WD drive, which volumes are mounted": ("pc", "list the WD drive, which volumes are mounted"),
            "At PC bot, list the WD drive": ("pc", "list the WD drive"),
            "ask the PCbot to list the drive": ("pc", "list the drive"),
            # ASR heard "At PC bot," as "That PC bot," in the 11-28 real-voice probe.
            "That PC bot, list the WD drive": ("pc", "list the WD drive"),
        }
        for text, want in cases.items():
            self.assertEqual(ask_bot.parse(text), want, text)
        for text in ("ask the team to list the drive", "ask him", "ask @guest to do x", "ask pcs what", "what's at pc?",
                     "list the drive", "what did the PC bot say", "ask pcbots what",
                     "that PC bot is slow", "that pc, list the drive", "that PC bot"):
            self.assertIsNone(ask_bot.parse(text), text)

    def test_guest_never(self):
        self.assertNotIn("guest", ask_bot.hosts())
        self.assertFalse(ask_bot.ask("guest", "hi")["ok"])
        self.assertFalse(ask_bot.ask("pc", "hi", caller="guest")["ok"])
        with mock.patch.dict(os.environ, {"HERMES_HOME": "/x/.hermes/profiles/guest"}):
            self.assertFalse(ask_bot.ask("pc", "hi")["ok"])
        self.assertEqual(FakeHost.seen, [])

    def test_secret_refused(self):
        out = ask_bot.ask("pc", "use password: swordfish to log in")
        self.assertFalse(out["ok"])
        self.assertEqual(FakeHost.seen, [])

    def test_delivery_and_ledger(self):
        out = ask_bot.ask("pc", "list the drive", call="c0ffee", by="voice", via="voice")
        self.assertTrue(out["ok"], out)
        self.assertEqual(out["delivery"]["id"], "chatcmpl-test1")
        self.assertNotIn("hunter22", out["answer"])  # masked before it is spoken or written
        self.assertTrue(out["say"].startswith("pc says: Drive has 3 folders."))
        req = FakeHost.seen[0]
        self.assertEqual(req["auth"], "Bearer k-pc")
        # One session per project and day on the asked bot, not per call ("list the drive" names none: general).
        self.assertEqual(req["session"], ask_bot.topic_sessions.day_session("z0live-ask-tek",
                                                                            ask_bot.topic_sessions.GENERAL_GROUP))
        self.assertEqual(req["body"]["messages"][-1], {"role": "user", "content": "list the drive"})
        led = ask_bot.ledger()
        it = led.get(out["item"])
        self.assertEqual(it["call"], "c0ffee")
        self.assertEqual(it["state"], "done")
        self.assertTrue(it["proven"])
        self.assertEqual(it["receipts"][0]["kind"], "ask")
        self.assertEqual(it["receipts"][0]["ref"], "pc:chatcmpl-test1")
        self.assertIn("Drive has 3 folders", it["results"][0]["text"])
        self.assertNotIn("hunter22", Path(os.environ["Z0LIVE_LEDGER"], "ledger.jsonl").read_text())

    def test_host_down_is_a_failed_item(self):
        with mock.patch.dict(os.environ, {"Z0LIVE_ASK_HOSTS": json.dumps({"pc": 1})}):
            out = ask_bot.ask("pc", "list the drive")
        self.assertFalse(out["ok"])
        self.assertIn("couldn't reach pc", out["say"])
        self.assertEqual(ask_bot.ledger().get(out["item"])["state"], "failed")

    def test_unknown_profile_lists_choices(self):
        out = ask_bot.ask("zz", "hi")
        self.assertFalse(out["ok"])
        self.assertIn("pc", out["error"])


class SlowHost(http.server.BaseHTTPRequestHandler):
    """A threaded host that answers after a beat and records how many turns ran at once in each session."""
    seen: list = []
    active: dict = {}
    peak: dict = {}
    lock = threading.Lock()

    def do_POST(self):  # noqa: N802
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        sid = self.headers.get("X-Hermes-Session-Id")
        with SlowHost.lock:
            SlowHost.active[sid] = SlowHost.active.get(sid, 0) + 1
            SlowHost.peak[sid] = max(SlowHost.peak.get(sid, 0), SlowHost.active[sid])
            SlowHost.seen.append((sid, body["messages"][-1]["content"]))
        threading.Event().wait(0.15)
        with SlowHost.lock:
            SlowHost.active[sid] -= 1
        out = json.dumps({"id": f"chatcmpl-{len(SlowHost.seen)}", "choices": [{"message": {"content": "done"}}]})
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("X-Hermes-Session-Id", sid)
        self.end_headers()
        self.wfile.write(out.encode())

    def log_message(self, *_):
        pass


class AskTopicSessionTests(unittest.TestCase):
    """ask @bot: asks about one repo share one warm session on the bot, a card/device/other repo gets its own, a
    generic follow-up stays with the last, and concurrent asks for one session take turns without dropping any."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for name in ("z0live", "hermes-lanes", "atlas-engine-tv"):
            (root / "repos" / name).mkdir(parents=True)
        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), SlowHost)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        (root / ".config/z0live/guest-hosts").mkdir(parents=True)
        (root / ".config/z0live/guest-hosts/pc.key").write_text("k-pc\n")
        self.patches = [
            mock.patch.dict(os.environ, {"Z0LIVE_ASK_HOSTS": json.dumps({"pc": self.server.server_address[1]}),
                                         "Z0LIVE_LEDGER": str(root / "ledger"), "Z0LIVE_ASK_STATE": str(root / "state"),
                                         "HERMES_HOME": str(root / ".hermes/profiles/tek")}),
            mock.patch.object(ask_bot, "HOME", root),
            mock.patch.object(ask_bot.topic_sessions, "REPO_ROOT", root / "repos"),
        ]
        for p in self.patches:
            p.start()
        SlowHost.seen, SlowHost.active, SlowHost.peak = [], {}, {}

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        self.server.shutdown()
        self.server.server_close()
        self.tmp.cleanup()

    def session(self, message):
        out = ask_bot.ask("pc", message, call="c1")
        self.assertTrue(out["ok"], out)
        return out["delivery"]["session"]

    def test_three_asks_about_one_repo_are_one_session_and_others_get_their_own(self):
        same = {self.session(m) for m in ("what changed in hermes-lanes today", "run the hermes-lanes tests",
                                          "is hermes-lanes on main")}
        self.assertEqual(len(same), 1, same)
        lanes = same.pop()
        followup = self.session("and send me the link")  # names nothing: stays with the last project
        card_a = self.session("what's blocking t_1a2b3c4d")
        card_b = self.session("close t_9f8e7d6c")
        s22 = self.session("is the s22 camera on")
        lenovo = self.session("restart the lenovo")
        tv = self.session("deploy atlas-engine-tv")  # a repo a group points at is that group
        self.assertEqual(followup, lanes)
        self.assertEqual(len({lanes, card_a, card_b, s22, lenovo, tv}), 6)
        self.assertTrue(tv.startswith("z0live-ask-tek-tv-"), tv)
        self.assertEqual(self.session("what's blocking t_1a2b3c4d now"), card_a)

    def test_concurrent_asks_for_one_session_take_turns_and_none_is_dropped(self):
        outs = []
        threads = [threading.Thread(target=lambda i=i: outs.append(ask_bot.ask("pc", f"hermes-lanes check {i}",
                                                                               call=f"c{i}")))
                   for i in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(len(outs), 3)
        self.assertTrue(all(o["ok"] for o in outs), outs)
        self.assertEqual(sorted(m for _, m in SlowHost.seen), [f"hermes-lanes check {i}" for i in range(3)])
        self.assertEqual({s for s, _ in SlowHost.seen}, {outs[0]["delivery"]["session"]}, "one warm session")
        self.assertEqual(max(SlowHost.peak.values()), 1, "never two turns at once in one session")

    def test_a_session_held_past_the_wait_runs_in_the_calls_own_session(self):
        group = ask_bot.topic_sessions.classify("hermes-lanes", ask_bot.topic_sessions.load_groups(), repos=None)[0]
        warm = ask_bot.topic_sessions.day_session("z0live-ask-tek", group)
        with mock.patch.object(ask_bot, "SESSION_WAIT_S", 0.2), \
                ask_bot.topic_sessions.session_flock(ask_bot.state_dir() / "ask-locks", "pc-" + warm, 1) as held:
            self.assertTrue(held)
            self.assertEqual(self.session("hermes-lanes status"), "z0live-ask-tek-c1")


class VoiceAskTests(unittest.IsolatedAsyncioTestCase):
    """The voice adapter turns a spoken "ask @pc ..." into ask_bot.ask, never a tek Hermes run."""

    async def asyncSetUp(self):
        import mobile_live as live
        self.live = live
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"Z0LIVE_ASK_HOSTS": json.dumps({"pc": 1}),
                                                "Z0LIVE_LEDGER": str(Path(self.tmp.name) / "ledger"),
                                                "Z0LIVE_ASK_STATE": str(Path(self.tmp.name) / "state")})
        self.env.start()
        self.call = live.Call(live.Manager(None), profile="tek")
        self.call.protocol.seen.extend(["d1", "d2", "d3"])
        self.sent = []
        self.call.send = lambda event, to_provider=False: self.sent.append(event)

    async def asyncTearDown(self):
        await self.call.close("test")
        self.env.stop()
        self.tmp.cleanup()

    def spoken(self):
        return " ".join(c.get("text", "") for e in self.sent for c in e.get("content", []) if isinstance(c, dict))

    async def settle(self):
        for _ in range(50):
            await asyncio.sleep(0.01)

    async def test_ask_goes_to_the_bot_and_answer_is_spoken(self):
        got = []

        def fake(profile, message, **kw):
            got.append((profile, message, kw))
            return {"ok": True, "say": "pc says: the drive holds backups and media.", "delivery": {"id": "chatcmpl-x"}}

        with mock.patch.object(self.live, "run_hermes", side_effect=AssertionError("tek Hermes must not run")), \
                mock.patch.object(self.live.ask_bot, "ask", side_effect=fake):
            await self.call.host_delegate("d1", "Ask the PC bot to list the drive.")
            await self.settle()
        self.assertEqual(got[0][:2], ("pc", "list the drive."))
        self.assertEqual(got[0][2]["item"], self.call.item_of["d1"])  # the ledger item this call opened
        self.assertEqual(got[0][2]["call"], self.call.ledger_call)
        self.assertIn("Sent to pc", self.spoken())
        self.assertIn("pc says: the drive holds backups", self.spoken())

    async def test_user_words_back_a_paraphrase_that_names_the_bot(self):
        self.call.protocol.turn_text["user"] = "uh ask @pc to list the drive"
        with mock.patch.object(self.live.ask_bot, "ask", return_value={"ok": True, "say": "pc says: ok"}) as ask:
            self.assertTrue(self.call.bot_ask("d1", "List the drive on pc"))
            await self.settle()
        self.assertEqual(ask.call_args[0], ("pc", "list the drive"))

    async def test_same_words_twice_is_one_ask_and_others_are_not_asks(self):
        with mock.patch.object(self.live.ask_bot, "ask", return_value={"ok": True, "say": "pc says: ok"}) as ask:
            self.assertTrue(self.call.bot_ask("d1", "ask pc to list the drive"))
            self.assertTrue(self.call.bot_ask("d2", "ask pc to list the drive"))
            self.assertFalse(self.call.bot_ask("d3", "what's the weather"))
            await self.settle()
        self.assertEqual(ask.call_count, 1)

    async def test_guest_profile_call_never_asks(self):
        self.call.profile = "guest"
        self.assertFalse(self.call.bot_ask("d1", "ask pc to list the drive"))

    async def test_failure_is_spoken(self):
        await self.call.host_delegate("d1", "ask pc to list the drive")  # port 1: nothing listens
        for _ in range(200):
            await asyncio.sleep(0.02)
            if "couldn't reach pc" in self.spoken():
                break
        self.assertIn("couldn't reach pc", self.spoken())
        self.assertEqual(self.live.request_ledger.get(self.call.item_of["d1"])["state"], "failed")


if __name__ == "__main__":
    unittest.main()
