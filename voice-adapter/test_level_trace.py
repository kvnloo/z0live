"""Opt-in per-frame audio level trace (Z0LIVE_TRACE_LEVELS=1): batched `audio.levels` events, default off."""
import math
import os
import unittest
from unittest.mock import patch

import av
import numpy as np

os.environ.setdefault('HERMES_MOBILE_VOICE_LIAISON', '0')
os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")
import mobile_live as live  # noqa: E402

RATE, SAMPLES = 48000, 960  # one 20 ms frame at 48 kHz


def frame(amplitude=0, hz=1000.0, start=0):
    """A packed-stereo s16 frame like aiortc delivers: a sine of `amplitude` (0 = digital silence)."""
    t = (np.arange(SAMPLES) + start) / RATE
    mono = np.round(amplitude * np.sin(2 * math.pi * hz * t)).astype(np.int16)
    stereo = np.repeat(mono, 2).reshape(1, -1)
    return av.AudioFrame.from_ndarray(stereo, format="s16", layout="stereo")


class FakeTrace:
    def __init__(self):
        self.t0, self.events = 100.0, []

    def __call__(self, ev, **data):
        self.events.append((ev, data))


class FakeCall:
    def __init__(self):
        self.trace, self.muted = FakeTrace(), False


class Clock:
    """Deterministic time.monotonic: each frame advances 20 ms from t0."""
    def __init__(self, start=100.0):
        self.now = start

    def __call__(self):
        return self.now


def feed(level, clock, frames, step=0.02):
    for i, f in enumerate(frames):
        level.frame(f)
        clock.now = round(clock.now + step, 6)


def run(env, frames, direction="input"):
    call, clock = FakeCall(), Clock()
    with patch.dict(os.environ, env), patch.object(live.time, "monotonic", clock):
        level = live.Level(call, direction)
        feed(level, clock, frames)
    return call.trace.events, level


class FlagTests(unittest.TestCase):
    def test_flag_parsing_follows_the_z0live_style(self):
        for raw, on in (("1", True), (" 1 ", True), ("", False), ("0", False), ("yes", False)):
            with patch.dict(os.environ, {"Z0LIVE_TRACE_LEVELS": raw}):
                self.assertEqual(live.trace_levels(), on, raw)
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("Z0LIVE_TRACE_LEVELS", None)
            self.assertFalse(live.trace_levels())

    def test_flag_off_emits_no_levels_and_on_off_spans_are_unchanged(self):
        frames = [frame(0)] * 5 + [frame(16384, start=i * SAMPLES) for i in range(10)] + [frame(0)] * 30
        off, _ = run({"Z0LIVE_TRACE_LEVELS": ""}, frames)
        self.assertFalse([e for e in off if e[0] == "audio.levels"])
        self.assertEqual([e[0] for e in off], ["audio.on", "audio.off"])
        self.assertEqual(off[0][1], {"dir": "input", "peak": 16384, "muted": False, "speaker": None})
        self.assertEqual(off[1][1]["span"], 0.2)
        # The flag only adds audio.levels; the on/off events are identical with it on.
        on, _ = run({"Z0LIVE_TRACE_LEVELS": "1"}, frames)
        self.assertEqual([e for e in on if e[0] != "audio.levels"], off)


class LevelTraceTests(unittest.TestCase):
    def test_sine_rms_and_peak_dbfs_match_and_silence_is_the_floor(self):
        amp = 16384  # half scale: peak -6.02 dBFS, sine RMS -9.03 dBFS
        frames = [frame(amp, start=i * SAMPLES) for i in range(13)] + [frame(0)] * 14  # 2 windows: loud, silent
        events, _ = run({"Z0LIVE_TRACE_LEVELS": "1"}, frames, "output")
        batches = [d for ev, d in events if ev == "audio.levels"]
        self.assertEqual(len(batches), 2)
        expect_rms = 20 * math.log10(amp / math.sqrt(2) / 32768)
        expect_peak = 20 * math.log10(amp / 32768)
        first = batches[0]
        self.assertEqual(first["dir"], "output")
        self.assertEqual(first["n"], SAMPLES)
        for r, p in zip(first["rms"], first["peak"]):
            self.assertAlmostEqual(r, expect_rms, delta=0.2)
            self.assertAlmostEqual(p, expect_peak, delta=0.2)
        silent = batches[-1]
        self.assertEqual(set(silent["rms"]), {live.LEVELS_FLOOR_DB})
        self.assertEqual(set(silent["peak"]), {live.LEVELS_FLOOR_DB})
        self.assertEqual(live.LEVELS_FLOOR_DB, -120.0)
        # Times are ms since call.trace.t0, one per frame, 20 ms apart.
        self.assertEqual(first["t"][:3], [0, 20, 40])
        self.assertTrue(all(isinstance(x, int) for x in first["t"]))

    def test_batches_once_per_250_ms_window(self):
        events, level = run({"Z0LIVE_TRACE_LEVELS": "1"}, [frame(0)] * 50)  # 1.0 s of frames
        batches = [d for ev, d in events if ev == "audio.levels"]
        # 50 frames over 1.0 s: a batch closes when a frame lands >= 250 ms after the window start.
        self.assertEqual(len(batches), 3)
        self.assertEqual([len(b["t"]) for b in batches], [13, 13, 13])
        self.assertEqual([b["t"][0] for b in batches], [0, 260, 520])
        for b in batches:
            self.assertLess(b["t"][-1] - b["t"][0], 250)
            self.assertEqual(len(b["t"]), len(b["rms"]))
            self.assertEqual(len(b["t"]), len(b["peak"]))
        # Every frame is accounted for: emitted ones plus the open window.
        self.assertEqual(sum(len(b["t"]) for b in batches) + len(level.levels_t), 50)

    def test_both_directions_are_traced_separately(self):
        call, clock = FakeCall(), Clock()
        with patch.dict(os.environ, {"Z0LIVE_TRACE_LEVELS": "1"}), patch.object(live.time, "monotonic", clock):
            a, b = live.Level(call, "input"), live.Level(call, "output")
            for _ in range(20):
                a.frame(frame(0))
                b.frame(frame(8000))
                clock.now = round(clock.now + 0.02, 6)
        dirs = {d["dir"] for ev, d in call.trace.events if ev == "audio.levels"}
        self.assertEqual(dirs, {"input", "output"})

    def test_level_errors_never_reach_the_audio_path(self):
        frames = [frame(16384, start=i * SAMPLES) for i in range(20)]
        with patch.object(live, "frame_dbfs", side_effect=RuntimeError("boom")):
            events, _ = run({"Z0LIVE_TRACE_LEVELS": "1"}, frames)
        self.assertEqual([e[0] for e in events], ["audio.on"])  # on/off detection still works
        call, clock = FakeCall(), Clock()

        def broken(ev, **data):
            if ev == "audio.levels":
                raise OSError("disk full")
            call.trace.events.append((ev, data))
        broken.t0 = 100.0
        call.trace = broken
        call.trace.events = []
        with patch.dict(os.environ, {"Z0LIVE_TRACE_LEVELS": "1"}), patch.object(live.time, "monotonic", clock):
            level = live.Level(call, "input")
            feed(level, clock, frames)
        self.assertEqual([e[0] for e in call.trace.events], ["audio.on"])


if __name__ == "__main__":
    unittest.main()
