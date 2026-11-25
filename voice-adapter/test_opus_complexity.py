"""F4q: opt-in libopus complexity for the mono Opus downlink encoder (Z0LIVE_DOWNLINK_OPUS_COMPLEXITY, MONO=1 only).

Unset (or invalid / out of [0, 10]) = the option is not set at all, byte for byte the F4o MonoOpusEncoder output.
Set = FFmpeg libopusenc `compression_level` (OPUS_SET_COMPLEXITY; FFmpeg default 10).
"""
import os
import unittest
from unittest import mock

from aiortc import rtcrtpsender
from aiortc.codecs import opus as aopus

import opus_mono
from test_opus_bitrate import F4oMono
from test_opus_mono import OPUS, encode_all

KEYS = ("Z0LIVE_DOWNLINK_OPUS_MONO", "Z0LIVE_DOWNLINK_OPUS_BITRATE", "Z0LIVE_DOWNLINK_OPUS_COMPLEXITY")


def env(**kv):
    """Patch os.environ with only the given F4o/F4p/F4q keys set (all removed otherwise)."""
    p = mock.patch.dict(os.environ, {})
    p.start()
    for k in KEYS:
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
    def test_env_name(self):
        self.assertEqual(opus_mono.COMPLEXITY_ENV, "Z0LIVE_DOWNLINK_OPUS_COMPLEXITY")

    def test_unset_sets_no_option_and_is_byte_identical(self):
        self.under(**{opus_mono.ENV: "1"})
        self.assertIsNone(opus_mono.complexity())
        self.assertTrue(opus_mono.install())
        enc = rtcrtpsender.get_encoder(OPUS)
        self.assertIsInstance(enc, opus_mono.MonoOpusEncoder)
        self.assertEqual(dict(enc.codec.options), {"application": "voip"})
        self.assertIsNone(enc.complexity)
        got, want = encode_all(enc, n=50), encode_all(F4oMono(), n=50)
        self.assertTrue(want)
        self.assertEqual(got, want)

    def test_invalid_values_set_no_option(self):
        for raw in ("", " ", "abc", "5.0", "-1", "11", "1e1", "05x", "+5"):
            self.under(**{opus_mono.ENV: "1", opus_mono.COMPLEXITY_ENV: raw})
            self.assertIsNone(opus_mono.complexity(), raw)
            enc = opus_mono.MonoOpusEncoder()
            self.assertEqual(dict(enc.codec.options), {"application": "voip"}, raw)
            self.assertEqual(encode_all(enc, n=20), encode_all(F4oMono(), n=20), raw)

    def test_ignored_without_mono_flag(self):
        self.under(**{opus_mono.COMPLEXITY_ENV: "0"})
        self.assertFalse(opus_mono.install())
        enc = rtcrtpsender.get_encoder(OPUS)
        self.assertIs(type(enc), aopus.OpusEncoder)
        self.assertEqual(encode_all(enc), encode_all(aopus.OpusEncoder()))


class KnobSetTest(Base):
    def test_valid_values_parsed(self):
        for raw, want in (("0", 0), ("5", 5), (" 10 ", 10), ("7", 7)):
            self.under(**{opus_mono.ENV: "1", opus_mono.COMPLEXITY_ENV: raw})
            self.assertEqual(opus_mono.complexity(), want, raw)
            self.assertTrue(opus_mono.install())
            enc = rtcrtpsender.get_encoder(OPUS)
            self.assertEqual(enc.codec.options.get("compression_level"), str(want), raw)
            self.assertEqual(enc.codec.options.get("application"), "voip", raw)
            self.assertEqual(enc.complexity, want, raw)
            encode_all(enc, n=2)
            self.assertEqual(enc.complexity, want, raw)  # survives codec open (options dict is consumed)

    def test_complexity_0_changes_packets_still_mono(self):
        self.under(**{opus_mono.ENV: "1", opus_mono.COMPLEXITY_ENV: "0"})
        low, ref = encode_all(opus_mono.MonoOpusEncoder(), n=50), encode_all(F4oMono(), n=50)
        self.assertEqual([ts for _, ts in low], [ts for _, ts in ref])
        self.assertNotEqual(low, ref)
        for p, _ in low:
            self.assertEqual(p[0] & 0x04, 0)  # still mono
            self.assertEqual(p[0] & 0x03, 0)  # one 20 ms frame per packet

    def test_complexity_10_equals_default(self):
        self.under(**{opus_mono.ENV: "1", opus_mono.COMPLEXITY_ENV: "10"})
        self.assertEqual(encode_all(opus_mono.MonoOpusEncoder(), n=50), encode_all(F4oMono(), n=50))

    def test_read_at_construction_and_explicit_arg(self):
        self.under(**{opus_mono.ENV: "1", opus_mono.COMPLEXITY_ENV: "5"})
        a = opus_mono.MonoOpusEncoder()
        os.environ[opus_mono.COMPLEXITY_ENV] = "3"
        b = opus_mono.MonoOpusEncoder()
        self.assertEqual((a.codec.options.get("compression_level"), b.codec.options.get("compression_level")),
                         ("5", "3"))
        self.assertEqual(opus_mono.MonoOpusEncoder(complexity=0).codec.options.get("compression_level"), "0")

    def test_composes_with_bitrate(self):
        self.under(**{opus_mono.ENV: "1", opus_mono.COMPLEXITY_ENV: "5", opus_mono.BITRATE_ENV: "48000"})
        enc = opus_mono.MonoOpusEncoder()
        self.assertEqual((enc.codec.bit_rate, enc.codec.options.get("compression_level")), (48000, "5"))


if __name__ == "__main__":
    unittest.main()
