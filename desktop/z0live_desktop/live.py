"""Broker-owned Codex Frameless Bidi calls (the OMP /live architecture).

The phone keeps direct WebRTC media with OpenAI. The broker performs signaling with the
host's Codex OAuth login, owns the call's sideband WebSocket, and answers the model's
native client delegations through Hermes (see delegation.py). No audio is proxied.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
import re
import threading
import time
import urllib.error
import urllib.request
import uuid

from . import bidi_protocol as bp

LOG = logging.getLogger("z0live.live")
PERSONA_VERSION = "z0live.bidi.v2"
TRANSPORTS = ("bidi", "realtime")
AUDIO_TYPES = (b"session.input_audio.append", b"session.output_audio.delta")
# A bare action acknowledgement with no delegation behind it is a missed delegation.
ACK_ONLY = re.compile(
    r"^\W*(?:ok(?:ay)?|sure|alright|got it|yes)?[\s,.!]*(?:on it|checking(?: (?:now|that|on that))?|"
    r"opening(?: it)?(?: now)?|let me (?:check|look|open|see)|one (?:sec|second|moment)|"
    r"i'?ll (?:check|look|open)(?: that| it)?)(?: (?:now|real quick|quickly))?\W*$",
    re.I,
)
RECOVERY_GRACE_SECONDS = 1.5


def transport_setting() -> str:
    """Broker setting: `bidi` (default) lets capable clients use Frameless Bidi.

    `realtime` forces the legacy gpt-realtime mint for every client. Clients that do not
    advertise `bidi` (every APK before this change) always get gpt-realtime.
    """
    value = os.environ.get("Z0LIVE_LIVE_TRANSPORT", "bidi").strip().lower()
    return value if value in TRANSPORTS else "bidi"


def choose_transport(requested) -> str:
    if requested is None:
        return "realtime"
    if not isinstance(requested, list) or not all(isinstance(t, str) for t in requested):
        raise ValueError("transports must be an array of strings")
    if "bidi" in requested and transport_setting() == "bidi":
        return "bidi"
    if "realtime" in requested or not requested:
        return "realtime"
    raise ValueError("no supported transport; broker offers bidi or realtime")


def voice() -> str:
    value = os.environ.get("Z0LIVE_LIVE_VOICE", bp.DEFAULT_VOICE)
    return value if value in bp.VOICES else bp.DEFAULT_VOICE


def codex_access(path: str | Path | None = None) -> str:
    path = Path(
        path or os.environ.get("Z0LIVE_CODEX_AUTH", str(Path.home() / ".codex/auth.json"))
    )
    try:
        token = json.loads(path.read_text())["tokens"]["access_token"]
    except (OSError, KeyError, TypeError, ValueError):
        raise RuntimeError("ChatGPT OAuth unavailable; refresh host Codex login") from None
    if not isinstance(token, str) or not bp.account_id(token):
        raise RuntimeError("ChatGPT OAuth unavailable; refresh host Codex login")
    return token


def persona(tools: list[dict]) -> str:
    """One-assistant persona for gpt-live-1-codex, written for spoken use."""
    phone = [
        f"{t['name']}: {str(t.get('description', ''))[:140]}"
        for t in tools
        if isinstance(t, dict) and isinstance(t.get("name"), str)
    ]
    phone_text = (
        "\n".join("- " + line for line in phone)
        if phone
        else "- none in this session (phone control is not wired up on this client)"
    )
    return f"""You are z0live, Kevin's voice assistant. You and your execution backend are one assistant: you talk with Kevin, the backend does the work on his computer and his phone, and its results are your own results.

<critical>
- You have no direct access to the phone, the computer or the internet. Every action and every lookup MUST go through a client delegation.
- MUST create a client delegation for every phone action (open an app or settings, media, timers, calls, texts, tapping, typing), all computer and project work, anything about Kevin's files, repos, agents, notes or history, and anything current.
- NEVER say an action happened, an app is open, or something was checked unless an "Agent Final Message" in your context says so.
</critical>

<talk>
- Speak naturally and briefly: usually one or two short sentences. No markdown, lists, code, file paths or URLs unless Kevin asks.
- Answer greetings, small talk and general knowledge you are sure of yourself, right away.
- If you only hear noise, silence or half a word, say nothing.
- Never talk over Kevin. If he starts speaking, stop and listen.
</talk>

<act>
- Bias to action. When Kevin asks you to do, check, find, open, change, remember or look up anything on his phone, his computer, his projects, his agents' work, the web, or anything current, create a client delegation immediately with the complete plain-language request plus any needed context from the conversation. Do not attempt the work yourself and do not ask permission to do what he asked.
- You cannot touch the phone or the computer yourself. Every phone action (open an app, settings, media, timers, tapping or typing) is a delegation too. Never say an app is open or an action happened until an "Agent Final Message" confirms it.
- Ask one short question only when a request truly cannot be acted on as said.
- A new request while earlier work is running is a new delegation; the backend folds it into the same work.
- If Kevin says never mind, stop or cancel, or clearly drops a topic, let it go and do not bring it back.
</act>

<results>
- When you delegate you may say a couple of words such as "On it." (never "done" or "opened"). Then stay quiet until there is a result. Never narrate waiting. Never say "still working", "hang on", "no update yet", or fill silence.
- Delegation context on the commentary channel is your own silent progress. Never read it out. Use it only if Kevin asks what you are doing.
- Context that begins with "Agent Final Message": is the finished result. Tell Kevin the useful part in your own words as your own work, once. Never mention that label, delegations, a backend, Hermes, agents, tools or protocols.
- Never claim something is done, checked or changed before the result says so. If the result says it failed, needs his confirmation, or was not confirmed, say that plainly.
</results>

<capabilities>
Through delegation you can: work on Kevin's computer (repositories, code, files, terminal, tests, web search, notes, memory, what his agents are doing and have done), and run these phone actions on his phone:
{phone_text}
Anything else is not wired up yet; say exactly that. Never invent a privacy or safety reason for a missing ability. Sending messages or placing calls only opens a composer for Kevin to confirm.
</capabilities>"""


class SignalingError(RuntimeError):
    pass


def signal(offer: str, instructions: str, voice_name: str, access: str,
           scoped_session: str, realtime_session: str, opener=None, timeout=20.0):
    """POST the phone's SDP offer to Codex; return (answer_sdp, call_id)."""
    if not isinstance(offer, str) or not offer.startswith("v=0") or len(offer) > 20000:
        raise ValueError("sdp must be an SDP offer of at most 20000 characters")
    body = json.dumps(
        {"sdp": offer, "session": bp.session_payload(instructions, voice_name)}
    ).encode()
    request = urllib.request.Request(
        bp.SIGNALING_URL,
        data=body,
        method="POST",
        headers={
            **bp.headers(access, scoped_session, realtime_session),
            "Accept": "*/*",
            "Content-Type": "application/json",
        },
    )
    open_ = opener or urllib.request.urlopen
    try:
        with open_(request, timeout=timeout) as response:
            answer = response.read(200_001).decode()
            location = response.headers.get("Location")
    except urllib.error.HTTPError as exc:
        status = exc.code
        exc.close()
        raise SignalingError(f"live signaling HTTP {status}") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise SignalingError("live signaling unavailable") from None
    call_id = bp.parse_call_id(location)
    if not answer.strip() or not answer.startswith("v=0"):
        raise SignalingError("live signaling returned no SDP answer")
    if not call_id:
        raise SignalingError("live signaling returned no call id")
    return answer, call_id


def websocket_connect(url, headers):
    """Real sideband connector (lazy import keeps the broker stdlib-only otherwise)."""
    from websockets.sync.client import connect

    return connect(url, additional_headers=headers, open_timeout=15, max_size=2**22,
                   ping_interval=20, ping_timeout=20)


class LiveCall:
    """One Frameless Bidi call: sideband reader, serialized sender, event fan-out."""

    def __init__(self, store, sid, device, call_id, access, scoped_session,
                 realtime_session, delegator, connector=websocket_connect,
                 attempts=5, sleep=time.sleep):
        self.store, self.sid, self.device, self.call_id = store, sid, device, call_id
        self.access, self.scoped, self.realtime = access, scoped_session, realtime_session
        self.delegator = delegator
        self.connector, self.attempts, self.sleep = connector, attempts, sleep
        self.ws = None
        self.lock = threading.Lock()
        self.closed = threading.Event()
        self.opened = threading.Event()
        self.thread = None
        self.stats = {"events": 0, "audio_frames": 0, "delegations": 0, "errors": [],
                      "unknown": {}, "unreadable": []}
        self.marks: dict[str, float] = {}
        self.last_user = ""
        self.last_user_turn = ""
        self.delegated_turns: set[str] = set()
        self.delegated_since_user = False
        self.recovery_timer = None

    def start(self):
        self.thread = threading.Thread(target=self.run, name="live-" + self.sid[:8], daemon=True)
        self.thread.start()
        return self

    def connect(self):
        failure = None
        for attempt in range(self.attempts):
            if self.closed.is_set():
                return False
            try:
                self.ws = self.connector(
                    bp.sideband_url(self.call_id),
                    bp.headers(self.access, self.scoped, self.realtime),
                )
                self.opened.set()
                return True
            except Exception as exc:  # network/handshake: retry with backoff like OMP
                failure = type(exc).__name__
                if attempt + 1 < self.attempts:
                    self.sleep(0.2 * 2**attempt)
        LOG.warning("sideband failed session=%s error=%s", self.sid, failure)
        self.stats["errors"].append("sideband_connect:" + str(failure))
        return False

    def run(self):
        try:
            if not self.connect():
                return
            LOG.info("sideband open session=%s", self.sid)
            for raw in self.ws:
                if self.closed.is_set():
                    break
                self.receive(raw)
        except Exception as exc:
            if not self.closed.is_set():
                LOG.warning("sideband ended session=%s error=%s", self.sid, type(exc).__name__)
        finally:
            self.closed.set()
            self.delegator.call_closed(self)

    def receive(self, raw):
        head = raw[:80].encode() if isinstance(raw, str) else bytes(raw[:80])
        if any(kind in head for kind in AUDIO_TYPES):
            self.stats["audio_frames"] += 1
            return
        event = bp.parse_event(raw)
        if not event:
            if b"delegation" in head:
                # A delegation we cannot read would leave the voice waiting silently: record it.
                self.stats["unreadable"].append(str(raw)[:400])
                LOG.warning("unreadable delegation event session=%s", self.sid)
            return
        self.stats["events"] += 1
        kind = event["type"]
        if kind == "unknown":
            wire = event["wire_type"]
            self.stats["unknown"][wire] = self.stats["unknown"].get(wire, 0) + 1
        if kind == "delegation.created":
            # Delegations can arrive before the user's turn.done: key them by user turn id.
            self.delegated_since_user = True
            if event["user_turn_id"]:
                self.delegated_turns.add(event["user_turn_id"])
            if event["text"]:
                self.stats["delegations"] += 1
                self.delegator.delegate(self, event["id"], event["text"])
            else:
                self.stats["unreadable"].append(str(raw)[:400])
                LOG.warning("empty delegation session=%s", self.sid)
        elif kind == "turn.done":
            transcript = event["transcript"].strip()
            if transcript:
                self.record(
                    "user_transcript" if event["role"] == "user" else "assistant_transcript",
                    transcript,
                    {"source": "bidi.turn.done"},
                )
                if event["role"] == "user":
                    self.last_user, self.last_user_turn = transcript, event["id"]
                    self.delegated_since_user = event["id"] in self.delegated_turns
                    self.delegator.user_turn(self, transcript)
                elif ACK_ONLY.match(transcript) and self.last_user and not self.delegated():
                    self.schedule_recovery(self.last_user)
        elif kind == "error":
            self.stats["errors"].append(event["message"][:200])
            LOG.warning("live error session=%s: %s", self.sid, event["message"][:200])

    def delegated(self):
        return self.delegated_since_user or self.last_user_turn in self.delegated_turns

    def schedule_recovery(self, request):
        """The voice acknowledged an action but created no delegation: run it anyway."""
        def recover():
            if self.closed.is_set() or self.delegated():
                return
            self.delegated_since_user = True
            self.stats["recovered"] = self.stats.get("recovered", 0) + 1
            LOG.warning("missed delegation recovered session=%s", self.sid)
            self.delegator.delegate(self, "recovered-" + uuid.uuid4().hex, request)

        self.recovery_timer = threading.Timer(RECOVERY_GRACE_SECONDS, recover)
        self.recovery_timer.daemon = True
        self.recovery_timer.start()

    def record(self, kind, text, data=None):
        try:
            self.store.add_events(
                self.device,
                [{"session_id": self.sid, "type": kind, "text": text[:16000],
                  "data": {**(data or {}), "event_id": "bidi-" + uuid.uuid4().hex}}],
            )
        except (ValueError, LookupError):
            pass  # ended session: transcripts are best-effort history

    def send(self, message: dict) -> bool:
        with self.lock:
            if self.closed.is_set() or self.ws is None:
                return False
            try:
                self.ws.send(json.dumps(message, ensure_ascii=False))
                return True
            except Exception as exc:
                LOG.warning("sideband send failed session=%s error=%s", self.sid, type(exc).__name__)
                return False

    def append(self, delegation_id, text, channel=None) -> bool:
        ok = True
        for part in bp.chunk(text):
            # Recovered work has no provider delegation item: it goes to the session context.
            message = (
                bp.session_append(part, channel)
                if delegation_id.startswith("recovered-")
                else bp.delegation_append(delegation_id, part, channel)
            )
            ok = self.send(message) and ok
        return ok

    def close(self):
        if self.recovery_timer is not None:
            self.recovery_timer.cancel()
        if self.closed.is_set():
            return
        self.send(bp.session_close())
        self.closed.set()
        with self.lock:
            ws, self.ws = self.ws, None
        if ws is not None:
            try:
                ws.close()
            except Exception:
                pass


class LiveCalls:
    """Registry of active broker-owned calls, keyed by broker session id."""

    def __init__(self, store, delegator=None, connector=websocket_connect, signaler=signal,
                 access=codex_access):
        self.store = store
        self.calls: dict[str, LiveCall] = {}
        self.lock = threading.Lock()
        if delegator is None:
            from .delegation import Delegator

            delegator = Delegator(store)
        self.delegator = delegator
        self.connector, self.signaler, self.access = connector, signaler, access

    def open(self, device, body):
        if not isinstance(body, dict):
            raise ValueError("expected object")
        sid = body.get("session_id")
        offer = body.get("sdp")
        with self.store.connect() as db:
            row = self.store.owned(db, sid, device)
            if row["ended"]:
                raise ValueError("session has ended")
            if row["transport"] != "bidi":
                raise ValueError("session was not minted for bidi")
            tools = json.loads(row["tools"])
        with self.lock:
            if sid in self.calls and not self.calls[sid].closed.is_set():
                raise ValueError("session already has a live call")
        access = self.access()
        scoped, realtime = str(uuid.uuid4()), str(uuid.uuid4())
        started = time.monotonic()
        answer, call_id = self.signaler(offer, persona(tools), voice(), access, scoped, realtime)
        call = LiveCall(self.store, sid, device, call_id, access, scoped, realtime,
                        self.delegator, connector=self.connector)
        call.marks["signaled_ms"] = round((time.monotonic() - started) * 1000)
        with self.lock:
            self.calls[sid] = call
        call.start()
        LOG.info("live call session=%s signaled_ms=%s", sid, call.marks["signaled_ms"])
        return {"sdp": answer, "call_id": call_id, "model": bp.LIVE_MODEL,
                "voice": voice(), "instructions_version": PERSONA_VERSION}

    def get(self, sid):
        with self.lock:
            return self.calls.get(sid)

    def close(self, sid):
        with self.lock:
            call = self.calls.pop(sid, None)
        if call:
            call.close()
        return call is not None

    def close_all(self):
        with self.lock:
            calls, self.calls = list(self.calls.values()), {}
        for call in calls:
            call.close()
