"""N230: a Hermes approval prompt is spoken on the call; the pass-phrase approves it once, anything else denies it."""
import asyncio
import json
import os
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock

import voice_approvals as va

PHRASE = "blue harbor lantern"


class PhraseTests(unittest.TestCase):
    def test_match_is_contiguous_words_any_case_or_punctuation(self):
        self.assertTrue(va.matches("Blue harbor, lantern.", PHRASE))
        self.assertTrue(va.matches("okay approve it: blue harbor lantern", PHRASE))
        self.assertFalse(va.matches("blue lantern harbor", PHRASE))
        self.assertFalse(va.matches("green mountain river", PHRASE))
        self.assertFalse(va.matches("yes approve", PHRASE))
        self.assertFalse(va.matches("", PHRASE))
        self.assertFalse(va.matches("blue harbor lantern", ""))  # no phrase set: never a match

    def test_decide_fails_closed(self):
        self.assertEqual(va.decide("blue harbor lantern", PHRASE), "once")
        self.assertEqual(va.decide("yes do it", PHRASE), "deny")
        self.assertEqual(va.decide("blue harbor lantern", ""), "deny")
        self.assertEqual(va.decide("blue harbor lantern " * 40, PHRASE), "deny")  # an over-long answer is refused

    def test_load_phrase_env_then_profile_env_and_short_is_unset(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            env_file = home / ".hermes/profiles/tek/.env"
            env_file.parent.mkdir(parents=True)
            env_file.write_text("OTHER=1\nZ0LIVE_APPROVAL_PHRASE=\"Blue Harbor Lantern\"\n")
            with mock.patch.dict(os.environ, {}, clear=False):
                os.environ.pop(va.PHRASE_ENV, None)
                self.assertEqual(va.load_phrase("tek", home), PHRASE)
                self.assertEqual(va.load_phrase("pc", home), "")  # no .env for pc
                self.assertEqual(va.load_phrase("../tek", home), "")  # never a path
                os.environ[va.PHRASE_ENV] = "Red Fox Seven"
                self.assertEqual(va.load_phrase("tek", home), "red fox seven")  # the service env wins
                os.environ[va.PHRASE_ENV] = "abc"
                self.assertEqual(va.load_phrase("pc", home), "")  # too short: treated as unset (deny)

    def test_mask_and_prompt_never_carry_the_phrase(self):
        self.assertNotIn("harbor", va.mask("Blue harbor lantern", PHRASE))
        self.assertNotIn("green", va.mask("green mountain river", PHRASE))
        line = va.spoken_prompt({"command": "rm -r /Users/kvn/workspace/repos/x", "description": "recursive delete"}, "tek")
        self.assertIn("rm -r /Users/kvn/workspace/repos/x", line)
        self.assertIn("recursive delete", line)
        self.assertIn("pass-phrase", line)

    def test_respond_only_once_or_deny(self):
        client = va.RunsClient(1, "k")
        for choice in ("always", "session", ""):
            with self.assertRaises(ValueError):
                client.respond("run_1", "req", choice)
        with self.assertRaises(ValueError):
            client.respond("run_1", "", "once")


class FakeRuns:
    """A loopback Runs API host: one run, one approval request, answered by POST .../approval."""

    def __init__(self):
        self.answer = threading.Event()
        self.posts, self.choice = [], None
        owner = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002 - base signature
                pass

            def _json(self, code, body):
                data = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])) or b"{}")
                owner.posts.append((self.path, body, self.headers.get("Authorization")))
                if self.path == "/v1/runs":
                    return self._json(202, {"run_id": "run_1", "status": "started"})
                if self.path == "/v1/runs/run_1/approval":
                    owner.choice = body.get("choice")
                    owner.answer.set()
                    return self._json(200, {"resolved": 1, "choice": owner.choice})
                self._json(404, {})

            def do_GET(self):
                if self.path == "/v1/runs/run_1":
                    return self._json(200, {"status": "completed" if owner.answer.is_set() else "waiting_for_approval"})
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def ev(e):
                    self.wfile.write(b"data: " + json.dumps(e).encode() + b"\n\n")
                    self.wfile.flush()
                self.wfile.write(b": open\n\n")
                ev({"event": "tool.started", "tool": "terminal"})
                ev({"event": "approval.request", "request_id": "req_1", "command": "rm -r /tmp/x",
                    "description": "recursive delete", "choices": ["once", "session", "always", "deny"]})
                owner.answer.wait(10)
                ev({"event": "approval.responded", "request_id": "req_1", "choice": owner.choice})
                ev({"event": "tool.completed", "tool": "terminal", "error": owner.choice != "once"})
                ev({"event": "run.completed", "output": "ran it" if owner.choice == "once" else "denied, did not run"})
                self.wfile.write(b": stream closed\n\n")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class RunsClientTests(unittest.TestCase):
    def setUp(self):
        self.host = FakeRuns()
        self.client = va.RunsClient(self.host.port, "secret-key")

    def tearDown(self):
        self.host.close()

    def follow(self, heard):
        seen = []

        def on_request(run_id, event):
            seen.append(va.event_kind(event))
            if va.event_kind(event) == "approval.request":  # the call answers later, off the reader thread
                choice = va.decide(heard, PHRASE)
                threading.Thread(target=self.client.respond, args=(run_id, va.request_id(event), choice)).start()
        return va.run(self.client, "do it", "sess", "system", on_request, 10), seen

    def test_right_phrase_approves_once(self):
        (run_id, output, failed, approvals), seen = self.follow("blue harbor lantern")
        self.assertEqual((run_id, output, failed), ("run_1", "ran it", ""))
        self.assertEqual(self.host.choice, "once")
        approval_post = [p for p in self.host.posts if p[0].endswith("/approval")][0]
        self.assertEqual(approval_post[1], {"request_id": "req_1", "choice": "once"})
        self.assertEqual(approval_post[2], "Bearer secret-key")
        self.assertEqual(seen, ["approval.request", "approval.responded"])
        start = self.host.posts[0][1]
        self.assertEqual(start, {"input": "do it", "session_id": "sess", "instructions": "system"})

    def test_wrong_phrase_denies(self):
        (_, output, _, _), _ = self.follow("green mountain river")
        self.assertEqual(self.host.choice, "deny")
        self.assertEqual(output, "denied, did not run")


class CallTests(unittest.IsolatedAsyncioTestCase):
    """The call speaks the pending command, takes Kevin's next words as the answer, and keeps the phrase out of the
    trace, the lanes and new Hermes runs."""

    async def asyncSetUp(self):
        import mobile_live as live
        self.live = live
        self.tmp = tempfile.TemporaryDirectory()
        self.env = mock.patch.dict(os.environ, {"Z0LIVE_ASK_HOSTS": json.dumps({"pc": 1}), va.PHRASE_ENV: PHRASE,
                                                "Z0LIVE_LEDGER": str(Path(self.tmp.name) / "ledger")})
        self.env.start()
        self.trace_dir = mock.patch.object(live, "_call_trace_dir", return_value=Path(self.tmp.name) / "traces")
        self.trace_dir.start()
        self.call = live.Call(live.Manager(None), profile="tek")
        self.call.protocol.seen.extend(["d1", "d2"])
        self.sent = []
        self.call.send = lambda event, to_provider=False, **kw: self.sent.append(event)
        self.responded = []
        test = self

        class Client:
            def respond(self, run_id, request_id, choice, generation=None):
                test.responded.append((run_id, request_id, choice))
                return {"resolved": 1}
        self.client = Client()

    async def asyncTearDown(self):
        await self.call.close("test")
        self.trace_dir.stop()
        self.env.stop()
        self.tmp.cleanup()

    def spoken(self):
        return " ".join(c.get("text", "") for e in self.sent for c in e.get("content", []) if isinstance(c, dict))

    async def settle(self):
        for _ in range(30):
            await asyncio.sleep(0.01)

    def trace_text(self):
        return "".join(p.read_text() for p in (Path(self.tmp.name) / "traces").rglob("*.jsonl"))

    async def raise_prompt(self):
        """An ask to pc whose run raises one approval request; the ask then waits like a real run."""
        released = asyncio.Event()
        loop = asyncio.get_running_loop()

        def fake_ask(profile, message, **kw):
            kw["on_approval"](self.client, "run_1", {"event": "approval.request", "request_id": "req_1",
                                                     "command": "rm -r /Users/kvn/workspace/repos/probe",
                                                     "description": "recursive delete"})
            asyncio.run_coroutine_threadsafe(released.wait(), loop).result(5)
            return {"ok": True, "say": "pc says: done."}
        self.patch = mock.patch.object(self.live.ask_bot, "ask", side_effect=fake_ask)
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.addCleanup(released.set)
        self.assertTrue(self.call.bot_ask("d1", "ask pc to delete the probe folder"))
        await self.settle()
        return released

    def user_says(self, text):
        self.call.message(json.dumps({"type": "input_transcript.added", "item": {"text": text}}), True)
        self.call.message(json.dumps({"type": "turn.done", "turn": {"id": "u9", "role": "user", "transcript": text}}), True)

    async def test_pending_command_is_spoken_and_the_phrase_approves_once(self):
        await self.raise_prompt()
        self.assertIn("Approval needed. pc wants to run rm -r /Users/kvn/workspace/repos/probe", self.spoken())
        self.user_says("Blue harbor, lantern.")
        await self.settle()
        self.assertEqual(self.responded, [("run_1", "req_1", "once")])
        self.assertIn(va.APPROVED, self.spoken())
        self.assertEqual(self.call.approvals, [])
        self.assertNotIn("harbor", self.trace_text().lower())  # the phrase never reaches the trace
        self.assertIn('"approval.answer"', self.trace_text())

    async def test_wrong_words_deny(self):
        await self.raise_prompt()
        self.user_says("green mountain river")
        await self.settle()
        self.assertEqual(self.responded, [("run_1", "req_1", "deny")])
        self.assertIn(va.DENIED, self.spoken())

    async def test_no_phrase_set_denies(self):
        os.environ.pop(va.PHRASE_ENV)
        with mock.patch.object(va, "load_phrase", return_value=""):
            await self.raise_prompt()
            self.user_says("blue harbor lantern")
            await self.settle()
        self.assertEqual(self.responded, [("run_1", "req_1", "deny")])
        self.assertIn(va.NO_PHRASE, self.spoken())

    async def test_silence_leaves_it_to_hermes_timeout(self):
        await self.raise_prompt()
        await self.settle()
        self.assertEqual(self.responded, [])  # no answer sent: Hermes' approvals.timeout decides (deny)
        self.call.approval_event("d1", "pc", self.client, "run_1", {"event": "approval.cleared"})
        self.assertEqual(self.call.approvals, [])

    async def test_delegation_of_the_answer_starts_no_run(self):
        await self.raise_prompt()
        with mock.patch.object(self.live, "run_hermes", side_effect=AssertionError("no Hermes run")):
            await self.call.host_delegate("d2", "approve with blue harbor lantern")
        self.assertEqual(self.call.counts["approval_delegation_swallowed"], 1)
        self.assertNotIn("harbor", self.trace_text().lower())

    async def test_guest_call_never_takes_a_prompt(self):
        self.call.profile = "guest"
        self.call.approval_event("d1", "pc", self.client, "run_1", {"event": "approval.request", "request_id": "r"})
        self.assertEqual(self.call.approvals, [])


if __name__ == "__main__":
    unittest.main()
