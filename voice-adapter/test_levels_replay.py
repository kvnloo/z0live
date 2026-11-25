"""bench/levels_replay.py: offline scorer for opt-in `audio.levels` call traces (Z0LIVE_TRACE_LEVELS=1)."""
import contextlib
import io
import json
import math
import os
from pathlib import Path
import sys
import tempfile
import unittest

import numpy as np

original_path = sys.path[:]
sys.path.insert(0, str(Path(__file__).resolve().parent / 'bench'))
try:
    import levels_replay as lr
finally:
    sys.path[:] = original_path

UP, DOWN = "input", "output"  # the adapter's real AudioBridge/Level direction strings
N = 960                       # one 20 ms frame at 48 kHz (frame.samples)
FRAME_MS = 20
FLOOR = -120.0


def line(ev, t_s=0.0, **data):
    """One call-trace JSONL line, laid out like call_trace.Trace.__call__ writes it."""
    return json.dumps({"t": round(t_s, 3), "wall": 1_800_000_000.0 + t_s, "ev": ev, **data})


def levels_lines(direction, t, rms, peak, n=N, window=13):
    """Batch per-frame arrays into audio.levels events the way Level.trace_level does (n int when uniform)."""
    out = []
    for i in range(0, len(t), window):
        ns = n[i:i + window] if isinstance(n, list) else [n] * len(t[i:i + window])
        out.append(line("audio.levels", t[i] / 1000, dir=direction, t=t[i:i + window], rms=rms[i:i + window],
                        peak=peak[i:i + window], n=ns[0] if len(set(ns)) == 1 else ns))
    return out


def call(down_rms, up_rms, crest=3.0):
    """Two-direction trace: one frame per 20 ms per direction; peak = rms + crest (floor stays at floor)."""
    t = [i * FRAME_MS for i in range(len(down_rms))]

    def pk(r):
        return [FLOOR if x <= FLOOR else round(min(0.0, x + crest), 1) for x in r]
    lines = [line("call.start")]
    lines += levels_lines(DOWN, t, down_rms, pk(down_rms))
    lines += levels_lines(UP, t[:len(up_rms)], up_rms, pk(up_rms))
    return lines


def score(lines, **kw):
    return lr.score(lr.parse(lines), lr.Params(**kw))


def speechy(i, base=-20.0):
    """A deterministic speech-like downlink level: 10 dB of syllable modulation around base."""
    return round(base + 5.0 * math.sin(i / 3.0), 1)


class ParseTests(unittest.TestCase):
    def test_n_int_and_n_list_expand_to_the_same_frames(self):
        t, r, p = [0, 20, 40], [-30.0, -31.0, -32.0], [-27.0, -28.0, -29.0]
        a = lr.parse(levels_lines(UP, t, r, p, n=N))
        b = lr.parse(levels_lines(UP, t, r, p, n=[N, N, 480]))
        self.assertEqual(a.dirs[UP].n.tolist(), [N, N, N])
        self.assertEqual(b.dirs[UP].n.tolist(), [N, N, 480])
        self.assertEqual(a.dirs[UP].t.tolist(), t)
        self.assertEqual(b.dirs[UP].rms.tolist(), r)
        self.assertEqual(a.malformed_lines + a.malformed_events, 0)

    def test_malformed_lines_and_events_are_counted_not_fatal(self):
        good = levels_lines(UP, [0, 20], [-30.0, -30.0], [-27.0, -27.0])
        bad = ["{not json", "", "[1, 2]", "   ",
               line("audio.levels", dir=UP, t=[40, 60], rms=[-30.0], peak=[-27.0, -27.0], n=N),  # length mismatch
               line("audio.levels", dir=UP, t=[40], rms=["loud"], peak=[-27.0], n=N),            # non-numeric
               line("audio.levels", dir=UP, t=[40], rms=[-30.0], peak=[-27.0], n=[N, N]),        # n list mismatch
               line("audio.levels", t=[40], rms=[-30.0], peak=[-27.0], n=N),                    # no dir
               line("audio.levels", dir=UP, t=[40], rms=[-30.0], peak=[-27.0], n=0)]            # n <= 0
        tr = lr.parse(good + bad + [line("audio.on", dir=UP, peak=900)])
        self.assertEqual(tr.malformed_lines, 2)   # "{not json" and the JSON array; blank lines are skipped
        self.assertEqual(tr.malformed_events, 5)
        self.assertEqual(len(tr.dirs[UP].t), 2)
        rep = lr.score(tr, lr.Params())
        self.assertEqual(rep["integrity"]["malformed_lines"], 2)
        self.assertEqual(rep["integrity"]["malformed_events"], 5)

    def test_gap_detection_counts_holes_over_two_frames(self):
        t = [0, 20, 40, 60, 160, 180, 200, 240]  # 100 ms hole (gap 80 ms) and a 40 ms step (not a gap)
        tr = lr.parse(levels_lines(UP, t, [-30.0] * 8, [-27.0] * 8))
        integ = lr.score(tr, lr.Params())["integrity"]["dirs"][UP]
        self.assertEqual(integ["gaps"], 1)
        self.assertEqual(integ["gap_ms"], 80)
        self.assertEqual(integ["frames"], 8)
        self.assertEqual(integ["windows"], 1)

    def test_monotonic_check_counts_duplicates_and_reversals(self):
        t = [0, 20, 20, 40, 30, 60, 80]
        tr = lr.parse(levels_lines(UP, t, [-30.0] * 7, [-27.0] * 7))
        integ = lr.score(tr, lr.Params())["integrity"]["dirs"][UP]
        self.assertEqual(integ["duplicate_t"], 1)
        self.assertEqual(integ["non_monotonic_t"], 1)
        # Metrics run on the sorted, de-duplicated frames.
        self.assertEqual(tr.dirs[UP].clean()[0].tolist(), [0, 20, 30, 40, 60, 80])

    def test_level_constants_match_the_adapter(self):
        with open(Path(__file__).resolve().parent / "mobile_live.py", encoding="utf-8") as f:
            src = f.read()
        self.assertIn(f"LEVELS_FLOOR_DB = {lr.LEVELS_FLOOR_DB}", src)
        self.assertIn(f"LEVEL_OFF_SECONDS = {lr.LEVEL_OFF_MS / 1000}", src)
        self.assertIn(f"LEVEL_ON = {lr.LEVEL_ON}", src)
        # The default --on-db reproduces peak >= LEVEL_ON on the 1-decimal dBFS the trace carries.
        dbfs = lambda v: round(20 * math.log10(v / 32768.0), 1)  # noqa: E731 - mobile_live.dbfs
        self.assertGreaterEqual(dbfs(lr.LEVEL_ON), lr.Params().on_db)
        self.assertLess(dbfs(lr.LEVEL_ON - 1), lr.Params().on_db)


class MetricTests(unittest.TestCase):
    def test_echo_floor_uses_only_downlink_active_frames(self):
        # 2 s downlink speech then 2 s silence; uplink is -50 while downlink plays and -10 (owner talking) after.
        down = [speechy(i) for i in range(100)] + [FLOOR] * 100
        up = [-50.0] * 100 + [-10.0] * 100
        rep = score(call(down, up))
        self.assertEqual(rep["echo"]["floor_db"], -50.0)
        self.assertEqual(rep["uplink"]["down_active"]["p50"], -50.0)
        self.assertEqual(rep["uplink"]["down_silent"]["p50"], -10.0)
        # Hangover (300 ms = 15 frames) keeps the first silent frames in the active set, like Level.
        self.assertEqual(rep["uplink"]["down_active"]["frames"], 115)
        self.assertEqual(rep["downlink"]["spans"][0]["start_ms"], 0)
        self.assertEqual(rep["downlink"]["spans"][0]["end_ms"], 2000)

    def test_injected_barge_in_is_detected_within_one_frame(self):
        down = [FLOOR] * 25 + [speechy(i) for i in range(500)] + [FLOOR] * 50  # span starts at 500 ms
        up = [round(d - 20, 1) if d > FLOOR else -70.0 for d in down]          # pure echo
        inject_ms = 500 + 6000                                                  # owner talks 6.0 s into the span
        for i in range(inject_ms // FRAME_MS, inject_ms // FRAME_MS + 20):      # 400 ms at -12 dBFS
            up[i] = -12.0
        rep = score(call(down, up), margin_db=6.0, min_run_ms=100)
        onsets = rep["bargein"]["onsets"]
        self.assertEqual(len(onsets), 1, onsets)
        self.assertLessEqual(abs(onsets[0]["t_ms"] - inject_ms), FRAME_MS)
        self.assertEqual(onsets[0]["span_start_ms"], 500)
        self.assertLessEqual(abs(onsets[0]["rel_ms"] - 6000), FRAME_MS)
        self.assertEqual(onsets[0]["run_ms"], 400)
        self.assertEqual(rep["bargein"]["double_talk_frames"], 20)

    def test_pure_echo_yields_no_onset(self):
        down = [FLOOR] * 25 + [speechy(i) for i in range(1500)] + [FLOOR] * 50
        up = [round(d - 20, 1) if d > FLOOR else -70.0 for d in down]
        rep = score(call(down, up))
        self.assertEqual(rep["bargein"]["onsets"], [])
        self.assertEqual(rep["bargein"]["double_talk_frames"], 0)
        self.assertEqual(len(rep["downlink"]["spans"]), 1)

    def test_short_blip_under_min_run_is_not_an_onset(self):
        down = [speechy(i) for i in range(300)]
        up = [round(d - 20, 1) for d in down]
        for i in range(150, 153):  # 60 ms
            up[i] = -12.0
        rep = score(call(down, up), min_run_ms=100)
        self.assertEqual(rep["bargein"]["onsets"], [])
        self.assertEqual(rep["bargein"]["double_talk_frames"], 3)

    def test_empty_trace_gives_zero_metrics(self):
        for lines in ([], [line("call.start"), line("audio.on", dir=UP, peak=900)]):
            rep = score(lines)
            self.assertEqual(rep["schema"], "levels_replay/1")
            self.assertEqual(rep["downlink"]["spans"], [])
            self.assertEqual(rep["downlink"]["active_frames"], 0)
            self.assertEqual(rep["bargein"]["onsets"], [])
            self.assertEqual(rep["bargein"]["double_talk_frames"], 0)
            self.assertIsNone(rep["echo"]["floor_db"])
            self.assertEqual(rep["integrity"]["dirs"][UP]["frames"], 0)
            self.assertEqual(rep["integrity"]["dirs"][DOWN]["frames"], 0)


def double_talk_call(caller_frac=0.35, caller_db=-12.0):
    """One 6 s downlink span (starts at 500 ms); uplink = echo (downlink - 20 dB) with the caller at caller_db on the
    last caller_frac of the span (double talk dominates the echo set's top 5 %)."""
    down = [FLOOR] * 25 + [speechy(i) for i in range(300)] + [FLOOR] * 50
    up = [round(d - 20, 1) if d > FLOOR else -70.0 for d in down]
    talk = 25 + int(300 * (1 - caller_frac))
    for i in range(talk, 325):
        up[i] = caller_db
    return down, up, talk * FRAME_MS


class FloorModeTests(unittest.TestCase):
    """floor_mode: the echo-floor estimator over the downlink-loud uplink frames (the echo set)."""
    def echo_set(self, down, up):
        return np.asarray([u for d, u in zip(down, up) if d > FLOOR], dtype=np.float64)

    def test_default_floor_mode_is_p95(self):
        self.assertEqual(lr.Params().floor_mode, "p95")
        self.assertEqual(set(lr.FLOOR_MODES), {"p95", "p50", "min", "early"})

    def test_unknown_floor_mode_is_rejected(self):
        down, up, _ = double_talk_call()
        with self.assertRaises(ValueError):
            score(call(down, up), floor_mode="p99")

    def test_p95_and_p50_follow_their_percentile(self):
        down, up, _ = double_talk_call()
        echo = self.echo_set(down, up)
        self.assertEqual(score(call(down, up), floor_mode="p95")["echo"]["floor_db"],
                         round(float(np.percentile(echo, 95)), 1))
        self.assertEqual(score(call(down, up), floor_mode="p50")["echo"]["floor_db"],
                         round(float(np.percentile(echo, 50)), 1))

    def test_p95_floor_is_lifted_by_double_talk_and_misses_the_barge_in(self):
        down, up, talk_ms = double_talk_call()
        rep = score(call(down, up), floor_mode="p95")
        self.assertEqual(rep["echo"]["floor_db"], -12.0)
        self.assertEqual(rep["bargein"]["onsets"], [])

    def test_robust_modes_detect_the_barge_in_and_stay_quiet_on_echo(self):
        down, up, talk_ms = double_talk_call()
        echo_only = [round(d - 20, 1) if d > FLOOR else -70.0 for d in down]
        for mode in ("p50", "min", "early"):
            with self.subTest(mode=mode):
                rep = score(call(down, up), floor_mode=mode)
                self.assertLess(rep["echo"]["floor_db"], -30.0)
                onsets = rep["bargein"]["onsets"]
                self.assertTrue(any(o["t_ms"] <= talk_ms < o["t_ms"] + o["run_ms"] for o in onsets), onsets)
                if mode == "min":  # p10 + 6 dB sits under this 10 dB-modulated echo's peaks: it can fire early
                    continue
                self.assertEqual(len(onsets), 1, onsets)
                self.assertEqual(onsets[0]["t_ms"], talk_ms)
                self.assertEqual(score(call(down, echo_only), floor_mode=mode)["bargein"]["onsets"], [])

    def test_min_is_the_median_of_per_span_p10(self):
        # Three spans (each 1 s of speech, 1 s of silence apart), echo at -40/-30/-50 + modulation.
        down, up = [], []
        for base in (-40.0, -30.0, -50.0):
            down += [speechy(i) for i in range(50)] + [FLOOR] * 50
            up += [round(base + (i % 7), 1) for i in range(50)] + [-70.0] * 50
        rep = score(call(down, up), floor_mode="min")
        self.assertEqual(len(rep["downlink"]["spans"]), 3)
        p10 = [float(np.percentile([b + (i % 7) for i in range(50)], 10)) for b in (-40.0, -30.0, -50.0)]
        self.assertEqual(rep["echo"]["floor_db"], round(float(np.median(p10)), 1))

    def test_early_uses_the_first_200_ms_of_each_span(self):
        down, up, _ = double_talk_call()
        up = list(up)
        for i in range(25, 35):        # first 200 ms of the span: 10 echo frames at a known level
            up[i] = -44.0 + (i - 25) * 0.5
        rep = score(call(down, up), floor_mode="early")
        self.assertEqual(rep["echo"]["floor_db"], round(float(np.percentile(up[25:35], 95)), 1))

    def test_early_falls_back_to_p50_with_under_5_early_frames(self):
        # Uplink trace starts 600 ms into the downlink span: no echo frame lies in the span's first 200 ms.
        down, up, _ = double_talk_call()
        t = [i * FRAME_MS for i in range(len(down))]
        pk = [FLOOR if x <= FLOOR else round(min(0.0, x + 3.0), 1) for x in down]
        lines = levels_lines(DOWN, t, down, pk) + levels_lines(UP, t[55:], up[55:], [u + 3.0 for u in up[55:]])
        rep = score(lines, floor_mode="early")
        echo = np.asarray([u for d, u in zip(down[55:], up[55:]) if d > FLOOR])
        self.assertEqual(rep["echo"]["floor_db"], round(float(np.percentile(echo, 50)), 1))
        self.assertEqual(rep["echo"]["floor_mode"], "early")

    def test_cli_floor_mode(self):
        down, up, _ = double_talk_call()
        out = io.StringIO()
        old = sys.stdin
        try:
            sys.stdin = io.StringIO("\n".join(call(down, up)) + "\n")
            with contextlib.redirect_stdout(out):
                self.assertEqual(lr.main(["--json", "--floor-mode", "p50"]), 0)
        finally:
            sys.stdin = old
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["params"]["floor_mode"], "p50")
        self.assertEqual(doc["echo"]["floor_mode"], "p50")


def hindsight_call(talk=((85, 156),), down_frames=100, lead=25, tail=50):
    """Downlink span from lead*20 ms for down_frames frames; echo uplink 20 dB under it; caller at -15 dBFS over
    each [a, b) frame range in `talk` (frame indices on the shared 20 ms grid)."""
    down = [FLOOR] * lead + [speechy(i) for i in range(down_frames)] + [FLOOR] * tail
    up = [round(d - 20, 1) if d > FLOOR else -70.0 for d in down]
    for a, b in talk:
        for i in range(a, min(b, len(up))):
            up[i] = -15.0
    return down, up


class HindsightTests(unittest.TestCase):
    """onset_mode hindsight: a caller still talking after the downlink span -> trace back to the in-span onset."""

    def onsets(self, down, up, **kw):
        return score(call(down, up), onset_mode="hindsight", **kw)["bargein"]["onsets"]

    def test_default_onset_mode_is_run(self):
        self.assertEqual(lr.Params().onset_mode, "run")
        self.assertEqual(set(lr.ONSET_MODES), {"run", "hindsight"})

    def test_unknown_onset_mode_is_rejected(self):
        down, up = hindsight_call()
        with self.assertRaises(ValueError):
            score(call(down, up), onset_mode="oracle")

    def test_continuing_caller_is_traced_back_to_the_in_span_onset(self):
        down, up = hindsight_call()                     # caller from frame 85 (1700 ms) to 3100 ms
        for k in (6.0, 9.0, 12.0):
            with self.subTest(k=k):
                got = self.onsets(down, up, hindsight_db=k)
                self.assertEqual(len(got), 1, got)
                self.assertEqual(got[0]["t_ms"], 1700)
                self.assertEqual(got[0]["span_start_ms"], 500)
                self.assertEqual(got[0]["rel_ms"], 1200)
                self.assertEqual(got[0]["max_rms_db"], -15.0)

    def test_caller_who_stops_before_the_span_ends_gives_no_onset(self):
        down, up = hindsight_call(talk=((85, 115),))     # stops at 2300 ms, downlink ends 2500 ms
        self.assertEqual(self.onsets(down, up, hindsight_db=9.0), [])

    def test_echo_only_gives_no_onset(self):
        down, up = hindsight_call(talk=())
        self.assertEqual(self.onsets(down, up, hindsight_db=6.0), [])

    def test_caller_starting_after_the_span_gives_no_onset(self):
        down, up = hindsight_call(talk=((141, 170),))    # after active_until (2800 ms): not a barge-in
        self.assertEqual(self.onsets(down, up, hindsight_db=9.0), [])

    def test_pause_shorter_than_gap_bridges_longer_pause_breaks(self):
        down, up = hindsight_call(talk=((85, 95), (105, 156)))   # 200 ms pause: bridged -> 1700
        self.assertEqual([o["t_ms"] for o in self.onsets(down, up, hindsight_db=9.0)], [1700])
        down, up = hindsight_call(talk=((85, 95), (115, 156)))   # 400 ms pause (> 300): onset 2300
        self.assertEqual([o["t_ms"] for o in self.onsets(down, up, hindsight_db=9.0)], [2300])

    def test_open_span_gives_no_onset(self):
        down, up = hindsight_call(tail=0, talk=((85, 125),))     # trace ends while the downlink still plays
        self.assertEqual(self.onsets(down, up, hindsight_db=9.0), [])

    def test_run_mode_output_is_unchanged_by_the_new_fields(self):
        down, up, _ = double_talk_call()
        a = score(call(down, up), floor_mode="p50")
        b = score(call(down, up), floor_mode="p50", onset_mode="run", hindsight_db=3.0)
        self.assertEqual(a["bargein"], b["bargein"])
        self.assertEqual(a["echo"], b["echo"])

    def test_cli_onset_mode(self):
        down, up = hindsight_call()
        out = io.StringIO()
        old = sys.stdin
        try:
            sys.stdin = io.StringIO("\n".join(call(down, up)) + "\n")
            with contextlib.redirect_stdout(out):
                self.assertEqual(lr.main(["--json", "--onset-mode", "hindsight", "--hindsight-db", "9"]), 0)
        finally:
            sys.stdin = old
        doc = json.loads(out.getvalue())
        self.assertEqual(doc["params"]["onset_mode"], "hindsight")
        self.assertEqual([o["t_ms"] for o in doc["bargein"]["onsets"]], [1700])


class AdapterRoundTripTests(unittest.TestCase):
    def test_real_level_events_parse_and_spans_match_audio_on_off(self):
        """Frames through mobile_live.Level (flag on) -> Trace-format JSONL -> scorer: frame counts and the
        downlink span equal the adapter's own audio.on / audio.off."""
        import test_level_trace as tlt
        frames = [tlt.frame(0)] * 10 + [tlt.frame(16384, start=i * tlt.SAMPLES) for i in range(40)] \
            + [tlt.frame(0)] * 40
        events, level = tlt.run({"Z0LIVE_TRACE_LEVELS": "1"}, frames, DOWN)
        lines = [line(ev, **data) for ev, data in events]
        rep = lr.score(lr.parse(lines), lr.Params())
        emitted = sum(len(d["t"]) for ev, d in events if ev == "audio.levels")
        self.assertEqual(rep["integrity"]["dirs"][DOWN]["frames"], emitted)
        self.assertEqual(emitted + len(level.levels_t), len(frames))
        self.assertEqual(rep["integrity"]["malformed_events"], 0)
        self.assertEqual(rep["integrity"]["dirs"][DOWN]["gaps"], 0)
        on = [d for ev, d in events if ev == "audio.on"]
        off = [d for ev, d in events if ev == "audio.off"]
        self.assertEqual((len(on), len(off)), (1, 1))
        span = rep["downlink"]["spans"][0]
        self.assertEqual(span["start_ms"], 200)  # 10 silent 20 ms frames, then loud
        self.assertEqual(span["end_ms"] - span["start_ms"], round(off[0]["span"] * 1000))
        self.assertEqual(span["end_ms"], round(off[0]["at"] * 1000))


class CliTests(unittest.TestCase):
    def run_cli(self, argv, stdin_text=None):
        out = io.StringIO()
        old_stdin = sys.stdin
        try:
            if stdin_text is not None:
                sys.stdin = io.StringIO(stdin_text)
            with contextlib.redirect_stdout(out):
                code = lr.main(argv)
        finally:
            sys.stdin = old_stdin
        return code, out.getvalue()

    def test_json_from_path_and_stdin_is_identical_and_sorted(self):
        down = [speechy(i) for i in range(200)]
        text = "\n".join(call(down, [round(d - 20, 1) for d in down])) + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "call.jsonl")
            with open(path, "w", encoding="utf-8") as f:
                f.write(text)
            code, a = self.run_cli([path, "--json"])
        self.assertEqual(code, 0)
        code, b = self.run_cli(["--json"], text)
        self.assertEqual(a, b)
        doc = json.loads(a)
        self.assertEqual(doc["schema"], "levels_replay/1")
        self.assertEqual(a, json.dumps(doc, sort_keys=True, indent=1) + "\n")
        _, again = self.run_cli(["--json"], text)
        self.assertEqual(again, b)

    def test_human_summary_mentions_the_key_numbers(self):
        down = [speechy(i) for i in range(200)]
        code, out = self.run_cli([], "\n".join(call(down, [round(d - 20, 1) for d in down])))
        self.assertEqual(code, 0)
        self.assertIn("levels_replay/1", out)
        self.assertIn("echo floor", out)
        self.assertIn("barge-in onsets: 0", out)


if __name__ == "__main__":
    unittest.main()
