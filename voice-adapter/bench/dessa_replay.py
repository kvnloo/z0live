"""Dessa replay: how close is a real z0live call's turn-taking to Dessa's?

For each Dessa reference call (two tracks, kept private under ~/.local/share/z0live/dessa-ref/), the HUMAN track is
streamed in real time into a real z0live call (mobile_live.Call, the same path a phone call takes; the engine
comes from HERMES_VOICE_ENGINE), what the phone would hear is recorded, and the AI side of the pair
(our voice against that human) is scored with dessa_timing: floor-take gap, overlap starts, waits over 1 s,
backchannels per minute. Dessa's own AI-side numbers on the same 20 calls are the target.

The human track is canned: it was spoken to Dessa, not to us, so only the AI-side numbers mean anything.

  env -u PYTHONPATH .venv/bin/python bench/dessa_replay.py [--calls a,b] [--clip 60] [--out DIR]
    -> one JSON summary on stdout (pooled AI-side timing, per call, errors), WAVs + traces in DIR.
Hermes delegation is faked (REPLAY_HERMES_DELAY s, then a short answer) so no agent turns run.
"""
import argparse
import asyncio
import json
import os
import sys
import time
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
os.environ.setdefault("PROBE_SAVE", "")  # context_probe.drain: set per call below

import numpy as np  # noqa: E402
from aiohttp import ClientSession  # noqa: E402
from aiortc import RTCConfiguration, RTCPeerConnection, RTCSessionDescription  # noqa: E402

import context_probe as cp  # noqa: E402  (Prompt: real-time sender)
import dessa_timing as dt  # noqa: E402
import mobile_live as live  # noqa: E402

REF = Path(os.environ.get("DESSA_REF", str(Path.home() / ".local/share/z0live/dessa-ref/raw-20261119")))
# Frozen subset (2026-11-19): four calls with the most human speech in their first minute.
DEFAULT_CALLS = ("coffee-ding", "ai-release", "anniversary", "dc-comics")
DESSA = {"median_s": 0.31, "overlap_start_pct": 26.5, "over_1s_pct": 14.9, "backchannels_per_min": 2.6}
LEAD_S = 2.0  # silence before the human starts: the voice must not speak first


def human_track(name: str):
    """(48 kHz mono s16 PCM bytes) of the call's human side: the track with the louder noise floor."""
    tracks = sorted(REF.glob(f"{name}-speaker*.*"))
    if len(tracks) != 2:
        raise FileNotFoundError(f"{name}: need two speaker tracks in {REF}")
    x = [dt.pcm(p) for p in tracks]
    floor = lambda a: np.percentile(np.abs(a[: len(a) // dt.FRAME * dt.FRAME]).reshape(-1, dt.FRAME).mean(1), 10)
    human = x[0] if floor(x[0]) > floor(x[1]) else x[1]
    up = np.repeat(human, 3)  # 16 kHz -> 48 kHz (sample repeat is fine for VAD/ASR at this rate)
    return (np.clip(up, -1, 1) * 32767).astype(np.int16).tobytes()


class Recorder:
    """The phone's ear: every output frame as 48 kHz mono, with the monotonic time of the first frame."""
    def __init__(self):
        self.pcm, self.first = [], None

    async def drain(self, track):
        try:
            while True:
                frame = await track.recv()
                if self.first is None:
                    self.first = time.monotonic()
                a = frame.to_ndarray()
                mono = a.reshape(-1, len(frame.layout.channels))[:, 0] if frame.format.is_packed else a[0]
                self.pcm.append(mono.astype(np.int16).tobytes())
        except Exception:  # noqa: BLE001 - track ended
            pass


def save(path: Path, pcm: bytes, rate: int = 48000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(rate), w.writeframes(pcm)


async def replay(name: str, clip_s: float, out: Path):
    pcm = human_track(name)[: int(48000 * 2 * clip_s)]
    pcm = bytes(int(48000 * 2 * LEAD_S)) + pcm + bytes(48000 * 2 * 8)
    client = RTCPeerConnection(RTCConfiguration(iceServers=[]))
    client.createDataChannel("oai-events")
    rec, sender = Recorder(), cp.Prompt(pcm)
    client.on("track", lambda track: asyncio.ensure_future(rec.drain(track)))
    client.addTrack(sender)
    delay = float(os.environ.get("REPLAY_HERMES_DELAY", "2.5"))
    async with ClientSession() as http:
        manager = live.Manager(http)
        call = live.Call(manager)
        manager.calls[call.id] = call

        async def hermes(did, request):  # no agent turn: a quick, neutral answer
            await asyncio.sleep(delay)
            return "Done."
        call.ask_hermes = hermes
        try:
            await client.setLocalDescription(await client.createOffer())
            answer = await call.start(client.localDescription.sdp, [])
            await client.setRemoteDescription(RTCSessionDescription(sdp=answer["transport"]["sdp"], type="answer"))
            await asyncio.sleep(len(pcm) / 2 / 48000 + 1.0)
        finally:
            for closer in (manager.close(), client.close()):
                try:
                    await asyncio.wait_for(closer, 5)
                except Exception:  # noqa: BLE001
                    pass
    if rec.first is None or sender.started is None:
        raise RuntimeError(f"{name}: no audio flowed (sender={sender.started}, recorder={rec.first})")
    # Align: output sample 0 was heard at (rec.first - sender.started) seconds into the human stream.
    shift = int(round((rec.first - sender.started) * 48000))
    ai = b"".join(rec.pcm)
    ai = bytes(shift * 2) + ai if shift > 0 else ai[-shift * 2:]
    n = min(len(ai), len(pcm))
    save(out / f"{name}-human.wav", pcm[:n])
    save(out / f"{name}-ai.wav", ai[:n])
    to16 = lambda b: np.frombuffer(b[:n], np.int16)[::3].astype(np.float32) / 32768.0
    xa, xh = to16(ai), to16(pcm)
    sa, sh = dt.spurts(xa), dt.spurts(xh)
    lead_speech = [s for s in sa if s[0] < LEAD_S - 0.2]
    offsets, bc = dt.timing(sa, sh)
    minutes = len(xh) / 16000 / 60
    return {"minutes": round(minutes, 2), "align_shift_ms": round(shift / 48, 1), "spoke_first": bool(lead_speech),
            "ai_talk_s": round(sum(e - s for s, e in sa), 1), "offsets": offsets, "backchannels": bc}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calls", default=",".join(DEFAULT_CALLS))
    ap.add_argument("--clip", type=float, default=60.0)
    ap.add_argument("--out", default=os.environ.get("TMPDIR", "/tmp") + "/dessa-replay")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    engine = os.environ.get("HERMES_VOICE_ENGINE", "gpt-live")
    per, offsets, bc, minutes, errors = {}, [], 0, 0.0, []
    for name in [c for c in a.calls.split(",") if c]:
        try:
            r = asyncio.run(replay(name, a.clip, out))
        except Exception as exc:  # noqa: BLE001 - count it, keep going
            errors.append(f"{name}: {type(exc).__name__}: {str(exc)[:200]}")
            continue
        offsets += r.pop("offsets")
        bc += r["backchannels"]
        minutes += r["minutes"]
        per[name] = r
    pooled = dt.summary(offsets, bc, minutes) if offsets else {}
    print(json.dumps({"engine": engine, "calls_done": len(per), "errors": errors, "minutes": round(minutes, 2),
                      "ai": pooled, "dessa": DESSA, "per_call": per, "out": str(out)}, indent=1))
    return 1 if errors and not per else 0


if __name__ == "__main__":
    sys.exit(main())
