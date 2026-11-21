"""Fast, medium and slow model lanes for one delegated request (the simplest working version, 11-19).

Context: every lane gets the same snapshot, the chat brief plus the call's recent turns plus the request. Lanes
keep no state of their own, so none can drift from the call. Within a tier the models race; the first real
answer (not SKIP) wins. Speaking is decided in mobile_live.Call.tier_lanes: first answer from any tier is
spoken, a later tier only if it adds something, at most MAX_SPOKEN per request.

Models go through lane_server.py (plain model calls). A {"provider": "local", "model": "gemma4-e4b"}
binding uses its loopback endpoint (Z0LIVE_LOCAL_BASE_URL). See docs/examples/tiers-gemma.json for an
opt-in layout. Override the tiers per call without a restart using ~/.config/z0live/tiers.json.
"""
import asyncio
import json
import os
import re
from pathlib import Path

from aiohttp import ClientTimeout

CLAUDE = "claude-subscription-directsdk-experimental"
CODEX = "openai-codex"
# GPT: newest of each tier this ChatGPT account's Codex catalog offers (11-19: 6.2 has only sol/astra, 6.1
# terra/sol, luna is newest at 6.0; gpt-6-terra is refused for ChatGPT accounts). Measured through
# lane_server: luna 1.38 s, terra 1.49 s, sol 1.54 s for a one-sentence answer.
TIERS = {
    "fast": [{"provider": CLAUDE, "model": "claude-haiku-5-5"}, {"provider": CODEX, "model": "gpt-6-luna"}],
    "medium": [{"provider": CLAUDE, "model": "claude-sonnet-5-5"}, {"provider": CODEX, "model": "gpt-6.1-terra"}],
    "slow": [{"provider": CLAUDE, "model": "claude-opus-5-5"}, {"provider": CODEX, "model": "gpt-6.2-sol"}],
}
DEADLINE = {"fast": 4, "medium": 15, "slow": 45}  # seconds; a tier past its deadline is no answer
MAX_SPOKEN = 3  # first answer + two updates per request (a 22 s lane monologue on 11-16 was too much)
FILE = Path(os.environ.get("Z0LIVE_TIERS_FILE", str(Path.home() / ".config/z0live/tiers.json")))
URL = os.environ.get("Z0LIVE_LANE_URL", "http://127.0.0.1:8646/v1/ask")
# SKIP anywhere is a skip: "I can't send that right now. SKIP" was spoken whole (slm-v1, 11-19).
SKIP = re.compile(r"(\bSKIP\b|^\W*(model guard|the hook flagged))")
# v2 (11-19): v1 said "any lookup you cannot do from the text below" and Gemma 4 E4B / Qwen3.5-9B / LFM3 then
# skipped general knowledge ("What's the capital of Canada?" -> SKIP) while Haiku did not.
SYSTEM = ("You are the {tier} lane on Kevin's live voice call; what you write may be spoken aloud. Answer in one or "
          "two short spoken sentences, most important fact first. Answer general knowledge, math and reasoning "
          "yourself. For Kevin's own plans and facts use only the conversation and context below. Reply with exactly "
          "SKIP and nothing else when the request needs his phone, screen, apps, files, messages, email or any "
          "action; needs live data such as weather, news or the state of his builds; or asks a personal fact that "
          "is not in the context. Never say you can't do something; reply SKIP instead (his Hermes chat, which has "
          "the tools, is already on it). Never invent numbers or claim something worked.")


def load() -> dict:
    """TIERS, or the override file when it holds a valid {tier: [{provider, model}]} map (a bad edit is ignored)."""
    try:
        cfg = json.loads(FILE.read_text())
        if isinstance(cfg, dict) and all(
                t in DEADLINE and isinstance(v, list) and all(isinstance(b, dict) and b.get("provider") and b.get("model")
                                                              for b in v) for t, v in cfg.items()):
            return cfg
    except (OSError, ValueError):
        pass
    return TIERS


def snapshot(brief: str, exchanges) -> str:
    """The one context every lane sees: the chat brief, then the call so far."""
    turns = "\n".join(f"{'Kevin' if role == 'user' else 'You'}: {text}" for role, text in exchanges)
    return (f"Context from his Hermes chat:\n{brief[-6000:]}\n\n" if brief else "") + \
           (f"This call so far:\n{turns}" if turns else "")


async def ask(http, binding, system, user, timeout):
    body = {**binding, "system": system, "user": user, "max_tokens": 300, "timeout": timeout}
    async with http.post(URL, json=body, timeout=ClientTimeout(total=timeout + 2)) as resp:
        data = await resp.json()
    if resp.status != 200:
        raise RuntimeError(data.get("error") or resp.status)
    return data


async def race(http, tier, bindings, context, request):
    """First real answer in a tier: (text, model, first_s, errors). Empty text when none came in time."""
    system = SYSTEM.format(tier=tier) + ("\n\n" + context if context else "")
    jobs = {asyncio.ensure_future(ask(http, b, system, request, DEADLINE[tier])): b["model"] for b in bindings}
    errors, pending = {}, set(jobs)
    try:
        while pending:
            done, pending = await asyncio.wait(pending, timeout=DEADLINE[tier] + 2, return_when=asyncio.FIRST_COMPLETED)
            if not done:
                break
            for job in done:
                try:
                    out = job.result()
                except Exception as exc:  # noqa: BLE001
                    errors[jobs[job]] = str(exc)[:160]
                    continue
                if out["text"] and not SKIP.search(out["text"]):
                    return out["text"], jobs[job], out["first_s"], errors
                errors[jobs[job]] = "SKIP" if out["text"] else "empty"
    finally:
        for job in pending:
            job.cancel()
    return "", None, None, errors
