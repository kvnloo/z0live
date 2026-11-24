"""Opt-in, phone-WebSocket uplink RTP pacer; aiortc 1.14.0 pilot only."""

import asyncio
from collections import Counter, deque
from math import ceil

import aiortc
from aiortc.jitterbuffer import JitterBuffer, JitterFrame

FRAME = 20
RTP_FRAME = 960


# BEGIN SHARED POLICY (copied verbatim into patch.diff)
class Playout:
    """Indexed, one 20 ms frame per RTP packet; caller owns timers and PCM."""

    def __init__(self, fixed=None):
        self.fixed = fixed
        self.i = None
        self.due = None
        self.last = None
        self.buf = {}
        self.samples = deque(maxlen=128)
        self.seen = set()
        self.floor = None
        self.n = 0
        self.goal = 80 if fixed is None else FRAME * fixed
        self.pressure = float("-inf")
        self.held = False
        self.holds = self.skips = self.late = self.starved = 0

    def put(self, i, now, value):
        if self.due is None:
            self.i, self.last = i, now
            self.floor = now - FRAME * i
            self.due = now + self.goal
        # Bound memory and don't let old duplicates pollute the window.
        if i in self.seen or not self.i - 128 <= i < self.i + 64:
            return
        self.last = now
        if len(self.samples) == self.samples.maxlen:
            self.seen.remove(self.samples[0][0])
        self.samples.append((i, now - FRAME * i))
        self.seen.add(i)
        self.n += 1
        if self.fixed is None and (self.n == 1 or self.n % 8 == 0):
            offsets = sorted(offset for _, offset in self.samples)
            self.floor = offsets[0]
            if self.n >= 32:
                q = offsets[ceil(0.98 * len(offsets)) - 1] - self.floor
                self.goal = max(20, min(80, FRAME * ceil((q + 8) / FRAME)))
        if i < self.i:
            self.late += 1
        else:
            self.buf[i] = (now, value)

    def tick(self):
        """Consume at self.due; return (kind, slot, arrival/value or None)."""
        i, now = self.i, self.due
        item = self.buf.get(i)
        if item is not None and item[0] > now:
            item = None
        phase = now - FRAME * i
        if item is None:
            self.starved += 1
            if self.fixed is None:
                self.pressure = now
                if not self.held and phase + FRAME <= self.floor + 80:
                    self.held = True
                    self.holds += 1
                    self.due += FRAME
                    return "hold", i, None
            self.skips += 1
        expired = self.buf.pop(i, None)
        if item is None and expired is not None:
            self.late += 1
        self.held = False
        self.i += 1
        self.due += FRAME
        if item is not None and self.fixed is None:
            depth = 80 if now - self.pressure < 1000 else self.goal
            delta = self.floor + depth - phase
            # Accelerate enqueue cadence (15 ms floor), NOT PCM samples.
            self.due += max(-5, min(5, delta))
        return ("skip" if item is None else "emit"), i, item
# END SHARED POLICY


def _is_20ms(data):
    """Check Opus TOC duration, not bitstream validity (decoder's job)."""
    if not data or (data[0] & 3 == 3 and len(data) < 2):
        return False
    toc = data[0]
    config = toc >> 3
    if config >= 16:
        samples = 120 << (config & 3)
    elif config >= 12:
        samples = 480 << (config & 1)
    else:
        samples = (480, 960, 1920, 2880)[config & 3]
    code = toc & 3
    frames = (data[1] & 63) if code == 3 else (1 if code == 0 else 2)
    return frames * samples == RTP_FRAME


class AdaptiveAudioJitterBuffer(JitterBuffer):
    """Take complete 20 ms Opus packets from add; emit on an asyncio clock."""

    def __init__(self, loop, opus_payload, emit, active):
        super().__init__(capacity=16, prefetch=4)
        self.loop, self.opus_payload, self.emit, self.active = loop, opus_payload, emit, active
        self.policy = Playout()
        self.handle = None
        self.base = None
        self.ref_ts = self.ref_i = 0
        self.closed = self.fallback = False
        self.counts = Counter()

    def _cancel(self):
        if self.handle is not None:
            self.handle.cancel()
            self.handle = None

    def _reset(self):
        self._cancel()
        self.policy = Playout()
        self.base = None

    def close(self):
        self.closed = True
        self._reset()

    def _stock(self, packet):
        self.fallback = True
        self.counts["stock_fallback"] += 1
        self._reset()
        return super().add(packet)

    def add(self, packet):
        if self.closed:
            return False, None
        if self.fallback:
            return super().add(packet)
        if not self.opus_payload(packet.payload_type) or not _is_20ms(packet._data):
            return self._stock(packet)
        if self.base is None:
            self.base = (packet.ssrc, packet.payload_type, packet.sequence_number)
            self.ref_ts, self.ref_i = packet.timestamp, 0
        ssrc, payload_type, seq = self.base
        # Signed modulo delta relative to most recent RTP timestamp; tolerate wrap.
        delta = ((packet.timestamp - self.ref_ts + (1 << 31)) & 0xffffffff) - (1 << 31)
        offset, remainder = divmod(delta, RTP_FRAME)
        i = self.ref_i + offset
        if (ssrc != packet.ssrc or payload_type != packet.payload_type or remainder
                or ((seq + i) & 0xffff) != packet.sequence_number):
            # Includes DTX timestamp jumps without matching sequence-number jumps.
            return self._stock(packet)
        if self.policy.i is not None and i >= self.policy.i + 64:
            return self._stock(packet)
        if i > self.ref_i:
            self.ref_i, self.ref_ts = i, packet.timestamp

        now = self.loop.time() * 1000
        before = self.policy.late
        self.policy.put(i, now, packet)
        self.counts["late"] += self.policy.late - before
        if self.handle is None:
            self._arm()
        return False, None

    def _arm(self):
        self.handle = self.loop.call_at(self.policy.due / 1000, self._tick)

    def _tick(self):
        self.handle = None
        now = self.loop.time() * 1000
        if self.closed or self.fallback:
            return
        if not self.active() or now - self.policy.last > 1000:
            self.counts["clock_resets"] += 1
            # Keep the RTP shape/sequence anchor so DTX after idle still falls back.
            self.policy = Playout()
            return
        self.counts["timer_over_5ms"] += now - self.policy.due > 5
        before = self.policy.late
        kind, _, item = self.policy.tick()
        self.counts[kind] += 1
        self.counts["late"] += self.policy.late - before
        if kind == "emit":
            self.emit(item[1])
        self._arm()


def install(receiver, loop=None):
    """Replace only the receiver allocated for this audio transceiver, before receive()."""
    if aiortc.__version__ != "1.14.0":
        return None
    prefix = "_RTCRtpReceiver__"
    fields = ("jitter_buffer", "kind", "started", "codecs", "decoder_thread",
              "decoder_queue", "timestamp_mapper")
    if not all(hasattr(receiver, prefix + field) for field in fields):
        return None

    def get(field):
        return getattr(receiver, prefix + field)

    jb = get("jitter_buffer")
    if (get("kind") != "audio" or get("started") or type(jb) is not JitterBuffer
            or jb.capacity != 16 or jb._prefetch != 4):
        return None

    def opus_payload(pt):
        codec = get("codecs").get(pt)
        return codec is not None and codec.mimeType.lower() == "audio/opus" and codec.clockRate == 48000

    def active():
        return receiver._enabled and get("decoder_thread") is not None

    def emit(packet):
        # Same queue/mapper as stock; do not decode on the RTP event loop.
        frame = JitterFrame(packet._data, get("timestamp_mapper").map(packet.timestamp))
        get("decoder_queue").put((get("codecs")[packet.payload_type], frame))

    jb = AdaptiveAudioJitterBuffer(loop or asyncio.get_running_loop(), opus_payload, emit, active)
    setattr(receiver, prefix + "jitter_buffer", jb)
    return jb
