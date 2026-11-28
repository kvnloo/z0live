"""Voice @-mentions of another bot profile (bot_dm.py) and their route in the voice adapter."""
import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bot_dm  # noqa: E402

NAMES = ["burn-ultra", "cfg", "guest", "kun", "matt", "pc", "poteto", "tek", "z0", "z0-judge"]


class ParseTests(unittest.TestCase):
    def p(self, text, me="tek"):
        return bot_dm.parse(text, NAMES, me)

    def test_mentions(self):
        cases = {
            "at PC bot list the WD drive": ("pc", "list the WD drive"),
            "At PC bot, list the WD drive.": ("pc", "list the WD drive"),
            # what gpt-live actually delegated for the spoken words on 11-28
            "At PCBot, list the WD drive, which volumes are mounted": ("pc", "list the WD drive, which volumes are mounted"),
            "ask the pcbot to list the WD drive": ("pc", "list the WD drive"),
            "@pc list the WD drive": ("pc", "list the WD drive"),
            "at p.c. bot what's on the WD drive?": ("pc", "what's on the WD drive"),
            "Ask the PC bot to list the WD drive.": ("pc", "list the WD drive"),
            "Can you ask the PC bot to list the WD drive?": ("pc", "list the WD drive"),
            "tell the potato bot that the run finished": ("poteto", "the run finished"),
            "Send a message to the PC bot saying hi there": ("pc", "hi there"),
            "ask @z0-judge to score take 3": ("z0-judge", "score take 3"),
            "ok at z0 bot: status of the JEPA run": ("z0", "status of the JEPA run"),
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(self.p(text), want)

    def test_not_mentions(self):
        for text in ("what did the PC bot say", "did you ask the PC bot to list the drive", "ask tek about it",
                     "list the WD drive", "at the moment, list the drive", "what's the weather", "at PC bot",
                     "ask the PC bot", "at 5 pm remind me"):
            with self.subTest(text=text):
                self.assertIsNone(self.p(text))

    def test_self_is_not_a_mention(self):
        self.assertIsNone(self.p("at tek bot list the drive", me="tek"))


class SendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        for n in NAMES:
            (self.tmp / n).mkdir()
        self.env = patch.dict(os.environ, {"Z0LIVE_BOT_DM_PROFILES": str(self.tmp), "Z0LIVE_HERMES_PY": sys.executable,
                                           "Z0LIVE_BOT_DM_RUNNER": str(self.tmp / "runner.py")})
        self.env.start()

    def tearDown(self):
        self.env.stop()

    def fake_runner(self, payload, receipt=None):
        """A stand-in for hermes-agent's bot_mode_dm.py runner: records its argv and dm text, prints payload."""
        (self.tmp / "runner.py").write_text(
            "import json, sys, pathlib\n"
            f"out = pathlib.Path({str(self.tmp)!r})\n"
            "a = sys.argv[1:]\n"
            "(out / 'argv.json').write_text(json.dumps(a))\n"
            "(out / 'dm.txt').write_text(pathlib.Path(a[a.index('query-file') + 1]).read_text())\n"
            f"print(json.dumps({payload!r}))\n")
        if receipt:
            d = self.tmp / "pc/runtime/bot_live_delivery"
            d.mkdir(parents=True)
            (d / f"{payload['delivery_id']}.json").write_text(json.dumps(receipt))

    def test_round_trip_through_the_message_agent_runner(self):
        self.fake_runner({"outcome": "@pc replied", "status": "settled", "delivery_id": "abc", "reply": "WD has 3 volumes."},
                         {"session_id": "local-pcbotchat", "status": "settled"})
        res = bot_dm.send("pc", "list the WD drive", "tek", "dd80")
        self.assertTrue(res["ok"], res)
        self.assertEqual((res["reply"], res["delivery_id"], res["session_id"]), ("WD has 3 volumes.", "abc", "local-pcbotchat"))
        argv = json.loads((self.tmp / "argv.json").read_text())
        self.assertEqual(argv[:2], ["--run-delivery", "--author"])
        self.assertEqual(json.loads(argv[2]), {"id": "bot:tek", "name": "tek", "is_bot": True})
        self.assertEqual(argv[3], "query-file")
        self.assertEqual(argv[5:], ["--profile-home", str(self.tmp / "pc"), "hermes", "-p", "pc"])
        dm = (self.tmp / "dm.txt").read_text()
        self.assertTrue(dm.startswith("Message from 🤖 tek (@tek): "))
        self.assertIn("list the WD drive", dm)
        self.assertFalse(Path(argv[4]).exists())  # the plaintext is removed after the run
        self.assertEqual(bot_dm.spoken(res, "The PC bot"), "The PC bot says: WD has 3 volumes.")

    def test_guest_is_refused_before_anything_runs(self):
        self.fake_runner({"status": "settled", "delivery_id": "x", "reply": "no"})
        res = bot_dm.send("pc", "list the WD drive", "guest", "c")
        self.assertFalse(res["ok"])
        self.assertTrue(res["refused"])
        self.assertFalse((self.tmp / "argv.json").exists())

    def test_failure_is_reported(self):
        self.fake_runner({"reason": "runtime_unavailable", "delivery_id": "y",
                          "error": "canonical Bot Chat target is unavailable — the message was NOT sent"})
        res = bot_dm.send("pc", "x", "tek", "c")
        self.assertFalse(res["ok"])
        self.assertIn("NOT sent", bot_dm.spoken(res, "The PC bot"))

    def test_unknown_target(self):
        self.assertFalse(bot_dm.send("nobody", "x", "tek")["ok"])


class VoiceRouteTests(unittest.IsolatedAsyncioTestCase):
    """host_delegate sends a bot mention through bot_dm instead of running Hermes, and keeps the ledger honest."""

    async def asyncSetUp(self):
        self.ledger = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"Z0LIVE_LEDGER": self.ledger})
        self.env.start()
        import mobile_live as live
        import ledger as request_ledger
        self.live, self.request_ledger = live, request_ledger
        self.calls = []

    async def asyncTearDown(self):
        self.env.stop()

    async def make(self, profile):
        call = self.live.Call(self.live.Manager(None), profile=profile)
        call.protocol.seen.append("d1")
        self.sent = []
        call.send = lambda event, to_provider=False: self.sent.append(event)
        self.calls.append(call)
        return call

    def spoken(self):
        return " ".join(c.get("text", "") for e in self.sent for c in e.get("content", []) if isinstance(c, dict))

    async def test_mention_reaches_the_bot_and_its_reply_is_spoken_with_proof(self):
        call = await self.make("tek")
        seen = {}

        def fake_send(target, message, sender, call_id="", timeout=0):
            seen.update(target=target, message=message, sender=sender)
            return {"ok": True, "target": target, "status": "settled", "delivery_id": "d" * 64,
                    "session_id": "local-pc", "reply": "Three volumes, 2.1 TB free."}

        with patch.object(self.live, "run_hermes", side_effect=AssertionError("Hermes must not run")), \
                patch.object(self.live.bot_dm, "parse", return_value=("pc", "list the WD drive")), \
                patch.object(self.live.bot_dm, "send", side_effect=fake_send):
            await call.host_delegate("d1", "at PC bot list the WD drive")
            await asyncio.gather(*list(call.tasks), return_exceptions=True)
        self.assertEqual(seen, {"target": "pc", "message": "list the WD drive", "sender": "tek"})
        self.assertIn("Three volumes, 2.1 TB free.", self.spoken())
        it = self.request_ledger.get(call.item_of["d1"])
        self.assertEqual(it["state"], "done")
        self.assertTrue(any(r["kind"] == "dm" and r["ref"] == "d" * 64 for r in it["receipts"]))
        self.assertIn("local-pc", json.dumps(it["receipts"]))
        await call.close("test")

    async def test_guest_call_never_relays(self):
        call = await self.make("guest")
        with patch.object(self.live.bot_dm, "send", side_effect=AssertionError("guest must not relay")), \
                patch.object(self.live.bot_dm, "parse", return_value=("pc", "list the WD drive")):
            handled = call.bot_mention("d1", "at PC bot list the WD drive")
        self.assertTrue(handled)
        self.assertIn("isn't available", self.spoken())
        await call.close("test")

    async def test_other_requests_are_not_mentions(self):
        call = await self.make("tek")
        with patch.object(self.live.bot_dm, "send", side_effect=AssertionError("not a mention")):
            self.assertFalse(call.bot_mention("d1", "what's the weather"))
        await call.close("test")


if __name__ == "__main__":
    unittest.main()
