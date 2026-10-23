"""Offline hybrid contract tests. All inference/audio is explicit fake transport data."""

import base64
import hashlib
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch
from urllib.parse import parse_qs
import urllib.error
import urllib.request
import wave

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop.core import Store
from z0live_desktop.hybrid import Cancellation, HTTPTransport, Interrupted, LocalHTTPBackend
from z0live_desktop.voice_runtime import load_plan
from z0live_desktop.server import Handler, Server


def selected_plan(local=True):
    value = {
        "schema": "z0int.voice_plan.v1", "model": "unit-slm" if local else "gpt-realtime",
        "voice": "alba" if local else "marin",
        "adapter": "desktop-hybrid-http" if local else "desktop-webrtc",
        "harness": "hermes", "meta_role": "plan", "fallbacks": [],
        "roles": {role: {"provider": "unit", "model": "unit-" + role}
                  for role in ("smol", "default", "plan", "slow", "task", "judge")},
    }
    if local:
        value.update(provider="local", local_http={"llm": "http://127.0.0.1:8091", "tts": "http://127.0.0.1:8088"})
    return signed(value)


def signed(value):
    value = {k: v for k, v in value.items() if k != "digest"}
    value["digest"] = hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return value


def fake_wav():
    output = io.BytesIO()
    with wave.open(output, "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(b"\x01\x00" * 480)
    return output.getvalue()


class FakeTransport:
    def __init__(self):
        self.requests = []
        self.say = "On it."
        self.unavailable = False
        self.hook = None
        self.wav = fake_wav()

    def request(self, method, url, body=None, headers=None, cancel=None):
        self.requests.append((method, url, body, headers))
        if self.unavailable:
            raise RuntimeError("local backend unavailable")
        if url.endswith("/v1/models"):
            return b'{"data":[{"id":"unit-slm"}]}'
        if url.endswith("/health"):
            return b'{"status":"healthy"}'
        if self.hook:
            self.hook(url, cancel)
        if url.endswith("/v1/chat/completions"):
            return json.dumps({"choices": [{"finish_reason": "stop", "message": {
                "content": json.dumps({"say": self.say})}}]}).encode()
        if url.endswith("/tts"):
            return self.wav
        raise AssertionError("unexpected fake endpoint " + url)


class HybridTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "plan.json"
        self.plan = selected_plan()
        self.path.write_text(json.dumps(self.plan))
        settings = patch.dict(os.environ, {
            "Z0LIVE_L0_PROVIDER": "local", "Z0LIVE_VOICE_PLAN": str(self.path),
            "Z0LIVE_HERMES_NOTES": "off",
        })
        settings.start()
        self.addCleanup(settings.stop)
        self.store = Store(Path(self.tmp.name) / "broker")
        self.device = self.store.authenticate(self.store.pair("unit"))
        self.other = self.store.authenticate(self.store.pair("other"))
        self.transport = FakeTransport()
        replacement = patch("z0live_desktop.hybrid.HTTPTransport", return_value=self.transport)
        replacement.start()
        self.addCleanup(replacement.stop)
        self.voice = self.store.voice
        self.addCleanup(self.voice.pool.shutdown, wait=True)
        self.voice.native = Mock()
        ticking = patch.object(self.voice, "tick")
        ticking.start()
        self.addCleanup(ticking.stop)
        self.session = self.store.new_session(self.device, {"client": "web", "transports": ["hybrid"]})
        self.sid = self.session["session_id"]

    def event(self, kind="stt.final", epoch=0, seq=0, **extra):
        return self.store.hybrid.request(self.device, {
            "session_id": self.sid, "kind": kind, "turn_epoch": epoch, "seq": seq,
            **({"text": "Investigate the unit request."} if kind.startswith("stt.") else {}), **extra,
        })

    def test_local_mint_no_oauth_and_immutable_plan(self):
        self.assertEqual(self.session["transport"], "hybrid")
        self.assertEqual(self.session["builtin_tools"], [])
        self.assertNotIn("value", self.session)
        self.assertEqual(self.session["voice_plan"], self.plan)
        self.path.write_text(json.dumps(selected_plan(False)))
        self.assertEqual(self.voice.plan(self.sid), self.plan)
        result = self.event()
        self.assertEqual(result["handoff"]["status"], "queued")
        self.assertEqual(result["say"], "On it.")
        self.assertEqual(result["turn_epoch"], 0)
        self.assertEqual(base64.b64decode(result["audio"]["pcm"]), b"\x01\x00" * 480)
        request = next(r for r in self.transport.requests if r[1].endswith("/v1/chat/completions"))
        payload = json.loads(request[2])
        self.assertEqual(payload["model"], self.plan["model"])
        self.assertNotIn("tools", payload)
        tts = next(r for r in self.transport.requests if r[1].endswith("/tts"))
        self.assertEqual(parse_qs(tts[2].decode()), {"text": ["On it."], "voice_url": ["alba"]})
        self.assertEqual(self.voice.plan(self.sid)["roles"], self.plan["roles"])

    def test_partial_never_dispatches_or_persists(self):
        self.assertEqual(self.event("stt.partial")["status"], "partial")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM voice_turns").fetchone()[0], 0)
            self.assertEqual(db.execute("SELECT count(*) FROM events").fetchone()[0], 0)
        self.assertEqual(len(self.transport.requests), 2)  # Mint health probes only.

    def test_final_idempotency_no_duplicate_ack_changed_final_rejected(self):
        first = self.event()
        count = len(self.transport.requests)
        repeated = self.event(seq=99)
        self.assertTrue(repeated["duplicate"])
        self.assertEqual(repeated["speech"], "silent")
        self.assertEqual(repeated["handoff"]["turn_id"], first["handoff"]["turn_id"])
        self.assertEqual(len(self.transport.requests), count)
        with self.assertRaisesRegex(ValueError, "different arguments"):
            self.event(text="A different request.")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM voice_turns").fetchone()[0], 1)

    def test_barge_stops_pending_local_speech_and_cancels_native_work(self):
        entered, release = threading.Event(), threading.Event()
        results = []

        def hook(url, cancel):
            if url.endswith("/tts"):
                entered.set()
                self.assertTrue(release.wait(3))
                cancel.check()

        self.transport.hook = hook
        worker = threading.Thread(target=lambda: results.append(self.event()))
        worker.start()
        try:
            self.assertTrue(entered.wait(3))
            stop = self.event("barge_in", epoch=1)
            self.assertTrue(stop["clear_playback"])
            with self.store.connect() as db:
                self.assertEqual(db.execute("SELECT status FROM voice_turns").fetchone()[0], "cancelled")
            self.voice.native.cancel_liaison.assert_called_once()
        finally:
            release.set()
            worker.join(3)
        self.assertFalse(worker.is_alive())
        self.assertEqual(results[0]["speech"], "silent")
        self.assertNotIn("audio", results[0])
        with self.assertRaisesRegex(ValueError, "turn_epoch"):
            self.event(epoch=0)
        self.transport.hook = None
        self.assertEqual(self.event(epoch=1, text="New request.")["speech"], "audio")

    def test_user_floor_stops_audio_but_does_not_cancel_work(self):
        self.event("floor", state="user")
        result = self.event()
        self.assertEqual(result["speech"], "silent")
        self.assertEqual(result["handoff"]["status"], "queued")
        self.voice.native.cancel_liaison.assert_not_called()
        self.event("floor", seq=1, state="quiet")
        turn = result["handoff"]["turn_id"]
        self.voice.finish(turn, "Unit verified result.")
        ident = self.store.inbox(self.device)["messages"][0]["id"]
        speech = self.store.hybrid.request(self.device, {"session_id": self.sid, "turn_epoch": 0, "inbox_id": ident}, speech=True)
        self.assertEqual(speech["say"], "Unit verified result.")
        again = self.store.hybrid.request(self.device, {"session_id": self.sid, "turn_epoch": 0, "inbox_id": ident}, speech=True)
        self.assertTrue(again["replayed"])
        self.assertEqual(again["audio"], speech["audio"])
        self.event("playback.ack", inbox_id=ident, speech_id=speech["speech_id"])
        heard = self.store.hybrid.request(self.device, {"session_id": self.sid, "turn_epoch": 0, "inbox_id": ident}, speech=True)
        self.assertTrue(heard["duplicate"])
        self.assertEqual(heard["speech"], "silent")

    def test_failed_result_synthesis_is_retryable_without_work_replay(self):
        turn = self.event()["handoff"]["turn_id"]
        self.voice.finish(turn, "Unit recoverable result.")
        ident = self.store.inbox(self.device)["messages"][0]["id"]
        body = {"session_id": self.sid, "turn_epoch": 0, "inbox_id": ident}
        self.transport.unavailable = True
        failure = self.store.hybrid.request(self.device, body, speech=True)
        self.assertFalse(failure["ok"])
        self.assertIn("unavailable", failure["error"])
        self.transport.unavailable = False
        recovered = self.store.hybrid.request(self.device, body, speech=True)
        self.assertEqual(recovered["speech"], "audio")
        self.assertEqual(recovered["say"], "Unit recoverable result.")
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM voice_turns").fetchone()[0], 1)

    def test_result_suppressed_during_synthesis_retries_when_quiet(self):
        turn = self.event()["handoff"]["turn_id"]
        self.voice.finish(turn, "Unit delayed result.")
        ident = self.store.inbox(self.device)["messages"][0]["id"]
        body = {"session_id": self.sid, "turn_epoch": 0, "inbox_id": ident}
        def hook(url, cancel):
            if url.endswith("/tts"):
                self.event("floor", seq=1, state="user")
        self.transport.hook = hook
        suppressed = self.store.hybrid.request(self.device, body, speech=True)
        self.assertEqual(suppressed["speech"], "silent")
        self.transport.hook = None
        self.event("floor", seq=2, state="quiet")
        recovered = self.store.hybrid.request(self.device, body, speech=True)
        self.assertEqual(recovered["speech"], "audio")
        self.assertEqual(recovered["say"], "Unit delayed result.")
        with self.assertRaisesRegex(ValueError, "exact speech ID"):
            self.event("playback.ack", inbox_id=ident, speech_id="wrong")
        with self.assertRaisesRegex(ValueError, "cannot advance"):
            self.event("playback.ack", epoch=1, inbox_id=ident, speech_id="wrong")
        self.assertTrue(self.store.hybrid.request(self.device, body, speech=True)["replayed"])

    def test_final_supersedes_late_ack_and_returns_via_existing_outbox(self):
        def hook(url, cancel):
            if url.endswith("/v1/chat/completions"):
                with self.store.connect() as db:
                    turn = db.execute("SELECT id FROM voice_turns").fetchone()[0]
                self.voice.finish(turn, "Unit final finding.")
        self.transport.hook = hook
        result = self.event()
        self.assertEqual(result["speech"], "silent")
        messages = self.store.inbox(self.device)["messages"]
        self.assertEqual([m["say"] for m in messages], ["Unit final finding."])
        self.assertFalse(any(r[1].endswith("/tts") for r in self.transport.requests))

    def test_backend_failure_visible_without_work_replay(self):
        self.transport.unavailable = True
        failed = self.event()
        self.assertFalse(failed["ok"])
        self.assertIn("unavailable", failed["error"])
        self.assertEqual(failed["handoff"]["status"], "queued")
        self.assertEqual(failed["speech"], "silent")
        self.assertTrue(self.event()["duplicate"])

    def test_invalid_model_speech_never_reaches_tts(self):
        self.transport.say = "The action is complete."
        result = self.event()
        self.assertFalse(result["ok"])
        self.assertIn("invalid backchannel", result["error"])
        self.assertFalse(any(r[1].endswith("/tts") for r in self.transport.requests))

    def test_cancelled_result_historical_or_arbitrary_inbox_stays_silent(self):
        turn = self.event()["handoff"]["turn_id"]
        self.voice.finish(turn, "Unit final finding.")
        ident = self.store.inbox(self.device)["messages"][0]["id"]
        self.event("barge_in", epoch=1)
        # The client must invalidate even a previously fetched final result on barge.
        self.voice.journal.speak(turn, "stale", "result")
        stale = self.store.hybrid.request(self.device, {"session_id": self.sid, "turn_epoch": 1, "inbox_id": ident}, speech=True)
        self.assertEqual(stale["speech"], "silent")
        arbitrary = self.store.publish("Unverified broadcast.", sid=self.sid)
        result = self.store.hybrid.request(self.device, {"session_id": self.sid, "turn_epoch": 1, "inbox_id": arbitrary}, speech=True)
        self.assertEqual(result["speech"], "silent")
        # Ending or superseding a session prevents any subsequent speech request.
        self.store.end(self.device, self.sid)
        with self.assertRaises(ValueError):
            self.store.hybrid.request(self.device, {"session_id": self.sid, "turn_epoch": 1, "inbox_id": ident}, speech=True)

    def test_ownership_restart_and_epoch_validation(self):
        with self.assertRaises(LookupError):
            self.store.hybrid.request(self.other, {"session_id": self.sid, "kind": "stt.partial", "turn_epoch": 0, "seq": 0, "text": "unit"})
        for epoch in (True, -1, "0", 2):
            with self.assertRaises(ValueError):
                self.event(epoch=epoch)
        self.store.hybrid.calls.clear()
        with self.assertRaisesRegex(RuntimeError, "restart"):
            self.event()

    def test_default_openai_compatibility_and_explicit_local_negotiation(self):
        with patch.dict(os.environ, {"Z0LIVE_L0_PROVIDER": "openai"}):
            self.path.write_text(json.dumps(selected_plan(False)))
            with patch("z0live_desktop.core.mint", return_value={"value": "fake-secret", "expires_at": 1}) as mint:
                old = self.store.new_session(self.device, {"client": "web"})
                self.assertEqual(old["transport"], "realtime")
                self.assertEqual(old["value"], "fake-secret")
                mint.assert_called_once()
            with patch.dict(os.environ, {"Z0LIVE_LIVE_TRANSPORT": "bidi"}):
                bidi = self.store.new_session(self.device, {"client": "web", "transports": ["bidi", "realtime"]})
                self.assertEqual(bidi["transport"], "bidi")
        with self.assertRaises(ValueError):
            self.store.new_session(self.device, {"client": "web"})
        with patch.dict(os.environ, {"Z0LIVE_L0_PROVIDER": "unknown"}):
            with self.assertRaisesRegex(RuntimeError, "must be"):
                self.store.new_session(self.device, {"client": "web"})

    def test_plan_digest_and_loopback_validation_and_probe_failure(self):
        value = {**self.plan, "voice": "changed"}
        self.path.write_text(json.dumps(value))
        with self.assertRaisesRegex(RuntimeError, "digest"):
            load_plan()
        for origin in ("http://example.com", "http://127.0.0.1:8088/path", "http://user:pass@127.0.0.1", "https://127.0.0.1", "http://127.0.0.1?token=x"):
            value = {**self.plan, "local_http": {"llm": origin, "tts": "http://127.0.0.1:8088"}}
            self.path.write_text(json.dumps(signed(value)))
            with self.assertRaisesRegex(RuntimeError, "loopback"):
                load_plan()
        self.path.write_text(json.dumps(self.plan))
        self.transport.unavailable = True
        with self.assertRaisesRegex(RuntimeError, "unavailable"):
            self.store.new_session(self.device, {"client": "web", "transports": ["hybrid"]})

    def test_invalid_tts_audio_fails_visibly(self):
        self.transport.wav = b"not a wave file"
        result = self.event()
        self.assertFalse(result["ok"])
        self.assertIn("PCM16", result["error"])
        self.assertNotIn("audio", result)

    def test_authenticated_http_final_and_result_routes(self):
        server = Server(("127.0.0.1", 0), Handler)
        server.store = self.store
        server.origins = set()
        server.stop = threading.Event()
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        bearer = self.store.pair("http unit")
        device = self.store.authenticate(bearer)
        session = self.store.new_session(device, {"client": "web", "transports": ["hybrid"]})
        sid = session["session_id"]

        def post(path, body, authorized=True):
            request = urllib.request.Request(
                f"http://127.0.0.1:{server.server_port}" + path,
                data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json",
                         **({"Authorization": "Bearer " + bearer} if authorized else {})},
            )
            with urllib.request.urlopen(request, timeout=3) as response:
                return json.load(response)

        body = {"session_id": sid, "kind": "stt.final", "turn_epoch": 0, "seq": 0, "text": "Unit HTTP request."}
        try:
            with self.assertRaises(urllib.error.HTTPError) as missing:
                post("/v1/hybrid/events", body, authorized=False)
            self.assertEqual(missing.exception.code, 401)
            missing.exception.close()
            result = post("/v1/hybrid/events", body)
            self.assertEqual(result["handoff"]["status"], "queued")
            self.assertEqual(result["speech"], "audio")
            self.voice.finish(result["handoff"]["turn_id"], "Unit HTTP final.")
            ident = self.store.inbox(device)["messages"][0]["id"]
            reply = post("/v1/hybrid/speech", {"session_id": sid, "turn_epoch": 0, "inbox_id": ident})
            self.assertEqual(reply["say"], "Unit HTTP final.")
            self.assertEqual(reply["speech"], "audio")
        finally:
            server.stop.set()
            server.shutdown()
            server.server_close()
            worker.join(3)


class TransportTests(unittest.TestCase):
    def test_real_transport_maps_network_failure_and_cancellation(self):
        connection = Mock()
        connection.connect.side_effect = OSError("unit connection refused")
        with patch("http.client.HTTPConnection", return_value=connection):
            with self.assertRaisesRegex(RuntimeError, "unavailable"):
                HTTPTransport().request("GET", "http://127.0.0.1:8091/v1/models")
        token = Cancellation()
        sock = Mock()
        token.attach(sock)
        token.cancel()
        sock.shutdown.assert_called_once()
        sock.close.assert_called_once()
        with self.assertRaises(Interrupted):
            token.check()

    def test_adapter_probe_rejects_missing_pinned_model(self):
        transport = Mock()
        transport.request.return_value = b'{"data":[{"id":"another-model"}]}'
        with self.assertRaisesRegex(RuntimeError, "pinned local model"):
            LocalHTTPBackend(selected_plan(), transport).probe()


if __name__ == "__main__":
    unittest.main()
