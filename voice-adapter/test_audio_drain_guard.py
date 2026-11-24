"""R15 guarded drain: packets behind the drain origin are ignored (no insert, no release, no ring reset)."""
import os
import unittest
from types import SimpleNamespace
from unittest import mock

from aiortc import RTCRtpReceiver

import mobile_live
from audio_drain import AudioDrainJitterBuffer
from test_audio_drain import ids, packet


class GuardedDrainTest(unittest.TestCase):
    def setUp(self):
        self.buf = AudioDrainJitterBuffer(guard=True)

    def feed(self, seqs, buf=None):
        buf = buf or self.buf
        return [i for seq in seqs for i in ids(buf.add_many(packet(seq)))]

    def test_behind_origin_ignored_without_reset(self):
        self.assertEqual(self.feed(range(120)), list(range(119)))
        origin, ring = self.buf._origin, list(self.buf._packets)
        self.assertEqual(origin, 119)
        self.assertEqual(self.feed([5]), [])  # 114 behind: stock would flush the ring and move the origin back
        self.assertEqual(self.buf._origin, origin)
        self.assertEqual(self.buf._packets, ring)  # not inserted, nothing removed
        self.assertEqual(self.feed([120, 121]), [119, 120])

    def test_near_behind_also_ignored(self):
        self.assertEqual(self.feed(range(10)), list(range(9)))
        self.assertEqual(self.feed([3]), [])
        self.assertEqual(self.buf._origin, 9)

    def test_unguarded_drain_still_resets(self):
        buf = AudioDrainJitterBuffer()
        self.feed(range(120), buf)
        self.feed([5], buf)
        self.assertEqual(buf._origin, 5)  # stock backward-misorder reset (the R13 behaviour stays selectable)

    def test_wrap_boundary_behind_across_wrap(self):
        start = 65500
        self.assertEqual(self.feed(range(start, start + 150)), list(range(start, start + 149)))
        origin = self.buf._origin
        self.assertEqual(origin, (start + 149) & 65535)  # wrapped
        ring = list(self.buf._packets)
        self.assertEqual(self.feed([65520]), [])  # 129 behind across the wrap
        self.assertEqual((self.buf._origin, self.buf._packets), (origin, ring))
        self.assertEqual(self.feed([start + 150]), [start + 149])

    def test_wrap_boundary_forward_across_wrap_not_ignored(self):
        self.assertEqual(self.feed([65533, 65534, 65536]), [65533])
        self.assertEqual(self.feed([65535, 65537, 65538]), list(range(65534, 65538)))

    def test_half_range_tie_stays_forward_as_stock(self):
        guarded, plain = AudioDrainJitterBuffer(guard=True), AudioDrainJitterBuffer()
        for b in (guarded, plain):
            self.feed(range(20), b)
            self.feed([(19 + 0x8000) & 65535], b)
        self.assertEqual(guarded._origin, plain._origin)
        self.assertNotEqual(guarded._origin, 19)

    def test_in_window_reorder_still_filled(self):
        self.assertEqual(self.feed([0, 1, 2, 4, 5]), [0, 1])
        self.assertEqual(self.feed([3]), [2, 3, 4])
        self.assertEqual(self.feed([6]), [5])

    def test_hole_fill_after_later_run(self):
        self.assertEqual(self.feed(range(8)), list(range(7)))
        self.assertEqual(self.feed(range(9, 14)), [])
        self.assertEqual(self.feed([8]), list(range(7, 13)))

    def test_far_late_startup_loses_only_the_late_packets(self):
        # R15 far-late: 0-1 arrive ~2 s late (>= 100 behind), 2-8 arrive 181 ms late.
        trace = list(range(9, 11)) + list(range(2, 9)) + list(range(11, 110)) + [0, 1] + list(range(110, 200))
        got_guard = set(self.feed(trace))
        got_plain = set(self.feed(trace, AudioDrainJitterBuffer()))
        # 2-8 trail the origin (9) in both and are dropped as stock does; 0-1 then arrive 109 behind.
        self.assertEqual(set(range(9, 199)) - got_guard, set())
        self.assertNotIn(0, got_guard)
        self.assertTrue(set(range(9, 199)) - got_plain)  # unguarded reset flushes frames beyond 0-8


class GuardFlagTest(unittest.TestCase):
    def install(self, raw):
        r = RTCRtpReceiver("audio", SimpleNamespace(state="new"))
        with mock.patch.dict(os.environ, {"Z0LIVE_UPLINK_DRAIN": raw}):
            got = mobile_live.install_uplink_drain([SimpleNamespace(kind="audio", receiver=r)])
        return got

    def test_guard_mode_installs_guarded_buffer(self):
        (jb,) = self.install("guard")
        self.assertIsInstance(jb, AudioDrainJitterBuffer)
        self.assertTrue(jb.guard)
        self.assertEqual(jb._prefetch, 1)

    def test_drain_1_stays_unguarded(self):
        (jb,) = self.install("1")
        self.assertFalse(jb.guard)

    def test_modes(self):
        for raw, want in (("1", "drain"), ("guard", "guard"), (" guard ", "guard"), ("0", ""), ("", ""),
                          ("true", "")):
            with mock.patch.dict(os.environ, {"Z0LIVE_UPLINK_DRAIN": raw}):
                self.assertEqual(mobile_live.uplink_drain_mode(), want, raw)
                self.assertEqual(mobile_live.uplink_drain(), bool(want), raw)


if __name__ == "__main__":
    unittest.main()
