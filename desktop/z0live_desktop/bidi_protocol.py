"""Codex Frameless Bidi (gpt-live-1-codex) wire contract, mirrored from OMP /live.

Pure functions only: payloads, event parsing, context chunking and headers.
Reference: oh-my-pi packages/coding-agent/src/live/{protocol,transport}.ts.
"""

from __future__ import annotations

import base64
import json
import re
from typing import Any

LIVE_MODEL = "gpt-live-1-codex"
CODEX_BASE_URL = "https://chatgpt.com/backend-api"
SIGNALING_URL = (
    CODEX_BASE_URL + "/codex/realtime/calls?intent=quicksilver&architecture=avas"
)
SIDEBAND_URL = "wss://api.openai.com/v1/live/{call_id}"
CODEX_CLIENT_VERSION = "0.153.0"
ORIGINATOR = "Codex Desktop"
CONTEXT_CHUNK_BYTES = 500
CHANNELS = {"speakable", "commentary"}
VOICES = {"arbor", "breeze", "cove", "ember", "juniper", "maple", "sol", "spruce", "vale"}
DEFAULT_VOICE = "sol"
CALL_ID = re.compile(r"^rtc_[\w-]+$")
FINAL_LABEL = '"Agent Final Message":'


def session_payload(instructions: str, voice: str = DEFAULT_VOICE) -> dict:
    if voice not in VOICES:
        raise ValueError("unsupported live voice")
    return {
        "model": LIVE_MODEL,
        "instructions": instructions,
        "audio": {"output": {"voice": voice}},
        "delegation": {"type": "client"},
    }


def parse_call_id(location: str | None) -> str | None:
    if not location:
        return None
    path = location.split("?", 1)[0]
    return next((s for s in path.split("/") if CALL_ID.fullmatch(s)), None)


def sideband_url(call_id: str) -> str:
    if not CALL_ID.fullmatch(call_id or ""):
        raise ValueError("invalid live call id")
    return SIDEBAND_URL.format(call_id=call_id)


def account_id(access_token: str) -> str | None:
    try:
        claims = json.loads(
            base64.urlsafe_b64decode(access_token.split(".")[1] + "===")
        )
        return claims["https://api.openai.com/auth"]["chatgpt_account_id"]
    except (IndexError, KeyError, TypeError, ValueError):
        return None


def headers(access_token: str, scoped_session: str, realtime_session: str) -> dict:
    """Codex OAuth headers shared by signaling and the sideband WebSocket."""
    result = {
        "Authorization": "Bearer " + access_token,
        "OpenAI-Alpha": "quicksilver=v2",
        "User-Agent": "Codex Desktop/" + CODEX_CLIENT_VERSION,
        "x-session-id": realtime_session,
        "originator": ORIGINATOR,
        "version": CODEX_CLIENT_VERSION,
        "session-id": scoped_session,
        "thread-id": scoped_session,
    }
    account = account_id(access_token)
    if account:
        result["chatgpt-account-id"] = account
    return result


def _text_content(text: str) -> list[dict]:
    return [{"type": "input_text", "text": text}]


def delegation_append(delegation_id: str, text: str, channel: str | None = None) -> dict:
    if channel is not None and channel not in CHANNELS:
        raise ValueError("invalid live context channel")
    return {
        "type": "delegation.context.append",
        "delegation_item_id": delegation_id,
        **({"channel": channel} if channel else {}),
        "content": _text_content(text),
    }


def session_append(text: str, channel: str | None = None) -> dict:
    if channel is not None and channel not in CHANNELS:
        raise ValueError("invalid live context channel")
    return {
        "type": "session.context.append",
        **({"channel": channel} if channel else {}),
        "content": _text_content(text),
    }


def session_close() -> dict:
    return {"type": "session.close"}


def final_message(text: str) -> str:
    return FINAL_LABEL + "\n\n" + text.strip()


def chunk(text: str, limit: int = CONTEXT_CHUNK_BYTES) -> list[str]:
    """Split on code points so every chunk is at most `limit` UTF-8 bytes."""
    if not text:
        return [""]
    chunks, current, size = [], [], 0
    for char in text:
        width = len(char.encode("utf-8"))
        if size + width > limit:
            chunks.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    chunks.append("".join(current))
    return chunks


def parse_event(payload: Any) -> dict | None:
    """Normalize one server event; unknown wire types are kept as `unknown`."""
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except ValueError:
            return None
    if not isinstance(payload, dict) or not isinstance(payload.get("type"), str):
        return None
    kind = payload["type"]
    if kind in {"session.started", "session.updated"}:
        session = payload.get("session")
        if not isinstance(session, dict) or not isinstance(session.get("id"), str):
            return None
        return {"type": kind, "session": {"id": session["id"]}}
    if kind == "output_audio.delta":
        return {"type": kind} if isinstance(payload.get("audio"), str) else None
    if kind in {"input_transcript.added", "output_transcript.added"}:
        item = payload.get("item")
        if not isinstance(item, dict) or not isinstance(item.get("text"), str):
            return None
        return {"type": kind, "text": item["text"]}
    if kind == "turn.done":
        turn = payload.get("turn")
        if (
            not isinstance(turn, dict)
            or turn.get("role") not in {"user", "assistant"}
            or not isinstance(turn.get("transcript"), str)
        ):
            return None
        return {"type": kind, "role": turn["role"], "transcript": turn["transcript"]}
    if kind == "delegation.created":
        item = payload.get("item")
        if (
            not isinstance(item, dict)
            or item.get("type") != "delegation"
            or item.get("target") != "client"
            or not isinstance(item.get("id"), str)
            or not isinstance(item.get("content"), list)
        ):
            return None
        text = "\n".join(
            c["text"]
            for c in item["content"]
            if isinstance(c, dict)
            and c.get("type") == "input_text"
            and isinstance(c.get("text"), str)
        ).strip()
        return {"type": kind, "id": item["id"], "text": text}
    if kind == "error":
        error = payload.get("error")
        message = payload.get("message")
        if not isinstance(message, str):
            if isinstance(error, dict) and isinstance(error.get("message"), str):
                message = error["message"]
            elif error is not None:
                message = json.dumps(error)[:500]
            else:
                return None
        return {"type": "error", "message": message[:500]}
    return {"type": "unknown", "wire_type": kind[:100]}
