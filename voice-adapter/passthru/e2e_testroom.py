"""End-to-end passthru check with real browsers and real WebRTC, no voice provider.

A local "video app" (testroom/room.html) is joined by the real meeting leg (meeting_leg.mjs +
inject.js). A second, uninjected browser is the other participant: it speaks 440 Hz and measures
what it hears and sees. This script plays z0's side: it feeds an 880 Hz "voice" through
Passthru.voice() and listens through Passthru.ears(), exactly as the adapter's AudioBridge does.

Pass = the participant hears ~880 Hz, z0's ears get ~440 Hz, and "share" turns z0's video tile
magenta (the share page). Run with the voice-adapter venv:  .venv/bin/python passthru/e2e_testroom.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import av
import numpy as np
from aiohttp import web

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import passthru  # noqa: E402


class FakeCall:
    id = "e2e-" + str(int(time.time()))

    def __init__(self):
        self.events, self.tasks = [], set()

    def trace(self, ev, **data):
        self.events.append((ev, data))
        if ev not in ("passthru.leg.stats",):
            print("trace", ev, json.dumps(data)[:200], flush=True)

    def spawn(self, coro):
        t = asyncio.ensure_future(coro); self.tasks.add(t); t.add_done_callback(self.tasks.discard); return t


def tone(hz, n, t0):
    t = (np.arange(n) + t0) / 48000
    a = (np.sin(2 * np.pi * hz * t) * 9000).astype(np.int16)
    f = av.AudioFrame.from_ndarray(np.repeat(a, 2).reshape(1, -1), format="s16", layout="stereo")
    f.sample_rate, f.pts = 48000, t0
    return f


def dominant(a):
    spec = np.abs(np.fft.rfft(a.astype(np.float32) * np.hanning(len(a))))
    return int(np.argmax(spec[5:]) + 5) * 48000 // len(a)


async def main():
    call = FakeCall()
    p = passthru.Passthru(call)
    p.floor.policy = "open"
    sig = {}

    async def offer(req):
        if req.method == "POST":
            sig["offer"] = await req.text(); return web.Response(text="ok")
        return web.Response(text=sig["offer"]) if "offer" in sig else web.Response(status=404)

    async def answer(req):
        if req.method == "POST":
            sig["answer"] = await req.text(); return web.Response(text="ok")
        return web.Response(text=sig["answer"]) if "answer" in sig else web.Response(status=404)

    async def ws(req):
        sock = web.WebSocketResponse(max_msg_size=1 << 20); await sock.prepare(req)
        await p.socket(sock); return sock

    app = web.Application()
    app.router.add_route("*", "/sig/offer", offer)
    app.router.add_route("*", "/sig/answer", answer)
    app.router.add_get("/passthru/ws", ws)
    app.router.add_static("/", HERE / "testroom")
    runner = web.AppRunner(app); await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0); await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"

    await p.start(base + "/room.html" + (sys.argv[1] if len(sys.argv) > 1 else ""), f"ws://127.0.0.1:{port}/passthru/ws")
    peer = await asyncio.create_subprocess_exec("node", str(HERE / "testroom" / "peer.mjs"), base + "/peer.html", "26",
                                                stdout=asyncio.subprocess.PIPE)
    last = {}

    async def read_peer():
        while True:
            line = await peer.stdout.readline()
            if not line:
                return
            try:
                last.update(json.loads(line) or {})
            except ValueError:
                pass
    call.spawn(read_peer())

    heard, t0, start = [], 0, time.monotonic()
    shared = False
    results = {}
    while time.monotonic() - start < 24:
        p.voice(tone(880, 960, t0))                                   # z0 speaks
        mixed = p.ears(tone(0, 960, t0))                              # z0's mic frame (silent phone) + room
        heard.append(passthru.to_mono_s16(mixed)); t0 += 960
        if not shared and time.monotonic() - start > 12:
            results["before_share"] = dict(last)
            await p.share(base + "/share.html", "canvas"); shared = True
        await asyncio.sleep(0.02)
    results["after_share"] = dict(last)
    ears = np.concatenate(heard[-200:])
    results["z0_ears_hz"] = dominant(ears)
    results["z0_ears_rms"] = int(np.sqrt(np.mean(ears.astype(np.float32) ** 2)))
    results["room_frames"], results["room_dropped"], results["counts"] = p.room.received, p.room.dropped, p.counts
    await p.stop("test_done")
    await peer.wait()
    await runner.cleanup()
    print("RESULT", json.dumps(results))
    b, a = results["before_share"], results["after_share"]
    ok = (abs(b.get("heardHz", 0) - 880) < 20 and abs(results["z0_ears_hz"] - 440) < 20
          and a.get("centerPx") and a["centerPx"][0] > 200 and a["centerPx"][1] < 60 and a["centerPx"][2] > 200)
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
