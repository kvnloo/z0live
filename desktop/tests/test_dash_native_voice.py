import asyncio
import io
import json
import os
from pathlib import Path
import sqlite3
import tempfile

import unittest
from unittest.mock import patch

from z0live_desktop.identity_voice import voice_identity, voice_route
from z0live_desktop.attached_voice import bind, target_for, validate_target, attached_instructions
from z0live_desktop.gateway_reply import wait_reply
from z0live_desktop.core import Store
from z0live_desktop import relay


class IdentityVoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "introspection.json"
        self.path.write_text(json.dumps({"credential": "z0bc.cred_" + "a" * 16 + "." + "b" * 43, "scopes": ["z0bridge:introspect"]}))
        self.path.chmod(0o600)
        self.env = {"Z0LIVE_IDENTITY": "1", "Z0LIVE_IDENTITY_CREDENTIAL_FILE": str(self.path)}
        self.result = {"active": True, "token_type": "access", "aud": "z0live", "scope": "z0live:voice", "exp": 1100, "sub": "cred_" + "c" * 16, "dev": "dev_" + "d" * 16}

    def verify(self, result=None, env=None):
        value = self.result if result is None else result
        def request(req, timeout):
            self.assertEqual(req.full_url, "http://127.0.0.1:4789/v1/introspect")
            self.assertEqual(json.loads(req.data), {"token": "aaa.bbb.ccc"})
            return io.BytesIO(json.dumps(value).encode())
        return voice_identity("aaa.bbb.ccc", env=self.env if env is None else env, request=request, now=lambda: 1000)

    def test_exact_voice_audience_scope_expiry_and_revocation(self):
        self.assertTrue(self.verify().startswith("z0bridge:dev_"))
        for changes in [{"active": False}, {"aud": "dash"}, {"scope": "dash:chat"}, {"scope": "z0live:voice z0live:gesture"}, {"token_type": "credential"}, {"exp": 999}, {"exp": 1400}, {"dev": ""}, {"sub": "other"}]:
            self.assertIsNone(self.verify({**self.result, **changes}))
        self.assertIsNone(self.verify(env={**self.env, "Z0LIVE_IDENTITY": "0"}))
        self.assertIsNone(self.verify(env={**self.env, "Z0LIVE_IDENTITY_URL": "http://evil.invalid"}))
        self.path.chmod(0o644)
        self.assertIsNone(self.verify())

    def test_routes_do_not_grant_gesture_or_arbitrary_dispatch(self):
        self.assertTrue(voice_route("POST", "/v1/tools/ask_z0"))
        self.assertTrue(voice_route("POST", "/v1/sessions/abc/end"))
        for path in ["/v1/gesture", "/v1/gesture/ack", "/v1/tools/start_task", "/v1/hermes/send", "/v1/credentials/grant"]:
            self.assertFalse(voice_route("POST", path))


class AttachedVoiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name) / "profile"
        self.home.mkdir()
        with sqlite3.connect(self.home / "state.db") as db:
            db.execute("CREATE TABLE sessions(id TEXT PRIMARY KEY,title TEXT,hidden INTEGER,archived INTEGER)")
            db.execute("INSERT INTO sessions VALUES('exact-session','Attached',0,0)")
        self.store = Store(Path(self.tmp.name) / "broker")
        self.pin = {"profile": "qualification", "session": "exact-session"}

    def test_exact_pin_and_immutable_binding_survive_context_changes(self):
        with patch("z0live_desktop.attached_voice.target_home", return_value=self.home):
            self.assertEqual(validate_target(self.pin), self.pin)
            for bad in [{"profile": "qualification", "session": "exact"}, {**self.pin, "extra": True}]:
                with self.assertRaises(ValueError):
                    validate_target(bad)
        with self.store.connect() as db:
            bind(db, "voice-sid", self.pin)
            with self.assertRaises(sqlite3.IntegrityError):
                bind(db, "voice-sid", {"profile": "other", "session": "other"})
        self.assertEqual(target_for(self.store, "voice-sid"), self.pin)
        self.assertIn("--key dash:voice-sid:delegation", attached_instructions(self.store, "voice-sid", "delegation"))
        with patch("z0live_desktop.attached_voice.target_home", return_value=self.home):
            self.assertEqual(relay.relay("hello", to="other", store=self.store, sid="voice-sid")["error"], "attached_target_is_immutable")

    def test_waiting_reads_only_exact_admission_and_never_calls_submit(self):
        class Client:
            calls = []
            async def rpc(self, method, **kwargs):
                self.calls.append((method, kwargs))
                return {"status": "terminal", "outcome": "completed", "result": {"final_response": "Real causal reply"}}
        client = Client()
        result = asyncio.run(wait_reply(client, "exact-session", "own-admission", 3))
        self.assertEqual(result["reply_state"], "completed")
        self.assertEqual(result["final_reply"], "Real causal reply")
        self.assertEqual(client.calls, [("prompt.receipt", {"session_id": "exact-session", "admission_id": "own-admission", "include_result": True})])

    def test_pending_is_silent_not_a_fabricated_reply(self):
        class Client:
            async def rpc(self, method, **kwargs):
                return {"status": "queued"}
        result = asyncio.run(wait_reply(Client(), "exact-session", "own-admission", 0))
        self.assertEqual(result["reply_state"], "pending")
        self.assertEqual(result["final_reply"], "")

    def test_attached_call_is_routed_in_code_not_answered_by_the_delegate(self):
        import threading
        from z0live_desktop.delegation import Delegator

        class NoHermes:
            def __getattr__(self, name):
                raise AssertionError("attached calls must not start a delegate run")

        class NoRouter:
            def route(self, *args):
                raise AssertionError("attached calls must not be routed heuristically")

        class Call:
            sid = "voice-sid"
            closed = threading.Event()
            spoken = []
            def append(self, did, message):
                self.spoken.append(message)
                return True

        with self.store.connect() as db:
            bind(db, "voice-sid", self.pin)
        delegator = Delegator(self.store, hermes=NoHermes(), router=NoRouter())
        seen = []
        def fake_relay(text, **kwargs):
            seen.append((text, kwargs))
            return {"ok": True, "reply_state": "completed", "final_reply": "Real causal reply"}
        with patch("z0live_desktop.delegation.rl.relay", side_effect=fake_relay):
            delegator.handle(Call(), "did-1", "What changed?")
        self.assertEqual(seen, [("What changed?", {"key": "dash:voice-sid:did-1", "store": self.store, "sid": "voice-sid", "wait": 180})])
        self.assertIn("Real causal reply", json.dumps(Call.spoken))
        pending = []
        with patch("z0live_desktop.delegation.rl.relay", return_value={"ok": True, "reply_state": "pending", "final_reply": ""}):
            Call.spoken = pending
            delegator.handle(Call(), "did-2", "Another")
        self.assertNotIn("Real causal reply", json.dumps(pending))
        self.assertIn("not in yet", json.dumps(pending))


if __name__ == "__main__":
    unittest.main()
