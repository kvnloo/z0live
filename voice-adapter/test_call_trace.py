"""Exercise JSONL and WAV files produced by the call adapter without live providers."""
import asyncio
from fractions import Fraction
import hashlib
import json
import os
from pathlib import Path
import struct
import tempfile
import unicodedata
import unittest
from unittest.mock import AsyncMock, Mock, patch
import wave

import av
import mobile_live as live


class Frames:
    def __init__(self, *values):
        self.values = iter(values)
        self.pts = 0

    async def recv(self):
        frame = av.AudioFrame(format="s16", layout="stereo", samples=960)
        frame.planes[0].update(struct.pack("<hh", next(self.values), 0) * 960)
        frame.sample_rate, frame.pts, frame.time_base = 48000, self.pts, Fraction(1, 48000)
        self.pts += 960
        return frame


class Channel:
    readyState = "open"

    def __init__(self):
        self.sent = []

    def send(self, payload):
        self.sent.append(json.loads(payload))


class Socket:
    closed = False

    def __init__(self):
        self.channel = Channel()

    async def send_str(self, payload):
        self.channel.send(payload)

    async def close(self):
        self.closed = True


class CallTraceTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        tmp = self.enterContext(tempfile.TemporaryDirectory())
        self.directory = Path(tmp)
        self.enterContext(patch.object(live, "TRACE_DIR", self.directory))
        self.enterContext(patch.object(live, "HOST_DELEGATION", False))
        self.enterContext(patch.dict(os.environ, {"HERMES_MOBILE_VOICE_TRACE": "1", "Z0LIVE_GUEST": "off",
                                                  "HERMES_VOICE_ENGINE": "gpt-live"}))
        os.environ.pop("Z0LIVE_TRACE_AUDIO", None)

    async def make_call(self):
        call = live.Call(live.Manager(None))
        self.addAsyncCleanup(call.close, "test")
        return call

    async def test_unittest_cannot_write_to_the_real_trace_store(self):
        real = Path.home() / ".local/share/z0live/voice-traces"

        def snapshot():
            if not real.exists():
                return {}
            entries = {}
            for path in (real, *real.glob("**/*")):
                stat = path.stat()
                entries[path] = (stat.st_size, stat.st_mtime_ns)
            return entries

        before = snapshot()
        session = Path(live._TEST_TRACE_SESSION.name)
        self.assertEqual(Path(os.environ["HERMES_MOBILE_VOICE_TRACE_DIR"]), session)
        with patch.object(live, "TRACE_DIR", session):
            call = await self.make_call()
            self.assertTrue(call.trace.path.is_relative_to(session))
            self.assertTrue(call.trace.path.is_file())
        alias = self.directory / "real-traces"
        alias.symlink_to(real, target_is_directory=True)
        for unsafe in (real, alias):
            with patch.object(live, "TRACE_DIR", unsafe), patch.object(live, "Trace") as writer:
                with self.assertRaisesRegex(RuntimeError, "require a temporary directory"):
                    await self.make_call()
                writer.assert_not_called()
        with patch.object(live, "TRACE_DIR", real), patch.dict(os.environ, {"Z0LIVE_ALLOW_UNSAFE_TEST_TRACES": "1"}):
            self.assertEqual(live._call_trace_dir(), real)
        self.assertEqual(snapshot(), before)

    def rows(self, kind):
        return [row for path in self.directory.glob("*/*.jsonl") for row in map(json.loads, path.read_text().splitlines())
                if row["ev"] == kind]

    def from_provider(self, call, kind, **fields):
        call.message(json.dumps({"type": kind, **fields}), True)

    def user(self, call, text):
        self.from_provider(call, "turn.done", turn={"role": "user", "transcript": text})

    async def test_gpt_context_is_logged_on_send_and_preserves_delayed_delegation_id(self):
        call = await self.make_call()
        call.subscription.channel = channel = Channel()
        brief = "Brief: " + "é" * 1600
        call.send({"type": "session.context.append", "channel": "developer", "content": [{"text": brief}]}, True)
        self.assertEqual(self.rows("context.sent"), [])
        self.user(call, "find my package")
        self.from_provider(call, "delegation.created", item={"target": "client", "id": "d1"})
        call.speak_for("d1", "Lane estimate")
        self.user(call, "what about tomorrow")
        self.from_provider(call, "session.started")
        call.speak_for("d1", "Hermes confirmed delivery")
        rows = self.rows("context.sent")
        self.assertEqual([(r["turn_id"], r["did"]) for r in rows], [(0, None), (1, "d1"), (1, "d1")])
        self.assertEqual([r["texts"] for r in rows], [[e["content"][0]["text"]] for e in channel.sent])
        self.assertEqual(rows[0]["texts"], [brief])
        self.assertTrue(rows[2]["texts"][0].endswith("Hermes confirmed delivery"))

    async def test_grok_traces_instruction_hash_and_translated_items(self):
        os.environ["HERMES_VOICE_ENGINE"] = "grok"
        call = await self.make_call()
        leg = call.leg
        assert leg is not None
        socket = Socket()
        call.manager.http = Mock(ws_connect=AsyncMock(return_value=socket))
        leg.persona = "Réponds brièvement"
        with patch.object(live.grok_leg, "mint_secret", return_value="fixture"), patch.object(leg, "reader", AsyncMock()):
            await leg.start()
        call.note("Brief fact")
        self.user(call, "find my package")
        self.from_provider(call, "delegation.created", item={"target": "client", "id": "d1"})
        call.speak_for("d1", "Lane estimate")
        self.user(call, "what about tomorrow")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        call.speak_for("d1", "Hermes confirmed delivery")
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        rows = self.rows("context.sent")
        instructions = rows[0]
        wire = socket.channel.sent[0]["session"]["instructions"].encode()
        self.assertEqual(instructions["instructions_sha256"], hashlib.sha256(wire).hexdigest())
        self.assertEqual(instructions["instructions_bytes"], len(wire))
        self.assertNotIn(leg.persona, json.dumps(instructions, ensure_ascii=False))
        self.assertEqual([(r["turn_id"], r["type"], r.get("did")) for r in rows], [
            (0, "session.update", None), (0, "conversation.item.create", None),
            (1, "conversation.item.create", "d1"), (1, "conversation.item.create", "d1")])
        items = [e["item"] for e in socket.channel.sent if e["type"] == "conversation.item.create"]
        self.assertEqual([r["texts"] for r in rows[1:]], [[items[0]["content"][0]["text"]],
                         [items[1]["output"]], [items[2]["content"][0]["text"]]])

    async def test_reaction_window_jaccard_and_markers(self):
        call = await self.make_call()
        for text in ("RED green BLUE", "different topic", "another question", "red green blue white black",
                     "different topic", "Okay, NO, I meant the blue one, bro. What?", "broccoli is worthwhile"):
            self.user(call, text)
        rows = self.rows("user.reaction")
        self.assertEqual([row["turn_id"] for row in rows], list(range(1, 8)))
        self.assertEqual([row["repeat"] for row in rows], [False, False, False, True, True, False, False])
        # The same text four turns back is outside the window.
        self.user(call, "another question")
        self.assertFalse(self.rows("user.reaction")[-1]["repeat"])
        self.assertTrue(rows[5]["correction"] and rows[5]["frustration"])
        self.assertFalse(rows[-1]["correction"] or rows[-1]["frustration"])

    async def test_reaction_markers_in_tamil_and_mixed_turns(self):
        call = await self.make_call()
        cases = (
            ("இல்ல, use blue", True, False),
            ("இல்லை.", True, False),
            ("ILLA, use blue", True, False),
            ("நான் சொன்னது blue", True, False),
            ("NAAN  SONNADHU blue", True, False),
            ("காத்திரு!", True, False),
            ("KAATHIRU!", True, False),
            ("Wait, ஒரு நிமிடம்", True, False),
            ("டேய்!", False, True),
            ("ஏற்கனவே சொன்னேன்.", False, True),
            (unicodedata.normalize("NFD", "ஏற்கனவே சொன்னேன்."), False, True),
            ("ERKANAVE SONNEN.", False, True),
            ("already told him, நண்பா", False, True),
            ("இல்லை, use blue DEI.", True, True),
            ("விருப்பம் இல்லாமல் காத்திருந்து சொல்லுங்கள்", False, False),
            ("illustration and deities", False, False),
            # "No" inside a sentence is plain negation, as in English ("I have no time").
            ("எனக்கு நேரம் இல்லை", False, False),
            ("enakku neram illa", False, False),
        )
        for text, _, _ in cases:
            self.user(call, text)
        rows = self.rows("user.reaction")
        self.assertEqual(len(rows), len(cases))
        for (text, correction, frustration), row in zip(cases, rows):
            with self.subTest(text=text):
                self.assertEqual((row["correction"], row["frustration"]), (correction, frustration))

    async def test_barge_in_uses_audible_output_at_input_onset(self):
        call = await self.make_call()
        voice = live.AudioBridge(Frames(2000, 2000), call, "output")
        await voice.recv()  # gated: not audible to the caller
        await live.AudioBridge(Frames(2000), call, "input").recv()
        self.user(call, "first turn")
        await voice.recv()
        await live.AudioBridge(Frames(2000), call, "input").recv()
        self.from_provider(call, "input_transcript.added", item={"text": "Wait, that's not it"})
        self.user(call, "")  # final without text falls back to the transcript delta
        self.assertEqual([r["barge_in"] for r in self.rows("user.reaction")], [False, True])
        self.assertTrue(self.rows("user.reaction")[-1]["correction"])

    async def test_wav_is_opt_in_and_contains_only_unmuted_caller_sample_content(self):
        default = await self.make_call()
        await live.AudioBridge(Frames(2000), default, "input").recv()
        await default.close("test")
        self.assertEqual(list(self.directory.glob("*/*.wav")), [])

        os.environ["Z0LIVE_TRACE_AUDIO"] = "1"
        call = await self.make_call()
        mic = live.AudioBridge(Frames(3000, 3000, 3000), call, "input")
        await mic.recv()
        await live.AudioBridge(Frames(8000), call, "output").recv()
        call.muted = True
        await mic.recv()
        await mic.recv()
        await call.close("test")
        wav_path, = self.directory.glob("*/*.wav")
        self.assertTrue(wav_path.with_suffix(".jsonl").exists())
        self.assertEqual(wav_path.stat().st_mode & 0o777, 0o600)
        with wave.open(str(wav_path)) as wav:
            self.assertEqual((wav.getframerate(), wav.getnchannels(), wav.getsampwidth(), wav.getnframes()),
                             (16000, 1, 2, 960))
            samples = struct.unpack("<960h", wav.readframes(960))
        self.assertTrue(any(samples[:300]))
        self.assertFalse(any(samples[350:]))

    async def test_disabling_trace_disables_audio_even_when_requested(self):
        os.environ.update(Z0LIVE_TRACE_AUDIO="1", HERMES_MOBILE_VOICE_TRACE="0")
        call = await self.make_call()
        await live.AudioBridge(Frames(2000), call, "input").recv()
        await call.close("test")
        self.assertEqual(list(self.directory.iterdir()), [])
