"""Second (and third) caller on one z0live call, behind Z0LIVE_MULTI_CALLER=on (docs/MULTI-CALLER.md).

The host joins as today (dashboard bearer, POST /api/audio/voice-live/session). With the flag on the host may
mint a short-lived, single-use invite for its open call. A second device on the tailnet redeems it with its own
SDP offer at /api/audio/voice-live/join and gets its own WebRTC peer to the adapter:

* uplink: every caller's microphone is mixed into the one audio stream the voice provider hears (`mix`), so the
  agent hears both people; who spoke is attributed per input turn (`Attribution`) for the trace.
* downlink: the agent's reply is fanned out to every caller (`MediaRelay` over the host's output bridge).
* people to people: callers never hear each other through the adapter. They open direct browser-to-browser
  WebRTC links (DTLS-SRTP, end to end between the two devices) and use the adapter only as a signalling
  mailbox (`Mailbox`). The adapter never holds those media keys or that audio.

Pure pieces (mix, Attribution, Invites, Mailbox) carry no aiortc state and are unit tested in
test_multi_caller.py. Room is the aiortc glue for one call.
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass, field
import hashlib
import hmac
import logging
import os
import secrets
import time

import numpy as np

LOG = logging.getLogger("hermes.mobile.multi_caller")

MAX_CALLERS = 3          # host + 2: an SFU-less mesh stays small (each browser holds n-1 people links)
INVITE_SECONDS = 600     # an invite admits one device within 10 minutes, then it is spent
QUEUE_FRAMES = 6         # 120 ms of a guest's audio may wait for the host's frame clock; older frames drop
MAILBOX_MAX = 64         # signalling messages kept per call
SDP_MAX = 131072
LOUD = 800               # s16 peak that counts as sound (same threshold as mobile_live.LEVEL_ON)
HOST = "host"
PERSONA = """You are the guest-profile assistant in a shared voice call. Help the people in the room.
Your only background agent is the guest profile. Use information from this room and the guest profile."""


def enabled() -> bool:
    return os.environ.get("Z0LIVE_MULTI_CALLER", "off").strip().lower() in ("1", "on", "true", "yes")


# --- pure pieces -------------------------------------------------------------------------------------------

def mix(primary: np.ndarray, others: list[np.ndarray]) -> tuple[np.ndarray, int]:
    """Sum int16 PCM with saturation. Frames whose shape differs from the primary are skipped, not resampled.

    Returns (mixed, skipped)."""
    if not others:
        return primary, 0
    acc = primary.astype(np.int32)
    skipped = 0
    for pcm in others:
        if pcm.shape != primary.shape:
            skipped += 1
            continue
        acc += pcm
    np.clip(acc, -32768, 32767, out=acc)
    return acc.astype(np.int16), skipped


def peak(pcm: np.ndarray) -> int:
    return max(int(pcm.max()), -int(pcm.min())) if pcm.size else 0


@dataclass
class Attribution:
    """Who talked during one input turn: loud-frame counts per caller, and frames where two or more overlapped.

    The mix gives the provider one stream, so its transcript cannot say who spoke. This keeps the per-caller
    evidence beside it: `close_turn` names the caller with the most loud frames (ties go to the earliest
    speaker) and reports overlap, so a turn where both people talked at once is visible in the trace."""
    loud: dict[str, int] = field(default_factory=dict)
    first: dict[str, int] = field(default_factory=dict)
    overlap: int = 0
    frames: int = 0

    def frame(self, peaks: dict[str, int]) -> None:
        self.frames += 1
        talking = [c for c, p in peaks.items() if p >= LOUD]
        for c in talking:
            self.loud[c] = self.loud.get(c, 0) + 1
            self.first.setdefault(c, self.frames)
        if len(talking) > 1:
            self.overlap += 1

    def close_turn(self) -> dict:
        speakers = sorted(self.loud, key=lambda c: (-self.loud[c], self.first[c]))
        out = {"speaker": speakers[0] if speakers else None, "speakers": speakers,
               "loud_frames": dict(self.loud), "overlap_frames": self.overlap, "frames": self.frames}
        self.loud, self.first, self.overlap, self.frames = {}, {}, 0, 0
        return out


class Invites:
    """Single-use, expiring admission capabilities. Only a SHA-256 of each secret is kept."""

    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.live: dict[str, tuple[str, float]] = {}  # digest -> (call id, expires at)

    @staticmethod
    def digest(secret: str) -> str:
        return hashlib.sha256(secret.encode()).hexdigest()

    def issue(self, call_id: str, seconds: float = INVITE_SECONDS) -> str:
        self.prune()
        secret = secrets.token_urlsafe(32)
        self.live[self.digest(secret)] = (call_id, self.clock() + seconds)
        return secret

    def redeem(self, secret) -> str | None:
        if not isinstance(secret, str) or not 20 <= len(secret) <= 128:
            return None
        self.prune()
        want = self.digest(secret)
        for key in list(self.live):
            if hmac.compare_digest(key, want):
                call_id, _ = self.live.pop(key)
                return call_id
        return None

    def revoke_call(self, call_id: str) -> None:
        for key, (cid, _) in list(self.live.items()):
            if cid == call_id:
                self.live.pop(key, None)

    def prune(self) -> None:
        now = self.clock()
        for key, (_, until) in list(self.live.items()):
            if until <= now:
                self.live.pop(key, None)


class Mailbox:
    """Signalling relay for the people-to-people links. It carries SDP offers and answers between named
    members of one call; the DTLS fingerprints inside them are what the browsers then verify, so this relay
    must be trusted not to swap them (the design note says how to close that gap)."""

    KINDS = ("offer", "answer", "bye")

    def __init__(self, maxlen: int = MAILBOX_MAX):
        self.items: deque[dict] = deque(maxlen=maxlen)
        self.seq = 0

    def post(self, sender: str, members, to, kind, sdp=None) -> dict:
        if to not in members or to == sender:
            raise ValueError("unknown recipient")
        if kind not in self.KINDS:
            raise ValueError("unknown kind")
        if kind != "bye" and (not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp) > SDP_MAX):
            raise ValueError("a valid SDP is required")
        self.seq += 1
        item = {"seq": self.seq, "from": sender, "to": to, "kind": kind, "sdp": sdp if kind != "bye" else None}
        self.items.append(item)
        return {"seq": self.seq}

    def read(self, member: str, after: int = 0) -> list[dict]:
        return [m for m in self.items if m["to"] == member and m["seq"] > after]


INVITES = Invites()


# --- aiortc glue ---------------------------------------------------------------------------------------------

def _pcm(frame) -> np.ndarray:
    channels = len(frame.layout.channels) if frame.format.is_packed else 1
    return np.frombuffer(frame.planes[0], dtype=np.int16, count=frame.samples * channels)


def _write(frame, pcm: np.ndarray) -> None:
    """Put samples back into a frame. Decoded WebRTC frames carry a padded plane (23040 bytes for 3840 bytes of
    20 ms stereo s16), and plane.update() takes exactly buffer_size bytes, so pad the tail with zeros."""
    plane = frame.planes[0]
    raw = pcm.tobytes()
    plane.update(raw + bytes(plane.buffer_size - len(raw)))


class Caller:
    def __init__(self, cid: str, name: str, member: str, remote: str, peer=None):
        self.id, self.name, self.member, self.remote, self.peer = cid, name, member, remote, peer
        self.queue: deque = deque(maxlen=QUEUE_FRAMES)
        self.task: asyncio.Task | None = None
        self.on = False


def _track_class():
    from aiortc import MediaStreamTrack

    class MixedMic(MediaStreamTrack):
        """The host's microphone track with every guest's latest frame mixed in, on the host's frame clock."""
        kind = "audio"

        def __init__(self, source, room):
            super().__init__()
            self.source, self.room = source, room

        async def recv(self):
            frame = await self.source.recv()
            return self.room.mix_frame(frame)

        def stop(self):
            self.source.stop()
            super().stop()

    class Tap(MediaStreamTrack):
        """One caller's copy of the agent's reply; counts frames and loud frames per caller for the trace."""
        kind = "audio"

        def __init__(self, source, room, cid):
            super().__init__()
            self.source, self.room, self.cid = source, room, cid

        async def recv(self):
            frame = await self.source.recv()
            counts = self.room.call.counts
            counts["caller_out_frames:" + self.cid] += 1
            try:
                if peak(_pcm(frame)) >= LOUD:
                    counts["caller_out_loud:" + self.cid] += 1
                    if not self.room.heard_reply.get(self.cid):
                        self.room.heard_reply[self.cid] = True
                        self.room.trace("caller.reply_audio", caller=self.cid)
            except Exception:  # noqa: BLE001 - tracing must never break audio
                pass
            return frame

    return MixedMic, Tap


class Room:
    """Multi-caller state for one Call (mobile_live.Call): callers, uplink mix, reply fan-out, mesh mailbox."""

    def __init__(self, call, invites: Invites = INVITES):
        self.call, self.invites = call, invites
        self.callers: dict[str, Caller] = {HOST: Caller(HOST, "host", secrets.token_urlsafe(24), "")}
        self.mailbox = Mailbox()
        self.turn = Attribution()
        self.relay = None
        self.output = None
        self.output_ready = asyncio.Event()
        self.heard_reply: dict[str, bool] = {}
        self.skipped = 0
        self.closed = False
        self.seq = 0

    def trace(self, ev, **data):
        self.call.trace(ev, **data)

    # host side
    def host_member(self) -> dict:
        return {"caller_id": HOST, "member": self.callers[HOST].member, "max_callers": MAX_CALLERS}

    def mixed(self, track):
        MixedMic, _ = _track_class()
        return MixedMic(track, self)

    def fan_out(self, bridge):
        """Wrap the host's output bridge so guests can subscribe to the same reply. Returns the host's track."""
        from aiortc.contrib.media import MediaRelay
        _, Tap = _track_class()
        self.relay, self.output = MediaRelay(), bridge
        self.output_ready.set()
        return Tap(self.relay.subscribe(bridge, buffered=False), self, HOST)

    def mix_frame(self, frame):
        try:
            host = _pcm(frame)
        except Exception:  # noqa: BLE001
            return frame
        peaks = {HOST: peak(host)}
        others = []
        for c in list(self.callers.values()):
            if c.id == HOST:
                continue
            pcm = c.queue.popleft() if c.queue else None
            peaks[c.id] = peak(pcm) if pcm is not None else 0
            if pcm is not None:
                others.append(pcm)
                self.call.counts["caller_mixed:" + c.id] += 1
        for cid, p in peaks.items():
            c = self.callers.get(cid)
            if c is None:
                continue
            if p >= LOUD and not c.on:  # one event per caller per turn; end_turn re-arms it
                c.on = True
                self.trace("caller.audio.on", caller=cid, peak=p)
        self.turn.frame(peaks)
        if not others:
            return frame
        mixed, skipped = mix(host, others)
        if skipped:
            self.skipped += skipped
            self.call.counts["caller_mix_skipped"] += skipped
        try:
            _write(frame, mixed)
        except Exception:  # noqa: BLE001 - a failed mix passes the host's frame through; the uplink must live
            self.call.counts["caller_mix_error"] += 1
        return frame

    def end_turn(self, text: str = "") -> None:
        result = self.turn.close_turn()
        for c in self.callers.values():
            c.on = False
        if result["speakers"]:
            self.trace("caller.turn", text=text[:200], **result)

    # guest side
    async def join(self, sdp: str, name: str, remote: str) -> dict:
        from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription
        if self.closed or self.call.closed:
            raise PermissionError("call is closed")
        if len(self.callers) >= MAX_CALLERS:
            raise PermissionError("call is full")
        self.seq += 1
        cid = f"guest-{self.seq}"
        caller = Caller(cid, (name or cid)[:40], secrets.token_urlsafe(24), remote,
                        RTCPeerConnection(RTCConfiguration(iceServers=[])))
        peer = caller.peer
        self.callers[cid] = caller

        @peer.on("track")
        def on_track(track):
            if track.kind == "audio" and caller.task is None:
                caller.task = asyncio.create_task(self.read_guest(caller, track))

        @peer.on("connectionstatechange")
        async def changed():
            self.trace("caller.peer", caller=cid, state=peer.connectionState)
            if peer.connectionState in ("failed", "closed"):
                await self.leave(cid, "transport_closed")

        try:
            async with asyncio.timeout(30):
                await self.output_ready.wait()
                _, Tap = _track_class()
                await peer.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
                if not any(t.kind == "audio" for t in peer.getTransceivers()):
                    raise ValueError("An audio track is required")
                peer.addTrack(Tap(self.relay.subscribe(self.output, buffered=False), self, cid))
                await peer.setLocalDescription(await peer.createAnswer())
                if self.closed or self.call.closed or cid not in self.callers:
                    raise RuntimeError("call has closed")
        except BaseException:
            await self.leave(cid, "join_failed")
            raise
        self.trace("caller.joined", caller=cid, name=caller.name, remote=remote, callers=len(self.callers))
        return {"ok": True, "session": {"id": self.call.id}, "caller_id": cid, "member": caller.member,
                "peers": [c for c in self.callers if c != cid],
                "transport": {"type": "webrtc", "sdp": peer.localDescription.sdp}}

    async def read_guest(self, caller: Caller, track):
        from aiortc.mediastreams import MediaStreamError
        try:
            while not self.closed:
                frame = await track.recv()
                self.call.counts["caller_in_frames:" + caller.id] += 1
                caller.queue.append(_pcm(frame).copy())
        except (MediaStreamError, asyncio.CancelledError):
            pass

    async def leave(self, cid: str, reason: str):
        caller = self.callers.pop(cid, None) if cid != HOST else None
        if caller is None:
            return
        if caller.task:
            caller.task.cancel()
        if caller.peer:
            await caller.peer.close()
        for other in self.callers:
            try:
                self.mailbox.post(cid, [other], other, "bye")
            except ValueError:
                pass
        self.trace("caller.left", caller=cid, reason=reason, callers=len(self.callers))

    def member(self, token) -> str | None:
        if not isinstance(token, str) or not token.isascii() or len(token) != 32:
            return None
        for c in self.callers.values():
            if hmac.compare_digest(c.member, token):
                return c.id
        return None

    async def close(self):
        self.closed = True
        self.invites.revoke_call(self.call.id)
        for cid in [c for c in self.callers if c != HOST]:
            await self.leave(cid, "call_closed")
        self.trace("caller.summary", **{k: v for k, v in self.call.counts.items() if k.startswith("caller_")})


# --- HTTP ------------------------------------------------------------------------------------------------------

def _remote(request) -> str:
    return request.headers.get("X-Forwarded-For", "").split(",")[0].strip() or (request.remote or "")


def routes(app, authenticate):
    from aiohttp import web

    def room_of(request, call_id):
        call = request.app["manager"].calls.get(call_id)
        if not enabled() or call is None or call.profile != "guest" or getattr(call, "multi", None) is None:
            raise web.HTTPNotFound(text="No multi-caller call")
        if call.closed or call.multi.closed:
            raise web.HTTPNotFound(text="No multi-caller call")
        return call.multi

    async def invite(request):
        if not enabled():
            raise web.HTTPNotFound(text="No multi-caller call")
        await authenticate(request)  # only the host (a dashboard identity) mints invites
        data = await request.json()
        room = room_of(request, str(data.get("call_id", "")))
        if room.member(request.headers.get("X-Z0live-Member")) != HOST:
            raise web.HTTPForbidden(text="Only the host may invite")
        return web.json_response({"invite": room.invites.issue(room.call.id), "expires_in": INVITE_SECONDS})

    async def join(request):
        if not enabled():
            raise web.HTTPNotFound(text="No multi-caller call")
        try:
            data = await request.json()
            sdp = data.get("sdp")
            if not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp) > SDP_MAX:
                raise ValueError
        except (ValueError, AttributeError):
            raise web.HTTPBadRequest(text="A valid SDP offer is required") from None
        call_id = INVITES.redeem(data.get("invite"))
        if call_id is None:
            raise web.HTTPForbidden(text="Invite is invalid, spent or expired")
        room = room_of(request, call_id)
        try:
            return web.json_response(await room.join(sdp, str(data.get("name") or ""), _remote(request)))
        except PermissionError as exc:
            raise web.HTTPConflict(text=str(exc)) from None
        except (ValueError, RuntimeError, TimeoutError):
            raise web.HTTPBadRequest(text="Could not negotiate guest media") from None

    async def mesh(request):
        room = room_of(request, request.match_info["call_id"])
        me = room.member(request.headers.get("X-Z0live-Member"))
        if me is None:
            raise web.HTTPForbidden(text="Not a member of this call")
        if request.method == "GET":
            try:
                after = int(request.query.get("after", "0"))
            except ValueError:
                after = 0
            return web.json_response({"me": me, "members": list(room.callers),
                                      "messages": room.mailbox.read(me, after)})
        data = await request.json()
        try:
            out = room.mailbox.post(me, list(room.callers), data.get("to"), data.get("kind"), data.get("sdp"))
        except ValueError as exc:
            raise web.HTTPBadRequest(text=str(exc)) from None
        room.trace("mesh.signal", sender=me, to=data.get("to"), kind=data.get("kind"))
        return web.json_response(out)

    app.router.add_post("/api/audio/voice-live/invite", invite)
    app.router.add_post("/api/audio/voice-live/join", join)
    app.router.add_get("/api/audio/voice-live/mesh/{call_id}", mesh)
    app.router.add_post("/api/audio/voice-live/mesh/{call_id}", mesh)
