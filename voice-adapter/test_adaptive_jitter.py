import asyncio
import unittest
from types import SimpleNamespace

from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCRtpReceiver
from aiortc.rtp import RtpPacket
from aiortc.rtcrtpparameters import RTCRtpCodecParameters

from adaptive_jitter import AdaptiveAudioJitterBuffer, Playout, _is_20ms, install


class Clock:
    ms = 0

    def time(self):
        return self.ms / 1000

    def call_at(self, t, f):
        self.pending = SimpleNamespace(t=t, f=f, cancel=lambda: None)
        return self.pending

    def tick(self):
        self.ms = self.pending.t * 1000
        self.pending.f()


def packet(i, ssrc=7, opus=b"\x08\x00", extra_ts=0):
    pkt = RtpPacket(payload_type=111, sequence_number=(65535 + i) & 65535,
                    timestamp=(0xffffff00 + 960 * i + extra_ts) & 0xffffffff,
                    ssrc=ssrc, payload=opus)
    pkt._data = opus
    return pkt


class AdaptiveJitterTest(unittest.TestCase):
    def test_clocked_release_order_and_loss_on_pinned_receiver(self):
        r = RTCRtpReceiver("audio", SimpleNamespace(state="new"))
        clock = Clock()
        jb = install(r, clock)
        self.assertIsInstance(jb, AdaptiveAudioJitterBuffer)
        self.assertIs(r._RTCRtpReceiver__jitter_buffer, jb)
        codec = RTCRtpCodecParameters(mimeType="audio/opus", clockRate=48000, payloadType=111)
        r._RTCRtpReceiver__codecs[111] = codec
        r._RTCRtpReceiver__decoder_thread = object()  # test queue seam, not PyAV
        q = r._RTCRtpReceiver__decoder_queue
        for i in (0, 2, 1, 1, 4):
            # Reorder + duplicate across both counter wraps, with packet 3 lost.
            self.assertEqual(jb.add(packet(i)), (False, None))
            clock.ms += 10
        for i in (0, 1, 2):
            clock.tick()
            queued_codec, frame = q.get_nowait()
            self.assertIs(queued_codec, codec)
            self.assertEqual(frame.timestamp, 960 * i)
        clock.tick()
        self.assertTrue(q.empty())
        self.assertEqual(jb.counts["skip"], 1)
        clock.tick()
        self.assertEqual(q.get_nowait()[1].timestamp, 3840)
        jb.add(packet(3))
        self.assertEqual(jb.counts["late"], 1)
        stale_callback = clock.pending.f
        jb.close()
        stale_callback()
        self.assertIsNone(jb.handle)

    def test_warm_shrink_and_hold_recover_late_slot(self):
        p = Playout()
        for i in range(100):
            at = 20 * i
            while p.due is not None and p.due < at:
                self.assertEqual(p.tick()[0], "emit")
            p.put(i, at, i)
        self.assertEqual(p.due - 20 * p.i, 20)
        while p.i < 100:
            self.assertEqual(p.tick()[0], "emit")
        old_due = p.due
        self.assertEqual(p.tick(), ("hold", 100, None))
        p.put(100, old_due + 5, 100)
        self.assertEqual(p.tick()[0], "emit")
        self.assertEqual((p.holds, p.skips), (1, 0))
        self.assertGreater(p.due - 20 * p.i, 20)

    def test_no_double_hold_and_dtx_falls_back(self):
        p = Playout()
        p.put(0, 0, 0)
        p.due = 20
        p.tick()
        self.assertEqual(p.tick()[0], "hold")
        self.assertEqual(p.tick()[0], "skip")
        self.assertEqual(p.i, 2)

        clock = Clock()
        jb = AdaptiveAudioJitterBuffer(clock, lambda pt: True, self.fail, lambda: True)
        jb.add(packet(0))
        stale_callback = clock.pending.f
        jb.add(packet(1, extra_ts=960))  # sequence consecutive, timestamp not
        self.assertTrue(jb.fallback)
        self.assertIsNone(jb.handle)
        stale_callback()
        self.assertTrue(_is_20ms(b"\x08"))
        self.assertFalse(_is_20ms(b"\x00"))
        self.assertFalse(_is_20ms(b"\x03"))

    def test_video_keeps_stock(self):
        r = RTCRtpReceiver("video", SimpleNamespace(state="new"))
        self.assertIsNone(install(r, Clock()))


class AdaptiveLoopbackTest(unittest.IsolatedAsyncioTestCase):
    async def test_decodes_phone_audio_through_unchanged_receive_path(self):
        config = RTCConfiguration(iceServers=[])
        sender, answerer = RTCPeerConnection(config), RTCPeerConnection(config)
        sender.addTrack(AudioStreamTrack())
        jb = None
        try:
            await sender.setLocalDescription(await sender.createOffer())
            await answerer.setRemoteDescription(sender.localDescription)
            t = answerer.getTransceivers()[0]
            jb = install(t.receiver)
            self.assertIsNotNone(jb)
            await answerer.setLocalDescription(await answerer.createAnswer())
            await sender.setRemoteDescription(answerer.localDescription)
            frames = [await asyncio.wait_for(t.receiver.track.recv(), 3) for _ in range(8)]
            pts = [f.pts for f in frames]
            self.assertTrue(all(b - a == 960 for a, b in zip(pts, pts[1:])), pts)
            self.assertGreaterEqual(jb.counts["emit"], 8)
            self.assertFalse(jb.fallback)
        finally:
            if jb is not None:
                jb.close()
            await asyncio.gather(sender.close(), answerer.close())


if __name__ == "__main__":
    unittest.main()
