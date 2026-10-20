"""Deterministic tests for the Frameless Bidi broker path (no network, no provider)."""

import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop import bidi_protocol as bp  # noqa: E402
from z0live_desktop import live  # noqa: E402
from z0live_desktop.core import Store  # noqa: E402
from z0live_desktop.delegation import (  # noqa: E402
    CANCEL,
    Delegator,
    PhoneRouter,
    backend_instructions,
    describe_phone_result,
    parse_route,
)

TOKEN_CLAIMS = {"https://api.openai.com/auth": {"chatgpt_account_id": "acct-unit"}}


def fake_token():
    import base64

    body = base64.urlsafe_b64encode(json.dumps(TOKEN_CLAIMS).encode()).decode().rstrip("=")
    return "h." + body + ".s"


PHONE_TOOLS = [
    {
        "type": "function",
        "name": "open_app",
        "description": "Open an installed app by name",
        "parameters": {"type": "object", "properties": {"name": {"type": "string"}}},
    }
]


class ProtocolTests(unittest.TestCase):
    def test_session_payload_matches_omp_wire(self):
        self.assertEqual(
            bp.session_payload("Be brief.", "sol"),
            {
                "model": "gpt-live-1-codex",
                "instructions": "Be brief.",
                "audio": {"output": {"voice": "sol"}},
                "delegation": {"type": "client"},
            },
        )
        with self.assertRaises(ValueError):
            bp.session_payload("x", "marin")  # not a Frameless Bidi voice

    def test_call_id_and_sideband_url(self):
        self.assertEqual(
            bp.parse_call_id("/v1/realtime/calls/rtc_abc-1?x=y"), "rtc_abc-1"
        )
        self.assertIsNone(bp.parse_call_id("/v1/calls/other"))
        self.assertIsNone(bp.parse_call_id(None))
        self.assertEqual(bp.sideband_url("rtc_a"), "wss://api.openai.com/v1/live/rtc_a")
        with self.assertRaises(ValueError):
            bp.sideband_url("../evil")

    def test_headers_carry_codex_identity_and_account(self):
        headers = bp.headers(fake_token(), "scoped", "rt")
        self.assertEqual(headers["OpenAI-Alpha"], "quicksilver=v2")
        self.assertEqual(headers["originator"], "Codex Desktop")
        self.assertEqual(headers["chatgpt-account-id"], "acct-unit")
        self.assertEqual(headers["session-id"], "scoped")
        self.assertEqual(headers["x-session-id"], "rt")

    def test_context_messages_and_chunking(self):
        self.assertEqual(
            bp.delegation_append("item_1", "done", "commentary"),
            {
                "type": "delegation.context.append",
                "delegation_item_id": "item_1",
                "channel": "commentary",
                "content": [{"type": "input_text", "text": "done"}],
            },
        )
        self.assertNotIn("channel", bp.delegation_append("item_1", "x"))
        with self.assertRaises(ValueError):
            bp.delegation_append("item_1", "x", "loud")
        text = "é" * 400 + "a" * 300
        parts = bp.chunk(text)
        self.assertEqual("".join(parts), text)
        self.assertTrue(all(len(p.encode()) <= 500 for p in parts))
        self.assertTrue(bp.final_message(" ok ").startswith('"Agent Final Message":\n\nok'))

    def test_parse_events(self):
        created = bp.parse_event(
            json.dumps(
                {
                    "type": "delegation.created",
                    "item": {
                        "id": "item_9",
                        "type": "delegation",
                        "target": "client",
                        "content": [
                            {"type": "input_text", "text": "check the tests"},
                            {"type": "image", "url": "x"},
                        ],
                    },
                }
            )
        )
        self.assertEqual(created, {"type": "delegation.created", "id": "item_9",
                                   "text": "check the tests", "user_turn_id": ""})
        self.assertIsNone(bp.parse_event('{"type":"delegation.created","item":{"target":"server"}}'))
        self.assertEqual(
            bp.parse_event({"type": "turn.done", "turn": {"id": "turn_1", "role": "user", "transcript": "hi"}}),
            {"type": "turn.done", "role": "user", "transcript": "hi", "id": "turn_1"},
        )
        self.assertEqual(
            bp.parse_event({"type": "error", "error": {"message": "bad"}}),
            {"type": "error", "message": "bad"},
        )
        self.assertEqual(bp.parse_event({"type": "x.y"})["type"], "unknown")
        self.assertIsNone(bp.parse_event("not json"))


class Response:
    def __init__(self, body, location):
        self.body, self.headers = body, {"Location": location}

    def read(self, _limit):
        return self.body.encode()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class SignalingTests(unittest.TestCase):
    def test_signal_posts_offer_and_parses_answer(self):
        seen = {}

        def opener(request, timeout):
            seen["url"] = request.full_url
            seen["body"] = json.loads(request.data)
            seen["headers"] = {k.lower(): v for k, v in request.header_items()}
            return Response("v=0\r\nanswer", "/v1/realtime/calls/rtc_unit")

        answer, call = live.signal("v=0\r\noffer", "persona", "sol", fake_token(), "s", "r", opener=opener)
        self.assertEqual((answer, call), ("v=0\r\nanswer", "rtc_unit"))
        self.assertIn("intent=quicksilver", seen["url"])
        self.assertEqual(seen["body"]["session"]["model"], "gpt-live-1-codex")
        self.assertEqual(seen["body"]["sdp"], "v=0\r\noffer")
        self.assertEqual(seen["headers"]["openai-alpha"], "quicksilver=v2")

    def test_signal_rejects_bad_offer_and_missing_call(self):
        with self.assertRaises(ValueError):
            live.signal("garbage", "p", "sol", fake_token(), "s", "r", opener=lambda *a, **k: None)
        with self.assertRaises(live.SignalingError):
            live.signal(
                "v=0", "p", "sol", fake_token(), "s", "r",
                opener=lambda *a, **k: Response("v=0", "/nope"),
            )

    def test_transport_choice_keeps_old_clients_on_realtime(self):
        self.assertEqual(live.choose_transport(None), "realtime")
        with patch.dict(os.environ, {"Z0LIVE_LIVE_TRANSPORT": "bidi"}):
            self.assertEqual(live.choose_transport(["bidi", "realtime"]), "bidi")
        with patch.dict(os.environ, {"Z0LIVE_LIVE_TRANSPORT": "realtime"}):
            self.assertEqual(live.choose_transport(["bidi", "realtime"]), "realtime")
            with self.assertRaises(ValueError):
                live.choose_transport(["bidi"])
        with self.assertRaises(ValueError):
            live.choose_transport("bidi")

    def test_persona_is_one_assistant_and_lists_real_phone_tools(self):
        text = live.persona(PHONE_TOOLS)
        self.assertIn("one assistant", text)
        self.assertIn("open_app", text)
        self.assertIn("Never narrate waiting", text)
        self.assertIn("not wired up yet", text)
        self.assertIn("none in this session", live.persona([]))


class FakeSocket:
    def __init__(self, frames, linger=0.0):
        self.frames, self.sent, self.closed, self.linger = list(frames), [], False, linger

    def __iter__(self):
        yield from self.frames
        time.sleep(self.linger)  # the call stays open for a while after the last frame

    def send(self, raw):
        self.sent.append(json.loads(raw))

    def close(self):
        self.closed = True


class RecordingDelegator:
    def __init__(self):
        self.delegations, self.turns, self.closed = [], [], []

    def delegate(self, call, did, text):
        self.delegations.append((did, text))

    def user_turn(self, call, text):
        self.turns.append(text)

    def call_closed(self, call):
        self.closed.append(call.sid)


class StoreCase(unittest.TestCase):
    def setUp(self):
        selected = patch("z0live_desktop.voice_runtime.load_plan", return_value=None)
        selected.start()
        self.addCleanup(selected.stop)
        self.env = patch.dict(os.environ, {"Z0LIVE_LIVE_TRANSPORT": "bidi"})
        self.env.start()
        self.addCleanup(self.env.stop)
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.device = self.store.authenticate(self.store.pair("unit"))
        with patch("z0live_desktop.core.mint", side_effect=AssertionError("bidi must not mint")):
            self.session = self.store.new_session(
                self.device,
                {"client": "lawnchair", "tools": PHONE_TOOLS, "transports": ["bidi", "realtime"]},
            )
        self.sid = self.session["session_id"]


class LiveCallTests(StoreCase):
    def test_bidi_mint_has_no_secret_and_no_function_tools(self):
        self.assertEqual(self.session["transport"], "bidi")
        self.assertEqual(self.session["model"], "gpt-live-1-codex")
        self.assertNotIn("value", self.session)
        self.assertEqual(self.session["builtin_tools"], [])

    def test_old_client_still_mints_realtime(self):
        with patch("z0live_desktop.core.mint", return_value={"value": "s", "expires_at": 1}):
            old = self.store.new_session(self.device, {"client": "lawnchair"})
        self.assertEqual(old["transport"], "realtime")
        self.assertEqual(old["value"], "s")
        self.assertIn("ask_z0", old["builtin_tools"])

    def test_call_reads_sideband_skips_audio_and_records_transcripts(self):
        frames = [
            '{"type":"session.input_audio.append","audio":"AAAA"}',
            '{"type":"session.output_audio.delta","delta":"AAAA"}',
            json.dumps({"type": "turn.done", "turn": {"id": "t1", "role": "user", "transcript": "open spotify"}}),
            json.dumps({"type": "delegation.created", "item": {"id": "item_1", "type": "delegation",
                        "target": "client", "user_bidi_turn_id": "t1",
                        "content": [{"type": "input_text", "text": "Open Spotify"}]}}),
            json.dumps({"type": "turn.done", "turn": {"id": "t2", "role": "assistant", "transcript": "On it."}}),
        ]
        socket = FakeSocket(frames)
        delegator = RecordingDelegator()
        call = live.LiveCall(self.store, self.sid, self.device, "rtc_x", fake_token(), "s", "r",
                             delegator, connector=lambda url, headers: socket)
        call.run()
        self.assertEqual(call.stats["audio_frames"], 2)
        self.assertEqual(delegator.delegations, [("item_1", "Open Spotify")])
        self.assertEqual(delegator.turns, ["open spotify"])
        self.assertEqual(delegator.closed, [self.sid])
        history = (Path(self.tmp.name) / "sessions" / (self.sid + ".jsonl")).read_text()
        self.assertIn("open spotify", history)
        self.assertIn("On it.", history)

    def turn(self, role, text, turn_id):
        return json.dumps({"type": "turn.done", "turn": {"id": turn_id, "role": role, "transcript": text}})

    def delegation(self, item, text, user_turn):
        return json.dumps({"type": "delegation.created", "item": {
            "id": item, "type": "delegation", "target": "client", "user_bidi_turn_id": user_turn,
            "content": [{"type": "input_text", "text": text}]}})

    def run_call(self, frames, wait=0.0):
        socket = FakeSocket(frames, linger=wait)
        delegator = RecordingDelegator()
        call = live.LiveCall(self.store, self.sid, self.device, "rtc_x", fake_token(), "s", "r",
                             delegator, connector=lambda url, headers: socket)
        with patch.object(live, "RECOVERY_GRACE_SECONDS", 0.05):
            call.run()
        return call, delegator

    def test_bare_acknowledgement_without_delegation_is_recovered(self):
        call, delegator = self.run_call([
            self.turn("user", "Open Spotify.", "turn_u"),
            self.turn("assistant", "On it.", "turn_a"),
        ], wait=0.3)
        self.assertEqual(len(delegator.delegations), 1)
        did, text = delegator.delegations[0]
        self.assertTrue(did.startswith("recovered-"))
        self.assertEqual(text, "Open Spotify.")
        self.assertEqual(call.stats["recovered"], 1)

    def test_acknowledged_real_delegation_is_not_duplicated(self):
        # The provider sends delegation.created before the user's turn.done.
        _, delegator = self.run_call([
            self.delegation("item_1", "Run the tests", "turn_u"),
            self.turn("user", "Run the tests", "turn_u"),
            self.turn("assistant", "Checking.", "turn_a"),
        ], wait=0.3)
        self.assertEqual(delegator.delegations, [("item_1", "Run the tests")])

    def test_real_answers_are_not_recovered(self):
        _, delegator = self.run_call([
            self.turn("user", "What's the capital of Australia?", "turn_u"),
            self.turn("assistant", "Canberra.", "turn_a"),
        ], wait=0.3)
        self.assertEqual(delegator.delegations, [])
        self.assertTrue(live.ACK_ONLY.match("Okay, checking now."))
        self.assertFalse(live.ACK_ONLY.match("On it. Spotify's open."))

    def test_unreadable_delegation_is_recorded_not_dropped_silently(self):
        call, delegator = self.run_call(['{"type":"delegation.created","item":{"target":"client"}}'])
        self.assertEqual(delegator.delegations, [])
        self.assertEqual(len(call.stats["unreadable"]), 1)

    def test_recovered_results_use_session_context(self):
        socket = FakeSocket([])
        call = live.LiveCall(self.store, self.sid, self.device, "rtc_x", fake_token(), "s", "r",
                             RecordingDelegator(), connector=lambda url, headers: socket)
        call.ws = socket
        call.append("recovered-1", "done", None)
        call.append("item_2", "progress", "commentary")
        self.assertEqual(socket.sent[0]["type"], "session.context.append")
        self.assertEqual(socket.sent[1]["type"], "delegation.context.append")

    def test_dropped_sideband_reattaches_once_and_keeps_reading(self):
        class Dropping(FakeSocket):
            def __iter__(self):
                yield from self.frames
                raise ConnectionError("keepalive ping failed")

        sockets = [
            Dropping([self.turn("user", "hello", "t1")]),
            FakeSocket([self.turn("assistant", "Hi.", "t2")]),
        ]
        delegator = RecordingDelegator()
        call = live.LiveCall(self.store, self.sid, self.device, "rtc_x", fake_token(), "s", "r",
                             delegator, connector=lambda url, headers: sockets.pop(0))
        call.run()
        self.assertEqual(call.stats["reconnects"], 1)
        self.assertEqual(delegator.turns, ["hello"])
        history = (Path(self.tmp.name) / "sessions" / (self.sid + ".jsonl")).read_text()
        self.assertIn("Hi.", history)

    def test_side_effects_run_off_the_reader_thread_in_order(self):
        socket = FakeSocket([self.turn("user", "first", "t1"), self.turn("user", "second", "t2")], linger=0.3)
        delegator = RecordingDelegator()
        call = live.LiveCall(self.store, self.sid, self.device, "rtc_x", fake_token(), "s", "r",
                             delegator, connector=lambda url, headers: socket)
        call.start()
        call.thread.join(3)
        time.sleep(0.2)
        self.assertEqual(delegator.turns, ["first", "second"])

    def test_sideband_retries_then_reports(self):
        attempts = []

        def failing(url, headers):
            attempts.append(url)
            raise OSError("404")

        call = live.LiveCall(self.store, self.sid, self.device, "rtc_x", fake_token(), "s", "r",
                             RecordingDelegator(), connector=failing, attempts=3, sleep=lambda _s: None)
        call.run()
        self.assertEqual(len(attempts), 3)
        self.assertTrue(call.closed.is_set())
        self.assertFalse(call.send({"type": "x"}))

    def test_open_requires_bidi_owned_session_and_closes_on_end(self):
        socket = FakeSocket([])
        started = threading.Event()

        def connector(url, headers):
            started.set()
            time.sleep(0.05)
            return socket

        calls = live.LiveCalls(
            self.store, delegator=RecordingDelegator(), connector=connector,
            signaler=lambda offer, *a: ("v=0 answer", "rtc_live"), access=fake_token,
        )
        self.store._live = calls
        result = calls.open(self.device, {"session_id": self.sid, "sdp": "v=0 offer"})
        self.assertEqual(result["call_id"], "rtc_live")
        self.assertEqual(result["model"], "gpt-live-1-codex")
        self.assertTrue(started.wait(2))
        other = self.store.authenticate(self.store.pair("other"))
        with self.assertRaises(LookupError):
            calls.open(other, {"session_id": self.sid, "sdp": "v=0"})
        with patch("z0live_desktop.core.mint", return_value={"value": "s", "expires_at": 1}):
            legacy = self.store.new_session(self.device, {"client": "web"})["session_id"]
        with self.assertRaises(ValueError):
            calls.open(self.device, {"session_id": legacy, "sdp": "v=0"})
        self.store.end(self.device, self.sid)
        self.assertIsNone(calls.get(self.sid))
        with self.assertRaises(ValueError):
            calls.open(self.device, {"session_id": self.sid, "sdp": "v=0"})


class FakeCall:
    def __init__(self, sid):
        self.sid, self.call_id = sid, "rtc_fake"
        self.closed = threading.Event()
        self.appended = []

    def append(self, did, text, channel=None):
        self.appended.append((did, text, channel))
        return True


class FakeHermes:
    def __init__(self, output="Tests pass: 12 of 12.", status="completed", gate=None):
        self.output, self.final_status, self.gate = output, status, gate
        self.started, self.steered, self.stopped = [], [], []

    def start(self, text, instructions, idem):
        self.started.append((text, instructions, idem))
        return "run_1"

    def events(self, run_id):
        yield {"event": "run.started"}
        yield {"event": "message.interim", "text": "Running the test suite."}
        yield {"event": "message.interim", "text": "Running the test suite."}
        yield {"event": "tool.started", "tool": "terminal", "preview": "pytest -q"}
        if self.gate:
            self.gate.wait(5)
        yield {"event": "run." + self.final_status}

    def status(self, run_id):
        return {"status": "cancelled" if self.stopped else self.final_status, "output": self.output}

    def steer(self, run_id, text):
        self.steered.append(text)
        return True

    def stop(self, run_id):
        self.stopped.append(run_id)
        return {"status": "stopping"}


class NoRoute:
    def route(self, did, text, tools):
        return None


class DelegatorTests(StoreCase):
    def wait(self, predicate, timeout=5):
        end = time.time() + timeout
        while time.time() < end:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def test_hermes_progress_is_silent_commentary_and_final_is_speakable(self):
        hermes = FakeHermes()
        delegator = Delegator(self.store, hermes=hermes, router=NoRoute())
        call = FakeCall(self.sid)
        delegator.delegate(call, "item_1", "Run the tests")
        delegator.delegate(call, "item_1", "Run the tests")  # duplicate delivery ignored
        self.assertTrue(self.wait(lambda: any(c is None for _, _, c in call.appended)))
        commentary = [t for _, t, c in call.appended if c == "commentary"]
        finals = [t for _, t, c in call.appended if c is None]
        self.assertEqual(commentary, ["Running the test suite.", "Using terminal: pytest -q"])
        self.assertEqual(finals, ['"Agent Final Message":\n\nTests pass: 12 of 12.'])
        self.assertEqual(len(hermes.started), 1)
        self.assertIn(self.sid, hermes.started[0][1])
        self.assertTrue(self.wait(lambda: delegator.trace(self.sid)[0]["status"] == "completed"))
        trace = delegator.trace(self.sid)
        self.assertEqual(trace[0]["route"], "hermes")
        self.assertIn("final", trace[0]["marks"])

    def test_new_request_steers_running_work_and_owner_cancel_silences_it(self):
        gate = threading.Event()
        hermes = FakeHermes(gate=gate)
        delegator = Delegator(self.store, hermes=hermes, router=NoRoute())
        call = FakeCall(self.sid)
        delegator.delegate(call, "item_1", "Check the build")
        self.assertTrue(self.wait(lambda: self.sid in delegator.active))
        delegator.delegate(call, "item_2", "Also check lint")
        self.assertTrue(self.wait(lambda: hermes.steered == ["Also check lint"]))
        self.assertTrue(self.wait(lambda: delegator.active[self.sid]["delegation_id"] == "item_2"))
        self.assertTrue(CANCEL.match("Never mind."))
        self.assertFalse(CANCEL.match("never mind the lint, check tests"))
        delegator.user_turn(call, "Never mind.")
        gate.set()
        time.sleep(0.6)
        self.assertEqual(hermes.stopped, ["run_1"])
        self.assertFalse([t for _, t, c in call.appended if c is None], "no stale final speech")
        statuses = {d["id"]: d["status"] for d in delegator.trace(self.sid)}
        self.assertEqual(statuses["item_1"], "merged")

    def test_failed_work_is_reported_honestly(self):
        delegator = Delegator(self.store, hermes=FakeHermes(output="", status="failed"), router=NoRoute())
        call = FakeCall(self.sid)
        delegator.delegate(call, "item_1", "Deploy")
        self.assertTrue(self.wait(lambda: call.appended and call.appended[-1][2] is None))
        self.assertIn("failed without a result", call.appended[-1][1])

    def test_phone_route_queues_inbox_action_and_waits_for_phone_result(self):
        class Route:
            def route(self, did, text, tools):
                return {"tool": "open_app", "arguments": {"name": "Spotify"}}

        delegator = Delegator(self.store, hermes=FakeHermes(), router=Route(), phone_timeout=5)
        self.store._live = type("L", (), {"delegator": delegator, "close": lambda s, sid: None})()
        call = FakeCall(self.sid)
        delegator.delegate(call, "item_p", "Open Spotify")
        self.assertTrue(self.wait(lambda: self.store.inbox(self.device)["messages"]))
        item = self.store.inbox(self.device)["messages"][0]
        self.assertEqual((item["action"], item["params"], item["say"]), ("open_app", {"name": "Spotify"}, ""))
        self.store.add_events(self.device, [{
            "session_id": self.sid, "type": "tool_result",
            "data": {"id": item["id"], "name": "open_app", "success": True,
                     "result": {"ok": True, "opened": "Spotify"}, "event_id": "r1"},
        }])
        self.assertTrue(self.wait(lambda: call.appended))
        self.assertIn("open_app succeeded", call.appended[0][1])
        self.assertEqual(delegator.trace(self.sid)[0]["route"], "phone:open_app")

    def test_unconfirmed_phone_action_is_not_claimed(self):
        self.assertIn("did not confirm", describe_phone_result("open_app", None))
        self.assertIn("failed", describe_phone_result("open_app", {"success": False, "result": {"ok": False}}))

    def test_router_only_accepts_declared_tools(self):
        names = {"open_app"}
        self.assertEqual(
            parse_route('sure: {"tool":"open_app","arguments":{"name":"Maps"}}', names),
            {"tool": "open_app", "arguments": {"name": "Maps"}},
        )
        self.assertIsNone(parse_route('{"tool":"rm_rf","arguments":{}}', names))
        self.assertIsNone(parse_route('{"tool":null}', names))
        self.assertIsNone(parse_route("no json", names))

    def test_router_skips_inference_for_computer_work(self):
        calls = []
        router = PhoneRouter(self.store, small=lambda *a: calls.append(a) or {"text": "{}"})
        self.assertIsNone(router.route("d", "Why is CI failing on the broker?", PHONE_TOOLS))
        self.assertIsNone(router.route("d", "Open Spotify", []))
        self.assertEqual(calls, [])

    def test_backend_instructions_name_the_session_and_spoken_contract(self):
        text = backend_instructions(self.sid, PHONE_TOOLS)
        self.assertIn(self.sid, text)
        self.assertIn("open_app", text)
        self.assertIn("spoken aloud", text)


if __name__ == "__main__":
    unittest.main()
