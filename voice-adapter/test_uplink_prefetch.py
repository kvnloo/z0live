import os
import unittest
from types import SimpleNamespace
from unittest import mock

from aiortc import RTCRtpReceiver

import mobile_live
from adaptive_jitter import install


def receiver(kind="audio"):
    return RTCRtpReceiver(kind, SimpleNamespace(state="new"))


class UplinkPrefetchTest(unittest.TestCase):
    def test_default_is_two_and_env_restores_stock(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("Z0LIVE_UPLINK_PREFETCH", None)
            self.assertEqual(mobile_live.uplink_prefetch(), 2)
        for raw, want in (("4", 4), ("1", 1), (" 3 ", 3), ("0", 4), ("9", 4), ("x", 4), ("", 4)):
            with mock.patch.dict(os.environ, {"Z0LIVE_UPLINK_PREFETCH": raw}):
                self.assertEqual(mobile_live.uplink_prefetch(), want, raw)

    def test_sets_only_a_stock_audio_buffer_before_receive(self):
        r = receiver()
        self.assertEqual(r._RTCRtpReceiver__jitter_buffer._prefetch, 4)
        self.assertTrue(mobile_live.set_uplink_prefetch(r, 2))
        self.assertEqual(r._RTCRtpReceiver__jitter_buffer._prefetch, 2)
        self.assertEqual(r._RTCRtpReceiver__jitter_buffer.capacity, 16)

        video = receiver("video")
        self.assertFalse(mobile_live.set_uplink_prefetch(video, 2))
        self.assertEqual(video._RTCRtpReceiver__jitter_buffer._prefetch, 0)

        started = receiver()
        started._RTCRtpReceiver__started = True
        self.assertFalse(mobile_live.set_uplink_prefetch(started, 2))
        self.assertEqual(started._RTCRtpReceiver__jitter_buffer._prefetch, 4)

    def test_adaptive_pilot_buffer_is_left_alone(self):
        r = receiver()
        jb = install(r, SimpleNamespace(time=lambda: 0.0, call_at=lambda *a: None))
        self.assertIsNotNone(jb)
        self.assertFalse(mobile_live.set_uplink_prefetch(r, 2))
        self.assertIs(r._RTCRtpReceiver__jitter_buffer, jb)


if __name__ == "__main__":
    unittest.main()
