"""z0 on a real Element Call (call.element.io), end to end: z0 creates the encrypted call through the
element-call recipe, a real uninjected guest joins by the link, audio both ways, then z0 shares a page.

No voice provider: this script stands in for the adapter, speaking 880 Hz through Passthru.voice() and
listening through Passthru.ears(). Pass = guest call shows Encrypted, guest hears ~880 Hz, z0 hears
~440 Hz, guest sees the shared (magenta) page.   Run: .venv/bin/python passthru/e2e_element.py
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import numpy as np
from aiohttp import web

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
sys.path.insert(0, str(HERE))
import passthru  # noqa: E402
from e2e_testroom import FakeCall, tone, dominant  # noqa: E402


async def main():
    call = FakeCall()
    p = passthru.Passthru(call)
    p.floor.policy = "open"
    app = web.Application()
    app.router.add_static("/", HERE / "testroom")
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    share_url = f"http://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/share.html"
    await p.start("", create="element-call")
    link = await p.wait_link(45)
    assert link, "no link from element-call create"
    guest = await asyncio.create_subprocess_exec("node", str(HERE / "testroom/element_guest.mjs"), link, "55",
                                                 stdout=asyncio.subprocess.PIPE, stderr=open(HERE / "testroom/.guest.err", "w"))
    last = {}

    async def read():
        while line := await guest.stdout.readline():
            try:
                last.update(json.loads(line) or {})
            except ValueError:
                pass
    call.spawn(read())
    heard, t0, start, shared, res = [], 0, time.monotonic(), False, {}
    while time.monotonic() - start < 52:
        p.voice(tone(880, 960, t0))
        heard.append(passthru.to_mono_s16(p.ears(tone(0, 960, t0))))
        t0 += 960
        if not shared and time.monotonic() - start > 32:
            res["before_share"] = {k: v for k, v in last.items() if k != "videos"}
            await p.share(share_url, "canvas")
            shared = True
        await asyncio.sleep(0.02)
    res["after_share"] = dict(last)
    ears = np.concatenate(heard[-200:])
    res.update(z0_ears_hz=dominant(ears), z0_ears_rms=int(np.sqrt(np.mean(ears.astype(np.float32) ** 2))),
               room_frames=p.room.received, counts=p.counts)
    await p.stop("test_done")
    if guest.returncode is None:
        guest.kill()
    await guest.wait()
    res["guest_exit"] = guest.returncode
    await runner.cleanup()
    print("RESULT", json.dumps(res))
    b, a = res["before_share"], res["after_share"]
    ok = (b.get("encrypted") and abs(b.get("heardHz", 0) - 880) < 20 and abs(res["z0_ears_hz"] - 440) < 20
          and a.get("magentaVideo"))
    print("PASS" if ok else "FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
