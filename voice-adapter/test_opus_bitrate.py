"""F4p: opt-in bitrate for the mono Opus downlink encoder (Z0LIVE_DOWNLINK_OPUS_BITRATE, read only with MONO=1).

Unset (or invalid) = 96 kbit/s, byte for byte the F4o MonoOpusEncoder output. Set = that libopus bitrate.
"""
import os
import unittest
from unittest import mock

from av import AudioResampler, CodecContext
from aiortc import rtcrtpsender
from aiortc.codecs import opus as aopus

import opus_mono
from test_opus_mono import OPUS, encode_all


class F4oMono(aopus.OpusEncoder):
    """Verbatim replica of the F4o MonoOpusEncoder constructor (hard-coded 96 kbit/s) = the reference output."""

    def __init__(self):
        self.codec = CodecContext.create("libopus", "w")
        self.codec.bit_rate = 96000
        self.codec.format = "s16"
        self.codec.layout = "mono"
        self.codec.options = {"application": "voip"}
        self.codec.sample_rate = aopus.SAMPLE_RATE
        self.codec.time_base = aopus.TIME_BASE
        self.resampler = AudioResampler(format="s16", layout="mono", rate=aopus.SAMPLE_RATE,
                                        frame_size=aopus.SAMPLES_PER_FRAME)
        self.first_packet_pts = None


def env(**kv):
    """Patch os.environ with only the given F4o/F4p keys set (both removed otherwise)."""
    p = mock.patch.dict(os.environ, {})
    p.start()
    for k in (opus_mono.ENV, opus_mono.BITRATE_ENV):
        os.environ.pop(k, None)
    os.environ.update(kv)
    return p


class Base(unittest.TestCase):
    def setUp(self):
        opus_mono.uninstall()
        self.addCleanup(opus_mono.uninstall)

    def under(self, **kv):
        self.addCleanup(env(**kv).stop)


class DefaultUnchangedTest(Base):
    def test_env_name_and_default(self):
        self.assertEqual(opus_mono.BITRATE_ENV, "Z0LIVE_DOWNLINK_OPUS_BITRATE")
        self.assertEqual(opus_mono.DEFAULT_BITRATE, 96000)

    def test_unset_is_96k_and_byte_identical_to_f4o_mono(self):
        self.under(**{opus_mono.ENV: "1"})
        self.assertTrue(opus_mono.install())
        enc = rtcrtpsender.get_encoder(OPUS)
        self.assertIsInstance(enc, opus_mono.MonoOpusEncoder)
        self.assertEqual(enc.codec.bit_rate, 96000)
        got, want = encode_all(enc, n=50), encode_all(F4oMono(), n=50)
        self.assertTrue(want)
        self.assertEqual(got, want)

    def test_invalid_values_fall_back_to_96k(self):
        for raw in ("", " ", "abc", "32k", "0", "-32000", "5999", "510001", "1e5", "32000.5"):
            self.under(**{opus_mono.ENV: "1", opus_mono.BITRATE_ENV: raw})
            self.assertEqual(opus_mono.bitrate(), 96000, raw)
            self.assertEqual(opus_mono.MonoOpusEncoder().codec.bit_rate, 96000, raw)

    def test_explicit_96000_identical_to_default(self):
        self.under(**{opus_mono.ENV: "1", opus_mono.BITRATE_ENV: "96000"})
        self.assertEqual(encode_all(opus_mono.MonoOpusEncoder(), n=50), encode_all(F4oMono(), n=50))

    def test_bitrate_ignored_without_mono_flag(self):
        self.under(**{opus_mono.BITRATE_ENV: "32000"})
        self.assertFalse(opus_mono.install())
        enc = rtcrtpsender.get_encoder(OPUS)
        self.assertIs(type(enc), aopus.OpusEncoder)
        self.assertEqual(enc.codec.bit_rate, 96000)
        self.assertEqual(encode_all(enc), encode_all(aopus.OpusEncoder()))


class KnobSetTest(Base):
    def test_bitrate_applied_to_encoder(self):
        for raw, want in (("32000", 32000), (" 24000 ", 24000), ("6000", 6000), ("510000", 510000)):
            self.under(**{opus_mono.ENV: "1", opus_mono.BITRATE_ENV: raw})
            self.assertEqual(opus_mono.bitrate(), want, raw)
            self.assertTrue(opus_mono.install())
            self.assertEqual(rtcrtpsender.get_encoder(OPUS).codec.bit_rate, want, raw)

    def test_32k_packets_smaller_mono_same_timestamps(self):
        self.under(**{opus_mono.ENV: "1", opus_mono.BITRATE_ENV: "32000"})
        low, ref = encode_all(opus_mono.MonoOpusEncoder(), n=50), encode_all(F4oMono(), n=50)
        self.assertEqual([ts for _, ts in low], [ts for _, ts in ref])
        self.assertNotEqual(low, ref)
        self.assertLess(sum(len(p) for p, _ in low), 0.6 * sum(len(p) for p, _ in ref))
        for p, _ in low:
            self.assertEqual(p[0] & 0x04, 0)  # still mono
            self.assertEqual(p[0] & 0x03, 0)  # one 20 ms frame per packet

    def test_read_at_construction(self):
        self.under(**{opus_mono.ENV: "1", opus_mono.BITRATE_ENV: "32000"})
        a = opus_mono.MonoOpusEncoder()
        os.environ[opus_mono.BITRATE_ENV] = "48000"
        self.assertEqual((a.codec.bit_rate, opus_mono.MonoOpusEncoder().codec.bit_rate), (32000, 48000))
        self.assertEqual(opus_mono.MonoOpusEncoder(bit_rate=20000).codec.bit_rate, 20000)


if __name__ == "__main__":
    unittest.main()
