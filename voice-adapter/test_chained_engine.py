"""Contract for the on_device (`chained`) voice engine: STT -> model -> TTS with pluggable async stages.

Not wired into Call yet (mobile_live keeps falling back to gpt-live); these tests pin the slice the
wiring will use.
"""
import asyncio
import unittest

import chained_engine as ce


def run(coro):
    return asyncio.run(coro)


class Recorder:
    def __init__(self):
        self.traces, self.events, self.audio = [], [], []

    def trace(self, event, **data):
        self.traces.append((event, data))

    def emit(self, event):
        self.events.append(event)

    def on_audio(self, pcm):
        self.audio.append(pcm)

    def names(self):
        return [name for name, _ in self.traces]


def stages(text="what time is it", reply=("It is ", "noon. ", "Anything else?"), fail=None):
    seen = {"stt": [], "model": [], "tts": []}

    async def stt(pcm):
        seen["stt"].append(pcm)
        if fail == "stt":
            raise RuntimeError("stt broke")
        return text

    async def model(prompt):
        seen["model"].append(prompt)
        for token in reply:
            if fail == "model":
                raise RuntimeError("model broke")
            yield token

    async def tts(chunk):
        seen["tts"].append(chunk)
        if fail == "tts":
            raise RuntimeError("tts broke")
        yield b"\x01\x00" * 4
        yield b"\x02\x00" * 4

    return stt, model, tts, seen


class ChainedEngineTests(unittest.TestCase):
    def engine(self, rec, **kw):
        stt, model, tts, seen = stages(**kw)
        return ce.ChainedEngine(stt, model, tts, trace=rec.trace, emit=rec.emit, on_audio=rec.on_audio), seen

    def test_one_turn_eos_to_first_audio(self):
        rec = Recorder()
        engine, seen = self.engine(rec)
        result = run(engine.turn(b"\x00\x00" * 160, turn_id=1))
        self.assertTrue(result.ok)
        self.assertEqual(seen["stt"], [b"\x00\x00" * 160])
        self.assertEqual(seen["model"], ["what time is it"])
        self.assertEqual(result.text, "what time is it")
        self.assertEqual(result.reply, "It is noon. Anything else?")
        self.assertEqual(rec.audio[0], b"\x01\x00" * 4)
        self.assertEqual(result.chunks, len(rec.audio))
        self.assertEqual(result.bytes, sum(map(len, rec.audio)))
        self.assertIsNotNone(result.first_audio_ns)
        self.assertLessEqual(result.stt_ns, result.first_token_ns)
        self.assertLessEqual(result.first_token_ns, result.first_audio_ns)

    def test_trace_events_in_order(self):
        rec = Recorder()
        engine, _ = self.engine(rec)
        run(engine.turn(b"\x00\x00", turn_id=7))
        names = rec.names()
        for name in ("chained.eos", "chained.stt_done", "chained.first_token", "chained.first_audio", "chained.turn_done"):
            self.assertIn(name, names)
            self.assertEqual(names.count(name), 1, name)
        self.assertEqual(names, sorted(names, key=["chained.eos", "chained.stt_done", "chained.first_token",
                                                   "chained.first_audio", "chained.turn_done"].index))
        data = dict(rec.traces)
        self.assertEqual(data["chained.stt_done"]["turn_id"], 7)
        self.assertIn("ms", data["chained.first_audio"])
        self.assertEqual(data["chained.turn_done"]["status"], "ok")

    def test_speaks_call_dialect(self):
        rec = Recorder()
        engine, _ = self.engine(rec)
        run(engine.turn(b"\x00\x00", turn_id=2))
        types = [e["type"] for e in rec.events]
        self.assertEqual(types[0], "input_transcript.added")
        self.assertEqual(rec.events[0]["text"], "what time is it")
        self.assertIn("output_transcript.added", types)
        self.assertEqual(types[-1], "turn.done")

    def test_tts_gets_clauses_not_tokens(self):
        rec = Recorder()
        engine, seen = self.engine(rec)
        run(engine.turn(b"\x00\x00"))
        self.assertEqual(seen["tts"], ["It is noon.", "Anything else?"])

    def test_empty_transcript_skips_model(self):
        rec = Recorder()
        engine, seen = self.engine(rec, text="  ")
        result = run(engine.turn(b"\x00\x00"))
        self.assertTrue(result.ok)
        self.assertEqual(seen["model"], [])
        self.assertEqual(rec.audio, [])
        self.assertIn("chained.empty", rec.names())
        self.assertNotIn("chained.first_audio", rec.names())

    def test_stage_errors_are_traced_not_raised(self):
        for stage in ("stt", "model", "tts"):
            with self.subTest(stage=stage):
                rec = Recorder()
                engine, _ = self.engine(rec, fail=stage)
                result = run(engine.turn(b"\x00\x00"))
                self.assertFalse(result.ok)
                self.assertEqual(result.error_stage, stage)
                errors = [d for n, d in rec.traces if n == "chained.error"]
                self.assertEqual([e["stage"] for e in errors], [stage])
                self.assertEqual(dict(rec.traces)["chained.turn_done"]["status"], "error")
                self.assertEqual(rec.events[-1]["type"], "turn.done")

    def test_interrupt_stops_audio(self):
        rec = Recorder()
        gate = asyncio.Event()

        async def stt(pcm):
            return "talk"

        async def model(prompt):
            yield "One. "
            await gate.wait()
            yield "Two."

        async def tts(chunk):
            yield chunk.encode()

        async def go():
            engine = ce.ChainedEngine(stt, model, tts, trace=rec.trace, emit=rec.emit, on_audio=rec.on_audio)
            task = asyncio.ensure_future(engine.turn(b"\x00\x00"))
            while not rec.audio:
                await asyncio.sleep(0)
            self.assertTrue(engine.interrupt())
            result = await task
            self.assertFalse(engine.interrupt())
            return result

        result = run(go())
        self.assertFalse(result.ok)
        self.assertTrue(result.interrupted)
        self.assertEqual(rec.audio, [b"One."])
        self.assertIn("chained.interrupted", rec.names())
        self.assertEqual(dict(rec.traces)["chained.turn_done"]["status"], "interrupted")

    def test_async_on_audio_is_awaited(self):
        got = []

        async def sink(pcm):
            await asyncio.sleep(0)
            got.append(pcm)

        stt, model, tts, _ = stages()
        result = run(ce.ChainedEngine(stt, model, tts, on_audio=sink).turn(b"\x00\x00"))
        self.assertTrue(result.ok)
        self.assertEqual(len(got), result.chunks)

    def test_not_wired_into_production(self):
        import mobile_live
        self.assertEqual(mobile_live.ENGINES["on_device"], "chained")
        with open(mobile_live.__file__, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('if self.engine not in ("gpt-live", "grok"):', src)
        self.assertNotIn("chained_engine", src)


if __name__ == "__main__":
    unittest.main()
