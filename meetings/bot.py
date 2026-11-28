"""hermes-meetings room bot: a visible LiveKit participant that bridges the owner's audio into z0live's voice path.

  owner (identity == --owner, from the signed token)  -> z0live voice adapter, exactly as the Hermes phone app calls it:
        POST {voice_url}/api/audio/voice-live/session  {sdp, history} with a Bearer token, WebRTC audio + 'oai-events'
        data channel. z0's reply audio comes back on that peer and is published into the room as track "z0-voice".
  everyone else                                      -> context only (PRD R2.3): talkspurts are logged per identity,
        never forwarded to the voice leg, never able to trigger tools.

Runs in its own venv (livekit, aiortc, aiohttp, numpy, av). No LiveKit or z0live code is modified or forked.
usage: bot.py --url ws://127.0.0.1:7880 --token-file F --owner kevin [--voice-url http://127.0.0.1:9168
              --voice-bearer-file F --profile tek] [--status-file F]
"""
from __future__ import annotations

import argparse
import asyncio
import base64
import io
import json
import logging
import os
import signal
import time
import wave
from collections import Counter, defaultdict
from fractions import Fraction
from pathlib import Path
from urllib.parse import urlsplit

import av
import numpy as np
from aiohttp import ClientSession, ClientTimeout
from aiortc import MediaStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription
from aiortc.mediastreams import MediaStreamError
from livekit import rtc

LOG = logging.getLogger("hermes.meetings.bot")
SR = 48000
FRAME = 960  # 20 ms, what the phone sends
LOUD = 500   # s16 peak that counts as sound (z0live's Level uses 800 for its own gate)


def log(event, **kw):
    print(json.dumps({"t": round(time.time(), 3), "event": event, **kw}, ensure_ascii=False), flush=True)


def local_url(value):
    url = urlsplit(value)
    if (url.scheme != "http" or url.hostname != "127.0.0.1" or url.username is not None
            or url.path.rstrip("/") or url.query or url.fragment):
        raise ValueError("voice and ASR endpoints must be loopback HTTP origins")
    return value.rstrip("/")


async def transcribe(http, url, pcm):
    rs = av.AudioResampler(format="s16", layout="mono", rate=16000)
    frame = av.AudioFrame.from_ndarray(np.frombuffer(pcm, dtype=np.int16).reshape(1, -1), format="s16", layout="mono")
    frame.sample_rate = SR
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setparams((1, 2, 16000, 0, "NONE", "not compressed"))
        for out in rs.resample(frame) + rs.resample(None):
            w.writeframes(out.to_ndarray().astype("<i2").tobytes())
    body = {"messages": [{"role": "system", "content": "Perform ASR."},
                         {"role": "user", "content": [{"type": "input_audio", "input_audio": {
                             "format": "wav", "data": base64.b64encode(buf.getvalue()).decode()}}]}],
            "stream": True, "max_tokens": 160, "reset_context": True}
    async with http.post(url + "/v1/chat/completions", json=body, allow_redirects=False,
                         timeout=ClientTimeout(total=30)) as response:
        response.raise_for_status()
        text = []
        async for line in response.content:
            data = line[5:].strip() if line.startswith(b"data:") else b""
            if data and data != b"[DONE]":
                text.append(json.loads(data)["choices"][0]["delta"].get("content") or "")
        return "".join(text).strip()


class OwnerMic(MediaStreamTrack):
    """The owner's room audio as the 'phone mic' track: 20 ms mono s16 frames at real-time pace, silence when the
    owner is quiet or gone (the phone sends continuous audio too)."""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.buf = bytearray()
        self.pts = 0
        self.t0 = None

    def push(self, pcm: bytes):
        self.buf += pcm
        if len(self.buf) > SR * 2 * 2:  # >2 s behind: drop the oldest, latency beats completeness
            del self.buf[:len(self.buf) - SR * 2]

    async def recv(self):
        if self.readyState != "live":
            raise MediaStreamError
        if self.t0 is None:
            self.t0 = time.monotonic()
        await asyncio.sleep(max(0.0, self.t0 + self.pts / SR - time.monotonic()))
        need = FRAME * 2
        chunk = bytes(self.buf[:need])
        del self.buf[:need]
        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME)
        frame.planes[0].update(chunk + bytes(need - len(chunk)))
        frame.sample_rate, frame.pts, frame.time_base = SR, self.pts, Fraction(1, SR)
        self.pts += FRAME
        return frame


class VoiceLeg:
    """One z0live call for the owner, opened the way the Hermes mobile app opens it."""

    def __init__(self, bot, url, bearer, profile):
        self.bot, self.url, self.bearer, self.profile = bot, url.rstrip("/"), bearer, profile
        self.mic = OwnerMic()
        self.pc = None
        self.dc = None
        self.session = ""
        self.closed = False
        self.assistant = ""
        self.tasks = set()

    async def start(self, http: ClientSession):
        self.pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.dc = self.pc.createDataChannel("oai-events")
        self.dc.on("message", self.on_event)
        self.pc.on("track", self.on_track)
        self.pc.addTrack(self.mic)
        await self.pc.setLocalDescription(await self.pc.createOffer())
        headers = {"Authorization": "Bearer " + self.bearer}
        if self.profile:
            headers["X-Hermes-Profile"] = self.profile
        t0 = time.monotonic()
        async with http.post(self.url + "/api/audio/voice-live/session", headers=headers,
                             json={"sdp": self.pc.localDescription.sdp, "history": []},
                             allow_redirects=False,
                             timeout=ClientTimeout(total=60)) as resp:
            body = await resp.json(content_type=None)
            if resp.status != 200 or not body.get("ok"):
                raise RuntimeError(f"voice session refused: HTTP {resp.status}")
        self.session = body["session"]["id"]
        await self.pc.setRemoteDescription(RTCSessionDescription(sdp=body["transport"]["sdp"], type="answer"))
        log("voice.started", session=self.session, setup_ms=round((time.monotonic() - t0) * 1000))

    def on_track(self, track):
        if track.kind == "audio":
            t = asyncio.ensure_future(self.drain(track))
            self.tasks.add(t)

    async def drain(self, track):
        """z0's reply audio -> the room track. aiortc hands decoded frames (often s16 stereo 48 kHz)."""
        resample = av.AudioResampler(format="s16", layout="mono", rate=SR)
        while not self.closed:
            try:
                frame = await track.recv()
            except MediaStreamError:
                break
            for out in resample.resample(frame):
                pcm = np.frombuffer(bytes(out.planes[0])[:out.samples * 2], dtype=np.int16)
                await self.bot.speak(pcm)

    def on_event(self, raw):
        try:
            e = json.loads(raw)
        except (TypeError, ValueError):
            return
        kind = e.get("type", "")
        self.bot.counts["voice_event:" + kind] += 1
        if kind == "session.output_transcript.delta":
            self.assistant += e.get("delta", "")
        elif kind == "session.input_transcript.delta":
            self.bot.transcript(self.bot.owner, e.get("delta", ""), heard_by="z0live")
        elif kind == "session.closed":
            log("voice.closed", reason=e.get("reason"))
            self.closed = True
        # flush a finished assistant sentence into the room chat (R3.4: answers by voice and in chat)
        if self.assistant and (self.assistant.rstrip().endswith((".", "?", "!")) or len(self.assistant) > 400):
            text, self.assistant = self.assistant.strip(), ""
            self.bot.say_in_chat(text)

    async def close(self):
        if self.closed and self.pc is None:
            return
        self.closed = True
        try:
            if self.dc and self.dc.readyState == "open":
                self.dc.send(json.dumps({"type": "session.close"}))
                await asyncio.sleep(0.2)
        except Exception:  # noqa: BLE001
            pass
        self.mic.stop()
        for t in self.tasks:
            t.cancel()
        if self.pc:
            await self.pc.close()
            self.pc = None


class Bot:
    def __init__(self, a):
        self.a = a
        self.owner = a.owner
        self.room = rtc.Room()
        self.counts = Counter()
        self.frames_in = Counter()        # identity -> 10 ms frames received
        self.forwarded = Counter()        # identity -> frames sent to the voice leg (only the owner, ever)
        self.talk = defaultdict(lambda: {"on": None, "spurts": 0, "seconds": 0.0})
        self.voice = None
        self.voice_err = ""
        self.http = None
        self.out_src = rtc.AudioSource(SR, 1, queue_size_ms=200)  # small queue: reply latency, not smoothness
        self.out_buf = np.zeros(0, dtype=np.int16)
        self.lines = []
        self.stop = asyncio.Event()
        self.started = time.time()
        self.guest_asr = local_url(a.guest_asr_url) if a.guest_asr_url else ""
        self.voice_url = local_url(a.voice_url) if a.voice_url else ""
        self.asr_task = None
        self.track_tasks = {}

    # -- output --------------------------------------------------------------------------------------------------
    async def speak(self, pcm: np.ndarray):
        self.out_buf = np.concatenate([self.out_buf, pcm])
        while self.out_buf.size >= 480:
            chunk, self.out_buf = self.out_buf[:480], self.out_buf[480:]
            f = rtc.AudioFrame.create(SR, 1, 480)
            np.copyto(np.frombuffer(f.data, dtype=np.int16), chunk)
            if int(np.abs(chunk.astype(np.int32)).max()) >= LOUD:
                self.counts["out_loud_frames"] += 1
            await self.out_src.capture_frame(f)
            self.counts["out_frames"] += 1

    def say_in_chat(self, text):
        self.transcript("z0", text, heard_by="room")
        asyncio.ensure_future(self._chat(text))

    async def _chat(self, text):
        try:
            await asyncio.wait_for(self.room.local_participant.send_text(text, topic="lk.chat"), 5)
            self.counts["chat_sent"] += 1
        except Exception as exc:  # noqa: BLE001 - chat is best effort
            self.counts["chat_failed"] += 1
            log("chat.failed", error=type(exc).__name__)

    def transcript(self, who, text, **kw):
        if text and text.strip():
            self.lines.append({"t": round(time.time(), 3), "who": who, "text": text.strip(), **kw})
            del self.lines[:-200]

    # -- input ---------------------------------------------------------------------------------------------------
    async def guest_text(self, ident, pcm):
        try:
            text = await transcribe(self.http, self.guest_asr, pcm)
            self.transcript(ident, text, heard_by="local-context")
        except Exception as exc:
            self.counts["guest_asr_errors"] += 1
            log("guest_asr.failed", error=type(exc).__name__)

    async def pump(self, track, participant):
        ident = participant.identity
        role = (participant.attributes or {}).get("hermes.role", "")
        owner = ident == self.owner and role == "owner"
        log("track.audio", identity=ident, role=role, owner=owner, sid=track.sid)
        if owner and self.voice is None and self.a.voice_url:
            await self.open_voice()
        stream = rtc.AudioStream(track, sample_rate=SR, num_channels=1)
        try:
            await self.pump_frames(stream, ident, owner, self.talk[ident])
        finally:
            await stream.aclose()

    async def pump_frames(self, stream, ident, owner, spurt):
        clip = bytearray()
        async for ev in stream:
            f = ev.frame
            pcm = np.frombuffer(f.data, dtype=np.int16)
            self.frames_in[ident] += 1
            loud = pcm.size and int(np.abs(pcm.astype(np.int32)).max()) >= LOUD
            now = time.monotonic()
            if loud and spurt["on"] is None:
                spurt["on"] = now
                spurt["last"] = now
            elif loud:
                spurt["last"] = now
            elif spurt["on"] is not None and now - spurt.get("last", now) > 0.6:
                spurt["spurts"] += 1
                spurt["seconds"] += round(spurt["last"] - spurt["on"], 2)
                log("talkspurt", identity=ident, owner=owner, seconds=round(spurt["last"] - spurt["on"], 2),
                    routed="z0live" if owner else "context-only")
                spurt["on"] = None
            if owner:
                if self.voice and not self.voice.closed:
                    self.voice.mic.push(bytes(pcm))
                    self.forwarded[ident] += 1
            elif self.guest_asr:
                if spurt["on"] is not None:
                    clip.extend(bytes(pcm))
                if clip and (spurt["on"] is None or len(clip) >= 12 * SR * 2):
                    if len(clip) >= SR // 2 and (self.asr_task is None or self.asr_task.done()):
                        self.asr_task = asyncio.create_task(self.guest_text(ident, bytes(clip)))
                    elif len(clip) >= SR // 2:
                        self.counts["guest_asr_dropped"] += 1
                    clip.clear()

    async def open_voice(self):
        bearer = Path(self.a.voice_bearer_file).read_text().strip() if self.a.voice_bearer_file else ""
        if not bearer:
            self.voice_err = "no voice bearer configured: owner audio is not bridged"
            log("voice.unavailable", reason=self.voice_err)
            self.voice = None
            return
        leg = VoiceLeg(self, self.voice_url, bearer, self.a.profile)
        self.voice = leg
        try:
            await leg.start(self.http)
            self.voice_err = ""
        except (Exception, asyncio.CancelledError) as exc:
            self.voice_err = f"{type(exc).__name__}: {str(exc)[:200]}"
            log("voice.failed", error=self.voice_err)
            await leg.close()
            if self.voice is leg:
                self.voice = None
            if isinstance(exc, asyncio.CancelledError):
                raise

    # -- room ----------------------------------------------------------------------------------------------------
    async def run(self):
        token = Path(self.a.token_file).read_text().strip()
        self.http = ClientSession()

        @self.room.on("track_subscribed")
        def subscribed(track, pub, participant):
            if track.kind == rtc.TrackKind.KIND_AUDIO and pub.source == rtc.TrackSource.SOURCE_MICROPHONE:
                self.track_tasks[track.sid] = asyncio.create_task(self.pump(track, participant))

        @self.room.on("track_unsubscribed")
        def unsubscribed(track, pub, participant):
            task = self.track_tasks.pop(track.sid, None)
            if task:
                task.cancel()
            if participant.identity == self.owner and pub.source == rtc.TrackSource.SOURCE_MICROPHONE and self.voice:
                asyncio.create_task(self.voice.close())
                self.voice = None

        @self.room.on("participant_connected")
        def joined(p):
            log("participant.joined", identity=p.identity, role=(p.attributes or {}).get("hermes.role", ""))

        @self.room.on("participant_disconnected")
        def left(p):
            log("participant.left", identity=p.identity)
            if p.identity == self.owner and self.voice:
                asyncio.ensure_future(self.voice.close())
                self.voice = None

        t0 = time.monotonic()
        await self.room.connect(self.a.url, token)
        log("room.connected", identity=self.room.local_participant.identity,
            join_ms=round((time.monotonic() - t0) * 1000))
        track = rtc.LocalAudioTrack.create_audio_track("z0-voice", self.out_src)
        await self.room.local_participant.publish_track(
            track, rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE, dtx=False, red=True))
        log("room.published", track="z0-voice")
        asyncio.ensure_future(self._chat("z0 (Hermes) joined. I answer the owner by voice; other people here are "
                                         "context only."))
        # already-present participants are delivered via track_subscribed by the SDK
        asyncio.ensure_future(self.keep_alive())
        while not self.stop.is_set():
            self.write_status()
            try:
                await asyncio.wait_for(self.stop.wait(), 1.0)
            except asyncio.TimeoutError:
                pass
        for task in self.track_tasks.values():
            task.cancel()
        if self.asr_task:
            self.asr_task.cancel()
        if self.voice:
            await self.voice.close()
        self.write_status()
        await self.room.disconnect()
        await self.http.close()

    async def keep_alive(self):
        """Silence on the room track while z0 is quiet, so the output track keeps a steady clock."""
        while not self.stop.is_set():
            if self.out_buf.size == 0 and self.out_src.queued_duration < 0.05:
                await self.speak(np.zeros(480, dtype=np.int16))
            else:
                await asyncio.sleep(0.01)

    def write_status(self):
        if not self.a.status_file:
            return
        st = {"t": time.time(), "uptime_s": round(time.time() - self.started, 1), "owner": self.owner,
              "voice": {"session": self.voice.session if self.voice else "", "error": self.voice_err,
                        "url": self.a.voice_url},
              "frames_in": dict(self.frames_in), "forwarded_to_voice": dict(self.forwarded),
              "talkspurts": {k: {"spurts": v["spurts"], "seconds": round(v["seconds"], 2)} for k, v in self.talk.items()},
              "counts": dict(self.counts), "transcript": self.lines[-50:]}
        tmp = Path(self.a.status_file + ".tmp")
        tmp.write_text(json.dumps(st, ensure_ascii=False))
        tmp.replace(self.a.status_file)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--url", required=True)
    p.add_argument("--token-file", required=True)
    p.add_argument("--owner", default="kevin")
    p.add_argument("--voice-url", default=os.environ.get("MEETINGS_VOICE_URL", ""))
    p.add_argument("--voice-bearer-file", default=os.environ.get("MEETINGS_VOICE_BEARER_FILE", ""))
    p.add_argument("--profile", default=os.environ.get("MEETINGS_PROFILE", ""))
    p.add_argument("--guest-asr-url", default="")
    p.add_argument("--status-file", default="")
    a = p.parse_args()
    logging.basicConfig(level=logging.WARNING)
    async def go():
        bot = Bot(a)  # rtc.Room/AudioSource bind the running loop at construction: build them inside it
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, bot.stop.set)
        await bot.run()
    asyncio.run(go())


if __name__ == "__main__":
    main()
