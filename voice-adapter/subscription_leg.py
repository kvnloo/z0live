"""Replaceable GPT-Live transport. The phone keeps its own peer and RTP clock."""
import asyncio
from fractions import Fraction
import json
import time

from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection
from aiortc.mediastreams import MediaStreamError
import av


class Microphone(MediaStreamTrack):
    kind = "audio"

    def __init__(self, source, leg):
        super().__init__()
        self.source, self.leg = source, leg

    async def recv(self):
        frame = await self.source.recv()
        if self.leg.active:
            return frame
        silence = av.AudioFrame(format=frame.format.name, layout=frame.layout.name, samples=frame.samples)
        silence.pts, silence.time_base, silence.sample_rate = frame.pts, frame.time_base, frame.sample_rate
        for plane in silence.planes:
            plane.update(bytes(plane.buffer_size))
        return silence

    def stop(self):
        self.source.stop()
        super().stop()


class Speaker(MediaStreamTrack):
    kind = "audio"

    def __init__(self, source):
        super().__init__()
        self.source = source
        self.next_at = Fraction(0)

    async def recv(self):
        while True:
            source = self.source
            try:
                frame = await source.recv()
            except MediaStreamError:
                if source is self.source:
                    raise
                continue
            if source is self.source:
                break
        # The new provider starts its own RTP timestamps at zero.
        frame.time_base = Fraction(1, frame.sample_rate)
        frame.pts = int(self.next_at * frame.sample_rate)
        self.next_at += Fraction(frame.samples, frame.sample_rate)
        return frame


class SubscriptionLeg:
    def __init__(self, call, active=False):
        self.active = active
        self.created = time.monotonic()
        self.expires = None
        self.peer = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.channel = self.peer.createDataChannel("oai-events")
        self.ready = asyncio.Event()
        self.input = self.output = self.drain = None

        @self.peer.on("track")
        def track_received(track):
            if track.kind != "audio":
                return
            self.output = track
            if self.active:
                call.attach_primary_output(track)
            else:
                self.drain = asyncio.create_task(self.drain_candidate(track))

        @self.channel.on("message")
        def received(raw):
            try:
                event = json.loads(raw)
            except (TypeError, ValueError):
                return
            if not isinstance(event, dict):
                return
            if event.get("type") == "session.started":
                session = event.get("session")
                expiry = session.get("expires_at") if isinstance(session, dict) else None
                if isinstance(expiry, (int, float)) and expiry > time.time():
                    self.expires = time.monotonic() + expiry - time.time()
                self.ready.set()
            if self.active:
                call.message(raw, True)

        @self.peer.on("connectionstatechange")
        async def changed():
            call.trace("peer.state", peer="provider", state=self.peer.connectionState)
            if self.active and self.peer.connectionState in ("failed", "closed"):
                call.spawn(call.close("transport_closed"))

    def attach(self, source):
        self.input = Microphone(source, self)
        self.peer.addTrack(self.input)

    @staticmethod
    async def drain_candidate(track):
        try:
            while True:
                await track.recv()
        except MediaStreamError:
            pass

    def activate(self):
        if self.drain:
            self.drain.cancel()
        self.active = True

    async def close(self):
        self.active = False
        if self.drain:
            self.drain.cancel()
        if self.input:
            self.input.stop()
        if self.channel.readyState == "open":
            self.channel.send(json.dumps({"type": "session.close"}))
        await self.peer.close()

    def deadline(self, cap):
        return min(self.created + cap, self.expires or float("inf"))
