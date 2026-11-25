"""F4o: opt-in mono Opus downlink encode. Off = aiortc's stock encoder byte for byte; on = mono packets, same framing."""
import math
import os
import unittest
from fractions import Fraction
from unittest import mock

import av
from aiortc import rtcrtpsender
from aiortc.codecs import opus as aopus
from aiortc.codecs import PCMU_CODEC
from aiortc.jitterbuffer import JitterFrame
from aiortc.rtcrtpparameters import RTCRtpCodecParameters

import mobile_live
import opus_mono

OPUS = RTCRtpCodecParameters(mimeType="audio/opus", clockRate=48000, channels=2, payloadType=96)


def frames(n=25, layout="mono"):
    """Deterministic 20 ms s16 frames: a 220 Hz + 1.3 kHz tone with a slow envelope."""
    ch = 2 if layout == "stereo" else 1
    for i in range(n):
        f = av.AudioFrame(format="s16", layout=layout, samples=960)
        pcm = bytearray()
        for k in range(960):
            t = (i * 960 + k) / 48000
            v = int(9000 * (0.6 + 0.4 * math.sin(2 * math.pi * 2 * t))
                    * (0.7 * math.sin(2 * math.pi * 220 * t) + 0.3 * math.sin(2 * math.pi * 1300 * t)))
            pcm += v.to_bytes(2, "little", signed=True) * ch
        f.planes[0].update(bytes(pcm))
        f.sample_rate, f.pts, f.time_base = 48000, i * 960, Fraction(1, 48000)
        yield f


def encode_all(enc, n=25, layout="mono"):
    out = []
    for f in frames(n, layout):
        payloads, ts = enc.encode(f)
        out.extend((p, ts) for p in payloads)
    return out


class Base(unittest.TestCase):
    def setUp(self):
        opus_mono.uninstall()
        self.addCleanup(opus_mono.uninstall)


class DefaultOffTest(Base):
    def test_flag_unset_installs_nothing(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop(opus_mono.ENV, None)
            self.assertFalse(opus_mono.install())
        self.assertIs(rtcrtpsender.get_encoder, opus_mono._STOCK_GET_ENCODER)
        self.assertIs(type(rtcrtpsender.get_encoder(OPUS)), aopus.OpusEncoder)

    def test_other_values_stay_off(self):
        for raw in ("", "0", "true", "mono", "yes"):
            with mock.patch.dict(os.environ, {opus_mono.ENV: raw}):
                self.assertFalse(opus_mono.install(), raw)
                self.assertFalse(opus_mono.enabled(), raw)

    def test_off_bytes_identical_to_stock(self):
        os.environ.pop(opus_mono.ENV, None)
        opus_mono.install()
        got = encode_all(rtcrtpsender.get_encoder(OPUS))
        want = encode_all(aopus.OpusEncoder())
        self.assertEqual(got, want)
        self.assertTrue(all(p[0] & 0x04 for p, _ in want))  # stock aiortc: stereo TOC on every packet


class MonoOnTest(Base):
    def on(self):
        with mock.patch.dict(os.environ, {opus_mono.ENV: "1"}):
            self.assertTrue(opus_mono.install())

    def test_opus_gets_mono_encoder_other_codecs_unchanged(self):
        self.on()
        self.assertIsInstance(rtcrtpsender.get_encoder(OPUS), opus_mono.MonoOpusEncoder)
        self.assertNotIsInstance(rtcrtpsender.get_encoder(PCMU_CODEC), aopus.OpusEncoder)
        self.assertIs(type(rtcrtpsender.get_encoder(PCMU_CODEC)),
                      type(opus_mono._STOCK_GET_ENCODER(PCMU_CODEC)))

    def test_install_idempotent_and_uninstall_restores(self):
        self.on()
        self.on()
        self.assertTrue(opus_mono.installed())
        opus_mono.uninstall()
        self.assertIs(rtcrtpsender.get_encoder, opus_mono._STOCK_GET_ENCODER)

    def test_same_settings_one_change(self):
        m, s = opus_mono.MonoOpusEncoder(), aopus.OpusEncoder()
        self.assertEqual(m.codec.bit_rate, s.codec.bit_rate)
        self.assertEqual(m.codec.sample_rate, s.codec.sample_rate)
        self.assertEqual(m.codec.layout.name, "mono")
        self.assertEqual(s.codec.layout.name, "stereo")
        self.assertEqual(m.codec.channels if hasattr(m.codec, "channels") else 1, 1)

    def test_mono_packets_same_timestamps(self):
        mono, stock = encode_all(opus_mono.MonoOpusEncoder()), encode_all(aopus.OpusEncoder())
        self.assertEqual([ts for _, ts in mono], [ts for _, ts in stock])
        self.assertTrue(mono)
        for p, _ in mono:
            self.assertEqual(p[0] & 0x04, 0)  # TOC stereo bit clear = mono stream
            self.assertEqual(p[0] & 0x03, 0)  # code 0: one frame per packet
            self.assertEqual(p[0] >> 3, stock[0][0][0] >> 3)  # same Opus config (mode/bandwidth/20 ms)

    def test_mono_decodes_to_same_48k_frame_length(self):
        for enc in (opus_mono.MonoOpusEncoder(), aopus.OpusEncoder()):
            dec, lens = aopus.OpusDecoder(), []
            for p, ts in encode_all(enc):
                for fr in dec.decode(JitterFrame(p, ts)):
                    lens.append((fr.samples, fr.sample_rate, fr.layout.name))
            self.assertTrue(lens)
            self.assertEqual(set(lens), {(960, 48000, "stereo")}, type(enc).__name__)

    def test_stereo_input_accepted(self):
        self.assertEqual(len(encode_all(opus_mono.MonoOpusEncoder(), layout="stereo")),
                         len(encode_all(aopus.OpusEncoder(), layout="stereo")))


class CallHookTest(Base):
    def test_call_start_installs_only_with_flag(self):
        self.assertTrue(callable(mobile_live.install_downlink_opus_mono))
        os.environ.pop(opus_mono.ENV, None)
        self.assertFalse(mobile_live.install_downlink_opus_mono())
        self.assertFalse(opus_mono.installed())
        with mock.patch.dict(os.environ, {opus_mono.ENV: "1"}):
            self.assertTrue(mobile_live.install_downlink_opus_mono())
        self.assertTrue(opus_mono.installed())


if __name__ == "__main__":
    unittest.main()
