"""Opt-in phone-uplink "drain-1" audio jitter buffer (research lane R13); aiortc 1.14.0 only, default off.

Stock aiortc audio JitterBuffer(capacity 16, prefetch 4) releases at most one frame per arriving packet. After a hole
(loss or reorder) it stalls until a packet 16 sequence numbers past the hole arrives, then stays ~15 frames deep for
the rest of the call (F4a: ~280-290 ms added under any loss/reorder, whatever the prefetch). Drain-1 keeps stock's
hole/eviction rules (it calls the stock add) with prefetch 1, then keeps releasing frames until no complete frame is
left, so the ring catches up to one frame of depth as soon as a contiguous run is available.

aiortc's RTCRtpReceiver enqueues only the single frame add() returns, so install() replaces the receiver's audio
buffer with one whose add() enqueues every released frame itself onto the receiver's own decoder queue (same queue,
same TimestampMapper, codec looked up from each frame's own payload type) and returns (False, None). The installed
aiortc package is not modified.
"""

from collections import Counter
from typing import Optional

import aiortc
from aiortc.jitterbuffer import JitterBuffer, JitterFrame


class AudioJitterFrame(JitterFrame):
    def __init__(self, frame: JitterFrame, payload_type: int) -> None:
        super().__init__(frame.data, frame.timestamp)
        self.payload_type = payload_type


class AudioDrainJitterBuffer(JitterBuffer):
    """Stock hole handling, prefetch 1, then drain every complete frame per arrival."""

    def __init__(self, emit=None):
        super().__init__(capacity=16, prefetch=1)
        self.emit = emit
        self.closed = False
        self.counts = Counter()

    def _remove_frame(self, sequence_number: int) -> Optional[AudioJitterFrame]:
        first = self._packets[self._origin % self._capacity] if self._origin is not None else None
        frame = super()._remove_frame(sequence_number)
        if frame is None:
            return None
        return AudioJitterFrame(frame, first.payload_type)

    def add_many(self, packet) -> list:
        _, frame = JitterBuffer.add(self, packet)
        if frame is None:
            return []
        frames = [frame]
        while (nxt := self._remove_frame(packet.sequence_number)) is not None:
            frames.append(nxt)
        return frames

    def add(self, packet):
        if self.closed:
            return False, None
        frames = self.add_many(packet)
        if frames:
            self.counts["frames"] += len(frames)
            if len(frames) > 1:
                self.counts["multi_release"] += 1
                self.counts["max_release"] = max(self.counts["max_release"], len(frames))
            if self.emit is not None:
                for frame in frames:
                    self.emit(frame)
        return False, None  # every frame was already enqueued; the stock receiver path enqueues nothing

    def close(self):
        self.closed = True


def install(receiver):
    """Replace the stock audio JitterBuffer of one not-yet-started aiortc 1.14.0 receiver; else return None."""
    if aiortc.__version__ != "1.14.0":
        return None
    prefix = "_RTCRtpReceiver__"
    fields = ("jitter_buffer", "kind", "started", "codecs", "decoder_thread", "decoder_queue", "timestamp_mapper")
    if not all(hasattr(receiver, prefix + field) for field in fields):
        return None

    def get(field):
        return getattr(receiver, prefix + field)

    jb = get("jitter_buffer")
    if get("kind") != "audio" or get("started") or type(jb) is not JitterBuffer or jb.capacity != 16:
        return None

    def emit(frame):
        # Mirrors the stock receiver tail: decode only while the decoder thread runs, off the RTP event loop.
        if not get("decoder_thread"):
            return
        codec = get("codecs").get(frame.payload_type)
        if codec is None:
            return
        frame.timestamp = get("timestamp_mapper").map(frame.timestamp)
        get("decoder_queue").put((codec, frame))

    jb = AudioDrainJitterBuffer(emit)
    setattr(receiver, prefix + "jitter_buffer", jb)
    return jb
