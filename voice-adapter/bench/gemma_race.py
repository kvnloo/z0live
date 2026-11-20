"""Race the proposed fast tier against its three Haiku lanes alone, on a separate lane server."""
import asyncio
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

from aiohttp import ClientSession

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import tiers

CASES = (
    ("What time does the Juniper rehearsal start?", "6:45"),
    ("What is the capital of Canada?", "ottawa"),
    ("What is 12 times 12?", "144"),
    ("What is the symbol for gold?", "au"),
    ("Which room is the Juniper rehearsal in?", "cedar"),
    ("Open the camera on my phone.", None),
)
CONTEXT = tiers.snapshot("Fictional rehearsal notes: Juniper rehearses at 6:45 in the Cedar room.", [])


async def main():
    url = urlsplit(tiers.URL)
    if url.hostname != "127.0.0.1" or not url.port or url.port == 8646:
        raise SystemExit("Set Z0LIVE_LANE_URL to the isolated 127.0.0.1 lane server, not port 8646.")
    cfg = Path(__file__).resolve().parents[2] / "docs/examples/tiers-gemma.json"
    bindings = json.loads(cfg.read_text())["fast"]
    arms = {"haiku": bindings[1:], "gemma+haiku": bindings}
    async with ClientSession() as http:
        for case, (prompt, expected) in enumerate(CASES, 1):
            order = list(arms) if case % 2 else list(reversed(arms))
            for arm in order:
                t0 = time.monotonic()
                text, model, first_s, errors = await tiers.race(http, "fast", arms[arm], CONTEXT, prompt)
                wall_s = time.monotonic() - t0
                # tiers.race cancels losing clients, but the server can keep running those requests.
                print(json.dumps({"case": case, "arm": arm, "expected": expected, "text": text,
                                  "model": model, "first_s": first_s, "wall_s": round(wall_s, 3),
                                  "errors": errors}), flush=True)
                await asyncio.sleep(max(0, tiers.DEADLINE["fast"] + 2 - wall_s))


if __name__ == "__main__":
    asyncio.run(main())
