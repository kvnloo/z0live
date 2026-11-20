"""Subscription voice compatibility adapter for the official Hermes mobile client.

The existing dashboard remains the authentication authority. No account token,
audio, or transcript is returned over HTTP or persisted by this adapter.
Codex signaling contract: z0live/OMP Frameless Bidi (client delegation only).
"""
from __future__ import annotations

import asyncio
import base64
from collections import Counter, defaultdict, deque
from array import array
import hashlib
import json
import re
import logging
import os
from pathlib import Path
import selectors
import shutil
import sqlite3
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
import chat_continuity  # noqa: E402 - sibling module (voice turns land in the owner's open chat)
import call_brief  # noqa: E402 - sibling module (context packet in the session instructions)
import grok_leg  # noqa: E402 - sibling module (Grok Voice as the voice model)
import tiers  # noqa: E402 - sibling module (fast/medium/slow model lanes)
import guest_voice  # noqa: E402 - sibling module (a second voice, Grok, answering by name)
from aiortc.contrib.media import MediaRelay  # noqa: E402

# Z0LIVE_TIERS=on: a delegated request runs the tiers.py lanes (via lane_server.py) beside Hermes, instead of
# the warm-Haiku/Groq instant lane, the Fable lanes and the core lanes relay.
TIERS_ON = os.environ.get("Z0LIVE_TIERS", "off") == "on"
import handoff  # noqa: E402 - sibling module (call continues on z0live when Kevin leaves the screen)
CHAT_CONTINUITY = os.environ.get("HERMES_MOBILE_VOICE_CHAT", "1") != "0"
TTFT_MODEL = os.environ.get("VOICE_TTFT_MODEL", "qwen/qwen3.8-27b")  # Groq: ~0.2 s whole answer (11-15 probe)
# Children (claude, hermes, lanes) get their own transient scope. Inside this unit's 768M cap they pushed it
# to its limit; the kernel then stalled the adapter's event loop ~10 s and the phone hung up (4 calls, 11-16).
SCOPE = (["systemd-run", "--user", "--scope", "--quiet", "--collect", "-p", "MemoryMax=4G"]
         if shutil.which("systemd-run") and os.environ.get("HERMES_MOBILE_VOICE_SCOPE", "1") != "0" else [])
# Lane output that must never be spoken: hook chatter and the fast lane declining.
NOISE = re.compile(r"^\W*(skip\b|model guard|the hook flagged)", re.I)
SESSION_WORDS = re.compile(r"\b(sessions?|chief of staff|progress|what.{0,12}(running|working on|going on)|status)\b", re.I)


def same_words(a: str, b: str) -> bool:
    """True when two transcripts of one utterance share most of their words (ASR differs in small ways)."""
    wa, wb = (set(re.findall(r"[a-z0-9']{3,}", x.lower()[:400])) for x in (a, b))
    return bool(wa and wb) and len(wa & wb) >= 0.5 * min(len(wa), len(wb))


def live_sessions(hours: float = 3.0, limit: int = 12) -> str:
    """Open Hermes sessions active in the last `hours`, all profiles, newest first: plain facts for a summary."""
    out = []
    for db in sorted((Path.home() / ".hermes" / "profiles").glob("*/state.db")):
        try:
            with sqlite3.connect(f"file:{db}?mode=ro", uri=True, timeout=1) as con:
                rows = con.execute(
                    "SELECT s.title, s.last_activity_at, coalesce(s.last_activity_description,''), (SELECT substr(m.content,1,220) "
                    "FROM messages m WHERE m.session_id=s.id AND m.role='assistant' AND length(m.content)>40 "
                    "ORDER BY m.id DESC LIMIT 1) FROM sessions s WHERE s.ended_at IS NULL AND s.title IS NOT NULL "
                    "AND s.last_activity_at > strftime('%s','now') - ? ORDER BY s.last_activity_at DESC LIMIT ?",
                    (int(hours * 3600), limit)).fetchall()
        except sqlite3.Error:
            continue
        out += [(at, db.parent.name, t, d, " ".join((last or "").split())) for t, at, d, last in rows]
    now = time.time()
    return "\n".join(f"- [{p}] {t} ({int((now - at) // 60)} min ago{', ' + d if d else ''}): {last}"
                     for at, p, t, d, last in sorted(out, reverse=True)[:limit])


def _ttft_key():
    """GROQ_API_KEY from the process env (a bws-synced env file on hosts without the ttft bot), else the ttft
    bot's env (bws-deployed on groot); never logged."""
    if os.environ.get("GROQ_API_KEY"):
        return os.environ["GROQ_API_KEY"].strip()
    try:
        for line in (Path.home() / ".hermes/profiles/ttft/.env").read_text().splitlines():
            if line.startswith("GROQ_API_KEY="):
                return line.split("=", 1)[1].strip().strip("'\"")
    except OSError:
        pass
    return ""


FAST_SYSTEM = ("You are the instant fast lane ({who}) on Kevin's live voice call; this very answer is the fast lane he "
               "hears first. Answer in one or two short spoken sentences from the context. Never guess whether something "
               "worked or what he heard. If the context can't answer it, reply with exactly SKIP (slower lanes are already "
               "checking; never say so yourself). Most important fact first. Never invent numbers. "
               "Kevin's Hermes chat runs on his PC with adb to his phone: it can screenshot and read his screen, watch logs and run commands. Never say you can't see his screen or aren't connected to the call. Screen, phone and action requests: reply SKIP so the chat does them.")


class WarmHaiku:
    """One `claude -p` Haiku process kept warm for a whole call: ~0.7 s first token vs ~3 s cold (11-15 probe)."""

    def __init__(self, system):
        self.system, self.proc, self.lock = system, None, asyncio.Lock()
        self.stale = 0  # results still owed to asks that timed out; the next ask must skip them

    async def start(self):
        env = {**os.environ, "CLAUDE_CONFIG_DIR": str(Path.home() / ".claude-sub2"), "FAST_LANE_CHILD": "1"}
        self.proc = await asyncio.create_subprocess_exec(
            *SCOPE, "claude", "-p", "--model", "haiku", "--input-format", "stream-json", "--output-format", "stream-json",
            "--verbose", "--tools", "", "--strict-mcp-config", "--append-system-prompt", self.system,
            stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            env=env, cwd=str(Path.home()), limit=1 << 22)
        await self.ask("Reply with just: ready", 30)  # pays the cold start before Kevin's first question

    async def ask(self, text, timeout=8):
        async with self.lock:
            proc = self.proc
            if not proc or proc.returncode is not None or not proc.stdin or not proc.stdout:
                raise RuntimeError("warm haiku not running")
            out = proc.stdout
            msg = {"type": "user", "message": {"role": "user", "content": text}}
            proc.stdin.write((json.dumps(msg) + "\n").encode())
            await proc.stdin.drain()

            async def result():
                while line := await out.readline():
                    d = json.loads(line)
                    if d.get("type") == "result":
                        if self.stale:  # a timed-out ask's late answer (spoke "Model guard..." for a new question)
                            self.stale -= 1
                            continue
                        return " ".join(str(d.get("result") or "").split())
                raise RuntimeError("warm haiku exited")
            try:
                return await asyncio.wait_for(result(), timeout)
            except asyncio.TimeoutError:
                self.stale += 1
                raise

    def close(self):
        if self.proc and self.proc.returncode is None:
            self.proc.kill()


LANE_MERGE_WAIT = 20  # seconds after the first lane lands before speaking the merge without the stragglers
LANE_POLL = 1.0
FABLE_SCRIPT = os.environ.get("FAST_LANE_FABLE_SCRIPT", str(Path(__file__).with_name("fable_lanes.py")))
# The dashboard that validates the phone's bearer. groot binds it to loopback; the Studio binds its tailnet IP.
UPSTREAM_HOST = os.environ.get("HERMES_DASHBOARD_UPSTREAM", "127.0.0.1:9119")
UPSTREAM = f"http://{UPSTREAM_HOST}"
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
Be natural, brief and direct. Do not speak until the user speaks. While the user tells a longer
story, acknowledge often with a quick "mm-hm", "yeah" or "right" and let them keep going. Start
answering as soon as the user is clearly done; do not wait for a long silence. Listen while speaking
and stop immediately when interrupted. Handle greetings and ordinary small talk yourself.
You have no tools. For facts, lookups or actions, delegate the complete request to the client.
Kevin's Hermes chat runs on his PC with adb to his phone: it can screenshot and read his screen, watch logs and run commands. Never say you can't see his screen or aren't connected to the call. "Look at my screen" is a delegation, not a refusal.
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
if covered is None:  # a pure text check the tier lanes need even with the core relay off
    if Z0LIVE_DESKTOP not in sys.path:
        sys.path.insert(0, Z0LIVE_DESKTOP)
    try:
        from z0live_desktop.mobile_relay import covered
    except ImportError:
        pass


# The voice model is lane 0 of the z0live lanes config (z0live.lanes.v1 lanes.voice.actor), hot-reloaded per
# call, so `z0live engine grok` (or a config edit) before a call picks the engine. HERMES_VOICE_ENGINE overrides.
ENGINES = {"voice_plan": "gpt-live", "grok_voice": "grok", "on_device": "chained"}


def voice_engine() -> str:
    forced = os.environ.get("HERMES_VOICE_ENGINE", "").strip()
    if forced:
        return forced
    if Z0LIVE_DESKTOP not in sys.path:  # also when lanes are off (HERMES_MOBILE_VOICE_LANES=0)
        sys.path.insert(0, Z0LIVE_DESKTOP)
    try:
        from z0live_desktop import lanes as lanes_mod
        return ENGINES.get(lanes_mod.Lanes().get()["lanes"]["voice"]["actor"], "gpt-live")
    except Exception:  # noqa: BLE001 - no z0live checkout or a bad config: the subscription voice
        return "gpt-live"


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
    args = SCOPE + [HERMES_BIN] + (["-p", profile] if profile else []) + [
        # Explicit --source: a source inherited from a parent Hermes (HERMES_SESSION_SOURCE=kanban)
        # makes the run fail before any model call ("couldn't start the model connection: invalid_params").
        "chat", "-q", request, "--oneshot", "-Q", "--source", "oneshot"] + (["--resume", resume] if resume else [])
    # The lanes are this turn's fast lane: the driver's profile plugin must not start a second one.
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
        self.exchanges = deque(maxlen=8)  # finished (role, text) turns, for the z0live handoff
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
            if (text or prior).strip():
                self.exchanges.append((role, (text or prior).strip()))
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


# Per-call event trace (owner 2026-11-10: "make sure the trace works and it's being stored so we can
# analyze and optimize"). One JSONL per call, local only, includes transcripts; no audio is stored.
# Analyze with trace_report.py. HERMES_MOBILE_VOICE_TRACE=0 turns it off.
TRACE_DIR = Path(os.environ.get("HERMES_MOBILE_VOICE_TRACE_DIR",
                                "/mnt/zer0models/sft-svlm/data/voice-traces/hermes-mobile-live"))
ADAPTER_REV = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:12]
LEVEL_ON = 800          # s16 peak (~-32 dBFS) that counts as sound
LEVEL_OFF_SECONDS = 0.3  # quiet this long ends a sound span


class Trace:
    def __init__(self, call_id):
        self.t0, self.f = time.monotonic(), None
        if os.environ.get("HERMES_MOBILE_VOICE_TRACE", "1") == "0":
            return
        try:
            day = TRACE_DIR / time.strftime("%Y-%m-%d")
            day.mkdir(parents=True, exist_ok=True)
            self.path = day / f"{time.strftime('%H%M%S')}-{call_id}.jsonl"
            self.f = open(self.path, "a", buffering=1, encoding="utf-8")
        except OSError as exc:
            LOG.warning("voice trace unavailable: %s", type(exc).__name__)

    def __call__(self, ev, **data):
        if self.f:
            try:
                self.f.write(json.dumps({"t": round(time.monotonic() - self.t0, 3), "wall": round(time.time(), 3),
                                         "ev": ev, **data}, ensure_ascii=False, default=str) + "\n")
            except (OSError, ValueError):
                pass

    def close(self):
        if self.f:
            self.f.close()
            self.f = None


def slim(event, limit=1500):
    raw = json.dumps(event, ensure_ascii=False, default=str)
    return event if len(raw) <= limit else {"type": event.get("type"), "truncated": raw[:limit]}


class Level:
    """Sound on/off spans of one audio direction, from frame peaks (energy, not speech recognition)."""
    def __init__(self, call, direction):
        self.call, self.direction, self.on, self.quiet, self.peak = call, direction, None, 0.0, 0

    def frame(self, frame):
        try:
            pcm = array("h", bytes(frame.planes[0])[:frame.samples * len(frame.layout.channels) * 2])
            peak = max(max(pcm), -min(pcm)) if pcm else 0
        except Exception:  # noqa: BLE001 - tracing must never break audio
            return
        now = time.monotonic()
        if peak >= LEVEL_ON:
            self.quiet = 0.0
            self.peak = max(self.peak, peak)
            if self.on is None:
                self.on = now
                self.call.trace("audio.on", dir=self.direction, peak=peak, muted=self.call.muted)
        elif self.on is not None:
            self.quiet = self.quiet or now
            if now - self.quiet >= LEVEL_OFF_SECONDS:
                self.call.trace("audio.off", dir=self.direction, at=round(self.quiet - self.call.trace.t0, 3),
                                span=round(self.quiet - self.on, 3), peak=self.peak)
                self.on, self.quiet, self.peak = None, 0.0, 0


class AudioBridge(MediaStreamTrack):
    kind = "audio"

    def __init__(self, source, call, direction):
        super().__init__()
        self.source, self.call, self.direction = source, call, direction
        self.level = Level(call, direction)

    async def recv(self):
        frame = await self.source.recv()
        self.call.counts[self.direction + "_frames"] += 1
        self.level.frame(frame)
        call = self.call
        if self.direction == "input" and not call.heard_user and self.level.on is not None and not call.muted:
            call.heard_user = True
            call.trace("output.gate_open", gated_frames=call.counts["output_gated"])
        # The voice must not speak before Kevin does: GPT-Live sometimes starts a turn (a lone "Mm-h", a sniff,
        # "He Hey") ~0.4 s into the session while the persona context lands. Silence its audio until he speaks.
        if (self.direction == "input" and call.muted) or (self.direction == "output" and not call.heard_user):
            if self.direction == "output":
                call.counts["output_gated"] += 1
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
        self.heard_user = False  # output audio stays silent until the phone sends sound (AudioBridge)
        self.provider_ready = False
        self.mobile_dc = None
        self.provider_dc = self.provider.createDataChannel("oai-events")
        self.to_mobile = deque(maxlen=256)
        self.to_provider = deque(maxlen=256)
        self.tasks = set()
        self.relay = LaneRelay.from_env() if (LaneRelay and HOST_DELEGATION) else None
        self.lane_said = {}  # delegation id -> (lane answer, entry id)
        self.token = ""  # the app's own bearer, for submitting voice turns into its open chat
        self.chat_lookup = None
        self.context_text = ""  # recent chat history text, for the ttft lane
        self.haiku = None  # WarmHaiku, started once chat context is known
        self.chat = ""   # stored id of the chat Live Voice was started from ('' = hidden voice session)
        self.trace = Trace(self.id)
        self.engine = voice_engine()
        if self.engine not in ("gpt-live", "grok"):  # on_device (STT -> model -> TTS) is not built yet
            self.trace("engine.unavailable", wanted=self.engine, using="gpt-live")
            self.engine = "gpt-live"
        self.leg = grok_leg.GrokLeg(self, PERSONA) if self.engine == "grok" else None
        # A guest voice beside GPT-Live (guest_voice.py): ~/.config/z0live/voices.json {"guest": "grok"}, per call.
        self.guest = guest_voice.GuestVoice(self) if self.engine == "gpt-live" and guest_voice.wanted() == "grok" else None
        self.trace("call.created", profile=profile, engine=self.engine,
                   model=grok_leg.URL.rsplit("=", 1)[-1] if self.leg else MODEL, rev=ADAPTER_REV, pid=os.getpid(),
                   host_delegation=HOST_DELEGATION, lanes=bool(self.relay), guest="grok" if self.guest else "")

        @self.mobile.on("track")
        def mobile_track(track):
            if track.kind == "audio":
                if self.leg:
                    self.leg.attach(AudioBridge(track, self, "input"))
                elif self.guest:  # both voices hear Kevin
                    split = MediaRelay()
                    self.provider.addTrack(AudioBridge(split.subscribe(track), self, "input"))
                    self.guest.attach(split.subscribe(track))
                else:
                    self.provider.addTrack(AudioBridge(track, self, "input"))

        @self.provider.on("track")
        def provider_track(track):
            if track.kind == "audio":
                out = AudioBridge(track, self, "output")
                self.mobile.addTrack(guest_voice.FloorSwitch(out, self.guest) if self.guest else out)

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
                self.trace("peer.state", peer="mobile" if peer is self.mobile else "provider",
                           state=peer.connectionState)
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
        self.trace("tx", to="provider" if to_provider else "mobile", type=event.get("type"), event=slim(event))
        if to_provider and self.leg:
            self.leg.outbound(event)
            return
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
        self.trace("rx", src="provider" if from_provider else "mobile", type=kind, event=slim(event))
        self.counts[("provider:" if from_provider else "mobile:") + kind] += 1
        if from_provider and kind == "session.started":
            self.provider_ready = True
            self.flush(self.provider_dc, self.to_provider)
        if (from_provider and kind == "input_transcript.added" and not self.heard_user  # speech too quiet for Level
                and str((event.get("item") or {}).get("text") or "").strip()):
            self.heard_user = True
            self.trace("output.gate_open", gated_frames=self.counts["output_gated"], by="transcript")
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
        if self.guest and from_provider:
            self.guest.observe(event, self.protocol.turn_text["user"])
        while self.protocol.pending_host:
            did, request = self.protocol.pending_host.pop(0)
            self.spawn(self.host_delegate(did, request))
        if kind in ("session.close", "session.closed"):
            self.spawn(self.close("client_closed" if not from_provider else "provider_closed"))

    async def start(self, sdp, history):
        if CHAT_CONTINUITY and self.token:  # off the setup path: a slow lookup must never stall the phone's offer
            self.chat_lookup = asyncio.ensure_future(asyncio.to_thread(chat_continuity.find_chat, history, self.profile))
        self.trace("call.start", history_items=len(history) if isinstance(history, list) else None)
        try:
            await self.mobile.setRemoteDescription(RTCSessionDescription(sdp=sdp, type="offer"))
            if self.leg:  # Grok Voice: a WebSocket, no provider peer; its speech is our own track
                if not any(t.kind == "audio" for t in self.mobile.getTransceivers()):
                    raise ValueError("An audio track is required")
                self.trace("signaling.request", engine="grok")
                await self.leg.start()
                self.trace("signaling.answer", status="grok")
                self.mobile.addTrack(AudioBridge(self.leg.output, self, "output"))  # same level/frame trace
            else:
                if not any(t.kind == "audio" for t in self.provider.getTransceivers()):
                    raise ValueError("An audio track is required")
                offer = await self.provider.createOffer()
                await self.provider.setLocalDescription(offer)
                payload = {"sdp": self.provider.localDescription.sdp,
                           "session": {"model": MODEL, "instructions": PERSONA,
                                       "audio": {"output": {"voice": "sol"}},
                                       "delegation": {"type": "client"}}}
                headers = await asyncio.to_thread(codex_headers)
                self.trace("signaling.request")
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
                    self.trace("signaling.answer", status=resp.status)
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
                self.context_text = text
                if os.environ.get("VOICE_WARM_HAIKU", "1") != "0" and shutil.which("claude"):
                    self.haiku = WarmHaiku(FAST_SYSTEM.format(who="Haiku") + "\n\n" + text[-6000:])
                    self.spawn(self.start_haiku())
                # raw tail is for the lanes only: as live context items the model answered them (catch-up, 11-16)
            # The subscription endpoint ignores session instructions (11-16 canary: "I'm ChatGPT"), so the
            # persona and the call brief go in as context, framed as background so they are not answered.
            channel = os.environ.get("HERMES_VOICE_BRIEF_CHANNEL", "developer")
            brief = call_brief.build(history, self.chat)
            persona = PERSONA + ("\n" + guest_voice.PRIMARY_NOTE if self.guest else "")
            if self.guest:
                self.spawn(self.start_guest(brief))
            for part in chunks(persona + "\n" + brief, int(os.environ.get("HERMES_VOICE_BRIEF_CHUNK", "1800"))):
                self.send({"type": "session.context.append", "channel": channel,
                           "content": [{"type": "input_text", "text": part}]}, True)
            self.spawn(self.watchdog())
            self.spawn(self.audio_health())
            if self.relay:
                self.spawn(self.relay_start())
            self.trace("call.negotiated", setup_s=round(time.monotonic() - self.created, 3), context_items=len(context))
            LOG.info("subscription voice negotiated id=%s", self.id)
            return {"ok": True, "session": {"id": self.id},
                    "transport": {"type": "webrtc", "sdp": self.mobile.localDescription.sdp}}
        except BaseException as exc:
            self.trace("call.setup_failed", error=type(exc).__name__, detail=str(exc)[:300])
            await self.close("setup_failed")
            raise

    async def start_guest(self, brief):
        guest = self.guest
        if guest is None:
            return
        try:
            await guest.start()
            for _ in range(100):  # session.updated arrives within ~1 s
                if guest.ready or self.closed:
                    break
                await asyncio.sleep(0.05)
            if brief:
                await guest.note("Background on Kevin's chat (do not read aloud):\n" + brief[:6000])
        except Exception as exc:  # noqa: BLE001 - the call goes on with one voice
            self.trace("guest.failed", error=type(exc).__name__, detail=str(exc)[:300])
            self.guest = None

    async def relay_start(self):
        try:
            await self.relay.start(self.profile)
        except Exception as exc:  # noqa: BLE001 - no lanes for this call; the call itself goes on
            LOG.warning("lanes relay start failed id=%s: %s", self.id, type(exc).__name__)
            self.relay = None

    def speak_for(self, did, text):
        for msg in self.protocol.from_mobile({"type": "session.commentary.append", "delegation_id": did, "content": text}):
            self.send(msg, to_provider=True)

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

    async def host_delegate(self, did, request):
        """One fast-lane system (L2, 11-11): the driver starts at once and the z0live lanes race it: smol
        for chat and simple turns, the tool-using gather lane (the liaison's engine) for questions about
        Kevin's own work. The first lane answer is spoken once; the driver is spoken only if it adds to it."""
        self.counts["host_delegations"] += 1
        began = time.monotonic()
        self.trace("delegation.start", did=did, request=(request or "")[:4000])
        turn = self.relay.turn if self.relay else None
        configured = ((turn or {}).get("receipt") or {}).get("configured")
        if configured and not self.closed:  # a lane-layout line, not a request for Hermes
            self.speak_for(did, configured.get("say") or "")
            return
        lane = None
        try:
            if not request:
                raise RuntimeError("The delegated request was empty")
            # FAST_LANE_OFF: the lanes are this turn's fast lane; the profile plugin must not start another.
            job = asyncio.ensure_future(self.ask_hermes(did, request))
            self.latest_did = did  # an older question's lanes go quiet once this one starts
            if TIERS_ON:
                self.spawn(self.tier_lanes(did, request))
            else:
                lanes = asyncio.ensure_future(self.fable_lanes(did, time.time(), request))
                self.spawn(self.run_fable_lanes(did, request))
                self.spawn(self.ttft_lane(did, request))
                job.add_done_callback(lambda _: asyncio.get_running_loop().call_later(150, lanes.cancel))
            if not TIERS_ON and self.relay and await self.relay_turn_for(request):
                turn = self.relay.turn
                lane_job = asyncio.ensure_future(self.relay.answer(request))
                await asyncio.wait({lane_job, job}, return_when=asyncio.FIRST_COMPLETED)
                if lane_job.done() and not lane_job.cancelled():
                    try:
                        lane = lane_job.result()
                    except Exception as exc:  # noqa: BLE001
                        LOG.warning("lanes relay answer failed id=%s: %s", self.id, type(exc).__name__)
                else:
                    lane_job.cancel()  # the driver was first: no lane answer for this turn
                late = ((turn or {}).get("receipt") or {}).get("configured")
                if late:  # the receipt landed after delegation began: still a lane-layout line
                    self.counts["host_delegation_layout_late"] += 1
                    self.trace("delegation.layout_late", did=did, s=round(time.monotonic() - began, 3))
                    if not self.closed:
                        self.speak_for(did, late.get("say") or "")
                    return  # the driver's run (already started) is discarded
                gathered = bool(turn and turn.get("source") == "gather")
                self.trace("delegation.lane", did=did, text=(lane or "")[:2000], gather=gathered,
                           s=round(time.monotonic() - began, 3))
                if lane and gathered and UNHELPFUL.match(lane):
                    self.counts["lane_unhelpful"] += 1  # "I couldn't find it" adds nothing before the driver
                    lane = None
                if lane and self.say_first(did, lane):
                    self.counts["lane_answer_spoken"] += 1
                    self.lane_said[did] = (lane, ((turn or {}).get("receipt") or {}).get("entry"))
            answer = await job
            self.__dict__.setdefault("hermes_done", set()).add(did)  # tier lanes go quiet: Hermes has the answer
            if TIERS_ON:
                lane = " ".join(self.__dict__.get("tier_said", {}).get(did, [])) or None
            self.counts["host_delegation_ok"] += 1
            self.trace("delegation.done", did=did, s=round(time.monotonic() - began, 3), answer=answer[:4000],
                       closed=self.closed)
            # Tier lanes: Hermes is skipped when everything it says was already spoken (answer within said).
            if lane and covered and (covered(answer, lane, request) if TIERS_ON else covered(lane, answer, request)):
                self.counts["host_delegation_covered"] += 1  # the lane already said it
                return
        except Exception as exc:  # noqa: BLE001 - every failure is spoken, never silent
            LOG.warning("host delegation failed id=%s: %s", self.id, type(exc).__name__)
            self.counts["host_delegation_failed"] += 1
            self.trace("delegation.failed", did=did, s=round(time.monotonic() - began, 3), error=type(exc).__name__,
                       detail=str(exc)[:300])
            answer = "I couldn't finish that in Hermes: " + str(exc)[:200]
        if self.closed:
            self.trace("delegation.dropped_after_close", did=did)
            return
        for msg in self.protocol.from_mobile({"type": "session.commentary.append",
                                              "delegation_id": did, "content": answer}):
            self.send(msg, to_provider=True)

    async def tier_lanes(self, did, request):
        """Fast, medium and slow lanes race within each tier; tiers run at once. The first answer from any tier
        is spoken; a later tier speaks only if it adds to what was said (tiers.MAX_SPOKEN per request). Lanes go
        quiet once a newer request starts or Hermes answers."""
        began, cfg = time.monotonic(), tiers.load()
        context = tiers.snapshot(self.context_text, list(self.protocol.exchanges))
        said = self.__dict__.setdefault("tier_said", {}).setdefault(did, [])

        async def one(name):
            return name, await tiers.race(self.manager.http, name, cfg[name], context, request)

        for job in asyncio.as_completed([one(name) for name in tiers.DEADLINE if cfg.get(name)]):
            name, (text, model, first_s, errors) = await job
            self.trace("lanes.tier", did=did, tier=name, model=model, first_s=first_s, text=text[:500], errors=errors,
                       s=round(time.monotonic() - began, 3))
            quiet = (self.closed or getattr(self, "latest_did", did) != did
                     or did in self.__dict__.get("hermes_done", set()))
            if not text or quiet or len(said) >= tiers.MAX_SPOKEN:
                continue
            if not said:
                if self.say_first(did, text):
                    said.append(text)
                    self.counts["tier_first_spoken"] += 1
                    self.trace("lanes.spoken", did=did, tier=name, model=model, first=True)
            elif covered and not covered(text, " ".join(said), request):  # no check: no updates
                said.append(text)
                self.counts["tier_update_spoken"] += 1
                self.trace("lanes.spoken", did=did, tier=name, model=model, first=False)
                self.speak_for(did, text)

    async def start_haiku(self):
        began, haiku = time.monotonic(), self.haiku
        if not haiku:
            return
        try:
            await haiku.start()
            self.trace("haiku.warm", s=round(time.monotonic() - began, 2))
        except Exception as exc:  # noqa: BLE001 - Groq stays the fast lane
            self.trace("haiku.warm_failed", error=type(exc).__name__)
            haiku.close()
            self.haiku = None

    async def relay_turn_for(self, request, wait=2.0):
        """The relay's latest final, once it is THIS request. delegation.created can beat the user's turn.done,
        and the previous turn's lane answer ("Hey Kevin! Not much...") was spoken for a new question (11-16)."""
        deadline = time.monotonic() + wait
        while True:
            turn = self.relay.turn
            if turn and same_words(turn.get("text", ""), request):
                return turn
            if time.monotonic() > deadline:
                self.counts["relay_turn_stale"] += 1
                return None
            await asyncio.sleep(0.1)

    async def groq(self, system, user, max_tokens=120):
        """One Groq completion (~0.3 s), or '' on any failure. Key from the ttft bot's env; never logged."""
        key = _ttft_key()
        if not key:
            return ""
        body = {"model": TTFT_MODEL, "max_tokens": max_tokens,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        try:
            async with self.manager.http.post("https://api.groq.com/openai/v1/chat/completions", json=body,
                                              headers={"Authorization": "Bearer " + key},
                                              timeout=ClientTimeout(total=8)) as resp:
                data = await resp.json()
            return " ".join(str(data["choices"][0]["message"].get("content") or "").split())
        except Exception as exc:  # noqa: BLE001
            self.trace("groq_failed", error=type(exc).__name__)
            return ""

    def say_first(self, did, text):
        """Speak one instant answer per question; a second instant lane (relay vs Haiku/Groq) stays silent."""
        if not text or NOISE.match(text) or self.closed or getattr(self, "latest_did", did) != did:
            return False
        said = self.__dict__.setdefault("first_said", {})
        if did in said:
            return False
        said[did] = text
        self.speak_for(did, text)
        return True

    async def ttft_lane(self, did, request):
        """Instant answer: warm Haiku, else Groq. Session/progress questions get the live Hermes sessions as facts."""
        facts = ""
        if SESSION_WORDS.search(request or ""):
            facts = await asyncio.to_thread(live_sessions)
            self.trace("delegation.sessions", did=did, n=len(facts.splitlines()))
        ask = request + (f"\n\nLive Hermes sessions right now (newest first):\n{facts}\nSummarize the top few in at most "
                         "three short spoken sentences, most important first." if facts else "")
        text = ""
        if self.haiku:
            began = time.monotonic()
            try:
                text = await self.haiku.ask(ask)
                self.trace("delegation.haiku_fast", did=did, s=round(time.monotonic() - began, 3), text=text[:500])
            except Exception as exc:  # noqa: BLE001
                self.trace("delegation.haiku_fast_failed", did=did, error=type(exc).__name__)
        if not text or NOISE.match(text):
            began = time.monotonic()
            text = await self.groq(FAST_SYSTEM.format(who="Qwen on Groq") + "\n\n" + self.context_text[-6000:], ask,
                                   200 if facts else 120)
            self.trace("delegation.ttft", did=did, s=round(time.monotonic() - began, 3), text=text[:500])
        if self.say_first(did, text):
            self.counts["ttft_spoken"] += 1

    async def run_fable_lanes(self, did, request):
        """Start the Haiku+Fable lanes here: the chat's plugin hook does not fire for dashboard turns."""
        root = Path.home() / ".hermes" / "profiles" / (self.profile or "clean") / "cache" / "fast-lane"
        if not (root / "fable-on").exists():
            return
        try:
            n = str(max(1, int((root / "fable-n").read_text().strip())))
        except (OSError, ValueError):
            n = "5"
        self.__dict__.setdefault("lane_n", {})[did] = int(n)
        proc = await asyncio.create_subprocess_exec(
            *SCOPE, "python3", "-I", FABLE_SCRIPT, request, "--n", n, "--timeout", "150",
            "--out", str(root / "fable" / f"voice-{did[-24:]}"),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        self.trace("delegation.fable_start", did=did, n=n, pid=proc.pid)
        await proc.wait()

    async def fable_lanes(self, did, since, request=""):
        """Collect this question's helper lanes silently, then speak ONE merged update. Speaking each lane
        made a 22 s monologue (11-16 call); Kevin wants the most important fact first and little else."""
        root = Path.home() / ".hermes" / "profiles" / (self.profile or "clean") / "cache" / "fast-lane" / "fable"
        heard, answers, first = set(), [], None
        want = self.__dict__.get("lane_n", {}).get(did, 5)
        while not self.closed and getattr(self, "latest_did", did) == did:
            # only this delegation's own lanes: a shared glob made every open question re-speak every lane
            for f in sorted(root.glob(f"voice-{did[-24:]}/*.json"), key=lambda p: p.stat().st_mtime):
                try:
                    if f in heard or f.stat().st_mtime < since:
                        continue
                    heard.add(f)
                    lane = json.loads(f.read_text())
                except (OSError, ValueError):
                    continue
                said = " ".join(str(lane.get("answer") or "").split())[:600]
                if said and not NOISE.match(said):
                    answers.append((lane.get("lane"), said))
                    first = first or time.monotonic()
                    self.trace("delegation.fable_lane", did=did, lane=lane.get("lane"), secs=lane.get("secs"))
            if answers and (len(answers) >= want or time.monotonic() - first > LANE_MERGE_WAIT):
                break
            await asyncio.sleep(LANE_POLL)
        if not answers or self.closed or getattr(self, "latest_did", did) != did:
            return
        text = await self.merge_lanes(did, request, answers)
        self.trace("delegation.lanes_merged", did=did, n=len(answers), text=(text or "")[:500])
        if text and not NOISE.match(text) and not self.closed and getattr(self, "latest_did", did) == did:
            self.counts["fable_lanes_spoken"] += 1
            self.speak_for(did, text)

    async def merge_lanes(self, did, request, answers):
        """Two spoken sentences from the lanes, most important first; SKIP if they add nothing to what was said."""
        before = self.__dict__.get("first_said", {}).get(did, "")
        found = "\n".join(f"- {name}: {text}" for name, text in answers)
        text = await self.groq(
            "Merge helper findings into what Kevin hears on a live voice call: at most two short spoken sentences, "
            "the single most important fact first, no lane names, no plumbing. If the findings add nothing to what "
            "he already heard, reply exactly SKIP.",
            f"He asked: {request}\nAlready said: {before or '(nothing)'}\nFindings:\n{found}", 160)
        return text or answers[0][1][:280]

    async def ask_hermes(self, did, request):
        """The open chat when known (visible in the app afterwards), else the hidden voice session."""
        if self.chat_lookup and not self.chat:
            try:
                self.chat = await asyncio.wait_for(asyncio.shield(self.chat_lookup), 5)
            except Exception:  # noqa: BLE001
                self.chat = ""
            self.trace("chat.resolved", chat=self.chat)
        if self.chat:
            try:
                answer = await asyncio.wait_for(chat_continuity.ask_chat(
                    self.manager.http, self.token, self.profile, self.chat, request, DELEGATION_TIMEOUT_SECONDS),
                    DELEGATION_TIMEOUT_SECONDS)
                self.counts["chat_turns"] += 1
                self.trace("delegation.chat", did=did, chat=self.chat)
                return answer
            except Exception as exc:  # noqa: BLE001 - fall back to the voice session, never lose the turn
                self.counts["chat_failed"] += 1
                self.trace("delegation.chat_failed", did=did, chat=self.chat, error=type(exc).__name__,
                           detail=str(exc)[:300])
                LOG.warning("chat continuity failed id=%s: %s", self.id, str(exc)[:200])
        return await asyncio.to_thread(run_hermes, request, self.profile, True)

    async def audio_health(self):
        """Once a second for the first 60 s: frames each way and event-loop lag (choppy starts, 11-16)."""
        last = (0, 0)
        for _ in range(60):
            t = time.monotonic()
            await asyncio.sleep(1)
            lag = round((time.monotonic() - t - 1) * 1000)
            now = (self.counts["input_frames"], self.counts["output_frames"])
            self.trace("audio.rate", input=now[0] - last[0], output=now[1] - last[1], loop_lag_ms=lag)
            last = now
            if self.closed:
                return

    async def watchdog(self):
        await asyncio.sleep(35)
        voice_up = self.leg.ready if self.leg else self.provider.connectionState == "connected"
        if self.mobile.connectionState != "connected" or not voice_up:
            await self.close("connection_timeout")
            return
        await asyncio.sleep(MAX_CALL_SECONDS - 35)
        await self.close("duration_limit")

    async def maybe_handoff(self, reason):
        """Kevin left the call screen: hand the conversation to z0live on the phone (opt-out: HERMES_VOICE_HANDOFF=off)."""
        how = handoff.mode()
        if how == "off" or reason not in handoff.PHONE_CLOSES or not self.protocol.exchanges:
            return
        serial = os.environ.get("HERMES_VOICE_PHONE", "192.168.1.2:40059")
        try:
            front = await asyncio.to_thread(handoff.foreground, serial)  # traced only
            if not handoff.decide(reason, front, how):
                self.trace("handoff.skipped", front=front, mode=how)
                return
            await asyncio.to_thread(handoff.record, self.chat, self.profile, list(self.protocol.exchanges), self.id)
            started = await asyncio.to_thread(handoff.start_z0live, serial)
            self.trace("handoff.z0live", front=front, mode=how, started=started)
            LOG.info("handoff to z0live front=%s started=%s", front, started)
        except Exception as exc:  # noqa: BLE001 - a failed switch must never break the close
            LOG.warning("handoff failed: %s", type(exc).__name__)

    async def close(self, reason):
        if self.closed:
            return
        self.closed = True
        if self.haiku:
            self.haiku.close()
        self.send({"type": "session.closed", "reason": reason,
                   "usage": {"seconds": int(time.monotonic() - self.created)}})
        if self.provider_dc.readyState == "open":
            self.provider_dc.send(json.dumps({"type": "session.close"}))
        current = asyncio.current_task()
        for task in list(self.tasks):
            if task is not current:
                task.cancel()
        await asyncio.gather(self.mobile.close(), self.provider.close(), *([self.leg.close()] if self.leg else []),
                             *([self.guest.close()] if self.guest else []), return_exceptions=True)
        if self.relay:
            await self.relay.close()
            self.counts.update({"relay_" + k: v for k, v in self.relay.counts.items()})
        self.manager.calls.pop(self.id, None)
        self.manager.recent.append({"id": self.id, "reason": reason,
                                    "seconds": round(time.monotonic() - self.created, 1),
                                    "counts": dict(self.counts)})
        await self.maybe_handoff(reason)
        self.trace("call.closed", reason=reason, seconds=round(time.monotonic() - self.created, 1),
                   counts=dict(self.counts))
        self.trace.close()
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
    headers = {"Authorization": auth, "Host": UPSTREAM_HOST}
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
    auth = request.headers.get("Authorization", "")
    call.token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
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
