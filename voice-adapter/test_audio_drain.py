import os
import unittest
from types import SimpleNamespace
from unittest import mock

from aiortc import RTCRtpReceiver
from aiortc.jitterbuffer import JitterBuffer
from aiortc.rtcrtpparameters import RTCRtpCodecParameters
from aiortc.rtp import RtpPacket

import mobile_live
from audio_drain import AudioDrainJitterBuffer, install


def packet(i, payload_type=96):
    p = RtpPacket(payload_type=payload_type, sequence_number=i & 65535,
                  timestamp=(i * 960) & 0xffffffff)
    p._data = i.to_bytes(4, "big")
    return p


def ids(frames):
    return [int.from_bytes(frame.data, "big") for frame in frames]


def receiver(kind="audio"):
    return RTCRtpReceiver(kind, SimpleNamespace(state="new"))


class AudioDrainBufferTest(unittest.TestCase):
    """Ported from research lane R13 (evidence/R13/test_audio_drain.py)."""

    def setUp(self):
        self.buf = AudioDrainJitterBuffer()

    def feed(self, seqs):
        return [i for seq in seqs for i in ids(self.buf.add_many(packet(seq)))]

    def test_shape(self):
        self.assertEqual((self.buf.capacity, self.buf._prefetch), (16, 1))

    def test_hole_then_whole_contiguous_run_released(self):
        self.assertEqual(self.feed(range(8)), list(range(7)))
        self.assertEqual(self.feed(range(9, 23)), [])
        self.assertEqual(self.feed([23]), list(range(9, 23)))
        self.assertEqual(self.feed([24, 25]), [23, 24])
        self.assertEqual(self.feed([8]), [])  # too late once stock evicts it

    def test_reorder_recovers_without_skipping(self):
        self.assertEqual(self.feed([0, 1, 2, 4, 5]), [0, 1])
        self.assertEqual(self.feed([3]), [2, 3, 4])
        self.assertEqual(self.feed([6]), [5])

    def test_sequence_wrap_65535(self):
        self.assertEqual(self.feed([65533, 65534, 65536]), [65533])
        self.assertEqual(self.feed([65535, 65537, 65538]), list(range(65534, 65538)))

    def test_freeze_burst_then_normal_flow(self):
        initial = self.feed(range(20))
        burst = self.feed(range(20, 35))  # all arrive together on thaw
        self.assertEqual(initial + burst, list(range(34)))
        self.assertEqual(self.feed([35]), [34])

    def test_duplicates(self):
        self.assertEqual(self.feed([0, 1, 1, 2, 0, 3, 3, 4]), [0, 1, 2, 3])

    def test_payload_type_follows_frame_not_trigger(self):
        self.assertEqual(self.feed([0, 1, 2, 4, 5]), [0, 1])
        frames = self.buf.add_many(packet(3, 97))
        self.assertEqual(ids(frames), [2, 3, 4])
        self.assertEqual([f.payload_type for f in frames], [96, 97, 96])

    def test_never_drops_what_stock_delivers_single_hole(self):
        trace = [i for i in range(300) if i != 8]
        stock = JitterBuffer(16, 4)
        expected = {i for n in trace for i in ids(f for f in [stock.add(packet(n))[1]] if f is not None)}
        self.assertLessEqual(expected, set(self.feed(trace)))


class AudioDrainReceiverTest(unittest.TestCase):
    def setUp(self):
        self.r = receiver()
        self.jb = install(self.r)
        self.assertIsInstance(self.jb, AudioDrainJitterBuffer)
        self.assertIs(self.r._RTCRtpReceiver__jitter_buffer, self.jb)
        self.c96 = RTCRtpCodecParameters(mimeType="audio/opus", clockRate=48000, payloadType=96)
        self.c97 = RTCRtpCodecParameters(mimeType="audio/opus", clockRate=48000, payloadType=97)
        self.r._RTCRtpReceiver__codecs.update({96: self.c96, 97: self.c97})
        self.q = self.r._RTCRtpReceiver__decoder_queue

    def drain_queue(self):
        out = []
        while not self.q.empty():
            out.append(self.q.get_nowait())
        return out

    def test_add_enqueues_every_frame_with_its_codec_and_mapped_timestamp(self):
        self.r._RTCRtpReceiver__decoder_thread = object()  # queue seam, not PyAV
        for i in (0, 1, 2, 4, 5):
            self.assertEqual(self.jb.add(packet(1000 + i)), (False, None))
        self.assertEqual(self.jb.add(packet(1003, 97)), (False, None))
        got = self.drain_queue()
        self.assertEqual(ids(f for _c, f in got), [1000, 1001, 1002, 1003, 1004])
        self.assertEqual([c for c, _f in got], [self.c96, self.c96, self.c96, self.c97, self.c96])
        # TimestampMapper maps the first frame to 0 and keeps 960-sample steps.
        self.assertEqual([f.timestamp for _c, f in got], [960 * k for k in range(5)])
        self.assertEqual(self.jb.counts["frames"], 5)
        self.assertEqual(self.jb.counts["multi_release"], 1)
        self.assertEqual(self.jb.counts["max_release"], 3)

    def test_no_decoder_thread_nothing_queued(self):
        for i in range(5):
            self.jb.add(packet(i))
        self.assertTrue(self.q.empty())

    def test_install_only_on_unstarted_stock_audio_receiver(self):
        self.assertIsNone(install(self.r))  # already drained
        video = receiver("video")
        self.assertIsNone(install(video))
        self.assertIs(type(video._RTCRtpReceiver__jitter_buffer), JitterBuffer)
        started = receiver()
        started._RTCRtpReceiver__started = True
        self.assertIsNone(install(started))
        self.assertIs(type(started._RTCRtpReceiver__jitter_buffer), JitterBuffer)


class DrainFlagTest(unittest.TestCase):
    def test_default_off_and_only_1_enables(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("Z0LIVE_UPLINK_DRAIN", None)
            self.assertFalse(mobile_live.uplink_drain())
        for raw, want in (("1", True), (" 1 ", True), ("0", False), ("", False), ("true", False)):
            with mock.patch.dict(os.environ, {"Z0LIVE_UPLINK_DRAIN": raw}):
                self.assertEqual(mobile_live.uplink_drain(), want, raw)

    def test_flag_off_leaves_stock_receiver_path(self):
        r = receiver()
        with mock.patch.dict(os.environ, {"Z0LIVE_UPLINK_PREFETCH": "4"}):
            os.environ.pop("Z0LIVE_UPLINK_DRAIN", None)
            self.assertEqual(mobile_live.install_uplink_drain([SimpleNamespace(kind="audio", receiver=r)]), [])
        jb = r._RTCRtpReceiver__jitter_buffer
        self.assertIs(type(jb), JitterBuffer)
        self.assertEqual((jb.capacity, jb._prefetch), (16, 4))
        self.assertEqual(jb.add(packet(0)), (False, None))  # stock add() contract untouched

    def test_flag_on_installs_and_wins_over_prefetch(self):
        r = receiver()
        with mock.patch.dict(os.environ, {"Z0LIVE_UPLINK_DRAIN": "1", "Z0LIVE_UPLINK_PREFETCH": "2"}):
            got = mobile_live.install_uplink_drain([SimpleNamespace(kind="audio", receiver=r),
                                                    SimpleNamespace(kind="video", receiver=receiver("video"))])
        self.assertEqual(len(got), 1)
        self.assertIsInstance(r._RTCRtpReceiver__jitter_buffer, AudioDrainJitterBuffer)
        self.assertFalse(mobile_live.set_uplink_prefetch(r, 2))  # the prefetch knob leaves the drain buffer alone
        self.assertEqual(r._RTCRtpReceiver__jitter_buffer._prefetch, 1)


if __name__ == "__main__":
    unittest.main()
