"""hermes-meetings: our own "Claude Meetings" room on stock LiveKit, with z0 (z0live voice + Hermes) as a participant.

Tools (registered by the z0live plugin's register()):
  meetings_start   start livekit-server (loopback or tailnet only) and the room bot
  meetings_invite  a LiveKit Meet join link + token for owner / full / view roles
  meetings_status  server, bot, participants, listeners, the bot's live transcript tail
  meetings_leave   stop the bot and the server

Stdlib only on the Hermes side; the bot runs in `bot_python` (a venv with livekit, aiortc, aiohttp, numpy, av).
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Dict

from .server import Meetings

logger = logging.getLogger(__name__)
TOOLSET = "meetings"
DEFAULTS = {
    "meetings_state_dir": "",
    "meetings_livekit_bin": "/opt/homebrew/bin/livekit-server",
    "meetings_bot_python": "",          # venv python for bot.py; empty = start the room without the bot
    "meetings_voice_url": "http://127.0.0.1:9168",  # the z0live voice adapter (the phone app's endpoint)
    "meetings_voice_bearer_file": "",   # a Hermes dashboard bearer the adapter accepts; empty = owner not bridged
    "meetings_profile": "",             # Hermes profile for the owner's delegations (X-Hermes-Profile)
    "meetings_guest_asr_url": "",
    "meetings_meet_url": "http://127.0.0.1:3000/custom",
}


def make(cfg):
    def meetings() -> Meetings:
        state_dir = cfg("meetings_state_dir")
        if not state_dir:
            from hermes_constants import get_hermes_home
            state_dir = Path(get_hermes_home()) / "meetings"
        return Meetings(state_dir, cfg("meetings_livekit_bin"), os.path.expanduser(cfg("meetings_bot_python") or ""),
                        meet_url=cfg("meetings_meet_url"))

    def bot_args():
        args = ["--voice-url", cfg("meetings_voice_url")]
        if cfg("meetings_voice_bearer_file"):
            args += ["--voice-bearer-file", os.path.expanduser(cfg("meetings_voice_bearer_file"))]
        if cfg("meetings_profile"):
            args += ["--profile", cfg("meetings_profile")]
        if cfg("meetings_guest_asr_url"):
            args += ["--guest-asr-url", cfg("meetings_guest_asr_url")]
        return args

    def guard(fn):
        def handle(args: Dict[str, Any], **_kw: Any) -> str:
            try:
                return json.dumps(fn(args or {}), ensure_ascii=False, default=str)
            except Exception as exc:  # a tool handler never raises
                logger.warning("meetings: %s failed (%s)", fn.__name__, type(exc).__name__)
                return json.dumps({"ok": False, "error": f"{fn.__name__} failed: {type(exc).__name__}"})
        return handle

    def start(args):
        mode = args.get("mode") or "loopback"
        with_bot = args.get("bot", True)
        if with_bot and not all(Path(os.path.expanduser(cfg(key))).is_file()
                                for key in ("meetings_bot_python", "meetings_voice_bearer_file")):
            return {"ok": False, "error": "Configure the bot Python and private voice bearer file first."}
        return meetings().start(mode=mode, room=args.get("room") or "hermes", bot=with_bot,
                                owner="kevin", bot_args=bot_args())

    def invite(args):
        role = args.get("role") or "view"
        if role not in ("full", "view"):
            return {"ok": False, "error": "Only guest roles can be invited by a tool."}
        return meetings().invite(args.get("name", ""), role, int(args.get("ttl_minutes") or 120))

    def status(_args):
        result = meetings().status()
        if result.get("bot"):
            result["bot"].pop("transcript", None)  # meeting speech is not a Hermes/tool instruction
        return result

    def leave(_args):
        return meetings().leave()

    return {"meetings_start": guard(start), "meetings_invite": guard(invite),
            "meetings_status": guard(status), "meetings_leave": guard(leave)}


SCHEMAS = {
    "meetings_start": {
        "name": "meetings_start",
        "description": ("Start Kevin's own meeting room: a stock LiveKit server bound to loopback (default) or the "
                        "tailnet only, plus z0 as a visible participant that answers the owner by voice through "
                        "z0live and Hermes. Other people's audio is context only. Returns the room URL."),
        "parameters": {"type": "object", "properties": {
            "mode": {"type": "string", "enum": ["loopback", "tailnet"]},
            "room": {"type": "string"},
            "bot": {"type": "boolean", "description": "join z0 to the room (default true)"}}},
    },
    "meetings_invite": {
        "name": "meetings_invite",
        "description": ("Create a private LiveKit Meet guest link. Full guests can talk and share but cannot "
                        "command z0. View is the default and cannot publish. Never post the link publicly."),
        "parameters": {"type": "object", "properties": {
            "role": {"type": "string", "enum": ["full", "view"]},
            "name": {"type": "string", "description": "guest name; becomes identity guest-<name>"},
            "ttl_minutes": {"type": "integer"}}},
    },
    "meetings_status": {
        "name": "meetings_status",
        "description": "The meeting room: server and bot state, participants and their tracks, listeners.",
        "parameters": {"type": "object", "properties": {}},
    },
    "meetings_leave": {
        "name": "meetings_leave",
        "description": "End the meeting: z0 leaves, the LiveKit server stops. Nothing keeps running.",
        "parameters": {"type": "object", "properties": {}},
    },
}
EMOJI = {"meetings_start": "📹", "meetings_invite": "🔗", "meetings_status": "📋", "meetings_leave": "👋"}


def register(ctx, cfg) -> None:
    handlers = make(cfg)
    for name, schema in SCHEMAS.items():
        ctx.register_tool(name=name, toolset=TOOLSET, schema=schema, handler=handlers[name],
                          description=schema["description"][:80], emoji=EMOJI[name])
