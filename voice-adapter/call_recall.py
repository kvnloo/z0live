"""Recall on the call path (t_47576421, 11-28): what Kevin's records already hold about the request he just spoke.

One lookup per request through `avq recall` (kvnloo/home tools/avlocal/recall.py: sessions, memory, skills, kanban,
cron, docs, repos, PRs and issues, the fact ledger, corrections and call traces), the same ranker the typed-turn hook
uses. Its cited block goes to the medium and slow lanes and to the Hermes turn. The fast lane never waits on it.
Off when the library is not installed or ~/.local/state/recall/seam-off exists; a slow or broken lookup is no block.
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import time
from pathlib import Path

STATE = Path(os.environ.get("RECALL_STATE", str(Path.home() / ".local/state/recall")))
LIB = Path(os.environ.get("Z0LIVE_RECALL_LIB", str(STATE / "lib" / "recall.py")))
OFF = STATE / "seam-off"
LANE_WAIT = 1.5    # s the medium and slow lanes wait (they answer in 2 to 40 s); lookups: p50 0.7 s, p90 1.5 s
HERMES_WAIT = 3.0  # s the Hermes turn waits (it takes 10 s and up)
CHARS = 2400       # the lanes' whole chat brief is 2,810 chars (tiers.BRIEF_BUDGET)
MIN_WORDS = 4      # "name", "Yeah, that's it": nothing to look up
HEAD = ("Records that may already answer this (source, date, title, passage), found by search just now. When one "
        "covers the request, answer from it and say which one; when none does, do not guess from them.")
_mod = None


def lib():
    global _mod
    if _mod is None and LIB.exists():
        spec = importlib.util.spec_from_file_location("z0live_recall_lib", LIB)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        _mod = mod
    return _mod


def lookup(request: str, before=None, chars: int = CHARS) -> dict:
    """{'text', 'hits': [locator], 's', 'err'}: the block for one spoken request; text '' when there is none."""
    began, out = time.monotonic(), {"text": "", "hits": [], "err": ""}
    try:
        if OFF.exists() or len(str(request or "").split()) < MIN_WORDS:
            out["err"] = "off" if OFF.exists() else "short"
        elif (mod := lib()) is None:
            out["err"] = "no library"
        else:
            res = mod.recall(request, before=before, k=6, refresh=False)
            lines = mod.render(res, chars).splitlines()[1:]
            if lines:
                out["text"] = HEAD + "\n" + "\n".join(lines)
                out["hits"] = [h["locator"] for h in res["hits"][:len(lines)]]
    except Exception as exc:  # noqa: BLE001 - recall is an aid: a broken index never breaks the call
        out["err"] = f"{type(exc).__name__}: {exc}"[:160]
    out["s"] = round(time.monotonic() - began, 3)
    return out


async def within(task, seconds: float) -> dict | None:
    """The lookup's result when it lands inside `seconds`; None when it is still running (it keeps running)."""
    try:
        return await asyncio.wait_for(asyncio.shield(task), seconds)
    except asyncio.TimeoutError:
        return None
