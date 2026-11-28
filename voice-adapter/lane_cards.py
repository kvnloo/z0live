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
MAX_EARLIER = 2        # per caller turn, including an explicit read-back
SUMMARY_CHARS = 160

_AWAY = re.compile(r"\bnot (?:at|near|by|in front of) (?:the|my) tv\b|\btv(?: is|['’]s) off\b", re.I)
_READBACK = re.compile(
    r"(?:can you |could you |please )?(?:just )?"
    r"(?:(?:tell me|give me|read(?: (?:me|back|out)){0,2}) (?:the |those |my |your |the lane )?results"
    r"|(?:tell me|read me) (?:what (?:you|they|the lanes) found|what came back)|tell me)"
    r"(?: out loud| aloud)?(?: please)?[.!?]*", re.I)
_AWAY_PREFACE = re.compile(
    r"(?:(?:oh|bro|hey)[, ]+)*(?:(?:i(?:'m|’m| am) )?not (?:at|near|by|in front of) (?:the|my) tv"
    r"(?: and (?:the |my )?tv(?: is|['’]s) off)?|(?:the |my )?tv(?: is|['’]s) off)[,.!; ]+", re.I)
_TV_POINTER = re.compile(r"(?:[;,—-]\s*)?(?:\b(?:their |the )?(?:results|details)(?: are)?"
                         r"|\b(?:see|check|look))\s+on the TV\b[.!?]?", re.I)


def readback_requested(text: str) -> bool:
    text = " ".join((text or "").split())
    text = _AWAY_PREFACE.sub("", text, count=1) if _AWAY_PREFACE.match(text) else text
    text = re.sub(r"^(?:(?:oh|bro|hey)[, ]+)+", "", text, flags=re.I)
    return bool(_READBACK.fullmatch(text))


def away_requested(text: str) -> bool:
    return bool(_AWAY.search(text or "") or readback_requested(text))


def spoken_summary(text: str) -> str:
    text = _TV_POINTER.sub("", text).strip(" ,;—-")
    return first_sentence(text) if text else ""


def voice_only(text: str) -> str:
    """Drop display-only pointers; keep factual mentions of the television."""
    text = re.sub(r"\s+and on the TV\b(?=[.!?]|$)", "", _TV_POINTER.sub("", text), flags=re.I)
    return re.sub(r"\bon the TV\b", "on the television", text, flags=re.I).strip()


def speak_line(lane: dict, summary: str, earlier: str | None = None) -> str:
    if earlier is None:
        return f"{who(lane)} lane: {summary}"
    topic = " ".join(earlier.split())[:100].rstrip(" .?!,:;") or "request"
    return f"On your earlier request ({topic}): {summary}"


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


_CORRECTION = re.compile(r"^\s*(?:actually|correction|to correct that)\b", re.I)


def superseded(summary: str, did: str, spoken: dict[str, list[str]]) -> bool:
    """Skip a card when a correction on its request or a subsequent request covers the same subject."""
    same_or_later = did not in spoken
    for key, lines in spoken.items():
        same_or_later |= key == did
        if same_or_later and any(_CORRECTION.match(line) and said_already(summary, [line], share=0.4)
                                 for line in lines):
            return True
    return False
