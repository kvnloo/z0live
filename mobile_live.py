"""Subscription voice compatibility adapter for the official Hermes mobile client.

The existing dashboard remains the authentication authority. No account token,
audio, or transcript is returned over HTTP or persisted by this adapter.
Codex signaling contract: z0live/OMP Frameless Bidi (client delegation only).
"""
from __future__ import annotations

import asyncio
import base64
from collections import Counter, defaultdict, deque
import hashlib
import importlib.util
import json
import re
import logging
import os
from pathlib import Path
import selectors
import shutil
import subprocess
import sys
import threading
import time
import uuid

from aiohttp import ClientSession, ClientTimeout, web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration
from aiortc.mediastreams import MediaStreamError
from aiortc import MediaStreamTrack

LOG = logging.getLogger("hermes.mobile.subscription_voice")
UPSTREAM = "http://127.0.0.1:9119"
SIGNALING = "https://chatgpt.com/backend-api/codex/realtime/calls?intent=quicksilver&architecture=avas"
MODEL = "gpt-live-1-codex"
MAX_CALL_SECONDS = 1800
MAX_CALLS = 2
# A stop/restart drains: open calls keep running until they end (bounded), new ones are refused.
# Agents restarted this adapter mid-call twice on 2026-11-06 ("GPT-Live ended: service_shutdown").
DRAIN_SECONDS = int(os.environ.get("HERMES_MOBILE_VOICE_DRAIN", str(MAX_CALL_SECONDS)))
# A fast-lane "I found nothing" is not a first answer; saying it then being contradicted is worse than silence.
UNHELPFUL = re.compile(r"^\W*(not found|nothing found|no (local )?(evidence|results|record)|i (couldn.?t|could not|can.?t) find)", re.I)
PERSONA = """You are Hermes, speaking with the user in the official Hermes mobile app.
Be natural, brief and direct. Do not speak until the user speaks. Listen while speaking
and stop immediately when interrupted. Handle greetings and ordinary small talk yourself.
You have no tools. For facts, lookups or actions, delegate the complete request to the client.
The client is the user's existing Hermes chat with its chosen model, tools and approvals.
Do not claim work happened until the client returns its actual result. Progress arriving on
the commentary channel is silent context, not something to read aloud. Present an Agent
Final Message as your own concise answer. Never narrate routing or backend plumbing.
Helper notes may arrive as silent context: a verified note or a correction about something
already answered is true; mention a correction briefly ("Actually, ...").
"""


REFRESH_LOCK = threading.Lock()

# Hermes Android 1.0.0 (versionCode 21) compiles Regex("(?U)\\s+") when it receives
# session.delegation.created; Android's ICU engine rejects (?U), so every delegation
# kills the call ("Syntax error in regexp pattern near index 3"). Until the app is
# fixed, run delegated requests on this host with the same Hermes profile and keep
# delegation events away from the phone. HERMES_MOBILE_VOICE_CLIENT_DELEGATION=1
# restores the original client delegation.
HOST_DELEGATION = os.environ.get("HERMES_MOBILE_VOICE_CLIENT_DELEGATION", "") != "1"
HERMES_BIN = os.environ.get("HERMES_BIN", str(Path.home() / ".hermes/hermes-agent/venv/bin/hermes"))
DELEGATION_TIMEOUT_SECONDS = 600

# z0live lanes relay (z0live_desktop/mobile_relay.py in the served z0live checkout): finals and
# partials go to the broker's lanes, a delegation gets the lane answer at once, and Hermes' slow
# answer is spoken only if it changes it. Off without the relay token file or with
# HERMES_MOBILE_VOICE_LANES=0; then this adapter behaves exactly as before.
Z0LIVE_DESKTOP = os.environ.get("Z0LIVE_DESKTOP", "/mnt/zer0models/workspace/zer0/products/z0live/desktop")
LaneRelay = covered = None
if os.environ.get("HERMES_MOBILE_VOICE_LANES", "1") != "0":
    sys.path.insert(0, Z0LIVE_DESKTOP)
    try:
        from z0live_desktop.mobile_relay import LaneRelay, covered
    except ImportError as exc:  # an older z0live checkout: no lanes, same adapter
        logging.getLogger("hermes.mobile.subscription_voice").warning("lanes relay unavailable: %s", exc)


def profile_name(raw) -> str:
    """Only accept an existing profile name (never a path or flag)."""
    name = str(raw or "").strip()
    if not name or name == "default":
        return ""
    if not name.replace("-", "").replace("_", "").isalnum():
        return ""
    return name if (Path.home() / ".hermes/profiles" / name).is_dir() else ""


# Voice turns continue one Hermes session per profile (across calls) until it idles this long,
# so "yes" or "do it" lands in the conversation it answers. One turn at a time per session.
VOICE_SESSION_IDLE_SECONDS = int(os.environ.get("HERMES_MOBILE_VOICE_SESSION_IDLE", "1800"))
VOICE_SESSIONS = {}  # profile -> (session id, monotonic time of last turn)
VOICE_LOCKS = defaultdict(threading.Lock)


def _hermes_once(request: str, profile: str, resume: str, own_lane: bool = False):
    args = [HERMES_BIN] + (["-p", profile] if profile else []) + [
        # Explicit --source: a source inherited from a parent Hermes (HERMES_SESSION_SOURCE=kanban)
        # makes the run fail before any model call ("couldn't start the model connection: invalid_params").
        "chat", "-q", request, "--oneshot", "-Q", "--source", "oneshot"] + (["--resume", resume] if resume else [])
    # The adapter already runs this turn's liaison: the driver must not start a second one.
    env = {**os.environ, "FAST_LANE_OFF": "1"} if own_lane else None
    proc = subprocess.run(args, capture_output=True, text=True, timeout=DELEGATION_TIMEOUT_SECONDS,
                          cwd=str(Path.home()), stdin=subprocess.DEVNULL, env=env)
    # `hermes chat -Q` prints the session_id line on stderr, not stdout.
    sid = next((ln.split(":", 1)[1].strip() for ln in (proc.stdout + "\n" + (proc.stderr or "")).splitlines()
                if ln.startswith("session_id:")), "")
    lines = [ln for ln in proc.stdout.splitlines() if not ln.startswith("session_id:")]
    text = "\n".join(lines).strip()
    if proc.returncode != 0 or not text:
        raise RuntimeError("Hermes did not return an answer (exit %s)" % proc.returncode)
    return text, sid


def fast_lane_spool(request: str, profile: str) -> Path:
    """Where the Hermes fast-lane plugin drops its quick answer to this exact request (same key as the plugin)."""
    key = hashlib.sha1(" ".join(request.split())[:2000].encode()).hexdigest()
    return Path.home() / ".hermes/profiles" / (profile or "default") / "cache/fast-lane/answers" / f"{key}.json"


# Voice/driver split (owner design 2026-10-19): the realtime voice only talks. A fast liaison (the
# fast-lane plugin's engine, Luna via fx) starts the moment the voice delegates and its answer is spoken
# first; the persistent Hermes driver (Opus with its memory and tools) answers in parallel and corrects.
# The adapter owns the liaison for every profile: the plugin is installed in only some profiles (the
# app's `ayo` has none) and would start only after the driver's cold start.
# HERMES_MOBILE_VOICE_LIAISON=0 restores the plugin-spool-only behaviour.
LIAISON_PLUGIN = Path(os.environ.get("HERMES_MOBILE_VOICE_LIAISON_PLUGIN",
                                     str(Path.home() / ".hermes/profiles/clean/plugins/fast-lane/__init__.py")))
_LIAISON = []


def liaison():
    """The fast-lane plugin module (loaded once), or None when off or unloadable."""
    if os.environ.get("HERMES_MOBILE_VOICE_LIAISON", "1") == "0":
        return None
    if not _LIAISON:
        mod = None
        try:
            spec = importlib.util.spec_from_file_location("voice_liaison_fast_lane", LIAISON_PLUGIN)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
        except Exception as exc:  # noqa: BLE001 - no liaison; the driver still answers
            LOG.warning("voice liaison unavailable: %s", type(exc).__name__)
            mod = None
        _LIAISON.append(mod)
    return _LIAISON[0]


def run_hermes(request: str, profile: str, own_lane: bool = False) -> str:
    with VOICE_LOCKS[profile]:
        sid, last = VOICE_SESSIONS.get(profile, ("", 0.0))
        resume = sid if sid and time.monotonic() - last < VOICE_SESSION_IDLE_SECONDS else ""
        try:
            text, new_sid = _hermes_once(request, profile, resume, own_lane)
        except RuntimeError:
            if not resume:
                raise
            LOG.warning("voice session resume failed; starting a fresh session")
            text, new_sid = _hermes_once(request, profile, "", own_lane)
        VOICE_SESSIONS[profile] = (new_sid or resume, time.monotonic())
        return text


def refresh_codex_login():
    """Let the installed Codex auth owner refresh itself; never write its token file."""
    binary = shutil.which("codex") or str(Path.home() / ".local/bin/codex")
    env = os.environ.copy()
    for key in ("OPENAI_API_KEY", "CODEX_API_KEY"):
        env.pop(key, None)
    proc = subprocess.Popen([binary, "app-server", "--listen", "stdio://"],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, env=env)
    selector = selectors.DefaultSelector()
    selector.register(proc.stdout, selectors.EVENT_READ)
    deadline, buffer = time.monotonic() + 18, b""
    def send(rid, method, params):
        proc.stdin.write((json.dumps({"id": rid, "method": method, "params": params}) + "\n").encode())
        proc.stdin.flush()
    try:
        send(1, "initialize", {"clientInfo": {"name": "hermes-mobile-voice-auth", "version": "1"}})
        while time.monotonic() < deadline:
            if not selector.select(timeout=max(0, deadline-time.monotonic())):
                break
            part = os.read(proc.stdout.fileno(), 65536)
            if not part:
                break
            buffer += part
            if len(buffer) > 1048576:
                break
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                try:
                    response = json.loads(line)
                except ValueError:
                    continue
                if response.get("error"):
                    raise RuntimeError("Codex could not refresh the subscription login")
                if response.get("id") == 1:
                    proc.stdin.write(b'{"method":"initialized"}\n');proc.stdin.flush()
                    send(2, "account/read", {"refreshToken": True})
                elif response.get("id") == 2:
                    account = (response.get("result") or {}).get("account") or {}
                    if account.get("type") != "chatgpt":
                        raise RuntimeError("Codex needs a ChatGPT subscription login")
                    return
        raise RuntimeError("Codex subscription refresh timed out")
    finally:
        selector.close()
        proc.terminate()
        try:
            proc.wait(timeout=3)
        except subprocess.TimeoutExpired:
            proc.kill();proc.wait(timeout=3)
        proc.stdin.close();proc.stdout.close()


def codex_headers() -> dict[str, str]:
    # Read-only except for refresh performed by Codex's own account/read API.
    home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    def read():
        data = json.loads((home / "auth.json").read_text())
        if data.get("auth_mode") != "chatgpt":
            raise ValueError("not subscription auth")
        token = data["tokens"]["access_token"]
        claims = json.loads(base64.urlsafe_b64decode(token.split(".")[1] + "==="))
        account = claims["https://api.openai.com/auth"]["chatgpt_account_id"]
        return token, claims, account
    try:
        token, claims, account = read()
        if claims.get("exp", 0) < time.time() + 60:
            with REFRESH_LOCK:
                token, claims, account = read()
                if claims.get("exp", 0) < time.time() + 60:
                    refresh_codex_login()
                    token, claims, account = read()
            if claims.get("exp", 0) < time.time() + 60:
                raise ValueError("expired login")
    except (OSError, ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError("The host Codex subscription login needs refreshing") from None
    return {
        "Authorization": "Bearer " + token,
        "chatgpt-account-id": account,
        "OpenAI-Alpha": "quicksilver=v2",
        "User-Agent": "Codex Desktop/0.153.0",
        "originator": "Codex Desktop",
        "version": "0.153.0",
        "x-session-id": str(uuid.uuid4()),
        "session-id": str(uuid.uuid4()),
    }


def chunks(text: str, limit: int = 500) -> list[str]:
    result, current, size = [], "", 0
    for char in text:
        n = len(char.encode("utf-8"))
        if size + n > limit:
            result.append(current)
            current, size = "", 0
        current += char
        size += n
    if current:
        result.append(current)
    return result


class Protocol:
    """Pure, bounded translation between subscription and mobile voice events."""
    def __init__(self, host_delegation: bool = False):
        self.host_delegation = host_delegation
        self.pending_host = []  # (delegation_id, request) awaiting a host run
        self.turn_text = {"user": "", "assistant": ""}
        self.last_user = ""
        self.last_delegation = ""
        self.seen = deque(maxlen=256)
        self.started_at = time.monotonic()

    def transcript(self, role: str, text: str) -> dict:
        return {"type": f"session.{ 'input' if role == 'user' else 'output' }_transcript.delta",
                "delta": text, "start_ms": int((time.monotonic() - self.started_at) * 1000),
                "end_ms": int((time.monotonic() - self.started_at) * 1000)}

    def from_provider(self, event: dict) -> list[dict]:
        kind = event.get("type")
        if kind in ("session.started", "session.closed", "error"):
            if kind == "error" and not isinstance(event.get("error"), dict):
                return [{"type": "error", "error": {"message": str(event.get("message") or "Voice provider error")[:500]}}]
            return [event]
        if kind in ("input_transcript.added", "output_transcript.added"):
            role = "user" if kind.startswith("input") else "assistant"
            text = (event.get("item") or {}).get("text")
            if not isinstance(text, str) or not text:
                return []
            self.turn_text[role] = (self.turn_text[role] + text)[-16000:]
            if role == "user":
                self.last_user = self.turn_text[role]
            return [self.transcript(role, text)]
        if kind == "turn.done":
            turn = event.get("turn") or {}
            role, text = turn.get("role"), turn.get("transcript", "")
            if role not in self.turn_text or not isinstance(text, str):
                return []
            prior = self.turn_text[role]
            self.turn_text[role] = ""
            if role == "user":
                self.last_user = text
            remainder = text[len(prior):] if text.startswith(prior) else (text if not prior else "")
            return [self.transcript(role, remainder)] if remainder else []
        if kind == "delegation.created":
            item = event.get("item") or {}
            did = item.get("id")
            if item.get("target") != "client" or not isinstance(did, str) or not did or did in self.seen:
                return []
            self.seen.append(did)
            self.last_delegation = did
            request = "\n".join(c.get("text", "") for c in item.get("content", [])
                                 if isinstance(c, dict) and isinstance(c.get("text"), str)).strip()
            result = []
            if request and not self.last_user:
                result.append(self.transcript("user", request))
                self.last_user = request
            if self.host_delegation:
                self.pending_host.append((did, request or self.last_user))
                return result
            result.append({"type": "session.delegation.created", "delegation": {
                "id": did, "type": "delegation", "target": "client"}})
            return result
        return []

    def from_mobile(self, event: dict) -> list[dict]:
        kind = event.get("type")
        if kind == "session.close":
            return [{"type": "session.close"}]
        if kind not in ("session.commentary.append", "session.thinking.append", "session.instructions.append"):
            return []
        text = event.get("content")
        if not isinstance(text, str) or not text.strip():
            return []
        did = event.get("delegation_id")
        if did and did not in self.seen:
            return []  # Never route a stale/foreign chat answer into this call.
        channel = "commentary" if kind != "session.commentary.append" else "speakable"
        if channel == "speakable":
            text = '"Agent Final Message":\n\n' + text
        result = []
        for part in chunks(text[:16000]):
            msg = {"type": "delegation.context.append" if did else "session.context.append",
                   "channel": channel, "content": [{"type": "input_text", "text": part}]}
            if did:
                msg["delegation_item_id"] = did
            result.append(msg)
        return result


class AudioBridge(MediaStreamTrack):
    kind = "audio"

    def __init__(self, source, call, direction):
        super().__init__()
        self.source, self.call, self.direction = source, call, direction

    async def recv(self):
        frame = await self.source.recv()
        self.call.counts[self.direction + "_frames"] += 1
        if self.direction == "input" and self.call.muted:
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
        return frame


class Call:
    def __init__(self, manager, profile: str = ""):
        self.manager = manager
        self.profile = profile
        self.id = "hermes-subscription-" + uuid.uuid4().hex
        self.mobile = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.provider = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.protocol = Protocol(host_delegation=HOST_DELEGATION)
        self.counts = Counter()
        self.created = time.monotonic()
        self.closed = False
        self.muted = False
        self.provider_ready = False
        self.mobile_dc = None
        self.provider_dc = self.provider.createDataChannel("oai-events")
        self.to_mobile = deque(maxlen=256)
        self.to_provider = deque(maxlen=256)
        self.tasks = set()
        self.relay = LaneRelay.from_env() if (LaneRelay and HOST_DELEGATION) else None
        self.lane_said = {}  # delegation id -> (lane answer, entry id)

        @self.mobile.on("track")
        def mobile_track(track):
            if track.kind == "audio":
                self.provider.addTrack(AudioBridge(track, self, "input"))

        @self.provider.on("track")
        def provider_track(track):
            if track.kind == "audio":
                self.mobile.addTrack(AudioBridge(track, self, "output"))

        @self.mobile.on("datachannel")
        def mobile_channel(channel):
            self.mobile_dc = channel
            @channel.on("open")
            def opened():
                self.flush(channel, self.to_mobile)
            @channel.on("message")
            def received(raw):
                self.message(raw, False)
            if channel.readyState == "open":
                opened()

        @self.provider_dc.on("open")
        def provider_open():
            if self.provider_ready:
                self.flush(self.provider_dc, self.to_provider)

        @self.provider_dc.on("message")
        def provider_message(raw):
            self.message(raw, True)

        for peer in (self.mobile, self.provider):
            @peer.on("connectionstatechange")
            async def changed(peer=peer):
                if peer.connectionState in ("failed", "closed"):
                    self.spawn(self.close("transport_closed"))

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    @staticmethod
    def flush(channel, pending):
        while pending and channel.readyState == "open":
            channel.send(json.dumps(pending.popleft()))

    def send(self, event, to_provider=False):
        channel = self.provider_dc if to_provider else self.mobile_dc
        pending = self.to_provider if to_provider else self.to_mobile
        if channel is not None and channel.readyState == "open" and (not to_provider or self.provider_ready):
            channel.send(json.dumps(event))
        else:
            if len(pending) == pending.maxlen:
                self.spawn(self.close("event_buffer_full"))
                return
            pending.append(event)

    def message(self, raw, from_provider):
        if self.closed or not isinstance(raw, str) or len(raw) > 262144:
            return
        try:
            event = json.loads(raw)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = str(event.get("type", ""))[:80]
        self.counts[("provider:" if from_provider else "mobile:") + kind] += 1
        if from_provider and kind == "session.started":
            self.provider_ready = True
            self.flush(self.provider_dc, self.to_provider)
        if not from_provider and kind in ("session.input_audio.mute", "session.input_audio.unmute"):
            self.muted = kind.endswith(".mute")
            return
        translated = self.protocol.from_provider(event) if from_provider else self.protocol.from_mobile(event)
        for msg in translated:
            self.send(msg, to_provider=not from_provider)
        if self.relay and from_provider:
            if kind == "input_transcript.added" and self.protocol.turn_text["user"]:
                self.spawn(self.relay.transcript(self.protocol.turn_text["user"], False))
            elif kind == "turn.done" and (event.get("turn") or {}).get("role") == "user" and self.protocol.last_user:
                self.spawn(self.lane_final(self.protocol.last_user))
        while self.protocol.pending_host:
            did, request = self.protocol.pending_host.pop(0)
            self.spawn(self.host_delegate(did, request))
        if kind in ("session.close", "session.closed"):
            self.spawn(self.close("client_closed" if not from_provider else "provider_closed"))

    async def start(self, sdp, history):
        try:
            await self.mobile.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            if not any(t.kind == "audio" for t in self.provider.getTransceivers()):
                raise ValueError("An audio track is required")
            offer = await self.provider.createOffer()
            await self.provider.setLocalDescription(offer)
            payload = {"sdp": self.provider.localDescription.sdp,
                       "session": {"model": MODEL, "instructions": PERSONA,
                                   "audio": {"output": {"voice": "sol"}},
                                   "delegation": {"type": "client"}}}
            headers = await asyncio.to_thread(codex_headers)
            async with self.manager.http.post(SIGNALING, json=payload, headers=headers,
                                             timeout=ClientTimeout(total=35)) as resp:
                if resp.status not in (200, 201):
                    # Vendor bodies can contain account details. Keep them out of logs/responses.
                    LOG.warning("subscription signaling rejected status=%s", resp.status)
                    if resp.status in (401, 403):
                        raise RuntimeError("Codex subscription authorization was rejected; refresh the host Codex login")
                    if resp.status == 429:
                        raise RuntimeError("The ChatGPT subscription voice quota is exhausted")
                    raise RuntimeError("Subscription voice signaling failed (HTTP %s)" % resp.status)
                answer = await resp.text()
                if not answer.startswith("v=0"):
                    raise RuntimeError("Subscription voice returned an invalid SDP answer")
            await self.provider.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))
            answer = await self.mobile.createAnswer()
            await self.mobile.setLocalDescription(answer)
            context = []
            if isinstance(history, list):
                for item in history[-40:]:
                    if not isinstance(item, dict):
                        continue
                    role = item.get("role")
                    if role not in ("user", "assistant"):
                        continue
                    content = item.get("content") or []
                    if not isinstance(content, list):
                        continue
                    text = " ".join(c.get("text", "") for c in content
                                    if isinstance(c, dict) and isinstance(c.get("text"), str))
                    if text:
                        context.append(role + ": " + text[:2000])
            if context:
                text = "Recent chat context (do not read aloud):\n" + "\n".join(context)
                for part in chunks(text[-16000:]):
                    self.send({"type": "session.context.append", "channel": "commentary",
                               "content": [{"type": "input_text", "text": part}]}, True)
            self.spawn(self.watchdog())
            if self.relay:
                self.spawn(self.relay_start())
            LOG.info("subscription voice negotiated id=%s", self.id)
            return {"ok": True, "session": {"id": self.id},
                    "transport": {"type": "webrtc", "sdp": self.mobile.localDescription.sdp}}
        except BaseException:
            await self.close("setup_failed")
            raise

    async def relay_start(self):
        try:
            await self.relay.start(self.profile)
        except Exception as exc:  # noqa: BLE001 - no lanes for this call; the call itself goes on
            LOG.warning("lanes relay start failed id=%s: %s", self.id, type(exc).__name__)
            self.relay = None

    def speak_for(self, did, text):
        for msg in self.protocol.from_mobile({"type": "session.commentary.append", "delegation_id": did, "content": text}):
            self.send(msg, to_provider=True)

    async def speak_fast_lane(self, did, spool, job):
        """Say the Hermes fast lane's quick answer while the full answer is still running."""
        path = spool
        try:
            while not job.done() and not self.closed:
                if path.exists():
                    quick = json.loads(path.read_text()).get("answer", "")
                    if quick and UNHELPFUL.match(quick):
                        self.counts["fast_lane_unhelpful"] += 1
                    elif quick:
                        self.counts["fast_lane_spoken"] += 1
                        self.speak_for(did, "Quick first look, still being checked: " + quick)
                    return
                await asyncio.sleep(0.5)
        except (OSError, ValueError) as exc:
            LOG.warning("fast lane spool unreadable id=%s: %s", self.id, type(exc).__name__)
        finally:
            path.unlink(missing_ok=True)

    def note(self, text):
        for part in chunks(text[:4000]):
            self.send({"type": "session.context.append", "channel": "commentary",
                       "content": [{"type": "input_text", "text": part}]}, True)

    async def lane_final(self, text):
        """One user final through the lanes; later verified/corrected entries become silent notes."""
        receipt = await self.relay.transcript(text, True)
        if not receipt:
            return
        self.counts["lane_finals"] += 1
        if receipt.get("configured"):
            self.note("Helper lane layout changed; tell the user in one short sentence: "
                      + str(receipt["configured"].get("say", ""))[:300])
            return
        if receipt.get("answer") and receipt.get("checking"):
            update = await self.relay.updates(receipt["entry"])
            if update and not self.closed:
                status, value = update
                said = any(e == receipt["entry"] for _, e in self.lane_said.values())
                if status == "corrected" and said:
                    self.counts["lane_corrections"] += 1
                    self.note("Correction from a verifier to what you just said: " + value)
                elif status == "verified":
                    self.counts["lane_verified"] += 1
                    self.note(f'Verified note for "{text[:200]}": {value}')

    async def start_liaison(self, request):
        """Start this turn's liaison now, in parallel with the driver. Returns (lane, spool) or (None, None)."""
        fl = liaison()
        if fl is None or not request:
            return None, None
        try:
            key = fl.answer_key(request)
            runner = await asyncio.to_thread(self.new_lane, fl, request, key)
        except Exception as exc:  # noqa: BLE001 - e.g. engine binary missing: the driver still answers
            LOG.warning("voice liaison failed to start id=%s: %s", self.id, type(exc).__name__)
            self.counts["liaison_failed"] += 1
            return None, None
        self.counts["liaison_started"] += 1
        return runner, fl.LOGS / "answers" / f"{key}.json"

    def new_lane(self, fl, request, key):
        (fl.LOGS / "answers").mkdir(parents=True, exist_ok=True)
        (fl.LOGS / "answers" / f"{key}.json").unlink(missing_ok=True)
        with fl._lock:  # one live liaison per call: a newer question replaces the older one
            old = fl._lanes.pop(self.id, None)
            if old:
                old.stop()
            fl._lanes[self.id] = runner = fl.Lane(self.id, fl.brief(request), key)
        return runner

    async def host_delegate(self, did, request):
        self.counts["host_delegations"] += 1
        lane = None
        if self.relay:
            try:
                lane = await self.relay.answer(request)
            except Exception as exc:  # noqa: BLE001
                LOG.warning("lanes relay answer failed id=%s: %s", self.id, type(exc).__name__)
            if lane and not self.closed:
                self.counts["lane_answer_spoken"] += 1
                receipt = (self.relay.turn or {}).get("receipt") or {}
                self.lane_said[did] = (lane, receipt.get("entry"))
                self.speak_for(did, lane)
                if receipt.get("configured"):
                    return  # a lane-layout line, not a request for Hermes
        try:
            if not request:
                raise RuntimeError("The delegated request was empty")
            spool = runner = None
            if not lane:
                runner, spool = await self.start_liaison(request)
                spool = spool or fast_lane_spool(request, self.profile)
                spool.unlink(missing_ok=True)  # an answer to an earlier identical question is stale
                if runner:
                    runner.watch()
            job = asyncio.ensure_future(asyncio.to_thread(run_hermes, request, self.profile, runner is not None))
            if spool:
                await self.speak_fast_lane(did, spool, job)
            if runner:
                runner.stop()
            answer = await job
            self.counts["host_delegation_ok"] += 1
            if lane and covered(lane, answer, request):
                self.counts["host_delegation_covered"] += 1  # the lane already said it
                return
        except Exception as exc:  # noqa: BLE001 - every failure is spoken, never silent
            LOG.warning("host delegation failed id=%s: %s", self.id, type(exc).__name__)
            self.counts["host_delegation_failed"] += 1
            answer = "I couldn't finish that in Hermes: " + str(exc)[:200]
        if self.closed:
            return
        for msg in self.protocol.from_mobile({"type": "session.commentary.append",
                                              "delegation_id": did, "content": answer}):
            self.send(msg, to_provider=True)

    async def watchdog(self):
        await asyncio.sleep(35)
        if self.mobile.connectionState != "connected" or self.provider.connectionState != "connected":
            await self.close("connection_timeout")
            return
        await asyncio.sleep(MAX_CALL_SECONDS - 35)
        await self.close("duration_limit")

    async def close(self, reason):
        if self.closed:
            return
        self.closed = True
        self.send({"type": "session.closed", "reason": reason,
                   "usage": {"seconds": int(time.monotonic() - self.created)}})
        if self.provider_dc.readyState == "open":
            self.provider_dc.send(json.dumps({"type": "session.close"}))
        current = asyncio.current_task()
        for task in list(self.tasks):
            if task is not current:
                task.cancel()
        await asyncio.gather(self.mobile.close(), self.provider.close(), return_exceptions=True)
        if self.relay:
            await self.relay.close()
            self.counts.update({"relay_" + k: v for k, v in self.relay.counts.items()})
        self.manager.calls.pop(self.id, None)
        self.manager.recent.append({"id": self.id, "reason": reason,
                                    "seconds": round(time.monotonic() - self.created, 1),
                                    "counts": dict(self.counts)})
        LOG.info("subscription voice closed id=%s reason=%s counts=%s", self.id, reason, dict(self.counts))


class Manager:
    def __init__(self, http):
        self.http = http
        self.calls = {}
        self.recent = deque(maxlen=10)
        self.draining = False

    async def close(self):
        self.draining = True
        deadline = time.monotonic() + DRAIN_SECONDS
        if self.calls:
            LOG.info("draining: waiting for %d open call(s) before stopping", len(self.calls))
        while self.calls and time.monotonic() < deadline:
            await asyncio.sleep(0.5)
        await asyncio.gather(*(c.close("service_shutdown") for c in list(self.calls.values())), return_exceptions=True)


async def authenticate(request):
    # Mobile uses native bearer auth. Do not let a loopback caller inherit the
    # dashboard's local bypass or let cross-site cookie requests spend quota.
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer ") or not auth[7:].strip():
        raise web.HTTPUnauthorized(text="Hermes native authentication required")
    headers = {"Authorization": auth, "Host": "127.0.0.1:9119"}
    if request.headers.get("X-Hermes-Profile"):
        headers["X-Hermes-Profile"] = request.headers["X-Hermes-Profile"]
    query = {"profile": request.query["profile"]} if request.query.get("profile") else None
    async with request.app["http"].get(UPSTREAM + "/api/auth/me", headers=headers, params=query,
                                       allow_redirects=False, timeout=ClientTimeout(total=8)) as resp:
        if resp.status != 200:
            raise web.HTTPUnauthorized(text="Hermes authentication rejected")
        identity = await resp.json()
        # The real endpoint returns a provider/user identity, not just HTTP 200.
        if not isinstance(identity, dict) or not identity.get("authenticated", identity.get("user_id") or identity.get("user")):
            raise web.HTTPUnauthorized(text="Hermes identity missing")


async def status(request):
    await authenticate(request)
    try:
        await asyncio.to_thread(codex_headers)
        ready, reason = True, None
    except RuntimeError as exc:
        ready, reason = False, str(exc)
    LOG.info("mobile voice readiness available=%s", ready)
    return web.json_response({"ok": True, "mode": "gpt-live", "available": ready,
                              "reason": reason, "model": MODEL, "voice": "sol"})


async def session(request):
    await authenticate(request)
    manager = request.app["manager"]
    if manager.draining:
        raise web.HTTPServiceUnavailable(text="Voice is restarting; call back in a moment")
    if len(manager.calls) >= MAX_CALLS:
        raise web.HTTPTooManyRequests(text="Close an existing voice call first")
    try:
        data = await request.json()
        sdp = data.get("sdp")
        if not isinstance(sdp, str) or not sdp.startswith("v=0") or len(sdp) > 131072:
            raise ValueError("A valid SDP offer is required")
    except (ValueError, AttributeError):
        raise web.HTTPBadRequest(text="A valid SDP offer is required") from None
    call = Call(manager, profile_name(request.headers.get("X-Hermes-Profile") or request.query.get("profile")))
    manager.calls[call.id] = call
    try:
        async with asyncio.timeout(55):
            result = await call.start(sdp, data.get("history"))
        return web.json_response(result)
    except (ValueError, RuntimeError, TimeoutError) as exc:
        await call.close("setup_failed")
        return web.json_response({"detail": str(exc)[:300]}, status=503)


async def diagnostics(request):
    await authenticate(request)
    manager = request.app["manager"]
    return web.json_response({"active": [{"id": c.id, "mobile": c.mobile.connectionState,
        "provider": c.provider.connectionState, "counts": dict(c.counts)} for c in manager.calls.values()],
        "recent": list(manager.recent)})


async def resources(app):
    async with ClientSession() as http:
        app["http"] = http
        app["manager"] = Manager(http)
        yield
        await app["manager"].close()


def make_app():
    app = web.Application(client_max_size=512 * 1024)
    app.cleanup_ctx.append(resources)
    app.router.add_get("/api/audio/voice-live/status", status)
    app.router.add_post("/api/audio/voice-live/session", session)
    app.router.add_get("/api/audio/voice-live/diagnostics", diagnostics)
    return app


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(name)s %(levelname)s %(message)s")
    LOG.setLevel(logging.INFO)
    # aioice addresses and SDP payloads are deliberately not logged.
    web.run_app(make_app(), host="127.0.0.1", port=9168, access_log=None)
