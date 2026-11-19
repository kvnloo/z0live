"""Real-call context probe: does the live voice know the chat's context when a call opens?

Drives the real subscription voice through mobile_live.Call with synthetic speech (edge-tts),
the same way live_speech_smoke.py does, and checks the three failures Kevin hit on 11-16:
  1. catch-up: the voice speaks before Kevin does (answering old chat messages);
  2. refusal: "can you see my screen" gets "I can't" instead of a delegation;
  3. amnesia: the delegated request lacks what the chat already settled.
Usage: env -u PYTHONPATH .venv/bin/python context_probe.py [history.json]  -> receipt JSON on stdout.
"""
import asyncio
import json
import os
import re
import subprocess
import sys
import time
from collections import Counter
from fractions import Fraction
from pathlib import Path

os.environ.setdefault("HERMES_MOBILE_VOICE_LANES", "0")
os.environ.setdefault("VOICE_WARM_HAIKU", "0")
os.environ.setdefault("HERMES_VOICE_HANDOFF", "off")
os.environ.setdefault("HERMES_MOBILE_VOICE_TRACE_DIR", os.environ.get("TMPDIR", "/tmp") + "/voice-probe-traces")

import av  # noqa: E402
from aiohttp import ClientSession  # noqa: E402
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription  # noqa: E402

import mobile_live as live  # noqa: E402

ASK = os.environ.get("PROBE_ASK", "Hey. Can you take a look at my screen right now?")
REFUSAL = re.compile(r"can.?t (see|watch|access)|not (actually )?(tapped|connected|able to see)|don.?t have (your|access)|no access", re.I)
SILENCE_S = 6.0  # seconds of silence before the ask: any speech here is catch-up
# PROBE_HERMES_DELAY=N: keep the real delegation (and its lanes); only Hermes's agent turn is faked, answering
# PROBE_HERMES_ANSWER after N seconds. Then "spoken" shows when each part of the reply arrived.
HERMES_DELAY = float(os.environ.get("PROBE_HERMES_DELAY", "0"))
HERMES_ANSWER = os.environ.get("PROBE_HERMES_ANSWER", "Hermes checked: nothing to add.")
LISTEN_S = float(os.environ.get("PROBE_LISTEN", str(max(14, HERMES_DELAY + 10))))


def speech(text: str, path: Path) -> bytes:
    if not path.exists():
        subprocess.run(["edge-tts", "--text", text, "--write-media", str(path)], check=True, capture_output=True)
    resampler, parts = av.AudioResampler(format="s16", layout="mono", rate=48000), []
    with av.open(str(path)) as container:
        for frame in container.decode(audio=0):
            for out in resampler.resample(frame):
                parts.append(bytes(out.planes[0])[: out.samples * 2])
    return bytes(int(48000 * 2 * SILENCE_S)) + b"".join(parts) + bytes(48000 * 2 * 20)


class Prompt(AudioStreamTrack):
    def __init__(self, pcm):
        super().__init__()
        self.pcm, self.index, self.started = pcm, 0, None

    async def recv(self):
        if self.started is None:
            self.started = time.monotonic()
        wait = self.started + self.index / 48000 - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        chunk = self.pcm[self.index * 2: self.index * 2 + 1920] or bytes(1920)
        chunk = chunk.ljust(1920, b"\0")
        frame = av.AudioFrame(format="s16", layout="mono", samples=960)
        frame.planes[0].update(chunk)
        frame.sample_rate, frame.pts, frame.time_base = 48000, self.index, Fraction(1, 48000)
        self.index += 960
        return frame


async def probe(history):
    import hashlib
    pcm = speech(ASK, Path(os.environ.get("TMPDIR", "/tmp")) / f"probe-{hashlib.sha1(ASK.encode()).hexdigest()[:8]}.mp3")
    client = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    dc = client.createDataChannel("oai-events")
    events, said, heard, delegated = Counter(), [], [], []
    t0 = None

    @dc.on("message")
    def received(raw):
        e = json.loads(raw)
        kind = e.get("type", "")
        events[kind] += 1
        at = round(time.monotonic() - t0, 2) if t0 else 0
        if kind == "session.output_transcript.delta":
            said.append((at, e.get("delta", "")))
        elif kind == "session.input_transcript.delta":
            heard.append((at, e.get("delta", "")))

    client.on("track", lambda track: asyncio.ensure_future(drain(track)))
    client.addTrack(Prompt(pcm))
    async with ClientSession() as http:
        manager = live.Manager(http)
        call = live.Call(manager)
        manager.calls[call.id] = call

        async def fixture(did, request):  # no Hermes run: record what the voice asked for
            delegated.append(request)

        async def slow_hermes(did, request):  # real lanes, fake Hermes: the agent turn answers after the delay
            delegated.append(request)
            await asyncio.sleep(HERMES_DELAY)
            return HERMES_ANSWER
        if HERMES_DELAY:
            call.ask_hermes = slow_hermes
        else:
            call.host_delegate = fixture
        try:
            await client.setLocalDescription(await client.createOffer())
            answer = await call.start(client.localDescription.sdp, history)
            await client.setRemoteDescription(RTCSessionDescription(sdp=answer["transport"]["sdp"], type="answer"))
            t0 = time.monotonic()
            await asyncio.sleep(SILENCE_S + LISTEN_S)
        finally:
            for closer in (manager.close(), client.close()):
                try:
                    await asyncio.wait_for(closer, 5)
                except Exception:  # noqa: BLE001 - teardown must not hide the result
                    pass
    before = "".join(d for at, d in said if at < SILENCE_S).strip()
    reply = "".join(d for at, d in said if at >= SILENCE_S).strip()
    return {
        "catch_up_speech": before,
        "heard": "".join(d for _, d in heard).strip(),
        "reply": reply,
        "delegated": delegated,
        "pass_no_catch_up": not before,
        "pass_no_refusal": bool(reply or delegated) and not REFUSAL.search(reply),
        "pass_delegated": bool(delegated),
        "events": dict(events),
        "spoken": spoken(said),
    }


def spoken(said):
    """The reply as (seconds after the ask started, text) runs: a gap over 1 s starts a new run."""
    runs, last = [], None
    for at, d in said:
        if at < SILENCE_S:
            continue
        if last is None or at - last > 1.0:
            runs.append([round(at - SILENCE_S, 1), ""])
        runs[-1][1] += d
        last = at
    return [(at, text.strip()) for at, text in runs if text.strip()]


async def drain(track):
    try:
        while True:
            await track.recv()
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    hist = json.loads(Path(sys.argv[1]).read_text()) if len(sys.argv) > 1 else []
    result = asyncio.run(probe(hist))
    sys.stdout.flush()
    result["all_pass"] = result["pass_no_catch_up"] and result["pass_no_refusal"] and result["pass_delegated"]
    print(json.dumps(result, indent=2), flush=True)
    os._exit(0)  # aiortc leaves non-daemon decoder threads behind
