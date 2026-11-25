"""Track F1 frozen transport benchmark: added latency per stage per 20 ms frame, and CPU, of the adapter's audio path.

Two processes on one host (perf_counter_ns is mach_absolute_time on macOS, so stamps compare across them):
  adapter (this process): the real mobile_live.Call with engine=grok, its real aiortc peer, AudioBridge, Level,
           GrokLeg.pump / reader / GrokOutput and a real aiohttp websocket. Nothing in the adapter is replaced;
           functions are wrapped at class/module level to timestamp them (behaviour unchanged).
  world    (child, --role world): the phone (aiortc peer sending synthetic speech at real-time 20 ms pacing and
           receiving the adapter's output) and a fake Grok realtime WS server (session.updated, then reply PCM24
           deltas paced in real time; records every input_audio_buffer.append).

Uplink:   RTP rx -> jitter buffer -> Opus decode (aiortc thread) -> loop hop -> AudioBridge(input)+Level
          -> GrokLeg.pump resample 48k->24k -> 100 ms batch -> base64+json -> ws.send_str
Downlink: ws msg -> GrokLeg.on_event (b64 decode) -> GrokOutput.feed (resample 24k->48k) -> reservoir
          -> GrokOutput.recv (20 ms pacing) -> AudioBridge(output)+Level -> audio level + executor Opus encode
          -> RTP packetize -> SRTP + sendto

Usage (from voice-adapter/):
  Z0LIVE_DESKTOP=$PWD/../desktop env -u PYTHONPATH .venv/bin/python -I bench/transport_bench.py \
      --seconds 30 --warmup 3 --out /path/run1.json
  --hops counts asyncio callbacks per frame (adds wrapper overhead; do not mix with the timing runs).
Stdout: a stage table (p50/p99 us) and CPU %; --out writes the full JSON. Frozen: change the harness only with a
new version string, never in the same commit as an adapter change.

transport_bench/2 (track F4a) adds, without changing any /1 key:
  --condition  a deterministic seeded network shaper on the phone's outgoing RTP (world side, after SRTP protect, so
               it acts like the network between phone and adapter): clean | jitter5 | jitter25 | loss1_jitter5 |
               freeze300_loss1. The schedule is a pure function of (--seed, condition, packet index), so every arm
               sees the same delays/losses. The first 50 packets (1 s) are never shaped. RTCP is not shaped.
  --prefetch N sets the adapter's phone-audio aiortc JitterBuffer prefetch before receive starts (stock 4).
  --adaptive   sets Z0LIVE_ADAPTIVE_UPLINK_JITTER=1 (R10 clocked buffer; requires stock prefetch 4).
  result["shaped"]: added latency per frame = provider-ws written (adapter) - unshaped phone send time (world),
               missing frames (= concealment demand: nothing in this path synthesises PLC), longest delivery gap at the
               provider ws, frames delivered/expected.
transport_bench/3: measurement identical to /2. --prefetch unset now means the adapter's own default
               (Z0LIVE_UPLINK_PREFETCH, 2 since F4a); --prefetch N pins both that knob and the receiver, so the
               stock-aiortc arm is `--prefetch 4`. config.jitter_buffer records the buffer type/prefetch actually used.
transport_bench/4 (track F4c): measurement identical to /3; the five /2 schedules are byte-identical.
  --condition reorder  3 % of packets (seeded, separate RNG stream) delayed 65 ms, no loss, no jitter.
  --drain      sets Z0LIVE_UPLINK_DRAIN=1 (R13 drain-1 buffer, audio_drain.py; replaces the stock buffer, so the
               prefetch knob does not apply). Its counters appear in shaped.adaptive_counts_window (key kept from /2).
  Worst inter-append pause at the provider = shaped.longest_provider_gap_ms (unchanged since /2).
transport_bench/5 (track F4d): measurement identical to /4; the six /4 schedules are byte-identical.
  --condition far_late      packets 500-501 +2001 ms, 502-508 +181 ms (R15 far-late, frame k = packet 500 + k).
  --condition far_late_mid  packets 700-704 +2001 ms (R15 far-late-midstream, frames 200-204).
  --drain-guard  sets Z0LIVE_UPLINK_DRAIN=guard (R15 guarded drain-1). config.jitter_buffer[].guard records it.
transport_bench/13 (track F4o; staging-based, numbered past the /6-/12 of the unmerged F4e-F4m branches): measurement
  identical to /5; the seven /5 schedules are unchanged.
  --opus-mono   sets Z0LIVE_DOWNLINK_OPUS_MONO=1 (opus_mono.py: the phone downlink sender encodes mono Opus, same
               96 kbit/s). Installed before the isolated stage, so the isolated "opus_encode (...)" and "paced20ms
               opus_encode" rows time the encoder the call uses (row keys unchanged from /1 for comparability; under
               --opus-mono the "mono->stereo" in that key no longer happens). config.opus_encoder records it.
  result["downlink_packets"]: in-window Opus payload bytes per downlink packet (n, mean, p50, max) and the encoder
               class(es) seen in the call; result["isolated_packets"]: the same for the isolated fixture encode.
  F4p (still /13: output without the new flag is unchanged): --opus-bitrate N (needs --opus-mono) sets
               Z0LIVE_DOWNLINK_OPUS_BITRATE=N; only then config.opus_bitrate (the encoder's bit_rate) is added. Row
               keys that say "96k" are kept for comparability.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import random
import resource
import shutil
import subprocess
import sys
import tempfile
import time
import wave
from collections import defaultdict
from pathlib import Path

VERSION = "transport_bench/13"
NS = time.perf_counter_ns
HERE = Path(__file__).resolve().parent
ADAPTER_DIR = HERE.parent
FRAME = 960  # 20 ms at 48 kHz
UP_TEXT = ("So I was thinking about the trip next week. We could leave early on Friday, stop for lunch near the lake, "
           "and still get there before dark. What do you think about that plan? Also remind me to call my sister.")
REPLY_TEXT = ("Sure. Leaving early on Friday works, and the lake is a good halfway point. I'll remind you to call your "
              "sister tonight. Do you want me to check the weather for the drive?")


# ----------------------------------------------------------------------------------------------- shaper
# scale = R10 SIM.md jitter model: delay = scale * (0.5 N(0,1) + 0.35 Lomax(2.5)), plus a constant 1.5*scale floor so
# the max(0, .) clip is rare (the constant is a pure offset, identical for every arm).
CONDITIONS = {
    "clean": {"scale": 0, "loss": 0.0, "freeze": False},
    "jitter5": {"scale": 5, "loss": 0.0, "freeze": False},
    "jitter25": {"scale": 25, "loss": 0.0, "freeze": False},
    "loss1_jitter5": {"scale": 5, "loss": 0.01, "freeze": False},
    "freeze300_loss1": {"scale": 0, "loss": 0.01, "freeze": True},
    # /4: 3 % of packets (seeded, own RNG stream) arrive 65 ms late = reordered ~3 places; no loss, no jitter.
    "reorder": {"scale": 0, "loss": 0.0, "freeze": False, "reorder": 0.03, "reorder_ms": 65.0},
    # /5 (F4d): R15's deterministic far-late reconstructions, no loss, no jitter. "late" = [(first, last, extra ms)]
    # packet indices; R15 frame k = packet FAR_AT + k so the events sit inside the scored window (R15 far-late is at
    # stream start, which the bench's warmup never scores). 2001 ms = ~100 packets, i.e. at or past aiortc's
    # backward-misorder reset once drain-1 has moved its origin on.
    "far_late": {"scale": 0, "loss": 0.0, "freeze": False, "late": [(500, 501, 2001.0), (502, 508, 181.0)]},
    "far_late_mid": {"scale": 0, "loss": 0.0, "freeze": False, "late": [(700, 704, 2001.0)]},
}
FAR_AT = 500  # 10 s media time
SHAPE_HEAD = 50          # first 1 s unshaped: stable origin for aiortc's jitter buffer and timestamp mapper
FREEZE_AT_MS = 15000     # media time of the freeze start (inside the scored window for --warmup 3 --seconds 30)
FREEZE_MS = 300
ISO_PACKETS = {"encoder": None, "bytes": [], "call_bit_rates": set()}  # /13: isolated encoder class, payload sizes


def shape_schedule(condition: str, seed: int, n: int) -> list:
    """Per packet index: extra one-way delay in ms, or None = lost. Deterministic in (seed, condition, index)."""
    c = CONDITIONS[condition]
    rng = random.Random(f"{seed}:{condition}")
    scale, out = c["scale"], []
    reorder = c.get("reorder", 0.0)
    rrng = random.Random(f"{seed}:{condition}:reorder")  # separate stream: /2-/3 conditions keep their schedules
    for i in range(n):
        g, lomax, u = rng.gauss(0.0, 1.0), rng.paretovariate(2.5) - 1.0, rng.random()  # common draws
        d = max(0.0, scale * (1.5 + 0.5 * g + 0.35 * lomax)) if scale else 0.0
        if reorder and rrng.random() < reorder:
            d += c["reorder_ms"]
        for lo, hi, ms in c.get("late", ()):
            if lo <= i <= hi:
                d += ms
        a = 20.0 * i + d
        if c["freeze"] and FREEZE_AT_MS <= a < FREEZE_AT_MS + FREEZE_MS:
            a = FREEZE_AT_MS + FREEZE_MS
        if i < SHAPE_HEAD:
            out.append(0.0)
        else:
            out.append(None if u < c["loss"] else a - 20.0 * i)
    return out


# ----------------------------------------------------------------------------------------------- fixtures
def fixture(path: Path, text: str, rate: int) -> bytes:
    """Mono PCM16 at `rate`: macOS `say` speech, or a 300/500 Hz tone burst pattern when `say` is missing."""
    if not path.exists():
        if shutil.which("say"):
            subprocess.run(["say", "-v", "Samantha", "-o", str(path), "--file-format=WAVE",
                            f"--data-format=LEI16@{rate}", text], check=True, timeout=120)
        else:
            import math
            import struct
            n, out = rate * 12, bytearray()
            for i in range(n):
                on = (i // (rate // 2)) % 3 != 2
                out += struct.pack("<h", int(8000 * math.sin(2 * math.pi * (300 + 200 * (i // rate % 2)) * i / rate))
                                   if on else 0)
            with wave.open(str(path), "wb") as w:
                w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate), w.writeframes(bytes(out))
    with wave.open(str(path), "rb") as w:
        assert w.getnchannels() == 1 and w.getsampwidth() == 2 and w.getframerate() == rate, path
        return w.readframes(w.getnframes())


def pct(values, q):
    if not values:
        return None
    s = sorted(values)
    k = (len(s) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (k - lo)


def stat(ns_values):
    us = [v / 1000 for v in ns_values]
    return {"n": len(us), "p50_us": round(pct(us, .5), 1) if us else None, "p99_us": round(pct(us, .99), 1) if us else None,
            "mean_us": round(sum(us) / len(us), 1) if us else None, "max_us": round(max(us), 1) if us else None}


# ----------------------------------------------------------------------------------------------- world (child)
async def world(args):
    import aiohttp
    from aiohttp import web
    from fractions import Fraction
    import av
    from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration, MediaStreamTrack
    from aiortc.codecs import opus as aopus

    fix = Path(args.fixtures)
    up = fixture(fix / "up48.wav", UP_TEXT, 48000)
    reply = fixture(fix / "reply24.wav", REPLY_TEXT, 24000)
    rec = {"capture": [], "enc": [], "rx": [], "appends": [], "deltas": [], "errors": 0, "shape": []}

    # Network shaper on the phone's outgoing RTP (RTCP untouched). Delayed packets are SRTP-protected at their
    # nominal send time and handed to ICE later, i.e. the delay sits on the wire, not in the phone.
    from aiortc import rtcdtlstransport
    from aiortc.rtp import is_rtcp
    sched = shape_schedule(args.condition, args.seed, 50 * 900)
    first_seq = []
    orig_send_rtp = rtcdtlstransport.RTCDtlsTransport._send_rtp

    async def send_rtp(self, data):
        if is_rtcp(data):
            return await orig_send_rtp(self, data)
        seq, ts, t = int.from_bytes(data[2:4], "big"), int.from_bytes(data[4:8], "big"), NS()
        if not first_seq:
            first_seq.append(seq)
        idx = (seq - first_seq[0]) % 65536
        d = sched[idx] if idx < len(sched) else 0.0
        rec["shape"].append((ts, seq, t, -1.0 if d is None else d))
        if d is None:
            return
        if d <= 0:
            return await orig_send_rtp(self, data)
        protected = self._tx_srtp.protect(data)
        loop = asyncio.get_running_loop()
        loop.call_at(loop.time() + d / 1000, lambda p=protected, s=self: asyncio.ensure_future(s.transport._send(p)))
    rtcdtlstransport.RTCDtlsTransport._send_rtp = send_rtp

    orig_encode = aopus.OpusEncoder.encode

    def encode(self, frame, force_keyframe=False):
        pts = frame.pts
        out = orig_encode(self, frame, force_keyframe)
        rec["enc"].append((pts, out[1], NS()))
        return out
    aopus.OpusEncoder.encode = encode

    class Speech(MediaStreamTrack):
        """The phone microphone: the fixture looped with 1 s of silence, 20 ms frames at real-time deadlines."""
        kind = "audio"

        def __init__(self):
            super().__init__()
            self.pcm = up + bytes(48000 * 2)
            self.pts = 0
            self.start = None

        async def recv(self):
            if self.start is None:
                self.start = NS()
            due = self.start + self.pts * 1_000_000_000 // 48000
            wait = (due - NS()) / 1e9
            if wait > 0:
                await asyncio.sleep(wait)
            off = (self.pts * 2) % len(self.pcm)
            chunk = (self.pcm[off:off + FRAME * 2] + self.pcm[:FRAME * 2])[:FRAME * 2]
            frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME)
            frame.planes[0].update(chunk)
            frame.sample_rate, frame.pts, frame.time_base = 48000, self.pts, Fraction(1, 48000)
            rec["capture"].append((self.pts, NS()))
            self.pts += FRAME
            return frame

    # Fake Grok realtime server.
    async def ws_handler(request):
        ws = web.WebSocketResponse(max_msg_size=4 * 1024 * 1024)
        await ws.prepare(request)
        streamer = None

        async def stream_reply():
            await asyncio.sleep(args.reply_delay)
            chunk = int(24000 * args.reply_chunk_ms / 1000) * 2
            pcm = reply + bytes(24000 * 2)  # reply then 1 s of silence, looped
            pos = sent = 0
            t0 = NS()
            while not ws.closed:
                part = (pcm[pos:pos + chunk] + pcm[:chunk])[:chunk]
                pos = (pos + chunk) % len(pcm)
                import base64
                msg = json.dumps({"type": "response.output_audio.delta", "response_id": "r1",
                                  "delta": base64.b64encode(part).decode()})
                rec["deltas"].append((sent, len(part), NS()))
                await ws.send_str(msg)
                sent += len(part)
                due = t0 + sent // 2 * 1_000_000_000 // 24000
                wait = (due - NS()) / 1e9
                if wait > 0:
                    await asyncio.sleep(wait)

        async for msg in ws:
            if msg.type != aiohttp.WSMsgType.TEXT:
                continue
            t = NS()
            if msg.data.startswith('{"type": "input_audio_buffer.append"'):
                ev = json.loads(msg.data)
                import base64
                rec["appends"].append((t, len(base64.b64decode(ev["audio"]))))
                if streamer is None:
                    streamer = asyncio.create_task(stream_reply())
            else:
                ev = json.loads(msg.data)
                if ev.get("type") == "session.update":
                    await ws.send_str(json.dumps({"type": "session.updated", "session": {}}))
        if streamer:
            streamer.cancel()
        return ws

    app = web.Application()
    app.router.add_get("/v1/realtime", ws_handler)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]

    phone = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    phone.addTrack(Speech())
    phone.createDataChannel("oai-events")

    async def consume(track):
        try:
            while True:
                frame = await track.recv()
                t = NS()
                import numpy as np
                pcm = np.frombuffer(frame.planes[0], dtype=np.int16)
                rec["rx"].append((frame.pts, t, int(np.abs(pcm).max()) if pcm.size else 0))
        except Exception:  # noqa: BLE001 - track ended
            pass

    @phone.on("track")
    def on_track(track):
        if track.kind == "audio":
            asyncio.ensure_future(consume(track))

    await phone.setLocalDescription(await phone.createOffer())
    out = sys.stdout
    out.write(json.dumps({"offer": phone.localDescription.sdp, "ws_port": port}) + "\n")
    out.flush()
    loop = asyncio.get_running_loop()
    reader = asyncio.StreamReader()
    await loop.connect_read_pipe(lambda: asyncio.StreamReaderProtocol(reader), sys.stdin)
    answer = json.loads(await reader.readline())
    await phone.setRemoteDescription(RTCSessionDescription(sdp=answer["answer"], type="answer"))
    await reader.readline()  # stop
    ru = resource.getrusage(resource.RUSAGE_SELF)
    rec["cpu_s"] = ru.ru_utime + ru.ru_stime
    Path(args.world_out).write_text(json.dumps(rec))
    await phone.close()
    await runner.cleanup()
    out.write(json.dumps({"done": True}) + "\n")
    out.flush()


# ----------------------------------------------------------------------------------------------- isolated
def isolated(fixtures: str, frames: int = 500) -> dict:
    """Single-thread service time of each stage on the same fixture, no loop and no GIL contention.

    Real adapter/aiortc functions (unwrapped: runs before install()). The gap between these and the in-call
    numbers is contention (GIL, thread wakeups), not compute. `numpy_*` rows are candidate rewrites, not shipped.
    """
    import base64
    from fractions import Fraction
    import av
    import numpy as np
    import aiortc.rtp as artp
    from aiortc.codecs import opus as aopus
    from aiortc.jitterbuffer import JitterFrame
    import mobile_live as live
    import grok_leg

    up = fixture(Path(fixtures) / "up48.wav", UP_TEXT, 48000)
    reply = fixture(Path(fixtures) / "reply24.wav", REPLY_TEXT, 24000)
    from aiortc import rtcrtpsender
    from aiortc.rtcrtpparameters import RTCRtpCodecParameters
    opus = RTCRtpCodecParameters(mimeType="audio/opus", clockRate=48000, channels=2, payloadType=96)
    T = defaultdict(list)
    enc, dec = rtcrtpsender.get_encoder(opus), aopus.OpusDecoder()  # /13: the encoder the call's sender would build
    ISO_PACKETS["encoder"] = type(enc).__name__
    ISO_PACKETS["bit_rate"] = enc.codec.bit_rate  # F4p: reported only under --opus-bitrate
    rs_up = av.AudioResampler(format="s16", layout="mono", rate=24000)
    stereo = av.AudioResampler(format="s16", layout="stereo", rate=48000, frame_size=960)
    out = grok_leg.GrokOutput()

    class Dummy:
        muted = False
        trace = staticmethod(lambda *a, **k: None)
    level = live.Level(Dummy(), "input")
    level.call.trace.t0 = 0
    packets, decoded = [], []
    n = min(frames, len(up) // (FRAME * 2))
    for i in range(n):
        f = av.AudioFrame(format="s16", layout="mono", samples=FRAME)
        f.planes[0].update(up[i * FRAME * 2:(i + 1) * FRAME * 2])
        f.sample_rate, f.pts, f.time_base = 48000, i * FRAME, Fraction(1, 48000)
        t0 = NS()
        payloads, ts = enc.encode(f)
        T["opus_encode (aiortc OpusEncoder: mono->stereo + libopus 96k)"].append(NS() - t0)
        t0 = NS()
        for s in stereo.resample(f):
            pass
        T["  mono->stereo resampler alone"].append(NS() - t0)
        t0 = NS()
        artp.compute_audio_level_dbov(f)
        T["compute_audio_level_dbov (pure-Python RFC 6465 loop)"].append(NS() - t0)
        t0 = NS()
        pcm = np.frombuffer(f.planes[0], dtype=np.int16, count=FRAME).astype(np.float64)
        float(np.sqrt(np.dot(pcm, pcm) / (FRAME * 32767 * 32767)))
        T["  numpy_dbov candidate"].append(NS() - t0)
        for p in payloads:
            packets.append((p, ts))
            ISO_PACKETS["bytes"].append(len(p))
    for p, ts in packets:
        t0 = NS()
        frs = dec.decode(JitterFrame(p, ts))
        T["opus_decode (aiortc OpusDecoder, stereo s16 out)"].append(NS() - t0)
        decoded.extend(frs)
    buf = bytearray()
    for fr in decoded:
        t0 = NS()
        level.frame(fr)
        T["Level.frame (peak)"].append(NS() - t0)
        t0 = NS()
        for o in rs_up.resample(fr):
            buf += bytes(o.planes[0])[: o.samples * 2]
        T["resample 48k stereo -> 24k mono + bytes copy (pump)"].append(NS() - t0)
    for i in range(0, len(buf) - 4800, 4800):
        chunk = buf[i:i + 4800]
        t0 = NS()
        json.dumps({"type": "input_audio_buffer.append", "audio": base64.b64encode(bytes(chunk)).decode()})
        T["b64+json per 100 ms append"].append(NS() - t0)
    for i in range(0, min(len(reply), n * 960) - 960, 960):
        part = reply[i:i + 960]
        msg = json.dumps({"type": "response.output_audio.delta", "delta": base64.b64encode(part).decode()})
        t0 = NS()
        ev = json.loads(msg)
        out.feed(base64.b64decode(ev["delta"]))
        T["json.loads + b64decode + GrokOutput.feed (24k->48k) per 20 ms"].append(NS() - t0)
    # Same single thread, but idle 20 ms between frames like the call: separates wake-from-idle cost (cold caches,
    # clock ramp) from GIL/thread contention.
    enc2, dec2 = rtcrtpsender.get_encoder(opus), aopus.OpusDecoder()
    for i in range(min(200, n)):
        time.sleep(0.02)
        f = av.AudioFrame(format="s16", layout="mono", samples=FRAME)
        f.planes[0].update(up[i * FRAME * 2:(i + 1) * FRAME * 2])
        f.sample_rate, f.pts, f.time_base = 48000, i * FRAME, Fraction(1, 48000)
        t0 = NS()
        payloads, ts = enc2.encode(f)
        t1 = NS()
        artp.compute_audio_level_dbov(f)
        t2 = NS()
        frs = [fr for p in payloads for fr in dec2.decode(JitterFrame(p, ts))]
        t3 = NS()
        for fr in frs:
            level.frame(fr)
        t4 = NS()
        T["paced20ms opus_encode"].append(t1 - t0)
        T["paced20ms compute_audio_level_dbov"].append(t2 - t1)
        if frs:
            T["paced20ms opus_decode"].append(t3 - t2)
            T["paced20ms Level.frame"].append(t4 - t3)
    return {k: stat(v[5:]) for k, v in T.items()}  # drop codec warmup


# ----------------------------------------------------------------------------------------------- adapter
class Probe:
    def __init__(self, hops: bool):
        self.r = defaultdict(list)
        self.hops = hops
        self.callbacks = 0
        self.pending_up = []      # uplink frame pts resampled since the last append
        self.fed = 0              # downlink bytes fed to GrokOutput (48 kHz)
        self.feeds = []           # (start_byte, end_byte, t_end)

    def add(self, key, *row):
        self.r[key].append(row)


def install(probe: Probe):
    """Class/module-level wrappers that timestamp the real code paths. Behaviour is unchanged."""
    import asyncio.events as aev
    import aiohttp
    import av
    import aiortc.rtp as artp
    from aiortc import rtcrtpreceiver, rtcdtlstransport
    from aiortc.codecs import opus as aopus
    import mobile_live as live
    import grok_leg

    add = probe.add

    orig_rx = rtcrtpreceiver.RTCRtpReceiver._handle_rtp_packet

    async def handle_rtp(self, packet, arrival_time_ms):
        add("rx", packet.timestamp, packet.sequence_number, NS())
        return await orig_rx(self, packet, arrival_time_ms)
    rtcrtpreceiver.RTCRtpReceiver._handle_rtp_packet = handle_rtp

    orig_dec = aopus.OpusDecoder.decode

    def decode(self, encoded_frame):
        t0 = NS()
        out = orig_dec(self, encoded_frame)
        add("dec", encoded_frame.timestamp, t0, NS())
        return out
    aopus.OpusDecoder.decode = decode

    orig_track = rtcrtpreceiver.RemoteStreamTrack.recv

    async def track_recv(self):
        frame = await orig_track(self)
        add("track", frame.pts, NS())
        return frame
    rtcrtpreceiver.RemoteStreamTrack.recv = track_recv

    orig_level = live.Level.frame

    def level(self, frame, speaker=None):
        t0 = NS()
        out = orig_level(self, frame, speaker)
        add("level_" + self.direction, frame.pts, t0, NS())
        return out
    live.Level.frame = level

    orig_bridge = live.AudioBridge.recv

    async def bridge(self):
        frame = await orig_bridge(self)
        add("bridge_" + self.direction, frame.pts, NS())
        return frame
    live.AudioBridge.recv = bridge

    class Resampler:
        """Proxy for av.AudioResampler inside grok_leg: same object, timed .resample()."""
        def __init__(self, *a, **k):
            self._r = av.AudioResampler(*a, **k)
            self._rate = k.get("rate")

        def resample(self, frame):
            t0 = NS()
            out = self._r.resample(frame)
            t1 = NS()
            if self._rate == grok_leg.RATE_IN:
                add("rs_up", frame.pts, t0, t1)
                probe.pending_up.append((frame.pts, t1))
            else:
                add("rs_down", t0, t1)
            return out

    class AvShim:
        def __getattr__(self, name):
            return Resampler if name == "AudioResampler" else getattr(av, name)
    grok_leg.av = AvShim()

    orig_feed = grok_leg.GrokOutput.feed

    def feed(self, pcm24):
        t0 = NS()
        before = len(self.buf)
        orig_feed(self, pcm24)
        t1 = NS()
        n = len(self.buf) - before
        probe.feeds.append((probe.fed, probe.fed + n, t1))
        probe.fed += n
        add("feed", t0, t1)
    grok_leg.GrokOutput.feed = feed

    orig_out = grok_leg.GrokOutput.recv

    async def out_recv(self):
        t_call = NS()
        frame = await orig_out(self)
        t1 = NS()
        consumed = probe.fed - len(self.buf)
        add("gout", frame.pts, t_call, t1, consumed, self.started)
        return frame
    grok_leg.GrokOutput.recv = out_recv

    orig_event = grok_leg.GrokLeg.on_event

    def on_event(self, ev):
        t0 = NS()
        orig_event(self, ev)
        if ev.get("type") == "response.output_audio.delta":
            add("event", t0, NS())
    grok_leg.GrokLeg.on_event = on_event

    orig_send = aiohttp.ClientWebSocketResponse.send_str

    async def send_str(self, data, compress=None):
        t0 = NS()
        await orig_send(self, data, compress)
        if data.startswith('{"type": "input_audio_buffer.append"'):
            frames, probe.pending_up = probe.pending_up, []
            add("append", t0, NS(), len(data), frames)
    aiohttp.ClientWebSocketResponse.send_str = send_str

    orig_lvl = artp.compute_audio_level_dbov

    def level_dbov(frame):
        t0 = NS()
        out = orig_lvl(frame)
        add("dbov", frame.pts, t0, NS())
        return out
    artp.compute_audio_level_dbov = level_dbov

    orig_enc = aopus.OpusEncoder.encode

    def encode(self, frame, force_keyframe=False):
        pts, t0 = frame.pts, NS()
        out = orig_enc(self, frame, force_keyframe)
        add("enc", pts, out[1], t0, NS())
        add("enc_bytes", t0, sum(len(b) for b in out[0]), len(out[0]), type(self).__name__)  # /13
        ISO_PACKETS["call_bit_rates"].add(self.codec.bit_rate)  # F4p: reported only under --opus-bitrate
        return out
    aopus.OpusEncoder.encode = encode

    orig_srtp = rtcdtlstransport.RTCDtlsTransport._send_rtp

    async def send_rtp(self, data):
        t0 = NS()
        await orig_srtp(self, data)
        if not artp.is_rtcp(data):
            add("rtp", int.from_bytes(data[4:8], "big"), t0, NS())
    rtcdtlstransport.RTCDtlsTransport._send_rtp = send_rtp

    if probe.hops:
        orig_run = aev.Handle._run

        def run(self):
            probe.callbacks += 1
            return orig_run(self)
        aev.Handle._run = run


def shaped(p: Probe, w: dict, t_lo: int, t_hi: int, grace_ns: int = 1_000_000_000) -> dict:
    """Per uplink frame the phone sent in [t_lo, t_hi - grace] (unshaped send time): delivered to the provider ws
    (and when), or missing. Missing = lost on the network or dropped by the receiver (late/overflow)."""
    r = p.r
    if not r["rx"] or not w.get("shape"):
        return {}
    first_raw = r["rx"][0][0]
    written = {}
    for _t0, t1, _n, frames in r["append"]:
        for pts, _ in frames:
            written.setdefault(pts, t1)
    added, missing_idx, lost_net = [], [], 0
    sent = [row for row in w["shape"] if t_lo <= row[2] <= t_hi - grace_ns]
    for k, (ts, _seq, t_nom, d) in enumerate(sent):
        pts = (ts - first_raw) % (1 << 32)
        if pts in written:
            added.append(written[pts] - t_nom)
        else:
            missing_idx.append(k)
            lost_net += d < 0
    run = best = 0
    prev = None
    for k in missing_idx:
        run = run + 1 if prev is not None and k == prev + 1 else 1
        best, prev = max(best, run), k

    def gap(ts):
        ts = sorted(ts)
        return round(max((b - a for a, b in zip(ts, ts[1:])), default=0) / 1e6, 2)
    ms = [v / 1e6 for v in added]

    def q(x):
        return round(pct(ms, x), 2) if ms else None
    return {"frames_expected": len(sent), "frames_delivered": len(added), "missing_frames": len(missing_idx),
            "missing_lost_on_network": lost_net, "missing_dropped_by_receiver": len(missing_idx) - lost_net,
            "longest_missing_run_ms": 20 * best,
            "longest_provider_gap_ms": gap([t for t, _n in w["appends"] if t_lo <= t <= t_hi]),
            "longest_ws_write_gap_ms": gap([t1 for _t0, t1, _n, _f in r["append"] if t_lo <= t1 <= t_hi]),
            "added_ms": {"p50": q(.5), "p95": q(.95), "p99": q(.99),
                         "mean": round(sum(ms) / len(ms), 2) if ms else None, "max": round(max(ms), 2) if ms else None}}


def analyse(p: Probe, w: dict, t_lo: int, t_hi: int, wall_s: float, cpu_s: float, mono_off: int):
    r = p.r
    inwin = lambda t: t_lo <= t <= t_hi  # noqa: E731
    errors = {}
    # ---------------- uplink: key = mapped RTP ts == decoded frame pts
    rx = {}
    for ts, _seq, t in r["rx"]:
        rx.setdefault(ts, t)
    first_raw = r["rx"][0][0] if r["rx"] else 0
    dec = {ts: (t0, t1) for ts, t0, t1 in r["dec"]}
    track = {pts: t for pts, t in r["track"]}
    bridge_in = {pts: t for pts, t in r["bridge_input"]}
    level_in = {pts: (t0, t1) for pts, t0, t1 in r["level_input"]}
    rs_up = {pts: (t0, t1) for pts, t0, t1 in r["rs_up"]}
    append_of = {}
    marshal, ws_send = [], []
    for t0, t1, nbytes, frames in r["append"]:
        if frames and inwin(t0):
            marshal.append(t0 - frames[-1][1])
            ws_send.append(t1 - t0)
        for pts, _ in frames:
            append_of[pts] = (t0, t1)
    U = defaultdict(list)
    for ts, t_rx in rx.items():
        pts = (ts - first_raw) % (1 << 32)
        if not inwin(t_rx):
            continue
        if pts not in dec or pts not in track or pts not in bridge_in or pts not in rs_up or pts not in append_of:
            errors["uplink_incomplete"] = errors.get("uplink_incomplete", 0) + 1
            continue
        d0, d1 = dec[pts]
        U["U1 rx -> decode start (jitter buffer + thread wake)"].append(d0 - t_rx)
        U["U2 Opus decode (decoder thread)"].append(d1 - d0)
        U["U3 decoder thread -> loop (run_coroutine_threadsafe + queue)"].append(track[pts] - d1)
        U["U4 AudioBridge.recv input (incl. Level)"].append(bridge_in[pts] - track[pts])
        if pts in level_in:
            U["U4a   Level.frame input"].append(level_in[pts][1] - level_in[pts][0])
        U["U5 bridge -> pump resample start"].append(rs_up[pts][0] - bridge_in[pts])
        U["U6 resample 48k->24k"].append(rs_up[pts][1] - rs_up[pts][0])
        U["U7 batch dwell (wait for 100 ms append)"].append(append_of[pts][0] - rs_up[pts][1])
        U["U_total_service adapter uplink excl. jitter+batch"].append(
            (d1 - d0) + (track[pts] - d1) + (bridge_in[pts] - track[pts]) + (rs_up[pts][0] - bridge_in[pts])
            + (rs_up[pts][1] - rs_up[pts][0]))
        U["U_total adapter rx -> ws written"].append(append_of[pts][1] - t_rx)
    U["U8 base64+json marshal (per append)"] = marshal
    U["U9 ws.send_str (per append)"] = ws_send
    # world side of the uplink: phone capture -> adapter rx, and -> provider append arrival
    cap = {pts: t for pts, t in w["capture"]}
    enc_w = [(pts, ts, t) for pts, ts, t in w["enc"] if ts is not None]
    if enc_w:
        first_ts = enc_w[0][1]
        ts_to_cap = {ts - first_ts: cap.get(pts) for pts, ts, _ in enc_w}
        for ts, t_rx in rx.items():
            pts = (ts - first_raw) % (1 << 32)
            c = ts_to_cap.get(pts)
            if c and inwin(t_rx):
                U["W1 phone capture -> adapter rx (phone encode + UDP)"].append(t_rx - c)
        # provider append arrival: append k contains frames in adapter order
        arrivals = [t for t, _ in w["appends"]]
        if len(arrivals) == len(r["append"]):
            for (t0, _t1, _n, frames), t_arr in zip(r["append"], arrivals):
                for pts, _ in frames:
                    c = ts_to_cap.get(pts)
                    if c and inwin(t0):
                        U["W_e2e phone capture -> provider has frame"].append(t_arr - c)
        else:
            errors["append_count_mismatch"] = (len(r["append"]), len(arrivals))
    # ---------------- downlink: key = GrokOutput frame pts
    D = defaultdict(list)
    D["D1 on_event delta (json done -> b64 decode + feed)"] = [t1 - t0 for t0, t1 in r["event"] if inwin(t0)]
    D["D1a resample 24k->48k (per 20 ms delta)"] = [t1 - t0 for t0, t1 in r["rs_down"] if inwin(t0)]
    bridge_out = {pts: t for pts, t in r["bridge_output"]}
    level_out = {pts: (t0, t1) for pts, t0, t1 in r["level_output"]}
    dbov = {pts: (t0, t1) for pts, t0, t1 in r["dbov"]}
    enc = {pts: (ets, t0, t1) for pts, ets, t0, t1 in r["enc"]}
    enc_by_ts = {ets: pts for pts, ets, t0, t1 in r["enc"] if ets is not None}
    first_enc = next((ets for _p, ets, _a, _b in r["enc"] if ets is not None), None)
    rtp = {}
    first_rtp = r["rtp"][0][0] if r["rtp"] else None
    for raw, t0, t1 in r["rtp"]:
        ets = (raw - first_rtp + first_enc) % (1 << 32)
        rtp.setdefault(enc_by_ts.get(ets), (t0, t1))
    feeds = p.feeds
    fi = 0
    prev_consumed = 0
    voiced_frames = 0
    for pts, t_call, t1, consumed, started in r["gout"]:
        start_byte, prev_consumed = prev_consumed, consumed
        if not inwin(t1):
            continue
        if pts not in bridge_out or pts not in enc or pts not in rtp:
            errors["downlink_incomplete"] = errors.get("downlink_incomplete", 0) + 1
            continue
        D["D3 GrokOutput.recv return - deadline (pacing lateness)"].append(
            t1 - (int(started * 1e9) + mono_off + pts * 1_000_000_000 // 48000))
        if consumed > start_byte:  # the frame carries provider audio: its first byte's feed time
            while fi + 1 < len(feeds) and feeds[fi][1] <= start_byte:
                fi += 1
            if feeds and feeds[fi][0] <= start_byte < feeds[fi][1]:
                D["D2 reservoir dwell (feed end -> frame out)"].append(t1 - feeds[fi][2])
                voiced_frames += 1
        D["D4 AudioBridge.recv output (incl. Level)"].append(bridge_out[pts] - t1)
        if pts in level_out:
            D["D4a   Level.frame output"].append(level_out[pts][1] - level_out[pts][0])
        _ets, e0, e1 = enc[pts]
        if pts in dbov:
            D["D5a   compute_audio_level_dbov"].append(dbov[pts][1] - dbov[pts][0])
        D["D5 bridge -> encode start (audio level + executor hop)"].append(e0 - bridge_out[pts])
        D["D6 Opus encode (executor thread, incl. mono->stereo)"].append(e1 - e0)
        s0, s1 = rtp[pts]
        D["D7 encode end -> _send_rtp (loop hop + packetize)"].append(s0 - e1)
        D["D8 SRTP protect + sendto"].append(s1 - s0)
        D["D_total adapter frame out -> RTP sent (excl. reservoir)"].append(s1 - t1)
    # world: adapter RTP sent -> phone frame decoded
    rx_w = w["rx"]
    if rx_w and first_enc is not None:
        for pts_p, t, _peak in rx_w:
            pts = enc_by_ts.get((pts_p + first_enc) % (1 << 32))
            if pts in rtp and inwin(rtp[pts][1]):
                D["W2 adapter RTP sent -> phone decoded frame (UDP + phone jitter/decode)"].append(t - rtp[pts][1])
    out = {"uplink": {k: stat(v) for k, v in U.items()}, "downlink": {k: stat(v) for k, v in D.items()}}
    up_frames = sum(1 for _ts, t in rx.items() if inwin(t))
    down_frames = sum(1 for row in r["gout"] if inwin(row[2]))
    appended = sum(n for _t0, _t1, n, f in r["append"] if inwin(_t0))
    out["work"] = {"uplink_frames": up_frames, "downlink_frames": down_frames, "voiced_downlink_frames": voiced_frames,
                   "appends": len(marshal), "append_json_bytes": appended,
                   "provider_appends_received": len(w["appends"]),
                   "provider_pcm24_bytes_received": sum(n for _t, n in w["appends"]),
                   "phone_frames_received": len(rx_w),
                   "phone_voiced_frames": sum(1 for _p, _t, pk in rx_w if pk >= 800),
                   "errors": errors}
    out["cpu"] = {"adapter_cpu_s": round(cpu_s, 3), "wall_s": round(wall_s, 3),
                  "adapter_cpu_pct_one_core": round(100 * cpu_s / wall_s, 2),
                  "adapter_cpu_us_per_20ms_tick": round(cpu_s / wall_s * 20000, 1)}
    if p.hops:
        out["asyncio_callbacks_per_20ms_tick"] = round(p.callbacks / (wall_s / 0.02), 2)
    return out


async def adapter(args):
    os.environ.update({"HERMES_VOICE_ENGINE": "grok", "HERMES_MOBILE_VOICE_LANES": "0", "HERMES_MOBILE_VOICE_CHAT": "0",
                       "VOICE_WARM_HAIKU": "0", "HERMES_VOICE_HANDOFF": "off",
                       "HERMES_MOBILE_VOICE_TRACE_DIR": str(Path(args.workdir) / "traces")})
    os.environ.setdefault("Z0LIVE_DESKTOP", str(ADAPTER_DIR.parent / "desktop"))
    sys.path.insert(0, str(ADAPTER_DIR))
    if args.adaptive:
        assert args.prefetch in (None, 4), "--adaptive replaces the stock prefetch-4 buffer; do not combine with --prefetch"
        args.prefetch = 4
        os.environ["Z0LIVE_ADAPTIVE_UPLINK_JITTER"] = "1"
    else:
        os.environ.pop("Z0LIVE_ADAPTIVE_UPLINK_JITTER", None)
    if args.opus_mono:  # /13: before isolated() so both stages time the encoder the call will use
        os.environ["Z0LIVE_DOWNLINK_OPUS_MONO"] = "1"
    else:
        os.environ.pop("Z0LIVE_DOWNLINK_OPUS_MONO", None)
    if args.opus_bitrate is not None:  # F4p: mono encoder bitrate (opus_mono.BITRATE_ENV); unset = 96000 as in /13
        assert args.opus_mono, "--opus-bitrate only applies with --opus-mono"
        os.environ["Z0LIVE_DOWNLINK_OPUS_BITRATE"] = str(args.opus_bitrate)
    else:
        os.environ.pop("Z0LIVE_DOWNLINK_OPUS_BITRATE", None)
    if args.drain or args.drain_guard:
        assert not args.adaptive and args.prefetch is None, "--drain replaces the stock buffer; use it alone"
        assert not (args.drain and args.drain_guard), "--drain and --drain-guard are separate arms"
        os.environ["Z0LIVE_UPLINK_DRAIN"] = "guard" if args.drain_guard else "1"
    else:
        os.environ.pop("Z0LIVE_UPLINK_DRAIN", None)
    import mobile_live as live
    import grok_leg
    import opus_mono
    from aiohttp import ClientSession

    assert live.install_downlink_opus_mono() == bool(args.opus_mono), "opus_mono install state != --opus-mono"
    from aiortc import rtcrtpreceiver

    if args.prefetch is not None:  # pin both the adapter knob and the receiver's JitterBuffer before receive starts
        os.environ["Z0LIVE_UPLINK_PREFETCH"] = str(args.prefetch)
        orig_init = rtcrtpreceiver.RTCRtpReceiver.__init__

        def rx_init(self, kind, transport):
            orig_init(self, kind, transport)
            if kind == "audio":
                self._RTCRtpReceiver__jitter_buffer._prefetch = args.prefetch
        rtcrtpreceiver.RTCRtpReceiver.__init__ = rx_init

    if args.qos != "default":  # diagnostic knob (F3 lead): threads created later inherit the main thread's QoS
        import ctypes
        qos = {"interactive": 0x21, "initiated": 0x19, "utility": 0x11}[args.qos]
        rc = ctypes.CDLL("/usr/lib/libSystem.B.dylib").pthread_set_qos_class_self_np(qos, 0)
        assert rc == 0, rc
    iso = isolated(args.fixtures)
    probe = Probe(args.hops)
    install(probe)
    world_out = Path(args.workdir) / "world.json"
    child = await asyncio.create_subprocess_exec(
        sys.executable, "-I", str(Path(__file__).resolve()), "--role", "world", "--fixtures", args.fixtures,
        "--world-out", str(world_out), "--reply-chunk-ms", str(args.reply_chunk_ms),
        "--reply-delay", str(args.reply_delay), "--condition", args.condition, "--seed", str(args.seed),
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE)
    hello = json.loads(await asyncio.wait_for(child.stdout.readline(), 60))
    grok_leg.URL = f"ws://127.0.0.1:{hello['ws_port']}/v1/realtime"
    grok_leg.mint_secret = lambda: "bench"
    async with ClientSession() as http:
        manager = live.Manager(http)
        call = live.Call(manager)
        manager.calls[call.id] = call
        assert call.leg is not None, "engine must be grok"
        result = await call.start(hello["offer"], [])
        child.stdin.write((json.dumps({"answer": result["transport"]["sdp"]}) + "\n").encode())
        await child.stdin.drain()
        async with asyncio.timeout(30):
            while not (call.mobile.connectionState == "connected" and call.leg.ready and probe.r["append"]):
                await asyncio.sleep(0.05)
        await asyncio.sleep(args.warmup + args.reply_delay)
        jbs = [t.receiver._RTCRtpReceiver__jitter_buffer for t in call.mobile.getTransceivers() if t.kind == "audio"]
        jb_info = [{"type": type(j).__name__, "prefetch": j._prefetch, "capacity": j.capacity,
                    "guard": bool(getattr(j, "guard", False))} for j in jbs]
        pilot = list(getattr(call, "uplink_jitter", []))
        c0 = [dict(j.counts) for j in pilot]
        mono_off = NS() - int(time.monotonic() * 1e9)
        ru0, t_lo = resource.getrusage(resource.RUSAGE_SELF), NS()
        await asyncio.sleep(args.seconds)
        ru1, t_hi = resource.getrusage(resource.RUSAGE_SELF), NS()
        c1 = [dict(j.counts) for j in pilot]
        cpu = (ru1.ru_utime + ru1.ru_stime) - (ru0.ru_utime + ru0.ru_stime)
        await asyncio.sleep(0.5)  # let in-flight frames of the window land
        child.stdin.write(b'{"stop": true}\n')
        await child.stdin.drain()
        await asyncio.wait_for(child.stdout.readline(), 30)
        await call.close("bench_done")
    await asyncio.wait_for(child.wait(), 30)
    w = json.loads(world_out.read_text())
    res = analyse(probe, w, t_lo, t_hi, (t_hi - t_lo) / 1e9, cpu, mono_off)
    res["isolated"] = iso
    res["version"] = VERSION
    eb = [(n, k, cls) for t0, n, k, cls in probe.r["enc_bytes"] if t_lo <= t0 <= t_hi and k]
    sizes = [n / k for n, k, _c in eb]
    res["downlink_packets"] = {"n": len(sizes), "bytes_mean": round(sum(sizes) / len(sizes), 2) if sizes else None,
                               "bytes_p50": pct(sizes, .5), "bytes_max": max(sizes) if sizes else None,
                               "encoders": sorted({c for _n, _k, c in eb})}
    ib = ISO_PACKETS["bytes"]
    res["isolated_packets"] = {"encoder": ISO_PACKETS["encoder"], "n": len(ib),
                               "bytes_mean": round(sum(ib) / len(ib), 2) if ib else None, "bytes_p50": pct(ib, .5)}
    res["config"] = {"seconds": args.seconds, "warmup": args.warmup, "reply_chunk_ms": args.reply_chunk_ms,
                     "hops": args.hops, "qos": args.qos, "python": sys.version.split()[0], "engine": "grok", "frame_ms": 20,
                     "condition": args.condition, "seed": args.seed, "shaper": CONDITIONS[args.condition],
                     "prefetch": args.prefetch, "adaptive": args.adaptive, "drain": args.drain, "jitter_buffer": jb_info,
                     "opus_mono": bool(args.opus_mono), "opus_mono_installed": opus_mono.installed(),
                     "opus_encoder": ISO_PACKETS["encoder"]}
    if args.opus_bitrate is not None:  # F4p: key only present when the flag is given (default output unchanged)
        res["config"]["opus_bitrate"] = ISO_PACKETS["bit_rate"]
        res["config"]["opus_call_bit_rates"] = sorted(ISO_PACKETS["call_bit_rates"])
    sh = shaped(probe, w, t_lo, t_hi)
    if sh:
        t_first = w["shape"][0][2]
        sh["freeze_in_window"] = bool(CONDITIONS[args.condition]["freeze"]
                                      and t_lo <= t_first + FREEZE_AT_MS * 1_000_000 <= t_hi - 1_000_000_000)
        sh["cpu_pct_one_core"] = res["cpu"]["adapter_cpu_pct_one_core"]
        sh["adaptive_counts_window"] = [{k: b.get(k, 0) - a.get(k, 0) for k in b} for a, b in zip(c0, c1)]
    res["shaped"] = sh
    return res


def report(res):
    print(f"{res['version']}  window {res['cpu']['wall_s']} s  adapter CPU {res['cpu']['adapter_cpu_pct_one_core']} % "
          f"of one core ({res['cpu']['adapter_cpu_us_per_20ms_tick']} us per 20 ms tick)")
    for side in ("uplink", "downlink", "isolated"):
        print(f"\n{side:<70} {'n':>6} {'p50_us':>10} {'p99_us':>10}")
        for k, v in res[side].items():
            print(f"{k:<70} {v['n']:>6} {v['p50_us'] if v['p50_us'] is not None else '-':>10} "
                  f"{v['p99_us'] if v['p99_us'] is not None else '-':>10}")
    print("\nwork:", json.dumps(res["work"]))
    if res.get("downlink_packets"):
        print("downlink_packets:", json.dumps(res["downlink_packets"]), " isolated_packets:",
              json.dumps(res["isolated_packets"]))
    if res.get("shaped"):
        print("shaped:", json.dumps(res["shaped"]))
    if "asyncio_callbacks_per_20ms_tick" in res:
        print("asyncio callbacks per 20 ms tick:", res["asyncio_callbacks_per_20ms_tick"])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--role", default="adapter", choices=["adapter", "world"])
    ap.add_argument("--seconds", type=float, default=30)
    ap.add_argument("--warmup", type=float, default=3)
    ap.add_argument("--reply-chunk-ms", type=int, default=20)
    ap.add_argument("--reply-delay", type=float, default=1.0)
    ap.add_argument("--hops", action="store_true")
    ap.add_argument("--qos", default="default", choices=["default", "interactive", "initiated", "utility"],
                    help="diagnostic only: macOS QoS class of the adapter process threads (baseline = default)")
    ap.add_argument("--condition", default="clean", choices=sorted(CONDITIONS))
    ap.add_argument("--seed", type=int, default=4100)
    ap.add_argument("--prefetch", type=int, default=None,
                    help="pin the adapter phone-audio aiortc JitterBuffer prefetch (aiortc stock = 4); unset = adapter "
                         "default (Z0LIVE_UPLINK_PREFETCH, 2 since F4a). The used value is in config.jitter_buffer.")
    ap.add_argument("--adaptive", action="store_true", help="Z0LIVE_ADAPTIVE_UPLINK_JITTER=1 (R10 clocked buffer)")
    ap.add_argument("--drain", action="store_true", help="Z0LIVE_UPLINK_DRAIN=1 (R13 drain-1 buffer, prefetch 1)")
    ap.add_argument("--drain-guard", action="store_true",
                    help="Z0LIVE_UPLINK_DRAIN=guard (R15: drain-1 that ignores packets behind its origin)")
    ap.add_argument("--opus-mono", action="store_true",
                    help="Z0LIVE_DOWNLINK_OPUS_MONO=1 (F4o: mono Opus downlink encode, same bitrate)")
    ap.add_argument("--opus-bitrate", type=int, default=None,
                    help="with --opus-mono: Z0LIVE_DOWNLINK_OPUS_BITRATE (F4p; unset = 96000). Recorded as "
                         "config.opus_bitrate (the isolated encoder's libopus bit_rate), only when given.")
    ap.add_argument("--fixtures", default=str(Path(os.environ.get("TMPDIR", tempfile.gettempdir())) / "transport_bench_fx"))
    ap.add_argument("--workdir", default="")
    ap.add_argument("--world-out", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()
    Path(args.fixtures).mkdir(parents=True, exist_ok=True)
    if args.role == "world":
        asyncio.run(world(args))
        return
    with tempfile.TemporaryDirectory(prefix="tbench") as tmp:
        args.workdir = args.workdir or tmp
        res = asyncio.run(adapter(args))
    report(res)
    if args.out:
        Path(args.out).write_text(json.dumps(res, indent=1))


if __name__ == "__main__":
    main()
