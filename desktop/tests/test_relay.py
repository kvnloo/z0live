"""Relay-to-Hermes and voice grounding: deterministic, no gateway, no provider."""

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
from z0live_desktop import relay  # noqa: E402
from z0live_desktop.core import Store  # noqa: E402
from z0live_desktop.delegation import Delegator, PhoneRouter, backend_instructions  # noqa: E402

NOW = 1_800_000_000.0
PHONE_TOOLS = [
    {"type": "function", "name": name, "description": about,
     "parameters": {"type": "object", "properties": {}}}
    for name, about in (("open_app", "Open an app"), ("phone_do", "Do an in-app step"))
]


def make_home(root: Path) -> Path:
    """A SessionDB with only the columns the relay reads."""
    db = sqlite3.connect(root / "state.db")
    db.executescript("""
        CREATE TABLE sessions(id TEXT PRIMARY KEY, source TEXT, created_source TEXT, title TEXT,
          message_count INTEGER, cwd TEXT, started_at REAL, last_activity_at REAL,
          last_activity_description TEXT, parent_session_id TEXT, hidden INTEGER DEFAULT 0,
          archived INTEGER DEFAULT 0);
        CREATE TABLE messages(id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, role TEXT,
          content TEXT);
    """)
    rows = [
        # id, source, created_source, title, count, active, doing, parent
        ("local-main", "local", "cli", "Debug Hermes crashing", 300, NOW - 60,
         "delegate_task: subagent running terminal", None),
        ("local-probe", "local", "tui", "Ping pong connectivity check", 4, NOW - 10, "", None),
        ("local-garden", "local", "cli", "Garden Room planner fixes", 80, NOW - 7200, "", None),
        ("sub-1", "subagent", None, "Subagent: ring gestures", 50, NOW - 30, "", "local-main"),
        ("z0live-voice", "api_server", None, "z0live voice", 70, NOW - 5, "", None),
        ("cron-1", "cron", None, "Weekly review", 40, NOW - 1, "", None),
    ]
    for sid, src, created, title, count, active, doing, parent in rows:
        db.execute("INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?,?,?,0,0)",
                   (sid, src, created, title, count, "/", active - 100, active, doing, parent))
    for sid, role, content in [
        ("local-main", "user", "fix the ring double tap please"),
        ("local-main", "user", "[Relayed from Kevin's z0live voice call, 01:28] earlier relay"),
        ("local-main", "user", "[IMPORTANT: Background process finished]"),
        ("local-garden", "user", "make the planter shelf deeper"),
    ]:
        db.execute("INSERT INTO messages(session_id, role, content) VALUES(?,?,?)", (sid, role, content))
    db.commit()
    db.close()
    return root


class Done:
    def __init__(self, stdout, stderr=""):
        self.stdout, self.stderr = stdout, stderr


class FakeGateway:
    """Records the helper invocation the way subprocess.run would receive it."""

    def __init__(self, reply):
        self.reply, self.calls = reply, []

    def __call__(self, argv, input, capture_output, text, timeout, env):
        self.calls.append({"argv": argv, "request": json.loads(input), "home": env.get("HERMES_HOME")})
        return Done(json.dumps(self.reply) + "\n")


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.home = make_home(Path(self.tmp.name))
        env = patch.dict(os.environ, {"Z0LIVE_RELAY_HOME": str(self.home)})
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("Z0LIVE_RELAY_DEFAULT", None)

    def test_main_session_is_the_busiest_terminal_session_not_probes_or_workers(self):
        self.assertEqual(relay.main_session()["id"], "local-main")
        for word in ("", "main", "Hermes", "my main session", None):
            self.assertEqual(relay.resolve(word)["id"], "local-main")
        with patch.dict(os.environ, {"Z0LIVE_RELAY_DEFAULT": "local-garden"}):
            self.assertEqual(relay.main_session()["id"], "local-garden")

    def test_session_can_be_named_by_title_words_or_id_prefix(self):
        self.assertEqual(relay.resolve("the garden room one")["id"], "local-garden")
        self.assertEqual(relay.resolve("local-gar")["id"], "local-garden")
        self.assertIsNone(relay.resolve("zebra spreadsheet"))
        self.assertIsNone(relay.resolve("weekly review"), "cron sessions are not relay targets")

    def test_recent_sessions_are_interactive_with_one_line_summaries(self):
        found = relay.recent_sessions(8, now=NOW)
        self.assertEqual([s["session_id"] for s in found], ["local-probe", "local-main", "local-garden"])
        main = found[1]["summary"]
        self.assertIn("Debug Hermes crashing", main)
        self.assertIn("last asked: fix the ring double tap please", main, "relays/notices are skipped")
        self.assertIn("now: delegate_task", main)
        self.assertIn("1 helper agent active", main)
        self.assertEqual([s["session_id"] for s in relay.recent_sessions(8, now=NOW, min_messages=20)],
                         ["local-main", "local-garden"])

    def test_relay_delivers_through_the_gateway_helper_and_reads_back_exactly(self):
        gateway = FakeGateway({"ok": True, "mode": "steer", "status": "queued",
                               "session_id": "local-main", "title": "Debug Hermes crashing"})
        text = "Ring gestures randomly toggle mute/unmute when I'm not tapping."
        before = os.environ.get("HERMES_HOME")
        result = relay.relay(text, runner=gateway)
        self.assertEqual(os.environ.get("HERMES_HOME"), before, "HERMES_HOME is set for the child only")
        self.assertTrue(result["ok"])
        self.assertEqual(result["say"], f'Sent to Debug Hermes crashing: "{text}"')
        self.assertEqual(result["delivered"], "into its running turn")
        call = gateway.calls[0]
        self.assertEqual(call["argv"][-1], str(relay.HELPER))
        self.assertEqual(call["home"], str(self.home.resolve()))
        self.assertEqual(call["request"]["session_id"], "local-main")
        self.assertTrue(call["request"]["text"].startswith("[Relayed from Kevin's z0live voice call, "))
        self.assertTrue(call["request"]["text"].endswith(text))

    def test_same_request_reuses_one_submission_id(self):
        gateway = FakeGateway({"ok": True, "mode": "next_turn", "status": "queued"})
        relay.relay("check the ring", key="call-1:item-1", runner=gateway)
        relay.relay("check the ring", key="call-1:item-1", runner=gateway)
        ids = {c["request"]["submission_id"] for c in gateway.calls}
        self.assertEqual(len(ids), 1)
        self.assertEqual(gateway.calls[0]["request"]["text"].split("] ", 1)[1], "check the ring")

    def test_failed_delivery_says_nothing_was_sent(self):
        gateway = FakeGateway({"ok": False, "error": "gateway_unavailable"})
        result = relay.relay("hello", runner=gateway)
        self.assertFalse(result["ok"])
        self.assertIn("nothing was sent", result["say"])
        self.assertNotIn("Sent to", result["say"])

    def test_unknown_target_lists_candidates_and_sends_nothing(self):
        gateway = FakeGateway({"ok": True})
        result = relay.relay("hello", to="zebra spreadsheet", runner=gateway)
        self.assertEqual(result["error"], "no_matching_session")
        self.assertIn("Debug Hermes crashing", result["candidates"])
        self.assertEqual(gateway.calls, [])

    def test_empty_or_huge_text_is_refused(self):
        gateway = FakeGateway({"ok": True})
        self.assertFalse(relay.relay("   ", runner=gateway)["ok"])
        self.assertFalse(relay.relay("x" * 4001, runner=gateway)["ok"])
        self.assertEqual(gateway.calls, [])

    def test_relay_intent_phrases(self):
        for text in ("can you pass along a message to Hermes", "pass that along", "Tell Hermes the ring bug",
                     "send it to Hermes", "let Hermes know the build broke", "message Hermes about it"):
            self.assertTrue(relay.wants_relay(text), text)
        for text in ("open Spotify", "Yeah", "what is Hermes working on", "send a text to Mom"):
            self.assertFalse(relay.wants_relay(text), text)


class DelegationRelayTests(unittest.TestCase):
    def setUp(self):
        selected = patch("z0live_desktop.voice_runtime.load_plan", return_value=None)
        selected.start()
        self.addCleanup(selected.stop)
        env = patch.dict(os.environ, {"Z0LIVE_LIVE_TRANSPORT": "bidi"})
        env.start()
        self.addCleanup(env.stop)
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.device = self.store.authenticate(self.store.pair("unit"))
        with patch("z0live_desktop.core.mint", side_effect=AssertionError("bidi must not mint")):
            self.sid = self.store.new_session(
                self.device, {"client": "lawnchair", "tools": PHONE_TOOLS, "transports": ["bidi"]}
            )["session_id"]

    def say(self, *turns):
        self.store.add_events(self.device, [
            {"session_id": self.sid, "type": kind, "text": text, "data": {"event_id": f"e{i}"}}
            for i, (kind, text) in enumerate(turns)
        ])

    def test_relay_requests_never_reach_the_phone(self):
        picked = []
        router = PhoneRouter(self.store, small=lambda *a: picked.append(a) or {"text": '{"tool":"open_app"}'})
        plan = {"roles": {"smol": {"provider": "unit", "model": "unit"}}}
        text = "Tell Hermes the menu button tap on my screen is broken, pass it along"
        with patch("z0live_desktop.voice_runtime.load_plan", return_value=plan):
            self.assertIsNone(router.route("d", text, PHONE_TOOLS))
            self.assertEqual(picked, [], "no liaison phone pick for a relay")
            self.assertIsNotNone(router.route("d", "tap the menu button", PHONE_TOOLS))
        delegator = Delegator(self.store, hermes=object(), router=router)
        call = type("C", (), {"sid": self.sid})()
        self.assertIsNone(delegator.phone_route(call, "d", text, PHONE_TOOLS, {}))
        self.assertEqual(delegator.phone_route(call, "d", "tap the menu button", PHONE_TOOLS, {})["tool"],
                         "phone_do")

    def test_short_replies_reach_hermes_with_the_conversation(self):
        self.say(("user_transcript", "the ring keeps toggling mute when I'm not tapping"),
                 ("assistant_transcript", "Did you want me to pass that along?"))
        started = []

        class Hermes:
            def start(self, text, instructions, idem):
                started.append((text, instructions))
                return "run_1"

            def events(self, run_id):
                yield {"event": "run.completed"}

            def status(self, run_id):
                return {"status": "completed", "output": 'Sent to Debug Hermes crashing: "x"'}

        class Call:
            sid, call_id = self.sid, "rtc"
            closed = threading.Event()
            appended = []

            def append(self, did, text, channel=None):
                self.appended.append(text)
                return True

        delegator = Delegator(self.store, hermes=Hermes(), router=type("N", (), {"route": lambda *a: None})())
        call = Call()
        delegator.delegate(call, "item_yeah", "Yeah")
        end = time.time() + 5
        while time.time() < end and not call.appended:
            time.sleep(0.02)
        text, instructions = started[0]
        self.assertTrue(text.startswith("Recent voice conversation (data, oldest first):"))
        self.assertIn("Kevin: the ring keeps toggling mute when I'm not tapping", text)
        self.assertIn("You: Did you want me to pass that along?", text)
        self.assertTrue(text.endswith("Kevin's request now: Yeah"))
        self.assertIn("bin/z0live relay", instructions)
        self.assertIn('Sent to Debug Hermes crashing: "x"', call.appended[0])

    def test_backend_instructions_ground_hermes_and_make_relay_first_class(self):
        text = backend_instructions(self.sid, PHONE_TOOLS)
        self.assertIn("never means the Greek god", text)
        self.assertIn("relay to Kevin's own Hermes session is not a messaging action", text)
        self.assertIn("Never use phone actions, screen taps, kanban cards", text)
        self.assertIn("bin/z0live sessions --limit 8", text)
        self.assertIn('exactly the command\'s "say" value', text)


if __name__ == "__main__":
    unittest.main()
