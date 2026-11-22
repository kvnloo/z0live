"""Two voices on one real call: does every answer reach the caller once? Offline `say` speech -> adapter
(mobile_live.Call with the GPT-Live guest, Tek) -> two real gpt-live-1-codex sessions over the Codex subscription.
Primary answers are scripted (fixed words, fixed timing) so one lands while the voice is mid-reply; Tek's answer
comes from Tek's real guest Hermes host. Does not touch the running service or ~/.config/z0live/voices.json.

    .venv/bin/python live_two_voice_probe.py OUT_DIR

OUT_DIR gets the call trace and report.json: per answer, how often it was handed to a voice and whether the
phone heard it (replay.said_once: repeats and lost).
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from fractions import Fraction
from types import SimpleNamespace

OUT = Path(sys.argv[1]).resolve()
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "voices.json").write_text(json.dumps({"guest": {"engine": "gpt-live", "profile": "tek", "name": "tek",
                                                       "voice": "ember"}}))
os.environ["Z0LIVE_VOICES_FILE"] = str(OUT / "voices.json")
os.environ["HERMES_MOBILE_VOICE_TRACE_DIR"] = str(OUT)
os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")

import av  # noqa: E402
from aiohttp import ClientSession  # noqa: E402
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription  # noqa: E402

import mobile_live as live  # noqa: E402
import replay  # noqa: E402
import tiers  # noqa: E402

SAY = [  # (key, words); the primary's answers are scripted below, Tek's come from Tek's Hermes host
    ("garden", "Hey, can you check where the garden project and the boat inspection are at for me?"),
    ("tek", "Tek, what did you work on today?"),
    ("back", "Thanks Tek. Hermes, back to you. That's all for now, thanks."),
]
# The fast lane answers in 1 s and the voice starts speaking it; Hermes's answer adds facts and lands 3 s into
# that reply (the 192702 and 181920 shape: an answer arriving while the voice is mid-reply).
LANE = ("The garden project is halfway done. The zucchini bed is planted, the cedar trellis is up, and the "
        "irrigation timer still needs a new battery. The compost delivery comes Saturday morning.")
HERMES = ("Also, the boat inspection is Thursday at nine at the marina office, and you need to bring the "
          "registration papers and the trailer title.")


def speech(text: str) -> bytes:
    aiff = OUT / f"say-{abs(hash(text))}.aiff"
    subprocess.run(["say", "-o", str(aiff), text], check=True)
    rs, parts = av.AudioResampler(format="s16", layout="mono", rate=48000), []
    with av.open(str(aiff)) as container:
        for frame in container.decode(audio=0):
            parts += [bytes(o.planes[0])[:o.samples * 2] for o in rs.resample(frame)]
    return b"".join(parts)


class Mic(AudioStreamTrack):
    """Silence, and an utterance whenever play() queues one."""

    def __init__(self):
        super().__init__()
        self.pcm, self.i, self.t0, self.done = b"", 0, None, asyncio.Event()

    def play(self, pcm: bytes):
        self.pcm, self.i, self.done = pcm + bytes(48000), 0, asyncio.Event()

    async def recv(self):
        if self.t0 is None:
            self.t0, self.n = time.monotonic(), 0
        await asyncio.sleep(max(0, self.t0 + self.n / 48000 - time.monotonic()))
        self.n += 960
        pcm = self.pcm[self.i * 2:(self.i + 960) * 2]
        self.i += 960
        if self.pcm and self.i * 2 >= len(self.pcm):
            self.done.set()
        frame = av.AudioFrame(format="s16", layout="mono", samples=960)
        frame.planes[0].update(pcm + bytes(1920 - len(pcm)))
        frame.sample_rate, frame.pts, frame.time_base = 48000, self.n, Fraction(1, 48000)
        return frame


async def main():
    probe = SimpleNamespace(sent=[], sent_guest=[], heard=[], marks=[], sent_guest_answer=None)
    t0 = time.monotonic()

    def now():
        return round(time.monotonic() - t0, 3)

    _send = live.Call.send

    def send(call, event, to_provider=False, **kw):
        if to_provider:
            probe.sent.append((now(), event))
        elif event.get("type") == "session.output_transcript.delta":
            probe.heard.append((now(), event.get("delta") or ""))
            if "zucchini" in "".join(d for _, d in probe.heard[-12:]).lower() and not lane_spoken.done():
                lane_spoken.set_result(time.monotonic())
        return _send(call, event, to_provider, **kw)

    lane_spoken = asyncio.get_running_loop().create_future()

    async def race(http, name, bindings, context, request):
        if "garden" not in request.lower():
            return "", None, None, {}
        await asyncio.sleep(1.0)
        probe.marks.append((now(), "lane.answer", name))
        return LANE, "probe-lane", 1.0, {}

    async def ask_hermes(call, did, request):
        probe.marks.append((now(), "hermes.asked", request))
        if "garden" not in request.lower():  # any other request: Hermes has nothing new
            await asyncio.sleep(2)
            return "Nothing else is waiting on you right now."
        await asyncio.sleep(2)
        start = await asyncio.shield(lane_spoken)  # the voice has begun the lane answer
        await asyncio.sleep(max(0, start + 3 - time.monotonic()))
        probe.marks.append((now(), "hermes.answer"))
        return HERMES

    pcm = {key: speech(text) for key, text in SAY}
    mic = Mic()
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    dc = pc.createDataChannel("oai-events")
    dc.on("message", lambda raw: None)

    @pc.on("track")
    def track(tr):
        async def drain():
            while True:
                await tr.recv()
        asyncio.ensure_future(drain())

    pc.addTrack(mic)
    with_patches = [(live.Call, "send", send), (live.Call, "ask_hermes", ask_hermes), (tiers, "load", lambda: {"fast": [{"provider": "probe", "model": "lane"}]}),
                    (tiers, "race", race),
                    (live, "TIERS_ON", True)]
    saved = [(obj, name, getattr(obj, name)) for obj, name, _ in with_patches]
    for obj, name, value in with_patches:
        setattr(obj, name, value)
    async with ClientSession() as http:
        manager = live.Manager(http)
        call = live.Call(manager, "")
        call.relay = None
        manager.calls[call.id] = call
        guest = call.guest
        assert isinstance(guest, live.GPTGuest), "the guest is not configured"
        _guest_send = guest.send
        def guest_send(event):
            probe.sent_guest.append((now(), event))
            if event.get("channel") == "speakable" and probe.sent_guest_answer is None:
                probe.sent_guest_answer = now()
            _guest_send(event)

        guest.send = guest_send
        try:
            await pc.setLocalDescription(await pc.createOffer())
            answer = await call.start(pc.localDescription.sdp, [])
            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["transport"]["sdp"], type="answer"))
            for _ in range(200):
                if guest.floor.available:
                    break
                await asyncio.sleep(0.1)
            await asyncio.sleep(2)

            async def answered(after, seconds, limit):
                """A voice spoke after `after`, then both were silent for `seconds`."""
                end = time.monotonic() + limit
                while time.monotonic() < end:
                    spoken = [t for t, _ in probe.heard if t > after]
                    if spoken and not guest.replies and now() - probe.heard[-1][0] >= seconds:
                        return True
                    await asyncio.sleep(0.2)
                return False

            async def say(key):
                probe.marks.append((now(), "say", key))
                mic.play(pcm[key])
                await mic.done.wait()
                return now()

            await say("garden")
            await asyncio.wait_for(asyncio.shield(lane_spoken), 60)
            await asyncio.sleep(4)
            await answered(now(), 5, 90)  # the voice finished the lane answer and whatever it did with Hermes's
            await say("tek")
            end = time.monotonic() + 180
            while time.monotonic() < end and not probe.sent_guest_answer:
                await asyncio.sleep(0.5)
            await answered(probe.sent_guest_answer or now(), 4, 90)
            await answered(await say("back"), 4, 40)
            await asyncio.sleep(6)
        finally:
            for obj, name, value in saved:
                setattr(obj, name, value)
            await call.close("probe_done")
            await pc.close()
    repeats, lost = replay.said_once(probe)
    trace = [json.loads(line) for path in OUT.glob(f"*/*{call.id[-32:]}*.jsonl")
             for line in path.read_text().splitlines() if line.strip()]
    cut = {r.get("did") for r in trace if r.get("ev") == "floor.answer_dropped" and r.get("reason") == "barged_in"}
    cut_by_caller = [x for x in lost if x["did"] in cut]
    lost = [x for x in lost if x["did"] not in cut]
    speakable = [(t, leg, e.get("delegation_item_id"), "".join(c.get("text", "") for c in e.get("content", []))[:160])
                 for leg, rows in (("primary", probe.sent), ("guest", probe.sent_guest)) for t, e in rows
                 if e.get("channel") == "speakable"]
    report = {"repeats": repeats, "lost": lost, "cut_by_caller": cut_by_caller, "speakable": sorted(speakable), "marks": probe.marks,
              "heard": "".join(d for _, d in probe.heard), "counts": dict(call.counts),
              "floor": [(r["t"], r["ev"], r.get("did"), r.get("reason")) for r in trace if r["ev"].startswith("floor.")]}
    (OUT / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False))
    print(json.dumps({k: report[k] for k in ("repeats", "lost", "cut_by_caller", "speakable", "marks", "floor")}, indent=2, ensure_ascii=False))
    print("HEARD:", report["heard"])


asyncio.run(main())
