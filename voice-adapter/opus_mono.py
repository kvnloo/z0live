"""Opt-in mono Opus encode for the phone downlink (track F4o). Default off: nothing is patched.

aiortc's OpusEncoder always encodes stereo: every 20 ms frame (our downlink is mono s16 48 kHz) goes through a
mono->stereo resampler and a 2-channel libopus encoder at 96 kbit/s. With Z0LIVE_DOWNLINK_OPUS_MONO=1 the sender
gets MonoOpusEncoder instead: the same libopus encoder settings (96 kbit/s, application=voip, 48 kHz, 20 ms frames)
with one channel and a mono resampler. Only the encoder changes; the bitrate does not (unless F4p's knob below).

SDP is unchanged (opus/48000/2 stays: RFC 7587 requires 2 channels in the rtpmap whatever is sent). Each Opus packet
carries its channel count in the TOC byte (bit 0x04 = stereo), so every Opus decoder decodes a mono packet; a stereo
decoder (aiortc's, browsers, Android) outputs the mono signal on both channels. aiortc's offer/answer carry no
stereo/sprop-stereo fmtp, i.e. the RFC 7587 defaults stereo=0 / sprop-stereo=0 (mono preferred, mono likely sent).

Mechanism: aiortc's RTCRtpSender builds its encoder lazily with the module-level name aiortc.rtcrtpsender.get_encoder
(on the first frame after negotiation). install() wraps that name so audio/opus returns MonoOpusEncoder; every other
codec is untouched. The patch is process-wide and idempotent; the env flag is read when install() runs.

Bitrate (track F4p): Z0LIVE_DOWNLINK_OPUS_BITRATE=<bits/s> sets the mono encoder's libopus bitrate. It only matters
when MONO=1 (it is read in MonoOpusEncoder.__init__, i.e. when the sender builds the encoder). Unset or invalid (not
an integer in [6000, 510000], the libopus range) = 96000, byte for byte the F4o encoder. The stock stereo encoder is
never affected.
"""
from __future__ import annotations

import os

from av import AudioResampler, CodecContext

from aiortc import rtcrtpsender
from aiortc.codecs import opus as aopus

ENV = "Z0LIVE_DOWNLINK_OPUS_MONO"
BITRATE_ENV = "Z0LIVE_DOWNLINK_OPUS_BITRATE"
DEFAULT_BITRATE = 96000
_STOCK_GET_ENCODER = rtcrtpsender.get_encoder


def enabled() -> bool:
    return os.environ.get(ENV, "").strip() == "1"


def bitrate() -> int:
    """Mono encoder bitrate from Z0LIVE_DOWNLINK_OPUS_BITRATE; unset/invalid/out of range = DEFAULT_BITRATE."""
    raw = os.environ.get(BITRATE_ENV, "").strip()
    if not (raw.isascii() and raw.isdigit()):
        return DEFAULT_BITRATE
    value = int(raw)
    return value if 6000 <= value <= 510000 else DEFAULT_BITRATE


class MonoOpusEncoder(aopus.OpusEncoder):
    """aiortc OpusEncoder with one channel. encode()/pack() are inherited unchanged (same pts/timestamp logic)."""

    def __init__(self, bit_rate: int | None = None) -> None:  # noqa: D107 - OpusEncoder.__init__, layout mono
        self.codec = CodecContext.create("libopus", "w")
        self.codec.bit_rate = bitrate() if bit_rate is None else bit_rate
        self.codec.format = "s16"
        self.codec.layout = "mono"
        self.codec.options = {"application": "voip"}
        self.codec.sample_rate = aopus.SAMPLE_RATE
        self.codec.time_base = aopus.TIME_BASE
        self.resampler = AudioResampler(format="s16", layout="mono", rate=aopus.SAMPLE_RATE,
                                        frame_size=aopus.SAMPLES_PER_FRAME)
        self.first_packet_pts = None


def _get_encoder(codec):
    if codec.mimeType.lower() == "audio/opus":
        return MonoOpusEncoder()
    return _STOCK_GET_ENCODER(codec)


def installed() -> bool:
    return rtcrtpsender.get_encoder is _get_encoder


def install() -> bool:
    """Route aiortc's sender Opus encoder to MonoOpusEncoder when the flag is on. Returns whether it is installed."""
    if enabled():
        rtcrtpsender.get_encoder = _get_encoder
    return installed()


def uninstall() -> None:
    rtcrtpsender.get_encoder = _STOCK_GET_ENCODER
