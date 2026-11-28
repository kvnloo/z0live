"""Bring a second agent onto a live call by voice: "add Tek to the call", "bring PC in", "remove Tek from the call".

the owner, 11-28: "set up multi voice. Like if I want to add a second agent ... make sure that it's working." The
two-voice floor (guest_floor, GPTGuest) only ran when ~/.config/z0live/voices.json named a guest for every call, and
that file is {} since the 11-20 night (the guest answered every turn unheard). This turns the guest on for one call,
on request, and never writes voices.json, so the next call starts with one voice again.

A profile can join when it has a guest Hermes host: its key in ~/.config/z0live/guest-hosts/<profile>.key and a
loopback port (desktop/hermes_host.py --profile <name> --port N). Ports: Z0LIVE_JOIN_HOSTS (json {name: port}),
else ~/.config/z0live/join-hosts.json, else tek on Z0LIVE_GUEST_HOST_PORT (18644) plus ask_bot.hosts() (pc 18645).
The `guest` profile and `default` never join.

Each guest speaks in its own GPT-Live voice, never the primary's "sol": tek ember, pc cove, any other a fixed pick.

    parse(text) -> ("add" | "remove", profile) | None
    config(profile) -> guest_voice.GuestConfig | None
    port(profile) -> int | None
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import guest_voice

PRIMARY_VOICE = "sol"
VOICES = ("arbor", "breeze", "cove", "ember", "juniper", "maple", "spruce", "vale")  # guest_voice.configured's list
VOICE_OF = {"tek": "ember", "pc": "cove"}
NAME_OF = {"tek": "Tek", "pc": "PC"}
DENIED = {"guest", "default"}
# How the transcriber writes the names (GPTGuest's aliases for tek; ask_bot's spellings for pc).
SPOKEN = {"tek": ("tek", "tech", "teck", "tec", "teknium"), "pc": ("pc", "p c", "p.c.", "p.c", "pc bot")}


def _home() -> Path:
    return Path(os.environ.get("HOME") or Path.home())


def hosts() -> dict[str, int]:
    """Profiles that can join a call, with their guest host's loopback port."""
    raw = os.environ.get("Z0LIVE_JOIN_HOSTS")
    if not raw:
        f = _home() / ".config/z0live/join-hosts.json"
        raw = f.read_text() if f.is_file() else ""
    table: dict[str, int] = {}
    try:
        table = {str(k).lower(): int(v) for k, v in json.loads(raw).items()} if raw else {}
    except (ValueError, AttributeError, TypeError):
        table = {}
    if not table:
        table = {"tek": int(os.environ.get("Z0LIVE_GUEST_HOST_PORT", "18644"))}
        try:
            import ask_bot
            for name, p in ask_bot.hosts().items():
                table.setdefault(name, p)
        except Exception:  # noqa: BLE001 - tek alone still joins
            pass
    return {k: v for k, v in table.items() if k not in DENIED and re.fullmatch(r"[a-z0-9_-]{1,32}", k)}


def port(profile: str) -> int | None:
    return hosts().get(profile)


def has_key(profile: str) -> bool:
    return (_home() / ".config/z0live/guest-hosts" / f"{profile}.key").is_file()


def voice(profile: str) -> str:
    if profile in VOICE_OF:
        return VOICE_OF[profile]
    pool = [v for v in VOICES if v not in VOICE_OF.values()]
    return pool[sum(map(ord, profile)) % len(pool)]


def config(profile: str) -> guest_voice.GuestConfig | None:
    if profile in DENIED or profile not in hosts():
        return None
    name = NAME_OF.get(profile) or profile.capitalize()
    return guest_voice.GuestConfig("gpt-live", re.sub(r"[^A-Za-z0-9_-]", "", name)[:32] or profile, voice(profile),
                                   profile)


def greeting(config: guest_voice.GuestConfig) -> str:
    """What the guest says, in its own voice, when it has joined: the out-loud confirmation."""
    return (f"You just joined the owner's call. Say exactly this and nothing else: \"Hi, {config.name} here. "
            f"Say {config.name} when you want me.\"")


def _spellings() -> list[tuple[str, str]]:
    out = []
    for profile in hosts():
        for spoken in SPOKEN.get(profile, (profile,)):
            out.append((spoken, profile))
    return sorted(out, key=lambda pair: len(pair[0]), reverse=True)


def _name_pattern() -> str:
    return "|".join(r"[\s.]*".join(map(re.escape, spoken.replace(" ", "").replace(".", ""))) + r"\.?"
                    for spoken, _ in _spellings()) or "(?!)"


def _profile(word: str) -> str | None:
    key = re.sub(r"[\s.]", "", word.casefold())
    for spoken, profile in _spellings():
        if re.sub(r"[\s.]", "", spoken) == key:
            return profile
    return None


_LEAD = r"^\W*(?:(?:hey|okay|ok|so|and|um|uh|please|hermes|can you|could you|would you|will you|go ahead and|let's)\W+)*"
_CALL = r"(?:the|this|our|my)\s+(?:call|conversation|chat|meeting)"
_BOT = r"(?:\s*(?:bot|profile|agent|hermes))?"
_END = r"\W*(?:(?:please|now|too|as well|real quick|thanks|thank you)\W*)*$"


def parse(text: str) -> tuple[str, str] | None:
    """("add", "tek") for "add Tek to the call", "can you bring PC into this call", "add the tek bot", "bring in tek",
    "get Tek on the call", "let's add Tek"; ("remove", "tek") for "remove Tek from the call", "drop Tek", "Tek can go",
    "kick PC off the call". Only a profile that can join counts, and only when the words are the whole command:
    "add the tek repo to the list" or "tell Tek to add a card" are not."""
    t = " ".join(str(text or "").split()).strip()
    if not t or len(t) > 160:
        return None
    name = rf"(?:the\s+)?(?:@\s*)?(?P<p>{_name_pattern()}){_BOT}"
    forms = (
        ("add", rf"{_LEAD}(?:add|bring|get|invite|put|pull|loop)\s+{name}\s+(?:in|into|onto|on|to|in\s+on)\s+{_CALL}{_END}"),
        ("add", rf"{_LEAD}(?:add|bring|get|invite|pull|loop)\s+{name}(?:\s+(?:in|on))?{_END}"),
        ("add", rf"{_LEAD}(?:bring|get|pull|loop)\s+in\s+{name}{_END}"),
        ("add", rf"{_LEAD}(?:i\s+want|i'd\s+like|i\s+need)\s+{name}\s+(?:on|in)\s+{_CALL}{_END}"),
        ("remove", rf"{_LEAD}(?:remove|drop|kick|take|let)\s+{name}\s+(?:off|out\s+of|from)\s+{_CALL}{_END}"),
        ("remove", rf"{_LEAD}(?:remove|drop|kick\s+out|dismiss)\s+{name}{_END}"),
        ("remove", rf"{_LEAD}{name}\s*,?\s+(?:you\s+)?can\s+(?:go|leave|drop\s+off|hang\s+up){_END}"),
    )
    for action, form in forms:
        m = re.match(form, t, re.I)
        if m:
            profile = _profile(m.group("p"))
            if profile:
                return action, profile
    return None
