"""TV conductor lane cards, spoken on the call they were asked on (11-27: the answers only reached the TV; none of
them was heard on the call).

The conductor (atlas-engine-tv apps/z0live-tv/conductor.py) writes ~/.local/share/z0live/conductor/state.json:
asks[] {call (first 12 hex of the voice call id), text (the delegation.start request, whitespace-joined, [:400]),
lanes[] {id, title, model, state running/done/failed, card {title, summary, ...}}}. Its save() drops the ask's
did, so an ask maps to this call's request by call + request text. Pure functions; mobile_live.Call.watch_cards
polls the file and speaks.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

POLL_SECONDS = 1.0     # one stat() per call per second; the file is parsed only when its mtime moves
BATCH_SECONDS = 1.5    # lanes finishing this close together are one line
HOLD_SECONDS = 90.0    # a line the floor never let out by then is dropped (floor_busy)
QUIET_SECONDS = 1.0    # the phone's mic and the voice's output both quiet this long
HANDED_SECONDS = 4.0   # a line handed to the voice this recently: its reply has not opened yet (no merge into it)
IDLE_SECONDS = 180.0   # no lane of this call running or due: the watcher stops (the next request restarts it)
MAX_LANES = 3          # in one line; the rest are "on the TV"
SUMMARY_CHARS = 160


def call_key(call_id: str) -> str:
    """The conductor's `call` for a voice call id (Tail.poll: the trace stem's last dash part, 12 chars)."""
    return call_id.split("-")[-1][:12]


def request_key(request: str) -> str:
    """The conductor's ask text for a delegation.start request (on_ask)."""
    return " ".join(str(request or "").split())[:400]


def read(path: Path) -> dict | None:
    try:
        state = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return state if isinstance(state, dict) else None


def lanes_of(state: dict, call: str):
    """(ask, lane) for every lane of this call's asks."""
    for ask in state.get("asks") or []:
        if isinstance(ask, dict) and ask.get("call") == call:
            for lane in ask.get("lanes") or []:
                if isinstance(lane, dict) and lane.get("id"):
                    yield ask, lane


def busy(state: dict, call: str) -> bool:
    """Something of this call's is still coming: an ask being planned or a lane not finished."""
    for ask in state.get("asks") or []:
        if isinstance(ask, dict) and ask.get("call") == call:
            if ask.get("state") == "planning" or any(
                    isinstance(ln, dict) and ln.get("state") not in ("done", "failed") for ln in ask.get("lanes") or []):
                return True
    return False


def who(lane: dict) -> str:
    """'sol 6.2' -> 'Sol'; a lane with no model is just 'Lane'."""
    model = str(lane.get("model") or lane.get("slot") or "").split()
    return model[0].capitalize() if model else "Lane"


def first_sentence(text: str, limit: int = SUMMARY_CHARS) -> str:
    text = " ".join(str(text or "").split())
    m = re.match(r"(.+?[.!?])(\s|$)", text)
    out = m.group(1) if m else text
    if len(out) > limit:
        out = out[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "."
    return out if out.endswith((".", "!", "?")) else out + "."


def line(lanes: list[dict]) -> str:
    """One short spoken line for lanes that finished together."""
    lanes = [ln for ln in lanes if (ln.get("card") or {}).get("summary")]
    if not lanes:
        return ""
    if len(lanes) == 1:
        return f"{who(lanes[0])} lane: {first_sentence(lanes[0]['card']['summary'])}"
    parts = [f"{who(ln)}: {first_sentence(ln['card']['summary'])}" for ln in lanes[:MAX_LANES]]
    more = len(lanes) - MAX_LANES
    return (f"{len(lanes)} lanes back. " + " ".join(parts)
            + (f" And {more} more on the TV." if more > 0 else ""))


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9']+", str(text).lower()) if len(w) >= 3 or w.isdigit()}


def said_already(summary: str, said: list[str], share: float = 0.6) -> bool:
    """Most of the card's first sentence was already spoken for this request (a tier lane, Hermes, an earlier card)."""
    card, heard = _words(first_sentence(summary)), set().union(*(_words(s) for s in said)) if said else set()
    return bool(card) and len(card & heard) >= share * len(card)
