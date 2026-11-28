"""A second agent joins a real call by voice, then both voices answer: HARNESS proof for guest_join.

Offline `say` speech -> adapter (mobile_live.Call, started with NO guest: voices.json is {}) -> real gpt-live
sessions over the Codex subscription. Kevin says "Add Tek to the call."; Tek's session starts mid-call and says hello
in its own voice; a question to Hermes gets a scripted answer that lands while the voice is mid-reply (the 192702 /
181920 shape); a question to Tek goes to Tek's real guest Hermes host; "Hermes, back to you"; "Remove Tek from the
call." Does not touch the running service, its traces, the request ledger or ~/.config/z0live/voices.json.

    .venv/bin/python live_join_probe.py OUT_DIR [PROFILE]

OUT_DIR gets the call trace, phone.wav (what the phone heard, both voices) and report.json: repeats and lost
answers (replay.said_once), the floor's decision per utterance, and which voice said each line.
"""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import wave
from fractions import Fraction
from types import SimpleNamespace

OUT = Path(sys.argv[1]).resolve()
PROFILE = sys.argv[2] if len(sys.argv) > 2 else "tek"
OUT.mkdir(parents=True, exist_ok=True)
(OUT / "voices.json").write_text("{}")  # no guest at call start: it joins by voice
os.environ["Z0LIVE_VOICES_FILE"] = str(OUT / "voices.json")
os.environ["HERMES_MOBILE_VOICE_TRACE_DIR"] = str(OUT)
os.environ["Z0LIVE_ALLOW_UNSAFE_TEST_TRACES"] = "1"  # replay imports unittest; OUT is this probe's own dir
os.environ["Z0LIVE_LEDGER"] = str(OUT / "ledger")
os.environ["Z0LIVE_TV_URL"] = "http://127.0.0.1:9"  # never read the TV's state into the probe
os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")

import av  # noqa: E402
from aiohttp import ClientSession  # noqa: E402
from aiortc import AudioStreamTrack, RTCConfiguration, RTCPeerConnection, RTCSessionDescription  # noqa: E402

import guest_join  # noqa: E402
import mobile_live as live  # noqa: E402
import replay  # noqa: E402
import tiers  # noqa: E402

NAME = guest_join.config(PROFILE).name
SAY = [
    ("add", f"Add {NAME} to the call."),
    ("garden", "Hey Hermes, can you check where the garden project and the boat inspection are at for me?"),
    ("tek", f"{NAME}, what did you work on today?"),
    ("back", f"Thanks {NAME}. Hermes, back to you. That's all for now, thanks."),
    ("remove", f"Remove {NAME} from the call."),
]
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


def judge(probe, lines):
    """Per answer handed to a voice: did the phone hear that voice say it (answer_delivery's measure: enough of its
    words the call had not heard before it was handed), and in how many of that voice's lines (more than one is a
    repeat)? Lines are what the phone received, labelled by the voice that said them."""
    from answer_delivery import content, needed
    repeats, lost, answers = [], [], []
    for leg, sent in (("primary", probe.sent), ("guest", probe.sent_guest)):
        seen = {}
        for t, e in sent:
            if e.get("channel") != "speakable":
                continue
            text = "".join(c.get("text", "") for c in e.get("content", []))
            text = text.split("\n\n", 1)[-1] if text.startswith('"Agent Final Message"') else text
            text = text.replace("To finish what I was saying: ", "")
            seen.setdefault(text, []).append(t)
        for text, times in seen.items():
            first = times[0]
            before = content(" ".join(d for ht, d in probe.heard if ht < first))
            new = content(text) - before
            if "Say exactly this" in text:  # the hello: judge by the quoted words only
                new = content(text.split("Say exactly this and nothing else:", 1)[-1]) - before
            need = needed(len(new)) if new else 0
            mine = [(t, txt) for t, by, end, txt in lines if by == leg and end >= first]  # a line still playing counts
            hit_lines = [round(t, 1) for t, txt in mine if new and len(new & content(txt)) >= need]
            row = {"voice": leg, "handed_at": times, "text": text[:140], "new_words": len(new),
                   "heard_in_lines_at": hit_lines}
            answers.append(row)
            if new and not hit_lines:
                lost.append(row)
            elif len(hit_lines) > 1:
                repeats.append(row)
    return repeats, lost, answers


async def main():
    probe = SimpleNamespace(sent=[], sent_guest=[], heard=[], heard_from=[], marks=[], guest_said_at=None)
    t0 = time.monotonic()
    from_guest = [False]

    def now():
        return round(time.monotonic() - t0, 3)

    _send = live.Call.send

    def send(call, event, to_provider=False, **kw):
        if to_provider:
            probe.sent.append((now(), event))
        elif event.get("type") == "session.output_transcript.delta":
            probe.heard.append((now(), event.get("delta") or ""))
            probe.heard_from.append("guest" if from_guest[0] else "primary")
            if not lane_spoken.done() and "zucchini" in "".join(
                    d for (_, d), by in zip(probe.heard[-12:], probe.heard_from[-12:]) if by == "primary").lower():
                lane_spoken.set_result(time.monotonic())
        return _send(call, event, to_provider, **kw)

    lane_spoken = asyncio.get_running_loop().create_future()

    _gmessage, _gsend = live.GPTGuest.message, live.GPTGuest.send

    def gmessage(guest, raw):
        from_guest[0] = True
        try:
            return _gmessage(guest, raw)
        finally:
            from_guest[0] = False

    def gsend(guest, event):
        probe.sent_guest.append((now(), event))
        if event.get("channel") == "speakable" and "Hi Kevin" not in json.dumps(event) and probe.guest_said_at is None:
            probe.guest_said_at = now()  # the guest's Hermes answer (not its hello)
        return _gsend(guest, event)

    async def race(http, name, bindings, context, request):
        if "garden" not in request.lower():
            return "", None, None, {}
        await asyncio.sleep(1.0)
        probe.marks.append((now(), "lane.answer", name))
        return LANE, "probe-lane", 1.0, {}

    async def ask_hermes(call, did, request):
        probe.marks.append((now(), "hermes.asked", request))
        if "garden" not in request.lower():
            await asyncio.sleep(2)
            return "Nothing else is waiting on you right now."
        await asyncio.sleep(2)
        start = await asyncio.shield(lane_spoken)
        await asyncio.sleep(max(0, start + 3 - time.monotonic()))
        probe.marks.append((now(), "hermes.answer"))
        return HERMES

    pcm = {key: speech(text) for key, text in SAY}
    mic = Mic()
    pc = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    dc = pc.createDataChannel("oai-events")
    dc.on("message", lambda raw: None)
    phone = wave.open(str(OUT / "phone.wav"), "wb")
    phone.setnchannels(1)
    phone.setsampwidth(2)
    phone.setframerate(48000)

    @pc.on("track")
    def track(tr):
        async def drain():
            rs = av.AudioResampler(format="s16", layout="mono", rate=48000)
            while True:
                try:
                    frame = await tr.recv()
                except Exception:  # noqa: BLE001 - the call ended
                    return
                for out in rs.resample(frame):
                    phone.writeframes(bytes(out.planes[0])[:out.samples * 2])
        asyncio.ensure_future(drain())

    pc.addTrack(mic)
    patches = [(live.Call, "send", send), (live.Call, "ask_hermes", ask_hermes),
               (live.GPTGuest, "message", gmessage), (live.GPTGuest, "send", gsend),
               (tiers, "load", lambda: {"fast": [{"provider": "probe", "model": "lane"}]}), (tiers, "race", race),
               (live, "TIERS_ON", True)]
    saved = [(obj, name, getattr(obj, name)) for obj, name, _ in patches]
    for obj, name, value in patches:
        setattr(obj, name, value)
    async with ClientSession() as http:
        manager = live.Manager(http)
        call = live.Call(manager, "")
        call.relay = None
        manager.calls[call.id] = call
        assert call.guest is None and call.duplex is None, "a guest was configured at call start"
        try:
            await pc.setLocalDescription(await pc.createOffer())
            answer = await call.start(pc.localDescription.sdp, [])
            await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["transport"]["sdp"], type="answer"))
            await asyncio.sleep(3)

            def quiet():
                g = call.duplex
                return not (g and (g.replies or g.playing("primary") or g.playing("guest")))

            async def answered(after, seconds, limit):
                end = time.monotonic() + limit
                while time.monotonic() < end:
                    spoken = [t for t, _ in probe.heard if t > after]
                    if spoken and quiet() and now() - probe.heard[-1][0] >= seconds:
                        return True
                    await asyncio.sleep(0.2)
                return False

            async def say(key):
                probe.marks.append((now(), "say", key))
                mic.play(pcm[key])
                await mic.done.wait()
                return now()

            await say("add")
            end = time.monotonic() + 40
            while time.monotonic() < end and not (call.duplex and call.duplex.floor.available):
                await asyncio.sleep(0.2)
            probe.marks.append((now(), "guest.available", bool(call.duplex and call.duplex.floor.available)))
            await answered(now(), 4, 40)  # "Bringing Tek in." and Tek's hello
            await say("garden")
            await asyncio.wait_for(asyncio.shield(lane_spoken), 60)
            await asyncio.sleep(4)
            await answered(now(), 5, 90)
            await say("tek")
            end = time.monotonic() + 180
            while time.monotonic() < end and not probe.guest_said_at:
                await asyncio.sleep(0.5)
            await answered(probe.guest_said_at or now(), 4, 90)
            await answered(await say("back"), 4, 40)
            await answered(await say("remove"), 4, 30)
            await asyncio.sleep(4)
        finally:
            for obj, name, value in saved:
                setattr(obj, name, value)
            await call.close("probe_done")
            await pc.close()
            phone.close()
    trace = [json.loads(line) for path in OUT.glob(f"*/*{call.id[-32:]}*.jsonl")
             for line in path.read_text().splitlines() if line.strip()]
    lines, cur = [], None
    for (t, d), by in zip(probe.heard, probe.heard_from):
        if cur and cur[1] == by and t - cur[2] < 2.5:
            cur[3] += d
            cur[2] = t
        else:
            cur = [t, by, t, d]
            lines.append(cur)
    repeats, lost, answers = judge(probe, lines)
    # An answer the caller talked over (floor.barge_in on its voice), or moved on from by giving the floor to the
    # other voice ("Thanks Tek. Hermes, back to you" while Tek retold a skipped paragraph), is cut by him, not lost.
    barges = [(r["t"], r.get("speaker")) for r in trace if r["ev"] == "floor.barge_in"]
    barges += [(r["t"], r.get("previous")) for r in trace if r["ev"] == "floor.changed"
               and r.get("reason") in ("redirected", "guest_addressed", "new_user_turn")]
    cut_by_caller = [x for x in lost if any(by == x["voice"] and x["handed_at"][0] - 5 <= t <= x["handed_at"][-1] + 40
                                            for t, by in barges)]
    lost = [x for x in lost if x not in cut_by_caller]
    report = {
        "repeats": repeats, "lost": lost, "cut_by_caller": cut_by_caller, "answers": answers,
        "said_by": [{"t": t, "voice": by, "text": text.strip()} for t, by, _, text in lines],
        "utterances": [(r["t"], r.get("text"), r.get("address"), r.get("owner")) for r in trace if r["ev"] == "floor.utterance"],
        "floor": [(r["t"], r["ev"], r.get("speaker"), r.get("reason") or r.get("did")) for r in trace
                  if r["ev"].startswith("floor.") and r["ev"] != "floor.utterance"],
        "guest": [(r["t"], r["ev"], {k: v for k, v in r.items() if k in ("profile", "voice", "action", "name", "reason", "why")})
                  for r in trace if r["ev"].startswith("guest.") and r["ev"] not in ("guest.rx",)][:60],
        "marks": probe.marks, "counts": dict(call.counts), "voices_json_after": (OUT / "voices.json").read_text(),
        "call_id": call.id,
    }
    (OUT / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False, default=str))
    print(json.dumps({k: report[k] for k in ("repeats", "lost", "cut_by_caller", "said_by", "marks", "voices_json_after")},
                     indent=2, ensure_ascii=False, default=str))


asyncio.run(main())
