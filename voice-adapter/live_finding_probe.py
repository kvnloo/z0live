"""Finding page on a real call: offline `say` speech -> adapter (mobile_live.Call, real voice engine over the
subscription) with the real TV server (:8793) and conductor. Does not touch the running voice service.

    .venv/bin/python live_finding_probe.py OUT_DIR [SLUG]

1. queues "show me the call failures finding" in the conductor inbox (the conductor pushes the finding page) and
   says it on the call (the call's first exchange: the voice never speaks first);
2. waits for finding.ask_spoken (the voice reads the page's Ask Kevin);
3. says "What is F3?" (the TV should ring F3: finding.hl), then "Let's do B." (finding.decision).
OUT_DIR gets the call trace (*.jsonl) and report.json: the finding.* events, the assistant transcript, now.json pushes.
PROBE_ADAPTER=<path> runs another adapter revision.
"""
import asyncio
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import time
from fractions import Fraction
from pathlib import Path

import av
from aiohttp import ClientSession
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription

_path = os.environ.get("PROBE_ADAPTER", "mobile_live.py")
_spec = importlib.util.spec_from_file_location("mobile_live", _path,
                                               loader=importlib.machinery.SourceFileLoader("mobile_live", _path))
live = importlib.util.module_from_spec(_spec)
sys.modules["mobile_live"] = live
_spec.loader.exec_module(live)

OUT = Path(sys.argv[1]).resolve()
OUT.mkdir(parents=True, exist_ok=True)
SLUG = sys.argv[2] if len(sys.argv) > 2 else "call-failures"
PROFILE = os.environ.get("PROBE_PROFILE", "tek")
INBOX = Path.home() / ".local/share/z0live/conductor/inbox.jsonl"
NOW = Path.home() / ".local/share/z0live/conductor/now.json"
LINES = {"open": "Show me the call failures finding.", "item": "What is F3 on this page?", "answer": "Let's do B."}


def wav(name, text):
    p = OUT / f"{name}.aiff"
    if not p.exists():
        subprocess.run(["say", "-v", "Samantha", "-o", str(p), text], check=True)
    return p


class Mic(AudioStreamTrack):
    """Silence, plus one queued utterance at a time (play())."""

    def __init__(self):
        super().__init__()
        self.pcm, self.i, self.t0, self.n = b"", 0, None, 0

    def play(self, path):
        rs = av.AudioResampler(format="s16", layout="mono", rate=48000)
        parts = []
        with av.open(str(path)) as c:
            for f in c.decode(audio=0):
                parts += [bytes(o.planes[0])[:o.samples * 2] for o in rs.resample(f)]
        self.pcm += b"".join(parts) + bytes(48000)  # 0.5 s tail

    async def recv(self):
        if self.t0 is None:
            self.t0 = time.monotonic()
        await asyncio.sleep(max(0, self.t0 + self.n / 48000 - time.monotonic()))
        chunk = self.pcm[self.i * 2:(self.i + 960) * 2] if self.i * 2 < len(self.pcm) else b""
        if chunk:
            self.i += 960
        fr = av.AudioFrame(format="s16", layout="mono", samples=960)
        fr.planes[0].update(chunk + bytes(1920 - len(chunk)))
        fr.sample_rate, fr.pts, fr.time_base = 48000, self.n, Fraction(1, 48000)
        self.n += 960
        return fr

    def idle(self):
        return self.i * 2 >= len(self.pcm)


EVENTS = []
_trace_call = None


async def main():
    paths = {k: wav(k, v) for k, v in LINES.items()}
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    dc = pc.createDataChannel("oai-events")
    said = {"text": ""}

    @dc.on("message")
    def msg(raw):
        e = json.loads(raw)
        if e.get("type") == "session.output_transcript.delta":
            said["text"] += e.get("delta", "")

    @pc.on("track")
    def track(tr):
        async def drain():
            while True:
                await tr.recv()
        asyncio.ensure_future(drain())

    mic = Mic()
    pc.addTrack(mic)
    pushes = []
    async with ClientSession() as http:
        m = live.Manager(http)
        call = live.Call(m, PROFILE)
        m.calls[call.id] = call
        def read_events():  # the call's own trace file(s) under OUT: the finding.* rows
            rows = []
            for p in sorted(OUT.rglob("*.jsonl")):
                for line in p.read_text(errors="replace").splitlines():
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    if str(e.get("ev") or e.get("event") or "").startswith("finding."):
                        rows.append(e)
            EVENTS[:] = [{**e, "ev": e.get("ev") or e.get("event")} for e in rows]

        async def wait(cond, s):
            end = time.monotonic() + s
            while time.monotonic() < end and not cond():
                await asyncio.sleep(0.25)
                read_events()
                try:
                    n = json.loads(NOW.read_text())
                    if not pushes or pushes[-1]["seq"] != n.get("seq"):
                        pushes.append({k: n.get(k) for k in ("seq", "view", "args", "title")})
                except (OSError, ValueError):
                    pass
            return cond()

        try:
            await pc.setLocalDescription(await pc.createOffer())
            ans = await call.start(pc.localDescription.sdp, [])
            await pc.setRemoteDescription(RTCSessionDescription(sdp=ans["transport"]["sdp"], type="answer"))
            await asyncio.sleep(3)
            with open(INBOX, "a") as f:
                f.write(json.dumps({"t": time.time(), "text": LINES["open"]}) + "\n")
            mic.play(paths["open"])
            asked = await wait(lambda: any(e["ev"] == "finding.ask_spoken" for e in EVENTS), 90)
            await wait(lambda: False, 25)  # let the ask be read
            mic.play(paths["item"])
            await wait(lambda: any(e["ev"] == "finding.hl" and e.get("key") not in ("ask",) for e in EVENTS), 40)
            await wait(lambda: False, 15)
            mic.play(paths["answer"])
            await wait(lambda: any(e["ev"] == "finding.decision" for e in EVENTS), 40)
            await wait(lambda: False, 8)
        finally:
            r = {"slug": SLUG, "asked": any(e["ev"] == "finding.ask_spoken" for e in EVENTS), "events": EVENTS,
                 "now_pushes": pushes, "assistant_transcript": said["text"][-3000:], "call": call.id}
            (OUT / "report.json").write_text(json.dumps(r, indent=2))
            print(json.dumps({k: r[k] for k in ("asked", "events", "now_pushes")}, indent=1)[:4000])
            await call.close("probe_done")
            await pc.close()


live.TRACE_DIR = OUT
asyncio.run(main())
