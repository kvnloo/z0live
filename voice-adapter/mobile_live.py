"""Subscription voice compatibility adapter for the official Hermes mobile client.

The dashboard authenticates mobile calls. The adapter sends audio and context to
voice providers and writes local traces that may include transcripts.
Codex signaling uses the z0live/OMP Frameless Bidi contract.
"""
from __future__ import annotations

import asyncio
import base64
from collections import Counter, defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import re
import logging
import os
import signal
from pathlib import Path
import selectors
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import uuid

import aiohttp
from aiohttp import ClientSession, ClientTimeout, web
from aiortc import RTCPeerConnection, RTCSessionDescription, RTCConfiguration
from aiortc.mediastreams import MediaStreamError
from aiortc import MediaStreamTrack
from call_trace import Trace, context_sent
from guest_floor import Address, Addressing, Answer, Floor, GREETING, IDLE_SECONDS, Leg, substantive
import numpy as np

LOG = logging.getLogger("hermes.mobile.subscription_voice")
import chat_continuity  # noqa: E402 - sibling module (voice turns land in the owner's open chat)
import call_brief  # noqa: E402 - sibling module (context packet in the session instructions)
import grok_leg  # noqa: E402 - sibling module (Grok Voice as the voice model)
import tiers  # noqa: E402 - sibling module (fast/medium/slow model lanes)
import lane_cards  # noqa: E402 - sibling module (the TV conductor's lane cards, spoken on their call)
import lane_layout  # noqa: E402 - sibling module (the lanes attached to the conversation)
import ledger as request_ledger  # noqa: E402 - sibling module (the request ledger: asks, promises, proof, results)
import ask_bot  # noqa: E402 - sibling module ("ask @pc to ...": another bot profile answers, outside the agent sandbox)
import live_status  # noqa: E402 - sibling module (status answers and the proof gate, from the live records)
import voice_approvals  # noqa: E402 - sibling module (N230: Hermes approval prompts spoken on the call, pass-phrase answers)
import guest_voice  # noqa: E402
import guest_join  # noqa: E402 - sibling module ("add Tek to the call": a second agent for this call only)
import answer_delivery  # noqa: E402 - sibling module (what each voice has actually said)
from subscription_leg import Speaker, SubscriptionLeg  # noqa: E402
import av  # noqa: E402
from aiortc.contrib.media import MediaRelay  # noqa: E402

# Z0LIVE_TIERS=on: a delegated request runs the tiers.py lanes (via lane_server.py) beside Hermes, instead of
# the warm-Haiku/Groq instant lane, the Fable lanes and the core lanes relay.
TIERS_ON = os.environ.get("Z0LIVE_TIERS", "off") == "on"
import needs_voice  # noqa: E402 - sibling module (blockers spoken when the TV is away)
import handoff  # noqa: E402 - sibling module (call continues on z0live when the owner leaves the screen)
import owner as owner_identity  # noqa: E402 - sibling module (who the prompts name: Z0LIVE_OWNER_NAME, default "the owner")
import passthru  # noqa: E402 - sibling module (z0 joins the owner's other video call)
import turn_slots  # noqa: E402 - sibling module (a call's bounded line of Hermes runs)
import voice_fragments  # noqa: E402 - sibling module (F5/F7: fragments never start a run; segments coalesce)
import topic_sessions  # noqa: E402 - sibling module (one warm Hermes chat per project group, not one per request)
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


FAST_SYSTEM = ("You are the instant fast lane ({who}) on the owner's live voice call; this very answer is the fast lane he "
               "hears first. Answer in one or two short spoken sentences from the context. Never guess whether something "
               "worked or what he heard. If the context can't answer it, reply with exactly SKIP (slower lanes are already "
               "checking; never say so yourself). Most important fact first. Never invent numbers. "
               "The owner's Hermes chat runs on his PC with adb to his phone: it can screenshot and read his screen, watch logs and run commands. Never say you can't see his screen or aren't connected to the call. Screen, phone and action requests: reply SKIP so the chat does them. "
               "His TV runs z0 lanes, the dashboard these calls drive; each question comes with what the TV shows now and "
               "what was said on the call so far: use them, and answer questions about the TV from them.")


TV_URL = os.environ.get("Z0LIVE_TV_URL", "http://127.0.0.1:8793")


def tv_screen(timeout=0.8) -> str:
    """What the owner's TV (z0 lanes) shows right now, as plain lines: the view and its words. '' when it is down."""
    import urllib.parse
    import urllib.request
    try:
        with urllib.request.urlopen(TV_URL + "/api/now", timeout=timeout) as r:
            show = (json.loads(r.read()) or {}).get("show") or {}
        view = show.get("view") or "home"
        q = urllib.parse.urlencode({"name": view, **(show.get("args") or {})})
        with urllib.request.urlopen(f"{TV_URL}/api/view?{q}", timeout=timeout) as r:
            dl = json.loads(r.read())
    except Exception:  # noqa: BLE001 - the TV server is optional
        return ""
    words = [" ".join(str(i.get("s", "")).split()) for i in dl.get("items", []) if i.get("k") == "text" and i.get("s")]
    text = " | ".join(w for w in words if w)[:1500]
    return (owner_identity.fill("The owner's 85-inch TV") + f" is showing the z0 lanes '{view}' view right now. Its words, top to bottom: {text}"
            if text else "")


_LIVE = {"at": 0.0, "text": "", "busy": False}


def live_picture(max_age: float = 5.0) -> str:
    """The TV's words plus the live lanes, from a cache: the race never waits on it (a cold TV view takes ~1 s to
    build, and even a 0.4 s wait shifted lane timing). A stale cache refreshes in a background thread."""
    if time.monotonic() - _LIVE["at"] > max_age and not _LIVE["busy"]:
        _LIVE["busy"] = True

        def refresh():
            try:
                _LIVE["text"] = "\n\n".join(x for x in (tv_screen(0.8), tiers.live_facts()) if x)
                _LIVE["at"] = time.monotonic()
            finally:
                _LIVE["busy"] = False
        threading.Thread(target=refresh, daemon=True).start()
    return _LIVE["text"]


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
        await self.ask("Reply with just: ready", 30)  # pays the cold start before the owner's first question

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
MAX_CALL_SECONDS = max(1, int(os.environ.get("HERMES_VOICE_MAX_CALL_SECONDS", "14400")))
PROVIDER_CAP_SECONDS = max(10, int(os.environ.get("HERMES_VOICE_PROVIDER_CAP_SECONDS", "7200")))
MAX_CALLS = 2
# A stop/restart drains: open calls keep running until they end (bounded), new ones are refused.
# Agents restarted this adapter mid-call twice on 2026-11-06 ("GPT-Live ended: service_shutdown").
DRAIN_SECONDS = int(os.environ.get("HERMES_MOBILE_VOICE_DRAIN", str(MAX_CALL_SECONDS)))
# A fast-lane "I found nothing" is not a first answer; saying it then being contradicted is worse than silence.
UNHELPFUL = re.compile(r"^\W*(not found|nothing found|no (local )?(evidence|results|record)|i (couldn.?t|could not|can.?t) find)", re.I)
# A hand-off line that promises new work or claims it is handled, before Hermes said what happened (takes 7, 9, 14 and
# 15: "Got it, I'll handle that and keep receipts", "I'm taking that on end-to-end", "Got it—I'll go step by step and
# show receipts", "Mm-hm. I'll take that."), or promises a follow-up ("I'll tell you as soon as it's done").
# replay.py reports these.
PROMISE = re.compile(r"\b(i'?m|i am) (taking|handling|on) (that|it|this)\b|\b(take|taking) (that|it|this) on\b"
                     r"|\bi'?ll (handle|take care of|take (that|it|this)\b|get (that|it|this) (done|going)|be back"
                     r"|go step by step|tell you (as soon as|when|once)|let you know"
                     r"|keep (you )?(posted|updated)|(keep|send|show)( you)?( the)? receipts"
                     r"|report back|follow up|circle back)|\bend[- ]to[- ]end\b|\bon it\b|\bconsider it done\b"
                     r"|\b(it'?s|that'?s) (handled|taken care of)\b", re.I)


def promises_new_work(text: str) -> bool:
    return bool(PROMISE.search(text.replace("\u2019", "'")))


# How a voice's own hand-off line starts a promise (PROMISE): its transcript runs with its audio, so the floor mutes the
# line at these words, before the rest is heard (takes 15 and 16: "Mm-hm. I'll take that", "Okay, I'll take that").
PROMISE_OPENER = re.compile(r"\b(i'?ll|i will|i'?m (taking|handling|on)|on it)\b", re.I)


def opens_promise(text: str) -> bool:
    text = text.replace("\u2019", "'")
    return bool(PROMISE_OPENER.search(text) or PROMISE.search(text))


PERSONA = owner_identity.fill("""You are Hermes, speaking with the user in the official Hermes mobile app.
Be natural, brief and direct. Do not speak until the user speaks. While the user tells a longer
story or thinks out loud, stay quiet and let them keep going: do not fill their pauses with "mm-hm",
"yeah" or "right". Start answering as soon as the user is clearly done; do not wait for a long silence. Listen while speaking
and stop immediately when interrupted. Handle greetings and ordinary small talk yourself.
You have no tools. For facts, lookups or actions, delegate the complete request to the client.
While that request is outstanding, wait silently for its answer; never infer system facts from earlier turns or fill the wait with status chatter.
the owner's Hermes chat runs on his PC with adb to his phone: it can screenshot and read his screen, watch logs and run commands. Never say you can't see his screen or aren't connected to the call. "Look at my screen" is a delegation, not a refusal.
The client is the user's existing Hermes chat with its chosen model, tools and approvals.
When you delegate, say at most a short neutral line such as "Let me check." The request may
already be running, may wait in line or may be refused, so never promise new work, say you are
taking it on or handling it, or promise receipts or a follow-up. The client's answer, queued
notice or refusal says what happened; say that.
Do not claim work happened until the client returns its actual result. Progress arriving on
the commentary channel is silent context, not something to read aloud. Present an Agent
Final Message as your own concise answer. Never narrate routing or backend plumbing.
If the owner says he is away from the TV, the TV is off, or asks to hear the results, read
the returned results aloud, one sentence per card; never direct him to a display instead.
Helper notes may arrive as silent context: a verified note or a correction about something
already answered is true; mention a correction briefly ("Actually, ...").
Adding or removing another agent on this call ("add Tek to the call", "remove PC") is a request:
delegate it and say only "One sec." The client brings the agent in and says so.
""")


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
# Fire-and-receipt (A0190/A0124): a request made while this call's chat turn runs goes to a Hermes background agent
# beside it, so a 95-411 s turn no longer holds (or redirects) it. A Hermes request still running after
# RECEIPT_SECONDS gets one spoken receipt; its answer is spoken when it lands. SIDE_MAX bounds the background agents
# one call may run at once; past it, a request waits in the call's line (QUEUE_MAX) and the voice says so. It is
# never submitted into the busy chat: the dashboard redirects the running turn, so the request had no answer of its
# own and the running request was answered with the redirect's words.
RECEIPT_SECONDS = float(os.environ.get("Z0LIVE_RECEIPT_SECONDS", "8"))
RECEIPT = "Still working on that in Hermes; ask anything else meanwhile."  # says what runs, promises nothing
SIDE_MAX = 3
# Task H (11-27): after that one receipt the voice went silent for up to 224 s (chat turns of 232 and 338 s; a fold
# into a busy turn got no line at all for 184 s). A request still running hears its elapsed time at PROGRESS_AT, and at
# CAP_SECONDS one honest line saying where its answer will land; the work goes on and its answer is still spoken if the
# call is open. Background agents answered 6 of 7 requests in 2.8-6.5 s (all 11-27 ones by 16 s), so past 90 s is the
# tail that went silent. A status line waits for a gap: never over the owner, never over the voice's own reply.
# Task I (11-27 15:14 call): status lines every few seconds. Each request ran its own timer (two receipts 5 s apart
# for requests 8 s apart), a progress line deferred past its mark landed 7-13 s before the cap, and a receipt played
# 6 s after the voice's own ack. One budget for the call now: no status line (receipt, progress, cap, queued notice,
# Hermes's progress ingress) within STATUS_GAP of any earlier one or of the voice's ack of a request. A receipt or
# progress line that cannot fit is dropped, not deferred; one line covers every request running (PROGRESS_MANY). The
# cap is never dropped: it waits for the gap and a quiet floor (other requests' acks hold it at most one gap past its
# mark), and says the real elapsed time. With an ack the
# shape is ack, one progress line near 45 s, the cap near 90 s; a request the voice never acked hears the receipt.
STATUS_GAP = float(os.environ.get("Z0LIVE_STATUS_GAP", "45"))
PROGRESS_AT = (45,)
PROGRESS_SLACK = 10  # a progress line waits this long past CAP_SECONDS - STATUS_GAP for a gap in the talk
CAP_SECONDS = float(os.environ.get("Z0LIVE_CAP_SECONDS", "90"))
PROGRESS = "Still on that in Hermes, {seconds} seconds so far."
PROGRESS_MANY = "Still on {count} requests in Hermes, the oldest {seconds} seconds so far."
CAP = ("No answer on that after {seconds} seconds. It keeps running in Hermes; if it finishes while we're still on, "
       "you'll hear it here, and it lands {where} and on the TV.")
WHERE = {"chat": "in your chat", "background": "in Hermes as a background result", "host": "in Hermes",
         "topic": "in its project chat in Hermes"}
# Task K (11-27 15:14 call): the owner said three times that after a kickoff the voice need not say anything. A request
# hears the one receipt, then nothing until its state changes (the answer, a failure, a result); the repeating progress,
# cap and merged lines are gone. The one other line: an action with a promise and no proof after UNPROVEN_SECONDS.
UNPROVEN_SECONDS = request_ledger.NOT_STARTED_SECONDS

UNPROVEN = "I still have no proof that one started, so treat it as not started."
STATUS = re.compile(r"Still working on that in Hermes|Still on that in Hermes, \d+ seconds so far"
                    r"|Still on \d+ requests in Hermes, the oldest \d+ seconds so far|No answer on that after \d+ seconds"
                    r"|I still have no proof that one started")
ACTION_WORDS = re.compile(r"\b(start|kick\s*(?:it\s*)?off|run|set\s*up|approve|reject|eject|install|fix|deploy|launch|"
                          r"restart|begin|build|schedule|spin\s*up|encrypt)\b", re.I)
QUESTION_LEAD = re.compile(r"^\W*(?:(?:and|so|okay|ok|well|hey|um|uh)\W+)*(what|why|how|when|where|who|which|is|are|was|were|"
                           r"did|does|do|has|have|had|will|would|should|tell|show|list|explain|describe|give|read|look|check|find|"
                           r"summari[sz]e|report)\b", re.I)
POLITE_LEAD = re.compile(r"^\W*(?:(?:and|so|okay|ok|well|please)\W+)*(?:can|could|would|will)\s+you\b|\b(?:go ahead and|let'?s|"
                         r"i need you to|i want you to|you should|please)\b", re.I)
# An answer that says it did NOT do the work is honest as it is: no caveat, and the item is blocked, not promised.
NEGATED = re.compile(r"\b(did not|didn't|could not|couldn't|can't|cannot|unable|wasn't able|was not able|haven't|hasn't|have not|"
                     r"has not|never|failed|fails|refused|blocked|not (?:started|run|running|possible|done|able|yet)|"
                     r"nothing (?:started|ran|was)|need(?:s)? (?:you|your))\b", re.I)
# What a background agent says when it claims the work moved: started, running, done.
CLAIMS_WORK = re.compile(
    r"\bi(?:'ve|\s+have|\s+just)?\s+(?:now\s+)?(?:started|kicked(?:\s+it)?\s+off|launched|scheduled|queued|dispatched|spun\s+up|"
    r"ejected|installed|approved|rejected|deployed|fixed|set\s+up|begun|created|restarted|ran)\b"
    r"|\bkicked\s+(?:it\s+|that\s+|them\s+)?off\b|\bkicking\s+off\b|\bon\s+it\b|\bunderway\b"
    r"|\b(?:it|that|they)(?:'s|'re|\s+is|\s+are|\s+was|\s+were)\s+(?:now\s+)?(?:started|running|launched|scheduled|ejected|"
    r"installed|approved|rejected|deployed|fixed|done|finished|completed|queued|going)\b"
    r"|\bnow\s+(?:running|working)\b|\brunning\s+now\b|^\W*(?:done|started|launched|ejected|installed|deployed)\b", re.I)
PROMISES_WORK = re.compile(r"\b(?:i'?ll|i will|i'?m going to|going to|let me|gonna|i can)\s+(?:go\s+)?(?:start|kick|run|set|launch|begin|"
                           r"spin|deploy|fix|install|eject|schedule|build|get (?:it|that|them) (?:going|started))\b", re.I)
PROOF_LINE = re.compile(r"^\W*PROOF:\s*(pid|kanban|cron|commit|file)\s+(\S+)\s*(.*)$", re.I | re.M)
NOT_CONFIRMED = "I haven't confirmed it started."
DONE_WORDS = re.compile(r"\b(done|finished|completed|ejected|installed|approved|rejected|deployed|fixed|pushed|merged)\b", re.I)
RUNNING_WORDS = re.compile(r"\b(started|kicked|running|launched|scheduled|underway|in progress|working on)\b", re.I)


def request_kind(request: str) -> str:
    """action for a request that asks for work to be done (start/run/eject/approve...), else question. "What's
    running" and "how does the run work" are questions; "can you start it" is an action."""
    text = " ".join(str(request or "").split())
    if not ACTION_WORDS.search(text):
        return "question"
    if QUESTION_LEAD.match(text) and not POLITE_LEAD.search(text):
        return "question"
    return "action"


def split_proof(answer: str):
    """(answer without PROOF lines, [(kind, ref, detail)]): a background agent proves work with `PROOF: kind ref`."""
    proofs = [(m.group(1).lower(), m.group(2), m.group(3).strip()) for m in PROOF_LINE.finditer(answer or "")]
    return PROOF_LINE.sub("", answer or "").strip(), proofs


def unproven_claim(answer: str) -> str:
    """The agent's own words, plus the honest caveat: never discard what it said."""
    return f"{' '.join(answer.split())} {NOT_CONFIRMED}"


def proof_exists(kind: str, ref: str, profile: str = "") -> bool:
    """Does a PROOF line's ref exist? file: the path; pid: a live process; kanban/cron: the Hermes CLI knows the id;
    commit: some local repo has the object. Blocking (subprocess): call it off the event loop."""
    ref = str(ref).strip()
    try:
        if kind == "file":
            return Path(ref).expanduser().exists()
        if kind == "pid":
            os.kill(int(ref), 0)
            return True
        flags = ["-p", profile] if profile else []
        if kind == "kanban":
            return subprocess.run([HERMES_BIN, *flags, "kanban", "show", ref], capture_output=True, timeout=10).returncode == 0
        if kind == "cron":
            out = subprocess.run([HERMES_BIN, *flags, "cron", "list"], capture_output=True, timeout=10, text=True)
            return out.returncode == 0 and ref in out.stdout
        if kind == "commit" and re.fullmatch(r"[0-9a-f]{7,40}", ref):
            repos = [Path.home() / "workspace/repos" / d for d in os.listdir(Path.home() / "workspace/repos")]
            return any(subprocess.run(["git", "-C", str(r), "cat-file", "-e", ref + "^{commit}"], capture_output=True,
                                      timeout=10).returncode == 0 for r in repos if (r / ".git").exists())
    except (OSError, ValueError, subprocess.SubprocessError):
        return False
    return False
# Delegated requests run as background agents beside the chat (2.8-16 s on 11-27) even when it is idle: chat turns
# took 24.9-338 s. Z0LIVE_CHAT_TURNS=1 puts a request into an idle chat's own turn instead (never a running one).
CHAT_TURNS = os.environ.get("Z0LIVE_CHAT_TURNS", "") == "1"
# The call opens with a <=1500-char read-only brief of the owner's other threads (call_brief.cross_thread, 11-28 F4).
CROSS_BRIEF = os.environ.get("Z0LIVE_CROSS_BRIEF", "1") != "0"
CROSS_BRIEF_WAIT = float(os.environ.get("Z0LIVE_CROSS_BRIEF_WAIT", "1.0"))


def status_line(text: str, part: bool = False) -> bool:
    """A line saying the work runs (receipt, progress, cap), not an answer; part: a fragment of one."""
    if not part:
        return bool(STATUS.search(text))
    frag = re.sub(r"\d+", "N", " ".join(text.split()))
    lines = ([RECEIPT, PROGRESS.format(seconds="N"), PROGRESS_MANY.format(count="N", seconds="N")]
             + [CAP.format(seconds="N", where=w) for w in WHERE.values()])
    return bool(frag) and any(frag in line for line in lines)
# The background a Hermes run gets when it does not run in the call's own chat (Tek's Hermes, the oneshot path).
CALL_CONTEXT = ("This request comes from a live voice call; answer in a few short spoken sentences. Below is the call's "
                "brief and the chat the call was opened from: background, already handled. Use it to place the "
                "request (which repo, PR, card or device it means). If what the request asks for is already running "
                "(a kanban card, a session, a job), report its status and latest receipts instead of starting it again.")
# Hermes runs one call may have going at once on the host path (no linked chat). Past it a request waits for a free
# run, and the voice says it is queued instead of claiming a hand-off that has not happened.
HOST_MAX = 3
# N230: an ask to another bot's host runs on its Runs API, so a Hermes approval prompt is spoken on the call and the owner's
# pass-phrase approves it once (voice_approvals.py). Z0LIVE_VOICE_APPROVALS=0 restores the plain chat-completions ask.
VOICE_APPROVALS = os.environ.get("Z0LIVE_VOICE_APPROVALS", "1") != "0"
APPROVAL_QUIET_S = 2.5  # words heard while a prompt waits, then this long with none: answered (deny unless the phrase)
# The line past HOST_MAX (or past the chat turn plus SIDE_MAX): at most QUEUE_MAX requests wait, first in, first
# out. Past that a request is refused out loud at once and never runs, so nothing waits without the caller knowing.
QUEUE_MAX = max(0, int(os.environ.get("Z0LIVE_QUEUE_MAX", "3")))
QUEUED = ("That one hasn't started yet: I'm still on {running} earlier requests, so it's queued as number {position}. "
          "It starts as soon as one of them finishes.")
FULL = ("I can't take that one right now: {running} requests are running and {waiting} more are waiting, so I "
        "haven't started it. Ask me again once one of them finishes.")

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
    lock = VOICE_LOCKS[profile]
    if not lock.acquire(blocking=False):
        # The voice session is busy with an earlier turn. Waiting for it held the owner's long-run ask for the whole call
        # (11-22 film call, take 7), so this turn runs now, beside it, in a session of its own.
        return _hermes_once(request, profile, "", own_lane)[0]
    try:
        sid, last = VOICE_SESSIONS.get(profile, ("", 0.0))
        resume = sid if sid and time.monotonic() - last < VOICE_SESSION_IDLE_SECONDS else ""
        text, new_sid = _hermes_once(request, profile, resume, own_lane)
        VOICE_SESSIONS[profile] = (new_sid or resume, time.monotonic())
        return text
    finally:
        lock.release()


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


# The subscription endpoint rejects a context append over 500 tokens ("Context append text must not exceed 500
# tokens") and drops that piece. A token is at least one UTF-8 byte, so <= 500 bytes can never exceed it, whatever
# the text (dense IDs, paths and numbers run ~2 bytes/token, so a 1800-byte brief chunk failed on 11-20).
MAX_APPEND_BYTES = 500


def chunks(text: str, limit: int = MAX_APPEND_BYTES) -> list[str]:
    """Split text into pieces of at most min(limit, MAX_APPEND_BYTES) UTF-8 bytes, preferring line/word breaks."""
    limit = max(1, min(limit, MAX_APPEND_BYTES))
    result, current, size = [], "", 0
    for char in text:
        n = len(char.encode("utf-8"))
        if size + n > limit:
            cut = max(current.rfind("\n"), current.rfind(" "))
            if cut > len(current) // 2:  # break at a line/word boundary when one is reasonably close
                result.append(current[: cut + 1])
                current = current[cut + 1:]
            else:
                result.append(current)
                current = ""
            size = len(current.encode("utf-8"))
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
        # The caller's words since the voice last answered or delegated: a request is all of them. The provider's
        # delegation carries only its last speech segment ("Lane", "requirements, and how far we got").
        self.asked: list[str] = []
        # With two voices, only the caller's words to this voice: words to Tek never join a request to Hermes.
        self.own: Callable[[str], str] = lambda said: said
        self.user_open = False  # a user turn has started and its turn.done has not arrived
        self.assistant_open = False  # the voice's reply has started and its turn.done has not arrived
        self.deferred: list[tuple[str, str]] = []  # (did, segment): delegations that beat the user's turn.done
        self.last_request = ""  # the request most recently queued for a host run
        self.turn_text = {"user": "", "assistant": ""}
        self.last_user = ""
        self.last_delegation = ""
        self.exchanges = deque(maxlen=8)  # finished (role, text) turns, for the z0live handoff
        self.seen = deque(maxlen=256)
        self.started_at = time.monotonic()
        # F5/F7 input boundary: <=3-word fragments never run alone (held in `asked` for the next segment) unless they
        # answer the voice's open question; words older than the coalesce window never join a new request.
        self.fragments = voice_fragments.Gate()
        self.held: dict[str, str] = {}  # did -> the fragment its delegation carried (no run; traced by the caller)

    def transcript(self, role: str, text: str) -> dict:
        return {"type": f"session.{ 'input' if role == 'user' else 'output' }_transcript.delta",
                "delta": text, "start_ms": int((time.monotonic() - self.started_at) * 1000),
                "end_ms": int((time.monotonic() - self.started_at) * 1000)}

    def from_provider(self, event: dict, *, assistant_audible: bool = True) -> list[dict]:
        kind = event.get("type")
        if not assistant_audible and (kind == "output_transcript.added" or (
                kind == "turn.done" and (event.get("turn") or {}).get("role") == "assistant")):
            if kind == "turn.done":
                self.turn_text["assistant"] = ""
            elif isinstance(text := (event.get("item") or {}).get("text"), str):
                # Muted words are never the phone's, even at the turn.done of a reply unmuted later (take 16:
                # "Okay, I'll take that" reached the phone's transcript at 55.15 s, muted on its audio).
                self.turn_text["assistant"] = (self.turn_text["assistant"] + text)[-16000:]
            return []
        if kind in ("session.started", "session.closed", "error"):
            if kind == "session.started":  # a renewed session has no reply open (a lost turn.done never sticks)
                self.assistant_open = False
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
        if kind == "turn.created":
            if (event.get("turn") or {}).get("role") == "user":
                self.user_open = True
                self.assistant_open = False  # the caller talks: that turn gates now, and a cut reply never sticks
            else:  # the voice is replying, so the caller's turn is over even without its turn.done
                self.user_open = False
                self.assistant_open = True
                while self.deferred:
                    self.release(*self.deferred.pop(0))
            return []
        if kind == "turn.done":
            turn = event.get("turn") or {}
            role, text = turn.get("role"), turn.get("transcript", "")
            if role not in self.turn_text or not isinstance(text, str):
                return []
            prior = self.turn_text[role]
            self.turn_text[role] = ""
            said = (text or prior).strip()
            if role == "assistant":
                self.assistant_open = False
            if role == "user":
                self.last_user = text
                self.user_open = False
                if said and (mine := self.own(said)):
                    if self.fragments.user(mine):  # quiet past the coalesce window: the old words are stale
                        self.asked.clear()
                    self.asked.append(mine)
                while self.deferred:
                    self.release(*self.deferred.pop(0))
            elif self.fragments.assistant(said):
                said = ""  # a backchannel inside the owner's open thought: not an exchange, never ends his words
            elif substantive(said) and not self.deferred:
                self.asked.clear()  # the voice answered these words itself; a backchannel ("Mm-hm.") keeps them
            if said:
                self.exchanges.append((role, said))
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
                if self.user_open:  # the caller's turn.done (0.2-0.4 s behind) carries the whole utterance
                    self.deferred.append((did, request))
                else:
                    self.release(did, request)
                return result
            result.append({"type": "session.delegation.created", "delegation": {
                "id": did, "type": "delegation", "target": "client"}})
            return result
        return []

    def release(self, did: str, segment: str):
        """Queue one host run whose request is the caller's words since the voice last answered or delegated. GPT-Live
        can make several delegations for one utterance (11-21 probe: "Can you look at my studio" at 17.2 s, "and tell
        me how much disk space is free" at 20.7 s); a delegation with no new words joins the request already sent
        (request None: no run of its own)."""
        def vocab(text):
            return set(re.findall(r"\w+", text.casefold()))

        words, segment = " ".join(self.asked), self.own(segment) if segment else segment
        if not words and segment and vocab(segment) <= vocab(self.last_request):
            self.pending_host.append((did, None))
            return
        if segment and not vocab(segment) <= vocab(words):
            words = f"{words} {segment}".strip()
        request = words or self.own(self.last_user)
        if not request and self.last_user.strip():  # every word was to the other voice: nothing for this one to run
            self.asked.clear()
            self.pending_host.append((did, None))
            return
        verdict = self.fragments.check(request)
        if not verdict.run:  # "NPC", "Yeah": no run; the words stay in `asked` for the next segment in the window
            self.held[did] = request
            self.pending_host.append((did, None))
            return
        self.asked.clear()
        request = verdict.request
        self.last_request = request
        self.pending_host.append((did, request))

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


# Local per-call traces. Read them with trace_report.py.
_TRACE_ENV = "HERMES_MOBILE_VOICE_TRACE_DIR"
_TRACE_DEFAULT = Path("/mnt/zer0models/sft-svlm/data/voice-traces/hermes-mobile-live")
_TRACE_CONFIGURED = Path(os.environ.get(_TRACE_ENV, _TRACE_DEFAULT)).expanduser()
_TEST_TRACE_SESSION = None


def _test_trace_guard():
    return "unittest" in sys.modules and os.environ.get("Z0LIVE_ALLOW_UNSAFE_TEST_TRACES") != "1"


if _test_trace_guard():
    # Override inherited launchd/shell settings for the suite and any children it starts.
    _TEST_TRACE_SESSION = tempfile.TemporaryDirectory(prefix="z0live-unittest-traces-")
    os.environ[_TRACE_ENV] = _TEST_TRACE_SESSION.name
TRACE_DIR = Path(os.environ.get(_TRACE_ENV, _TRACE_DEFAULT)).expanduser()


def _call_trace_dir():
    if _test_trace_guard():
        # Check at use time so a patched TRACE_DIR cannot bypass isolation.
        day = (TRACE_DIR / time.strftime("%Y-%m-%d")).resolve()
        protected = (_TRACE_CONFIGURED, _TRACE_DEFAULT, Path.home() / ".local/share/z0live/voice-traces")
        if (not day.is_relative_to(Path(tempfile.gettempdir()).resolve())
                or any(day.is_relative_to(root.resolve()) for root in protected)):
            raise RuntimeError("unittest voice traces require a temporary directory")
    return TRACE_DIR


ADAPTER_REV = hashlib.sha1(Path(__file__).read_bytes()).hexdigest()[:12]
RELEASE = Path(__file__).resolve().parents[1].name  # releases/<sha>/voice-adapter: the deployed commit, on /_z0live/ready
LEVEL_ON = 800          # s16 peak (~-32 dBFS) that counts as sound
LEVEL_OFF_SECONDS = 0.3  # quiet this long ends a sound span
OUTPUT_FRAME_GRACE = 0.06  # the most recent loud frame still represents playback
_SILENCE = {1920: bytes(1920), 3840: bytes(3840)}


def slim(event, limit=1500):
    raw = json.dumps(event, ensure_ascii=False, default=str)
    return event if len(raw) <= limit else {"type": event.get("type"), "truncated": raw[:limit]}


class Level:
    """Sound on/off spans of one audio direction, from frame peaks (energy, not speech recognition)."""
    def __init__(self, call, direction):
        self.call, self.direction, self.on, self.quiet, self.peak = call, direction, None, 0.0, 0

    def frame(self, frame, speaker: Leg | None = None):
        try:
            pcm = np.frombuffer(frame.planes[0], dtype=np.int16,
                                count=frame.samples * (len(frame.layout.channels) if frame.format.is_packed else 1))
            peak = max(int(pcm.max()), -int(pcm.min())) if pcm.size else 0
        except Exception:  # noqa: BLE001 - tracing must never break audio
            return
        now = time.monotonic()
        if peak >= LEVEL_ON:
            self.quiet = 0.0
            self.peak = max(self.peak, peak)
            if self.on is None:
                self.on = now
                self.call.trace("audio.on", dir=self.direction, peak=peak, muted=self.call.muted, speaker=speaker)
        elif self.on is not None:
            self.quiet = self.quiet or now
            if now - self.quiet >= LEVEL_OFF_SECONDS:
                self.call.trace("audio.off", dir=self.direction, at=round(self.quiet - self.call.trace.t0, 3),
                                span=round(self.quiet - self.on, 3), peak=self.peak)
                self.on, self.quiet, self.peak = None, 0.0, 0
        return peak >= LEVEL_ON


class AudioBridge(MediaStreamTrack):
    kind = "audio"

    def __init__(self, source, call, direction):
        super().__init__()
        self.source, self.call, self.direction = source, call, direction
        self.level = Level(call, direction)
        self.counter = direction + "_frames"

    async def recv(self):
        frame = await self.source.recv()
        self.call.counts[self.counter] += 1
        call = self.call
        prior_on = self.level.on
        if self.direction == "input" and not call.muted:
            if self.level.frame(frame):
                call.input_loud_at = time.monotonic()
            if prior_on is None and self.level.on is not None:
                call.trace.begin_turn(self.level.on - call.output_loud_at <= OUTPUT_FRAME_GRACE)
        if self.direction == "output" and call.duplex:
            frame = call.duplex.output(frame)
        if self.direction == "input" and not call.heard_user and self.level.on is not None and not call.muted:
            call.heard_user = True
            call.trace("output.gate_open", gated_frames=call.counts["output_gated"])
        # The voice must not speak before the owner does: GPT-Live sometimes starts a turn (a lone "Mm-h", a sniff,
        # "He Hey") ~0.4 s into the session while the persona context lands. Silence its audio until he speaks.
        overlap = (self.direction == "output" and call.protocol.user_open
                   and time.monotonic() - call.input_loud_at < 0.25)
        if (self.direction == "input" and call.muted) or (self.direction == "output" and not call.heard_user) or overlap:
            if self.direction == "output":
                call.counts["output_gated"] += 1
            for plane in frame.planes:
                plane.update(bytes(plane.buffer_size))
        if self.direction == "output":
            if self.level.frame(frame, call.duplex.floor.owner if call.duplex else None):
                call.output_loud_at = time.monotonic()
            if call.passthru and call.passthru.active:
                call.passthru.voice(frame)  # z0's voice into the meeting (floor-gated)
                if call.passthru.phone_muted():
                    for plane in frame.planes:
                        plane.update(bytes(plane.buffer_size))
        else:
            if call.muted:
                self.level.frame(frame)  # let the input span end during mute
            # Group flow: the meeting is never mixed in here. This frame feeds the owner's own provider leg (his full
            # profile) and every other leg; no leg hears the room until a sandboxed guest route exists.
            call.trace.record_audio(frame)
        return frame


async def start_subscription(http, peer, persona, voice, trace):
    await peer.setLocalDescription(await peer.createOffer())
    payload = {"sdp": peer.localDescription.sdp,
               "session": {"model": MODEL, "instructions": persona,
                           "audio": {"output": {"voice": voice}}, "delegation": {"type": "client"}}}
    headers = await asyncio.to_thread(codex_headers)
    trace("signaling.request", voice=voice)
    async with http.post(SIGNALING, json=payload, headers=headers, timeout=ClientTimeout(total=35)) as resp:
        trace("signaling.answer", status=resp.status, voice=voice)
        if resp.status not in (200, 201):
            # Vendor bodies can contain account details. Keep them out of logs/responses.
            LOG.warning("subscription signaling rejected status=%s voice=%s", resp.status, voice)
            if resp.status in (401, 403):
                raise RuntimeError("Codex subscription authorization was rejected; refresh the host Codex login")
            if resp.status == 429:
                raise RuntimeError("The ChatGPT subscription voice quota is exhausted")
            raise RuntimeError(f"Subscription voice signaling failed (HTTP {resp.status})")
        context_sent(trace, {"type": "signaling.session", "session": payload["session"]}, 0, leg="primary" if persona == PERSONA else "guest")
        answer = await resp.text()
        if not answer.startswith("v=0"):
            raise RuntimeError("Subscription voice returned an invalid SDP answer")
    await peer.setRemoteDescription(RTCSessionDescription(sdp=answer, type="answer"))


@dataclass
class UserTurn:
    id: str
    began: float
    start_ms: int
    end_ms: int
    text: str = ""
    address: Address | None = None
    committed: bool = False
    continuation: bool = False
    interrupted: bool = False
    opened_on: Leg = "primary"  # the floor when the provider opened this turn (one turn can span exchanges)
    done: bool = False  # the provider closed it (turn.done)


@dataclass
class VoiceReply:
    allowed: bool
    began: float
    id: str | None = None
    spoken: str = ""
    held: str = ""  # what it said while the phone could not hear it: never heard, so never credited (unmute)
    cut: bool = False


# With more than one person at the phone, the people often talk to each other (11-22 film call, take 7).
PEOPLE_NOTE = ("(The people on the call are talking to each other, not to you. Nothing you said to that was heard and "
               "nothing was handed off. Stay quiet until someone says your name or asks you directly.)")
PEOPLE_PAUSE_SECONDS = 1.5  # a pause in the people's talk long enough for a bot to give an answer it was asked for
EOU_SECONDS = 0.8  # with people on the call, a caller utterance ends after this much mic quiet with no new words
# A rest waits for the caller's open turn to close, unless it has had no new words this long (a silent leg can hold a
# turn open across exchanges). Take 16 sent line 3's rest 0.8 s before its turn closed, and the voice merged the two.
OPEN_TURN_SECONDS = 3.0


class GPTGuest:
    """The call's floor and the second GPT-Live voice. With `config` None there is no second voice: the floor alone,
    for a call where several people share the phone with Hermes (Call.read_history)."""

    def __init__(self, call, config):
        self.call, self.config = call, None
        self.addressing = Addressing(())
        self.floor = Floor(time.monotonic(), self.floor_event)
        self.users: dict[Leg, UserTurn] = {}
        self.replies: dict[Leg, VoiceReply] = {}
        self.tails: dict[Leg, tuple[VoiceReply, float]] = {}  # last allowed reply and its turn.done; audio drains after
        self.ended: dict[Leg, tuple] = {}  # (start_ms, end_ms) of a reply that ended with the mic quiet
        # People on the call: caller turns since the floor last decided, and provider delegations held for its hand-off.
        self.utterance: list[UserTurn] = []
        self.offered: dict[Leg, list[tuple[str, float]]] = {}
        self.eou_timer = None
        self.last_output = ""
        self.note_budget = {"primary": 12000, "guest": 12000}
        # Answers handed to a voice, delivered once the voice has said them (answer_delivery).
        self.ledger = answer_delivery.Ledger()
        self.superseded: set[str] = set()  # delegations whose stopped answer the caller moved on from
        self.unanswered: set[Leg] = set()  # legs the floor handed a request and nothing of its answer yet
        self.quiet_since: dict[Leg, float | None] = {"primary": None, "guest": None}  # last reply or caller turn end
        self.user_heard_at = float("-inf")  # the latest caller transcript event on either leg
        self.flush_timer: asyncio.TimerHandle | None = None
        self.persona = ""
        self.subscription = None
        self.protocol = Protocol(host_delegation=True)
        self.closed = False
        self.audio = bytearray()
        self.session = "z0live-guest-" + call.id  # this call's own guest session: the fallback when the group's is busy
        self.topics = topic_sessions.Router()
        if config:
            self.adopt(config)

    def adopt(self, config):
        """The second voice on this floor: named by voices.json at call start, or added mid-call by the owner's words
        (guest_join: "add Tek to the call"). A floor that had no voice (people on the call) keeps its state."""
        # The transcriber hears Tek as "Tech", "Teck", "Tec" or "Tech's" (takes 9 and 14); only a vocative names a voice.
        aliases = (("tek", "tech's", "tech’s", "tech", "teck", "tec", "teknium", "டெக்", "டெக்னியம்")
                   if config.profile == "tek" else tuple(guest_join.SPOKEN.get(config.profile, ())))
        self.config = config
        self.addressing = Addressing(tuple(dict.fromkeys((config.name, *aliases))))
        identity = "Tek (also Teknium, டெக் or டெக்னியம்)" if config.profile == "tek" else config.name
        self.persona = owner_identity.fill(f"You are {identity}, a guest on the owner's call. When the owner addresses you or continues "
                        "that exchange, answer briefly. Do not reply to other exchanges or backchannels. "
                        "For questions about your work or history, delegate to your own Hermes, then speak "
                        "its answer. When you delegate, say at most a short neutral line such as \"Let me check\"; "
                        "never promise new work, say you are taking it on or handling it, or promise receipts or a "
                        "follow-up. Never invent work. Notes about what Hermes said are silent background.")
        self.subscription = self.new_subscription(active=True)
        self.protocol = Protocol(host_delegation=True)
        self.closed = False
        for table in (self.users, self.replies, self.tails, self.ended):
            table.pop("guest", None)

    def new_subscription(self, *, active=False):
        return SubscriptionLeg(self.trace, self.message, self.failed, self.on_track, active=active)

    def attach(self, track):
        self.subscription.attach(track)

    def trace(self, event, **data):
        self.call.trace("guest." + event, **data)

    async def start(self):
        await start_subscription(self.call.manager.http, self.subscription.peer, self.persona, self.config.voice,
                                 self.trace)
        await asyncio.wait_for(self.subscription.ready.wait(), 10)

    def send(self, event):
        channel = self.subscription and self.subscription.channel
        if not self.closed and channel and channel.readyState == "open":
            channel.send(json.dumps(event))

    def note(self, text):
        for part in chunks(text):
            self.send({"type": "session.context.append", "channel": "developer",
                       "content": [{"type": "input_text", "text": part}]})

    def floor_event(self, event, **data):
        if event == "floor.changed":
            reply, tail = self.replies.get(data["previous"]), self.tails.get(data["previous"])
            if reply:
                if reply.allowed and not reply.cut:
                    self.cut(data["previous"])
                reply.cut = True
            elif tail and not tail[0].cut and time.monotonic() - max(tail[1], self.call.output_loud_at) < LEVEL_OFF_SECONDS:
                self.cut(data["previous"])  # its last words were still playing out
            self.tails.pop(data["previous"], None)
            self.audio.clear()
        self.call.trace(event, **data)
        if event == "floor.changed" and data["reason"] not in ("answer_ready", "guest_idle", "guest_failed",
                                                                 "guest_left", "guest_joined"):
            self.supersede(data["speaker"])  # the caller gave this voice the floor with a new request
        if event == "floor.changed" and data["speaker"] == "people" and data["reason"] == "people_talk":
            # Both voices heard words that were not for them; what they said to it was never heard.
            self.note(PEOPLE_NOTE)
            self.call.guest_note(PEOPLE_NOTE)
        if event == "floor.changed" and data["reason"] == "answer_ready":
            self.unmute(data["speaker"], data["since"])

    def cut(self, leg: Leg):
        """Playback on this leg stopped before the phone heard it all: a barge-in, the floor moving off a playing
        reply, or the provider ending the reply under the caller's words. The unheard rest of each answer it was
        saying goes back to the head of this leg's queue, to be said once the caller is quiet (take 13, take 14)."""
        queue = self.floor.answers[leg]
        for did, text in reversed(self.ledger.cut(leg)):
            for retold in [a for a in queue if a.retold and a.delegation_id == did]:  # the cut's rest replaces it
                queue.remove(retold)
            queue.appendleft(Answer(leg, did, text, rest=True))
            self.call.trace("floor.answer_cut", speaker=leg, did=did)

    def retell(self, leg: Leg):
        """A reply on this leg ran its course: an answer it began is heard unless it left out a paragraph naming
        something it never said (answer_delivery.Entry.skipped). Those paragraphs go back to the head of the queue,
        once (take 16: the resumed line-3 rest was read as paragraph 2 and a paraphrase of paragraph 3; paragraph 1
        and card t_0a96e2de were never said, yet counted spoken as the reply began)."""
        for entry, rest in reversed(self.ledger.finished(leg)):
            if rest:
                self.floor.answers[leg].appendleft(Answer(leg, entry.did, rest, rest=True, retold=True))
                self.call.trace("floor.answer_unsaid", speaker=leg, did=entry.did, sends=entry.sends)
            else:
                self.call.trace("floor.answer_spoken", speaker=leg, did=entry.did, sends=entry.sends)

    def supersede(self, leg: str):
        """The caller asked this voice something new: a stopped answer is not coming back, its rest or any of its
        words still to come (take 15: line 2's whole answer went out as the reply to line 3, 83 ms after its rest
        was dropped)."""
        if leg == "people":
            return
        queue = self.floor.answers[leg]
        self.superseded |= {a.delegation_id for a in queue if a.rest and a.delegation_id is not None}
        for answer in [a for a in queue if a.rest or a.delegation_id in self.superseded]:
            queue.remove(answer)
            self.call.trace("floor.answer_dropped", speaker=leg, did=answer.delegation_id, reason="new_request")
        self.ledger.drop(leg, self.superseded)  # nor resent (take 16: resent 16 ms after it was dropped)

    def unmute(self, leg: Leg, now: float):
        """The floor came back to this leg so it can give the answer it was handed. A reply it has open was muted
        when it opened (the floor was elsewhere) or cut when the people talked, and a muted reply never becomes
        audible: take 12's line-3 answer was read into one and lost. That reply is where the voice reads the answer,
        so the phone hears it from here on, and the ledger credits what it says. Unless, before its answer came, it
        already began a promise unheard: take 19's hand-off ack said \" I'll\" muted, then \"get the full picture\" aloud."""
        reply = self.replies.get(leg)
        if reply is None or (reply.allowed and not reply.cut) or not self.call.heard_user:
            return
        if leg in self.unanswered and opens_promise(reply.held):
            self.audio.clear()  # its answer unmutes it (flush_answers)
            self.call.trace("floor.promise_muted", speaker=leg, turn=reply.id)
            return
        reply.allowed, reply.cut, reply.began = True, False, now
        self.quiet_since[leg] = None
        self.floor.reply_started(leg, now)
        self.call.trace("floor.reply_unmuted", speaker=leg, turn=reply.id)

    def set_people(self, people: int):
        """More than one person at the phone: a voice speaks only when named or asked right after its own reply."""
        self.floor.people = people
        if people > 1:
            self.call.trace("floor.people", people=people)
            if self.floor.reason == "call_start":
                self.floor.change("people", "people_on_call", time.monotonic())

    def transport_replaced(self, source: Leg):
        self.users.pop(source, None)
        self.replies.pop(source, None)  # the retired session never sends turn.done for a reply it had open
        self.tails.pop(source, None)
        self.ended.pop(source, None)
        # The new session never saw these answers: what it had not said goes out again, without the old session's
        # delegation ids.
        resend = [Answer(source, entry.did, text, entry.lane) for entry, text in self.ledger.retire(source)]
        self.floor.answers[source].extendleft(reversed(resend))
        self.flush_answers()

    def commit_user(self, source: Leg, now: float, *, final: bool):
        """One person on the call: the primary leg's caller turn moves the floor as it is heard (a name at once)."""
        user = self.users.get(source)
        if not user or user.committed or not user.text.strip():
            return
        if self.echoes(user.text, over=7):
            return
        address = self.addressing(user.text, final=final)
        if address is not None and address != user.address:
            user.address = address
            self.floor.user_turn(address, user.text, user.began, now, final=final)
        elif final and address is None and user.end_ms - user.start_ms >= 400:
            guest_reply = self.replies.get("guest")
            if not guest_reply or not guest_reply.allowed or guest_reply.cut:
                self.floor.user_turn(None, user.text, user.began, now, final=True, interrupted=user.interrupted)
        if final:
            user.committed = True
            if substantive(user.text):  # the next request to this voice: what a cut reply left unsaid is not coming back
                self.supersede(self.floor.owner)

    def heard_caller(self, user: UserTurn):
        """People on the call: the floor decides once per caller utterance, at EOU_SECONDS of mic quiet and no new
        words. Not at turn.done: a leg can hold a turn open, end it at a bot's "Right" (take 10) or split it."""
        if user.committed:
            return  # the floor already decided on this turn (its late turn.done)
        if user not in self.utterance:
            self.utterance.append(user)
        if self.eou_timer:
            self.eou_timer.cancel()
        self.eou_timer = self.later(EOU_SECONDS, self.end_of_utterance)

    def end_of_utterance(self):
        now = time.monotonic()
        quiet = now - max(self.user_heard_at, self.call.input_loud_at)  # new words, or the mic (words trail it, take 10)
        if quiet < EOU_SECONDS - 0.01 and not self.call.closed:
            self.eou_timer = self.later(EOU_SECONDS - quiet, self.end_of_utterance)
            return
        self.eou_timer, turns, self.utterance = None, self.utterance, []
        text = " ".join(u.text.strip() for u in turns).strip()
        if not text or self.call.closed or self.echoes(text, over=7):
            return
        address, began, before = self.addressing(text, final=True), turns[0].began, self.floor.owner
        follow = address is None and self.floor.follow_up(text, began)
        self.call.trace("floor.utterance", began=began, address=address and address.value, follow=follow,
                        owner=before, text=text[:200])
        self.floor.user_turn(address, text, began, now, final=True)
        leg = self.floor.owner
        routed = follow or (address, leg) in ((Address.GUEST, "guest"), (Address.OTHER, "primary"))
        mine = self.addressing.own_words(text, guest=leg == "guest", lead=follow or before == leg)
        for u in turns:
            u.committed = True
        # Only the name ("Hey, Tek."): the floor is that bot's, and the request that follows is its follow-up.
        if not routed or not substantive(self.addressing.vocative.sub(" ", mine)):
            return
        # F5: the adapter's own hand-off is a delegation route too: a fragment ("NPC", "Yes please") never runs alone.
        verdict = (self.protocol if leg == "guest" else self.call.protocol).fragments.check(mine)
        if not verdict.run:
            self.call.trace("floor.fragment_held", speaker=leg, request=mine[:200])
            return
        mine = verdict.request
        last = self.__dict__.setdefault("floor_asks", {})
        if follow:
            self.supersede(leg)  # a cut answer's rest is not coming back
            previous = last.get(leg)
            if previous and previous not in self.superseded:
                self.superseded.add(previous)
                self.call.trace("floor.answer_dropped", speaker=leg, did=previous, reason="new_request")
        self.unanswered.add(leg)  # until its answer comes, the voice's own line may not promise (observe, unmute)
        self.unmute(leg, now)  # the voice began answering before the utterance ended: that reply is for it
        held = [did for did, at in self.offered.pop(leg, []) if at >= began - 1.0]  # the provider's own delegation
        did = held[-1] if held else f"z0live-{leg}-{uuid.uuid4().hex[:12]}"
        last[leg] = did
        self.call.trace("floor.handoff", speaker=leg, did=did, provider=bool(held), request=mine[:300])
        if leg == "guest":
            self.floor.guest_jobs += 1
            self.call.spawn(self.delegate(did, mine))
        else:
            self.call.spawn(self.call.host_delegate(did, mine))

    def observe(self, source: Leg, event: dict) -> bool:
        """Return whether this leg's assistant transcript belongs on the phone."""
        now = time.monotonic()
        kind = event.get("type")
        turn = event.get("turn") or {}
        role = turn.get("role")
        user = self.users.get(source)
        if role == "user" and kind == "turn.created":
            span = self.ended.pop(source, None)
            if span and None not in span and span[0] < turn.get("start_ms", -1) < span[1]:
                self.cut(source)  # the provider ended that reply under these words
                self.flush_answers()
            self.users[source] = user = UserTurn(turn["id"], now, turn.get("start_ms", 0), turn.get("end_ms", 0),
                                                  turn.get("transcript") or "", opened_on=self.floor.owner)
        elif kind == "turn.delta" and user and event.get("turn_id") == user.id:
            end_ms = event.get("end_ms", user.end_ms)
            start_ms = event.get("start_ms", end_ms - 200)
            # A silent leg can keep one user ID open across several exchanges. Only split at a gap in the provider's
            # word timestamps (never at a mic onset or a delayed packet), or at the floor's decision on this turn.
            # Words that follow the decided ones closer than EOU_SECONDS were said before it, and the transcriber
            # was late with them: they end that utterance (take 16: line 3's "receipts" came 17 ms after the
            # decision, became its own utterance and gave the floor to the people).
            gap = start_ms - user.end_ms
            if gap >= 1500 or (user.committed and self.floor.people > 1 and gap >= EOU_SECONDS * 1000):
                self.users[source] = user = UserTurn(user.id, now, start_ms, end_ms, continuation=True,
                                                      opened_on=user.opened_on)
            user.end_ms = end_ms
            user.text = (user.text + event.get("delta", ""))[-2000:]
        elif role == "user" and kind == "turn.done":
            if not user or (user.committed and user.id != turn["id"]):  # a turn whose turn.created never came
                self.users[source] = user = UserTurn(turn["id"], now, turn.get("start_ms", 0), turn.get("end_ms", 0),
                                                      opened_on=self.floor.owner)
            if user.id != turn["id"]:
                return False
            user.done = True
            if not user.committed and not user.continuation:
                user.text = turn.get("transcript") or user.text
        else:
            user = None
        decides = source == "primary"  # one transcript decides: both legs hear the same mic (takes 9 and 14)
        if user:
            self.user_heard_at = now
            if decides:
                self.interrupt(user, now)
                if self.floor.people > 1:
                    self.heard_caller(user)
                else:
                    self.commit_user(source, now, final=kind == "turn.done")
            if kind == "turn.done":
                self.quiet_since[source] = now  # the voice gets its chance to answer this turn first
        if decides and self.floor.people == 1 and (kind in ("delegation.created", "output_transcript.added")
                                                   or (role == "assistant" and kind == "turn.created")):
            self.commit_user(source, now, final=True)
        if kind == "output_transcript.added" or role == "assistant":
            reply = self.replies.get(source)
            if kind != "turn.done" and reply is None:
                # Mid-utterance the floor has not decided: a reply opened now waits (end_of_utterance unmutes it).
                reply = self.replies[source] = VoiceReply(
                    self.call.heard_user and self.floor.owner == source and not self.utterance, now)
                self.ledger.reply_started(source)
                self.quiet_since[source] = None
                if reply.allowed:
                    self.floor.reply_started(source, now)
            if reply is not None:
                allowed = reply.allowed and not reply.cut and self.floor.owner == source
                words = (turn.get("transcript") if kind == "turn.created" else
                         (event.get("item") or {}).get("text") if kind == "output_transcript.added" else None)
                if allowed and source in self.unanswered and words and opens_promise(reply.spoken + words):
                    reply.cut, allowed = True, False  # its answer unmutes it (flush_answers)
                    self.audio.clear()
                    self.floor.reply_finished(source, now, reply.began)
                    self.call.trace("floor.promise_muted", speaker=source, turn=turn.get("id") or reply.id)
                if kind == "turn.created":
                    reply.id = turn.get("id")
                elif kind == "output_transcript.added" and allowed:
                    text = (event.get("item") or {}).get("text", "")
                    reply.spoken = (reply.spoken + text)[-1500:]
                    self.last_output = " ".join(re.findall(r"\w+", reply.spoken.casefold()))
                    self.ledger.heard(source, text)  # begun is not heard: the reply's end decides (retell)
                elif kind == "output_transcript.added":
                    reply.held = (reply.held + (event.get("item") or {}).get("text", ""))[-1500:]
                elif kind == "turn.done" and reply.id in (None, turn.get("id")):
                    self.replies.pop(source)
                    if reply.spoken.strip():
                        target: Leg = "guest" if source == "primary" else "primary"
                        # Only the gist: with the whole reply as context, the other voice re-told it when the owner
                        # came back to it (11-21 probe). Its first sentence keeps it oriented.
                        gist = re.match(r"\s*(.{1,200}?[.!?])(?=\s|$)", reply.spoken)
                        gist = (gist.group(1) if gist else reply.spoken.strip()[:200]).strip()
                        length = min(len(gist), self.note_budget[target])
                        if length:
                            self.note_budget[target] -= length
                            name = self.config.name if source == "guest" else "Hermes"
                            note = (f"{name} said " + owner_identity.fill("(the owner heard it; silent context, never repeat or summarize it), ")
                                    + f"starting: {gist[:length]}")
                            (self.note if target == "guest" else self.call.guest_note)(note)
                    if allowed:
                        self.floor.reply_finished(source, now, reply.began)
                        self.tails[source] = reply, now
                        self.delivered(source, self.ledger.reply_done(source))
                        # A reply that ends before its answer did is a cut when it was stopped: under the caller
                        # (take 14: the mic was loud while it played) or mid-sentence. One that ran its course, quiet
                        # and on a sentence end, said what the voice chose to say (take 15 re-queued its last
                        # sentence at 144.7 and 150.9 s, and Tek's at 106.0 s).
                        span = (turn.get("start_ms"), turn.get("end_ms"))
                        under = user is None and (caller := self.users.get(source)) and None not in span \
                            and span[0] < caller.start_ms < span[1]  # the caller's turn came first (take 14)
                        if (self.call.input_loud_at >= reply.began or under
                                or not re.search(r"[.!?][\"')]*\s*$", reply.spoken)):
                            self.cut(source)
                        else:  # the caller's turn can arrive just after (take 14): it began inside this reply
                            self.ended[source] = span
                            self.retell(source)
                    self.quiet_since[source] = now
                    self.flush_answers()
                return allowed
        return False

    def interrupt(self, user: UserTurn, now: float):
        if (user.committed or user.interrupted or user.end_ms - user.start_ms < 400 or not substantive(user.text)
                or now - self.call.output_loud_at > 0.18):
            return
        if self.echoes(user.text):
            return
        source = self.floor.owner
        if self.playing(source):
            reply = self.replies.get(source) or self.tails[source][0]
            reply.cut = True
            self.audio.clear()
            user.interrupted = True
            self.floor.reply_finished(source, now, reply.began)
            self.call.trace("floor.barge_in", speaker=source, user_turn=user.id)
            self.cut(source)

    def delivered(self, leg: Leg, entries):
        for entry in entries:
            self.call.trace("floor.answer_spoken", speaker=leg, did=entry.did, sends=entry.sends)

    def own_words(self, leg: Leg, said: str) -> str:
        """The part of a finished caller turn on this leg that was spoken to this leg's voice (its request)."""
        user = self.users.get(leg)
        lead = (user.opened_on if user else self.floor.owner) == leg
        return self.addressing.own_words(said, guest=leg == "guest", lead=lead)

    def echoes(self, text: str, *, over: int = 0) -> bool:
        """Whether the caller's words (over `over` characters) are the voice's own recent ones ("no" is not "know")."""
        words = " ".join(re.findall(r"\w+", text.casefold()))
        return len(words) > over and f" {words} " in f" {self.last_output} "

    def playing(self, leg: Leg) -> bool:
        """Whether the phone hears this leg: its open allowed reply, or the last one until the output has been quiet
        for LEVEL_OFF_SECONDS (WebRTC audio keeps arriving after the provider's turn.done)."""
        reply = self.replies.get(leg)
        if reply is None and leg in self.tails:
            tail, ended = self.tails[leg]
            if time.monotonic() - max(ended, self.call.output_loud_at) < LEVEL_OFF_SECONDS:
                reply = tail
        return bool(reply and reply.allowed and not reply.cut and self.floor.owner == leg)

    def may_delegate(self, leg: Leg, did: str) -> bool:
        """A voice's delegation runs only while that voice holds the floor (a request to Tek never runs in the owner's
        Hermes, and Tek never acts on the owner's requests to Hermes). With people on the call it is held for the
        adapter's own hand-off (end_of_utterance)."""
        if self.floor.people > 1:
            self.offered.setdefault(leg, []).append((did, time.monotonic()))
            self.call.trace("floor.delegation_held", speaker=leg, did=did, owner=self.floor.owner)
            return False
        if self.floor.owner == leg:
            return True
        self.call.trace("floor.delegation_dropped", speaker=leg, did=did, owner=self.floor.owner)
        return False

    def later(self, seconds, callback):
        return asyncio.get_running_loop().call_later(seconds, callback)

    def flush_answers(self):
        if self.call.closed:
            return
        if self.flush_timer:
            self.flush_timer.cancel()
            self.flush_timer = None
        now = time.monotonic()
        reply = self.replies.get(self.floor.owner)
        busy = (reply and reply.allowed and not reply.cut) or self.ledger.pending(self.floor.owner)
        if not busy:
            self.floor.expire(now)
        def waiting(leg, retold=True):
            return bool(self.ledger.pending(leg) or any(retold or not a.retold for a in self.floor.answers[leg]))
        owner = self.floor.owner
        # An answer asked for earlier waits for the people's pause, and Tek's for idle Hermes (floor_sim, line 5).
        # Sentences Hermes skipped wait for Tek's answer, and get the floor back once Tek is idle (take 16).
        idle = not busy and owner not in self.replies and not self.utterance
        handover = self.floor.people > 1 and idle and (
            (owner == "primary" and not getattr(self.call, "pending_delegations", ())
             and not waiting(owner, retold=False) and waiting("guest"))
            or (owner == "guest" and not self.floor.guest_jobs and not waiting(owner)
                and any(a.retold for a in self.floor.answers["primary"])))
        if handover and self.playing(owner):  # its last words are still playing out: hand over once they end
            self.flush_timer = self.later(LEVEL_OFF_SECONDS, self.flush_answers)
            return
        if owner == "people" or handover:
            if not (waiting("primary") or waiting("guest")):
                return
            if (not self.call.provider_ready
                    or now - max(self.call.input_loud_at, self.user_heard_at) < PEOPLE_PAUSE_SECONDS):
                self.flush_timer = self.later(0.5, self.flush_answers)
                return
            self.floor.answer_ready(lambda leg: bool(self.ledger.pending(leg)), now)
        source = self.floor.owner
        queue, pending = self.floor.answers[source], self.ledger.pending(source)
        # A stopped answer's rest waits for the caller's pause, counted from the stop too (take 14).
        user = self.users.get(source)
        rest_waits = bool(queue and queue[0].rest and (now - max(
            self.call.input_loud_at, self.user_heard_at, self.quiet_since.get(source) or 0) < PEOPLE_PAUSE_SECONDS
            or (user and not user.done and now - self.user_heard_at < OPEN_TURN_SECONDS)))
        if (queue or pending) and (not self.call.provider_ready or now - self.call.input_loud_at < 0.4 or rest_waits
                                   or self.utterance):  # mid-utterance: take 11 answered the ask with line 2's
            self.flush_timer = self.later(0.25, self.flush_answers)
            return
        protocol, send = ((self.protocol, self.send) if source == "guest" else
                          (self.call.protocol, lambda msg: self.call.send(msg, to_provider=True)))

        def hand(did, text):
            did = did if did in protocol.seen else None
            for msg in protocol.from_mobile({"type": "session.commentary.append", "delegation_id": did,
                                             "content": text}):
                send(msg)
            return did

        # A sent answer is resent only after the voice had its chance and stayed quiet without saying it (observe
        # credits what it says); a second miss is dropped.
        quiet = source not in self.replies and not self.playing(source)
        dropped = False
        if quiet:
            for entry, text in self.ledger.due(source, now, self.quiet_since[source]):
                if text is None:
                    self.call.trace("floor.answer_dropped", speaker=source, did=entry.did, reason="unspoken")
                    dropped = True
                    continue
                did = hand(entry.did, text)
                self.ledger.resent(entry, now)
                self.call.trace("floor.answer_resent", speaker=source, did=did, of=entry.did, sends=entry.sends)
        # New answers wait only for their floor. On it, they go out at once, even mid-reply: the voice merges them.
        while queue:
            answer = queue.popleft()
            if answer.lane and getattr(self.call, "latest_did", answer.delegation_id) != answer.delegation_id:
                self.call.trace("floor.answer_dropped", speaker=source, did=answer.delegation_id, reason="stale")
                continue  # a lane's guess for a question the owner has moved on from
            if answer.delegation_id in self.superseded:
                self.call.trace("floor.answer_dropped", speaker=source, did=answer.delegation_id, reason="new_request")
                continue
            # The cue is the floor's, not the answer's: the ledger keeps the rest bare, so a rest of a rest never
            # says it twice (take 15, 152.5 s).
            did = hand(answer.delegation_id, answer_delivery.resumed(answer.text) if answer.rest else answer.text)
            # Into a reply already playing: that reply was made before this answer existed (take 15, line 3).
            self.ledger.sent(source, answer.delegation_id, answer.text, now, lane=answer.lane, late=source in self.replies)
            if answer.rest:
                self.call.trace("floor.answer_resumed", speaker=source, did=answer.delegation_id)
            self.call.trace("floor.answer_sent", speaker=source, did=did, of=answer.delegation_id,  # did: the voice's id
                            lane=answer.lane)
            if source in self.unanswered:  # the voice reads its answer into the reply it has open
                self.unanswered.discard(source)
                self.unmute(source, now)
        due = self.ledger.next_due(source, self.quiet_since[source]) if source not in self.replies else None
        if due is not None:  # a reply still open on this leg flushes again when it ends (observe)
            self.flush_timer = self.later(max(0.05, due - now) if quiet else 0.25, self.flush_answers)
        elif source == "guest" and not busy and not self.floor.guest_jobs:
            since = self.floor.replied_at if self.floor.replied_at is not None else self.floor.since
            self.flush_timer = self.later(max(0.01, since + IDLE_SECONDS - now), self.flush_answers)
        elif dropped:  # this leg may have nothing left: another's answer can take the floor (floor_sim seed 49)
            self.flush_timer = self.later(0.05, self.flush_answers)

    def message(self, raw):
        if self.closed or not isinstance(raw, str) or len(raw) > 262144:
            return
        try:
            event = json.loads(raw)
        except ValueError:
            return
        if not isinstance(event, dict):
            return
        kind = event.get("type")
        self.call.trace("guest.rx", type=kind, event=slim(event))
        if kind == "session.started":
            self.floor.available = True
            self.call.trace("guest.ready", voice=self.config.voice, profile=self.config.profile)
        elif kind == "session.closed":
            self.failed("session_closed")
            return
        allowed = self.observe("guest", event)
        for message in self.protocol.from_provider(event, assistant_audible=allowed):
            if message.get("type") == "session.output_transcript.delta":
                self.call.send(message)
        self.run_pending()

    def run_pending(self):
        for did, request in self.protocol.pending_host:
            if (fragment := self.protocol.held.pop(did, None)) is not None:
                self.call.trace("guest.delegation.fragment", did=did, request=fragment[:200])
            elif request is not None and self.may_delegate("guest", did):
                self.floor.guest_jobs += 1
                self.call.spawn(self.delegate(did, request))
        self.protocol.pending_host.clear()
        if self.floor.owner == "guest" or self.floor.answers["primary"]:
            self.flush_answers()

    async def delegate(self, did, request):
        profile = self.config.profile
        # One guest session per project group and day (topic_sessions), not one per call: on 11-28 six calls each
        # asked "Tek, what did you work on today" in six fresh sessions. A busy one (another call's turn) falls back
        # to this call's own session.
        group, _ = self.topics.route(request)
        session = topic_sessions.day_session("z0live-guest", group) if topic_sessions.ENABLED else self.session
        self.call.trace("guest.delegation.start", profile=profile, did=did, session=session, group=group.key)
        try:
            answer = None
            # Asks for the day session take turns (another call's guest may be in it); past the wait, or on a 409
            # from another process, this call's own session runs it: never dropped.
            async with topic_sessions.hold(session, topic_sessions.HOLD_WAIT) as held:
                if held:
                    try:
                        answer = await ask_guest_host(self.call.manager.http, profile, session, request,
                                                      self.call.host_context())
                    except RuntimeError as exc:
                        if not topic_sessions.busy_error(str(exc)) or session == self.session:
                            raise
            if answer is None:
                self.call.trace("guest.delegation.busy", profile=profile, did=did, session=session)
                session = self.session
                answer = await ask_guest_host(self.call.manager.http, profile, session, request,
                                              self.call.host_context())
            self.call.trace("guest.delegation.done", profile=profile, did=did, session=session, answer=answer)
        except Exception as exc:  # noqa: BLE001 - explain failure on the guest, never fall back to the primary
            self.call.trace("guest.delegation.failed", profile=profile, did=did, detail=str(exc)[:300])
            answer = f"I couldn't reach the {profile} Hermes profile for that answer."
        self.floor.guest_jobs -= 1
        if not self.call.closed:
            self.floor.enqueue(Answer("guest", did, answer), self.config.name)
            self.flush_answers()

    def failed(self, reason, line=None):
        if self.closed or self.call.closed:
            return
        self.closed = True
        self.call.trace("guest.failed", reason=reason)
        self.off(line, "guest_failed")

    def leave(self, line):
        """the owner sent the guest off the call ("remove Tek from the call"). The floor stays for Hermes."""
        if self.closed or self.call.closed or self.config is None:
            return False
        self.closed = True
        self.call.trace("guest.left", profile=self.config.profile)
        self.off(line, "guest_left")
        return True

    def off(self, line, reason):
        for table in (self.users, self.replies, self.tails, self.ended):
            table.pop("guest", None)
        self.audio.clear()
        unsaid = self.ledger.retire("guest")
        if reason == "guest_left":
            # the owner sent Tek off: what Tek never said leaves with it. Join probe 13:29: the rest of Tek's answer, cut
            # by the owner's "Thanks Tek. Hermes, back to you", was read by Hermes right after "Tek is off the call".
            for did in [entry.did for entry, _ in unsaid] + [a.delegation_id for a in self.floor.answers["guest"]]:
                self.call.trace("floor.answer_dropped", speaker="guest", did=did, reason="guest_left")
            self.floor.answers["guest"].clear()
        else:  # it failed: what Tek was handed and never said goes to the primary voice with the rest of its answers
            self.floor.answers["guest"].extend(Answer("guest", entry.did, text) for entry, text in unsaid)
        self.floor.failed(self.config.name, time.monotonic(), line=line, reason=reason)  # (drops its hello)
        self.flush_answers()
        if self.subscription:
            self.call.spawn(self.subscription.close())

    def on_track(self, track):
        self.call.spawn(self.drain(track))

    async def drain(self, track):
        resampler = av.AudioResampler(format="s16", layout="mono", rate=48000)
        try:
            while not self.closed:
                frame = await track.recv()
                if track is not self.subscription.output:
                    return
                if self.playing("guest"):
                    for out in resampler.resample(frame):
                        self.audio.extend(memoryview(out.planes[0])[:out.samples * 2])
                    del self.audio[:-5760]  # at most 60 ms across the two RTP clocks
                elif self.audio:
                    self.audio.clear()
        except MediaStreamError:
            if track is self.subscription.output:
                self.failed("audio_ended")

    def output(self, frame):
        source = self.floor.owner
        if self.playing(source):
            if source == "primary":
                return frame
            if self.audio:
                n = frame.samples * 2
                pcm = bytes(self.audio[:n]).ljust(n, b"\0")
                del self.audio[:n]
                return guest_voice.shaped(pcm, frame)
        for plane in frame.planes:
            plane.update(_SILENCE.get(plane.buffer_size) or bytes(plane.buffer_size))
        return frame

    async def close(self):
        self.closed = True
        if self.flush_timer:
            self.flush_timer.cancel()
        if self.subscription:
            await self.subscription.close()


async def ask_guest_host(http, profile, session, request, context=""):
    key = (Path.home() / ".config/z0live/guest-hosts" / (profile + ".key")).read_text().strip()
    port = guest_join.port(profile) or int(os.environ.get("Z0LIVE_GUEST_HOST_PORT", "18644"))
    headers = {"Authorization": "Bearer " + key, "X-Hermes-Session-Id": session}
    messages = [{"role": "system", "content": "You are answering as a guest on a voice call. Give a brief spoken "
                 "answer. Never message, post, deploy, or change settings from this call. Do not quote secrets."},
                *([{"role": "system", "content": context}] if context else []),
                {"role": "user", "content": request}]
    async with http.post(f"http://127.0.0.1:{port}/v1/chat/completions", json={"messages": messages},
                         headers=headers, allow_redirects=False,
                         timeout=ClientTimeout(total=DELEGATION_TIMEOUT_SECONDS)) as response:
        if response.status != 200:
            raise RuntimeError(f"Guest Hermes host returned HTTP {response.status}")
        if response.headers.get("X-Hermes-Session-Id") != session:
            raise RuntimeError("Guest Hermes host did not confirm the voice session")
        answer = (await response.json())["choices"][0]["message"]["content"]
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("Guest Hermes host returned no answer")
        return answer


class Call:
    def __init__(self, manager, profile: str = ""):
        self.manager = manager
        self.profile = profile
        self.id = "hermes-subscription-" + uuid.uuid4().hex
        self.trace = Trace(self.id, _call_trace_dir())
        self.mobile = RTCPeerConnection(RTCConfiguration(iceServers=[]))
        self.subscription = self.new_subscription(active=True)
        self.primary_audio: Speaker | None = None
        self.microphone: tuple[MediaRelay, AudioBridge] | None = None
        self.opening_context = []
        self.pending_delegations = set()
        # One line of Hermes runs per call: host runs (no linked chat), or the chat turn plus its background agents.
        self.host_slots = turn_slots.TurnSlots(HOST_MAX, QUEUE_MAX)
        self.chat_slots = turn_slots.TurnSlots(1 + SIDE_MAX, QUEUE_MAX)
        self.told_queued: dict[str, float] = {}  # did -> when the voice was told it is queued (no "still working")
        self.status_at = float("-inf")  # the call's last status line or ack (STATUS_GAP): one budget for every source
        self.line_at = float("-inf")  # the call's last status line alone (a starved cap still keeps this gap)
        self.status_covered: dict[str, set] = {}  # did -> receipt/progress a line for the whole call already gave it
        self.status_running: dict[str, float] = {}  # did -> start, for requests whose status loop still runs
        self.ack_due = False  # a request started and the voice has not finished its reply (the ack) since
        self.hermes_path: dict[str, str] = {}  # did -> where its Hermes run went: background, chat or host (WHERE)
        # One warm Hermes chat per project group for this call's requests, not a bg_* session per request.
        self.topics = topic_sessions.Router()
        self.topic_primed: set[str] = set()  # group chats that already got this call's brief
        self.background_asks: dict[str, str] = {}
        self.findings: dict[str, str] = {}
        self.protocol = Protocol(host_delegation=HOST_DELEGATION)
        self.counts = Counter()
        self.created = time.monotonic()
        self.closed = False
        self.muted = False
        self.heard_user = False  # output audio stays silent until the phone sends sound (AudioBridge)
        self.mobile_dc = None
        self.to_mobile = deque(maxlen=256)
        self.to_provider = deque(maxlen=256)
        self.tasks = set()
        self.relay = LaneRelay.from_env() if (LaneRelay and HOST_DELEGATION) else None
        self.lane_said = {}  # delegation id -> (lane answer, entry id)
        self.spoken_for: dict = {}  # delegation id -> every line handed to a voice for it (lane cards never repeat)
        self.ask_dids: dict[str, str] = {}  # conductor ask text (lane_cards.request_key) -> delegation id
        self.item_of: dict[str, str] = {}  # delegation id -> request ledger item id
        self.earlier_cards: list[tuple[str, dict]] = []
        self.cards_read: set[str] = set()  # cards handed to voice, not merely seen in state.json
        self.earlier_this_turn = 0
        self.card_turn = 0
        self.readback_did: str | None = None
        self.readback_turn = -1
        self.tv_present = False  # unknown is not evidence that the caller can see a display
        self.speak_requested = False
        self.cards_seen: set[str] = set()  # conductor lane ids already spoken or skipped
        self.card_watch = None  # the watch_cards task, while one runs
        self.cards_busy = False  # a lane of this call's asks is still running (state.json, last read)
        self.last_delegation_at = float("-inf")
        self.handed_at = float("-inf")  # when speak_for last handed a voice a line
        # N216: status answers come from the live records (live_status); the voice must say them verbatim.
        self.status_sources = None  # live_status.Sources, built for this call's profile on first use (tests set it)
        self.status_clock = time.time  # the read time spoken with a status answer (the replay sets the call's clock)
        self.verbatim = None  # (did, status answer, handed at): the voice's next reading of it is checked once
        # Status answers and the proof gate use the records as they are now. replay.py turns this off: an offline
        # replay of a recorded call has no records of that call's minute, and today's would make it nondeterministic.
        self.live_records = True
        # N230: Hermes approval prompts this call's host runs raised, oldest first; the head is the one spoken and the
        # one the owner's next utterance answers (pass-phrase -> once, anything else -> deny, silence -> Hermes' timeout).
        self.approvals: list[dict] = []
        self.approval_quiet_until = float("-inf")  # the voice's own delegation of an approval answer is swallowed
        self.arm_scrub(voice_approvals.load_phrase(self.phrase_profile()))
        self.token = ""  # the app's own bearer, for submitting voice turns into its open chat
        self.chat_lookup = None
        self.context_text = ""  # recent chat history text, for the ttft lane
        self.call_note = ""  # the brief the chat agent wrote for this call (call_brief.written), read once at start
        self.haiku = None  # WarmHaiku, started once chat context is known
        self.chat = ""   # stored id of the chat Live Voice was started from ('' = hidden voice session)
        self.output_loud_at = self.input_loud_at = float("-inf")
        self.engine = voice_engine()
        if self.engine not in ("gpt-live", "grok"):  # on_device (STT -> model -> TTS) is not built yet
            self.trace("engine.unavailable", wanted=self.engine, using="gpt-live")
            self.engine = "gpt-live"
        self.leg = grok_leg.GrokLeg(self, PERSONA) if self.engine == "grok" else None
        self.passthru = passthru.Passthru(self) if passthru.enabled() else None
        self.guest_config = guest_voice.configured() if self.engine == "gpt-live" else None
        self.guest = None
        self.people = 1  # people sharing the phone (call_brief.people), set once the history is read
        if self.guest_config:
            self.guest = (GPTGuest(self, self.guest_config) if self.guest_config.engine == "gpt-live"
                          else guest_voice.GuestVoice(self, self.guest_config))
        self.brief = ""  # the call brief the primary opened with (shared with the Grok guest)
        self.duplex = self.guest if isinstance(self.guest, GPTGuest) else None  # the call's floor, if it has one
        if self.duplex:  # each voice's request is only the caller's words to that voice
            self.protocol.own = lambda said: self.guest.own_words("primary", said)
            self.guest.protocol.own = lambda said: self.guest.own_words("guest", said)
        self.trace("call.created", profile=profile, engine=self.engine,
                   model=grok_leg.URL.rsplit("=", 1)[-1] if self.leg else MODEL, rev=ADAPTER_REV, pid=os.getpid(),
                   host_delegation=HOST_DELEGATION, lanes=bool(self.relay),
                   guest=self.guest_config.engine if self.guest_config else "")

        @self.mobile.on("track")
        def mobile_track(track):
            if track.kind != "audio":
                return
            mic = AudioBridge(track, self, "input")
            if self.leg:
                self.leg.attach(mic)
            else:
                self.microphone = MediaRelay(), mic
                self.subscription.attach(self.subscribe_mic())
                if self.guest:
                    self.guest.attach(self.subscribe_mic())

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

        @self.mobile.on("connectionstatechange")
        async def changed():
            self.trace("peer.state", peer="mobile", state=self.mobile.connectionState)
            if self.mobile.connectionState in ("failed", "closed"):
                self.spawn(self.close("transport_closed"))

    @property
    def provider(self):
        return self.subscription.peer

    @property
    def provider_dc(self):
        return self.subscription.channel

    @property
    def provider_ready(self):
        return self.subscription.ready.is_set()

    def subscribe_mic(self):
        relay, mic = self.microphone
        return relay.subscribe(mic, buffered=False)

    def new_subscription(self, *, active=False):
        return SubscriptionLeg(self.trace, lambda raw: self.message(raw, True),
                               lambda reason: self.spawn(self.close(reason)), self.attach_primary_output, active=active)

    def attach_primary_output(self, track):
        self.primary_audio = Speaker(track)
        source = self.primary_audio
        if isinstance(self.guest, guest_voice.GuestVoice):
            source = guest_voice.FloorSwitch(source, self.guest)
        self.mobile.addTrack(AudioBridge(source, self, "output"))

    def spawn(self, coro):
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    def flush(self, channel, pending):
        while pending and channel.readyState == "open":
            item = pending.popleft()
            if channel is self.provider_dc:
                self.send_provider(*item)
            else:
                channel.send(json.dumps(item))

    def send_provider(self, event, turn_id):
        self.provider_dc.send(json.dumps(event))
        context_sent(self.trace, event, turn_id)

    def send(self, event, to_provider=False, *, turn_id=None):
        self.trace("tx", to="provider" if to_provider else "mobile", type=event.get("type"), event=slim(event))
        if to_provider:
            if turn_id is None:
                turn_id = self.trace.delegations.get(event.get("delegation_item_id"), self.trace.turn_id)
            if self.leg:
                self.leg.outbound(event, turn_id)
                return
        channel = self.provider_dc if to_provider else self.mobile_dc
        pending = self.to_provider if to_provider else self.to_mobile
        if channel is not None and channel.readyState == "open" and (not to_provider or self.provider_ready):
            if to_provider:
                self.send_provider(event, turn_id)
            else:
                channel.send(json.dumps(event))
        else:
            if len(pending) == pending.maxlen:
                self.spawn(self.close("event_buffer_full"))
                return
            pending.append((event, turn_id) if to_provider else event)

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
        heard, readback_final = None, False
        if from_provider and self.approvals:  # N230: his words while a prompt waits are its answer, not a turn
            try:
                event, heard = self.approval_mask(kind, event)
            except Exception as exc:  # noqa: BLE001 - an exception here would close the data channel (the call)
                # Fail closed: drop this event (it may carry the phrase unmasked) and leave the prompt to Hermes'
                # own timeout. Probe 11-28 05:08: a TypeError raised here hung up the whole call.
                self.counts["approval_mask_error"] += 1
                self.trace("approval.error", type=kind, error=type(exc).__name__)
                return
        self.trace("rx", src="provider" if from_provider else "mobile", type=kind, event=slim(event))
        self.counts[("provider:" if from_provider else "mobile:") + kind] += 1
        if from_provider and kind == "session.started":
            self.subscription.ready.set()
            self.flush(self.provider_dc, self.to_provider)
        if (from_provider and kind == "input_transcript.added" and not self.heard_user  # speech too quiet for Level
                and str((event.get("item") or {}).get("text") or "").strip()):
            self.heard_user = True
            self.trace("output.gate_open", gated_frames=self.counts["output_gated"], by="transcript")
        if not from_provider and kind in ("session.input_audio.mute", "session.input_audio.unmute"):
            self.muted = kind.endswith(".mute")
            return
        final_action, final_id = "", ""
        if from_provider:
            if kind == "input_transcript.added" or (kind == "turn.created" and (event.get("turn") or {}).get("role") == "user"):
                self.controlled_final = False
            elif kind == "delegation.created" and getattr(self, "controlled_final", False):
                self.trace("delegation.joined", did=(event.get("item") or {}).get("id"), reason="spoken_action")
                return  # a late provider paraphrase belongs to the already handled caller turn
            if kind == "input_transcript.added":
                text = (event.get("item") or {}).get("text")
                if isinstance(text, str) and text.strip():
                    self.trace.begin_turn()
            elif kind == "turn.done":
                turn = event.get("turn") or {}
                text = turn.get("transcript", "")
                if turn.get("role") == "user" and isinstance(text, str):
                    self.trace.finish_turn(text or self.protocol.turn_text["user"])
                    said = text or self.protocol.turn_text["user"]
                    if heard is not None:
                        pass  # N230: an approval answer (approval_mask decided it); never a status question
                    elif said:
                        final_action = said
                        final_id = str(turn.get("id") or uuid.uuid4().hex[:8])
                        self.card_turn += 1
                        self.earlier_this_turn = 0
                elif turn.get("role") == "assistant" and self.ack_due:  # the voice acked a request: the gap starts
                    self.ack_due = False
                    self.status_at = time.monotonic()
                if turn.get("role") == "assistant" and self.verbatim:  # N216: did it keep the status numbers?
                    self.check_verbatim(text if isinstance(text, str) and text else self.protocol.turn_text["assistant"])
            if self.passthru:
                self.passthru_event(kind, event)
        duplex = self.duplex
        if from_provider:
            audible = duplex.observe("primary", event) if duplex else True
            if isinstance(self.guest, guest_voice.GuestVoice) and self.guest.floor():
                audible = False  # do not put muted primary words into the shared recap
            translated = self.protocol.from_provider(event, assistant_audible=audible)
        else:
            translated = self.protocol.from_mobile(event)
        if from_provider and kind == "delegation.created":
            did = (event.get("item") or {}).get("id")
            if isinstance(did, str) and did in self.protocol.seen and did not in self.trace.delegations:
                self.trace.bind_delegation(did)
        if from_provider and isinstance(self.guest, guest_voice.GuestVoice):
            self.guest.observe(event, self.protocol.turn_text["user"], self.trace.turn_id)
        if not from_provider and duplex and kind == "session.commentary.append" and translated:
            self.speak_for(event.get("delegation_id"), event["content"])
            return
        for msg in translated:
            if from_provider and not self.heard_user and str(msg.get("type", "")).startswith("session.output_transcript"):
                self.counts["output_gated_text"] += 1  # its audio is silenced (AudioBridge): keep its words off the app
                continue
            self.send(msg, to_provider=not from_provider)
        handled = False
        if final_action and not self.closed and not self.muted and not self.approvals \
                and time.monotonic() >= self.approval_quiet_until:
            did = f"turn-{final_id}"
            pending = self.protocol.pending_host
            action_did = pending[0][0] if pending else did
            readback_final = self.route_readback(action_did, final_action)
            if readback_final:
                handled = self.controlled_final = True
                self.protocol.asked.clear()
            if not handled and (join := self.join_words(final_action)):
                self.join(join, action_did, final_action)
                handled = self.controlled_final = True
                self.protocol.asked.clear()
            if not handled and not duplex and self.profile != "guest":  # duplex keeps its addressed-utterance path
                handled = self.lane_command(action_did, final_action, final=True) or self.bot_ask(
                    action_did, final_action, final=True)
                if handled:
                    self.controlled_final = True
                    self.protocol.asked.clear()
                    self.protocol.last_request = final_action
            if not handled and live_status.topics(final_action):
                self.spawn(self.status_answer(did, final_action))
        if self.relay and from_provider and heard is None and not self.approvals and not handled:
            if kind == "input_transcript.added" and self.protocol.turn_text["user"]:
                self.spawn(self.relay.transcript(self.protocol.turn_text["user"], False))
            elif kind == "turn.done" and (event.get("turn") or {}).get("role") == "user" and self.protocol.last_user \
                    and self.readback_turn != self.card_turn:
                if self.protocol.fragments.holds(self.protocol.last_user):  # F5: a fragment never reaches the lanes
                    self.counts["lane_final_fragment"] += 1
                else:
                    self.spawn(self.lane_final(self.protocol.last_user, self.trace.turn_id))
        while self.protocol.pending_host:
            did, request = self.protocol.pending_host.pop(0)
            if readback_final or (kind == "delegation.created" and self.readback_turn == self.card_turn
                                  and not self.protocol.user_open and not self.protocol.turn_text["user"]):
                if self.readback_did:
                    self.readback_did = did
                self.trace("lanes.readback_joined", did=did)
            elif (fragment := self.protocol.held.pop(did, None)) is not None:  # F5: before delegation.start (conductor)
                self.counts["host_delegation_fragment"] += 1
                self.trace("delegation.fragment", did=did, request=fragment[:200])
            elif handled or request is None:  # deterministic action or duplicate delegation: no Hermes run
                self.counts["host_delegation_joined"] += 1
                self.trace("delegation.joined", did=did, request=self.protocol.last_request[:300])
            elif not duplex or duplex.may_delegate("primary", did):
                self.spawn(self.host_delegate(did, request))
        if from_provider and duplex and (duplex.floor.owner == "guest" or duplex.floor.answers["primary"]
                                         or duplex.ledger.entries):
            duplex.flush_answers()
        if kind in ("session.close", "session.closed"):
            self.spawn(self.close("client_closed" if not from_provider else "provider_closed"))

    async def start(self, sdp, history):
        if CHAT_CONTINUITY and self.token:  # off the setup path: a slow lookup must never stall the phone's offer
            self.chat_lookup = asyncio.ensure_future(asyncio.to_thread(chat_continuity.find_chat, history, self.profile))
        if CROSS_BRIEF and self.profile != "guest":  # read-only, off the setup path; awaited briefly before the brief
            self.cross_lookup = asyncio.ensure_future(asyncio.to_thread(call_brief.cross_thread, self.profile))
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
                await start_subscription(self.manager.http, self.provider, PERSONA, "sol", self.trace)
            answer = await self.mobile.createAnswer()
            await self.mobile.setLocalDescription(answer)
            context = self.read_history(history)
            if context:
                if os.environ.get("VOICE_WARM_HAIKU", "1") != "0" and shutil.which("claude"):
                    self.haiku = WarmHaiku(owner_identity.fill(FAST_SYSTEM.format(who="Haiku")) + "\n\n" + self.context_text[-6000:])
                    self.spawn(self.start_haiku())
                # raw tail is for the lanes only: as live context items the model answered them (catch-up, 11-16)
            # The subscription endpoint ignores session instructions (11-16 canary: "I'm ChatGPT"), so the
            # persona and the call brief go in as context, framed as background so they are not answered.
            channel = os.environ.get("HERMES_VOICE_BRIEF_CHANNEL", "developer")
            self.cross_brief = await self.read_cross_brief()
            brief = call_brief.build(history, self.chat, note=self.call_note, cross=self.cross_brief)
            self.brief = brief
            grok = self.guest if isinstance(self.guest, guest_voice.GuestVoice) else None
            opening = PERSONA + "\n" + (grok.config.grok_primary_note if grok else brief)
            self.opening_context = self.brief_events(opening, channel)
            snapshot = self.guest_context() if grok else ""
            if self.guest:
                self.spawn(self.start_guest(snapshot))
            for event in self.opening_context:
                self.send(event, True, turn_id=0)
            if grok:
                self.guest_note(snapshot)
            if not self.leg:
                self.spawn(self.renew_subscription())
            self.spawn(self.watchdog())
            self.spawn(self.audio_health())
            self.spawn(needs_voice.run(self))  # blockers out loud while the TV is away (the owner 11-28)
            if self.relay:
                self.spawn(self.relay_start())
            self.trace("call.negotiated", setup_s=round(time.monotonic() - self.created, 3), context_items=len(context))
            live_picture()  # warm the cache so the first question already sees the TV and the lanes
            LOG.info("subscription voice negotiated id=%s", self.id)
            return {"ok": True, "session": {"id": self.id},
                    "transport": {"type": "webrtc", "sdp": self.mobile.localDescription.sdp}}
        except BaseException as exc:
            self.trace("call.setup_failed", error=type(exc).__name__, detail=str(exc)[:300])
            await self.close("setup_failed")
            raise

    async def read_cross_brief(self) -> str:
        """The owner's other threads (latest asks, running/blocked cards, last complaint; call_brief.cross_thread), read
        at call start. A slow store never delays the call past CROSS_BRIEF_WAIT: it opens without the brief."""
        lookup = self.__dict__.get("cross_lookup")
        if lookup is None:
            return ""
        try:
            text = await asyncio.wait_for(asyncio.shield(lookup), CROSS_BRIEF_WAIT)
        except Exception as exc:  # noqa: BLE001 - a timeout or a store error: no brief, never a failed call
            self.trace("call.cross_brief", chars=0, error=type(exc).__name__)
            return ""
        self.trace("call.cross_brief", chars=len(text or ""))
        return text or ""

    def read_history(self, history) -> list[str]:
        """The chat the call opened from: its recent lines become this call's context text (lanes, hosts)."""
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
            self.context_text = "Recent chat context (do not read aloud):\n" + "\n".join(context)
        self.call_note = call_brief.written(self.chat)
        people = self.people = call_brief.people(history)
        if people > 1 and not self.guest and not self.leg:
            # Several people and Hermes the only bot (take 7 without Tek): the same floor, with no second voice.
            self.duplex = GPTGuest(self, None)
        if self.duplex:
            self.duplex.set_people(people)
        return context

    async def start_guest(self, snapshot):
        guest = self.guest
        if guest is None:
            return
        try:
            if isinstance(guest, GPTGuest):
                await guest.start()
                guest.note(guest.persona)
                self.guest_note(guest.config.primary_note)
                self.spawn(self.renew_subscription(guest))
            else:
                await guest.start(snapshot)
        except Exception as exc:  # noqa: BLE001 - the call goes on with one voice
            if isinstance(guest, GPTGuest):
                guest.failed("setup_" + type(exc).__name__)
            else:
                self.trace("guest.failed", error=type(exc).__name__, detail=str(exc)[:300])
                self.guest = None
                await guest.close()

    def guest_context(self) -> str:
        """Build once per refresh. Both destinations get these exact same bounded facts."""
        recent = self.call_recap(self.protocol)
        live = live_picture()
        ledger = self.ledger_brief(max_chars=2500, proof_rule=False)
        parts = ["Shared call background (latest snapshot; not a new request). Stay silent until addressed. "
                 "Use live facts over historical notes; do not repeat prior actions or read this aloud.",
                 self.brief[:6000], recent,
                 "Live right now:\n" + live[:1500] if live else "", ledger]
        return "\n\n".join(p for p in parts if p)

    @staticmethod
    def call_recap(protocol):
        return "\n".join(f"{role}: {' '.join(text.split())[:400]}" for role, text in protocol.exchanges)

    def refresh_guest_context(self):
        if isinstance(self.guest, guest_voice.GuestVoice):
            snapshot = self.guest_context()
            if self.guest.share(snapshot, "turns"):
                self.guest_note(snapshot)

    def guest_note(self, text):
        for part in chunks(text):
            self.send({"type": "session.context.append", "channel": "developer",
                       "content": [{"type": "input_text", "text": part}]}, True)

    async def relay_start(self):
        try:
            await self.relay.start(self.profile)
        except Exception as exc:  # noqa: BLE001 - no lanes for this call; the call itself goes on
            LOG.warning("lanes relay start failed id=%s: %s", self.id, type(exc).__name__)
            self.relay = None

    def speak_for(self, did, text, lane=False):
        if getattr(self, "speak_mode", False):
            text = lane_cards.voice_only(text)
        if self.closed or not text.strip():
            return
        self.__dict__.setdefault("spoken_for", {}).setdefault(did, []).append(text)
        self.handed_at = time.monotonic()  # the voice opens its reply to this a moment later: cards wait (HANDED)
        if self.duplex:
            if did in self.duplex.superseded:
                return
            config = self.duplex.config
            self.duplex.floor.enqueue(Answer("primary", did, text, lane), config.name if config else "")
            self.duplex.flush_answers()
            return
        # A session renewal clears the ids the new provider knows; an answer to an older delegation still speaks.
        did = did if did in self.protocol.seen else None
        for msg in self.protocol.from_mobile({"type": "session.commentary.append", "delegation_id": did, "content": text}):
            self.send(msg, to_provider=True)

    def passthru_event(self, kind, event):
        """Floor + voice commands for passthru, from the provider's transcripts."""
        p, now = self.passthru, time.monotonic()
        turn = event.get("turn") or {}
        if kind == "input_transcript.added":
            p.floor.user_said(str((event.get("item") or {}).get("text") or ""), now)
        elif kind in ("turn.created", "output_transcript.added") and turn.get("role", "assistant") == "assistant" \
                and not p.floor.reply_open and kind == "turn.created":
            p.floor.reply_started(now)
        elif kind == "output_transcript.added" and not p.floor.reply_open and now >= p.floor.tail_until:
            p.floor.reply_started(now)
        elif kind == "turn.done" and turn.get("role") == "assistant":
            p.floor.reply_done(now)
        elif kind == "turn.done" and turn.get("role") == "user":
            text = str(turn.get("transcript") or self.protocol.turn_text["user"] or "")
            p.floor.user_said(text, now)
            # This leg hears the phone alone (the room is never mixed in); a turn counts as the owner's when the
            # phone was loud for it and unmuted. Every command then waits for his spoken "confirm".
            from_owner = now - self.input_loud_at < 10 and not self.muted
            verdict = passthru.judge(self, text, from_owner=from_owner)
            if verdict is not None:
                self.spawn(passthru.run_verdict(self, verdict))
                return
            found = passthru.command(text, active=p.active)
            if found:
                self.trace("passthru.command", action=found[0], arg=found[1][:80], from_owner=from_owner)
                self.spawn(passthru.run_command(self, *found, from_owner=from_owner))

    def note(self, text, turn_id=None):
        for part in chunks(text[:4000]):
            self.send({"type": "session.context.append", "channel": "commentary",
                       "content": [{"type": "input_text", "text": part}]}, True, turn_id=turn_id)

    async def lane_final(self, text, turn_id):
        """One user final through the lanes; later verified/corrected entries become silent notes."""
        receipt = await self.relay.transcript(text, True)
        if not receipt:
            return
        self.counts["lane_finals"] += 1
        if receipt.get("configured") and not TIERS_ON:  # tiers on: the broker board answers nothing (lane_layout does)
            self.note("Helper lane layout changed; tell the user in one short sentence: "
                      + str(receipt["configured"].get("say", ""))[:300], turn_id)
            return
        if receipt.get("answer") and receipt.get("checking"):
            update = await self.relay.updates(receipt["entry"])
            if update and not self.closed:
                status, value = update
                said = any(e == receipt["entry"] for _, e in self.lane_said.values())
                if status == "corrected" and said:
                    self.counts["lane_corrections"] += 1
                    self.note("Correction from a verifier to what you just said: " + value, turn_id)
                elif status == "verified":
                    self.counts["lane_verified"] += 1
                    self.note(f'Verified note for "{text[:200]}": {value}', turn_id)

    async def host_delegate(self, did, request):
        """One fast-lane system (L2, 11-11): the driver starts at once and the z0live lanes race it: smol
        for chat and simple turns, the tool-using gather lane (the liaison's engine) for questions about
        the owner's own work. The first lane answer is spoken once; the driver is spoken only if it adds to it."""
        self.counts["host_delegations"] += 1
        began = time.monotonic()
        if self.approvals or time.monotonic() < self.approval_quiet_until:
            # N230: while a prompt waits (or just after its answer) the voice's delegation is the owner's approval answer
            # retold, never a new request: no Hermes run, and its words (maybe the pass-phrase) are not traced.
            self.counts["approval_delegation_swallowed"] += 1
            self.trace("approval.delegation_swallowed", did=did)
            return
        # Before delegation.start: the TV conductor turns every delegation.start into worker lanes.
        if self.route_readback(did, request):
            return
        if self.lane_command(did, request):  # "add two opus lanes": the layout changes here, Hermes never runs
            return
        if self.join_command(did, request):  # "add Tek to the call": a second voice joins, Hermes never runs
            return
        if self.bot_ask(did, request):  # "ask @pc to list the drive": pc answers, tek's agent never runs
            return
        if await self.status_answer(did, request):  # "how many lanes are running": the records answer, no model
            return
        # passthru=True marks a delegation made while z0 sat in a meeting (this leg still hears the phone alone).
        self.trace("delegation.start", did=did, request=(request or "")[:4000],
                   **({"passthru": True} if self.passthru and self.passthru.active else {}))
        self.ledger_open(did, request)
        turn = self.relay.turn if self.relay else None
        configured = ((turn or {}).get("receipt") or {}).get("configured") if not TIERS_ON else None
        if configured and not self.closed:  # a lane-layout line, not a request for Hermes
            self.speak_for(did, configured.get("say") or "")
            return
        lane = None
        try:
            if not request:
                raise RuntimeError("The delegated request was empty")
            self.background_asks[lane_cards.request_key(request)] = "active"
            # FAST_LANE_OFF: the lanes are this turn's fast lane; the profile plugin must not start another.
            job = self.spawn(self.ask_hermes(did, request))
            self.pending_delegations.add(job)
            job.add_done_callback(self.pending_delegations.discard)
            self.ack_due = True  # the voice's reply to this request is its ack (STATUS_GAP)
            self.spawn(self.receipt(did, job))
            self.latest_did = did  # an older question's lanes go quiet once this one starts
            self.watch_lane_cards(did, request)
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
            answer = await job
            if answer is None:  # joined the chat's running turn: that turn's one final answers this request too
                self.counts["host_delegation_folded"] += 1
                self.trace("delegation.folded", did=did, s=round(time.monotonic() - began, 3))
                return
            # Tier lanes go quiet: Hermes has answered this request and every request that joined its turn.
            self.__dict__.setdefault("hermes_done", set()).add(did)
            self.counts["host_delegation_ok"] += 1
            self.trace("delegation.done", did=did, s=round(time.monotonic() - began, 3), answer=answer[:4000],
                       closed=self.closed)
            answer = await self.ledger_answer(did, answer)
            answer = self.finish_finding(did, answer)
            if not answer:
                self.counts["host_delegation_covered"] += 1  # the lane already said it
                return
        except turn_slots.QueueFull as full:  # refused at once: it never started, so nothing "may still be running"
            self.counts["delegations_refused"] += 1
            self.trace("delegation.refused", did=did, running=full.running, waiting=full.waiting)
            answer = FULL.format(running=full.running, waiting=full.waiting)
            self.ledger_do(request_ledger.state, self.item_of.get(did), "dropped", "voice", "refused: the line was full")
        except Exception as exc:  # noqa: BLE001 - every failure is spoken, never silent
            LOG.warning("host delegation failed id=%s: %s", self.id, type(exc).__name__)
            self.counts["host_delegation_failed"] += 1
            self.trace("delegation.failed", did=did, s=round(time.monotonic() - began, 3), error=type(exc).__name__,
                       detail=str(exc)[:300])
            answer = "I couldn't finish that in Hermes. It may still be running; check the chat before trying again."
            self.ledger_do(request_ledger.result, self.item_of.get(did), "background", f"failed: {type(exc).__name__}",
                           False)
        finally:
            if lane_cards.request_key(request) in self.background_asks:
                self.background_asks[lane_cards.request_key(request)] = "completed"
        if self.closed:
            self.trace("delegation.dropped_after_close", did=did)
            return
        # Requests running side by side can come back with the same words: say them once (failures always).
        spoken = self.__dict__.setdefault("answers_spoken", set())
        key = " ".join(answer.split()).casefold()
        if key in spoken and not answer.startswith(("I couldn't finish", "I can't take")):
            self.counts["host_delegation_repeat"] += 1
            self.trace("delegation.repeat", did=did)
            return
        spoken.add(key)
        self.speak_for(did, answer)

    async def offer_finding(self, did, text, *, action):
        """Only a grounded agent's explicitly marked finding can go out before its final. Action proofs wait."""
        if action or did in self.findings or self.closed or self.latest_did != did or self.floor_busy():
            return
        match = re.match(r"^\s*FINDING:\s+([^\n]{8,260}?[.!?])(?=\s)", text, re.I)
        if not match:
            return
        sentence = match[1]
        if await self.gate_claims(did, sentence, "background_partial") != sentence:
            return
        if self.closed or self.latest_did != did or self.floor_busy() or did in self.findings:
            return
        self.findings[did] = sentence
        self.trace("delegation.finding", did=did, text=sentence)
        self.speak_for(did, sentence)

    def finish_finding(self, did, answer):
        prior = self.findings.pop(did, "")
        text = re.sub(r"^\s*FINDING:\s*", "", answer, flags=re.I).strip()
        if prior and text.startswith(prior):
            return text[len(prior):].strip()
        marked = re.match(r"^(?:(?:sorry|my mistake)[.!,:]?\s+)*correction\s*[:.,]\s*", text, re.I)
        if marked or (prior and text):
            self.trace("delegation.correction", did=did)
            return "correction: " + (text[marked.end():] if marked else text)
        return text

    def ledger_do(self, fn, item, *args, **kw):
        """A ledger write never breaks the call: a failure is logged and the call goes on."""
        if not item:
            return None
        try:
            return fn(item, *args, **kw)
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ledger write failed id=%s: %s", self.id, type(exc).__name__)
            self.counts["ledger_errors"] += 1
            return None

    @property
    def ledger_call(self) -> str:
        """The ledger's call key: the trace stem's hex (what the conductor and the TV call this call)."""
        return self.id.split("-")[-1]

    def ledger_open(self, did, request):
        """Every delegation is one ledger item. An action also gets the background agent's launch as a claim: that
        is a promise with no proof until the agent (or a job) records a receipt."""
        if not request or did in self.item_of:
            return
        try:
            kind = request_kind(request)
            item = request_ledger.open_item(self.ledger_call, request, kind, "background")
        except Exception as exc:  # noqa: BLE001
            LOG.warning("ledger open failed id=%s: %s", self.id, type(exc).__name__)
            self.counts["ledger_errors"] += 1
            return
        self.item_of[did] = item
        self.counts["ledger_items"] += 1
        if kind == "action":
            self.ledger_do(request_ledger.claim, item, "background", "a background agent was asked to do this")
        self.trace("ledger.open", did=did, item=item, kind=kind)

    def ledger_item(self, did):
        try:
            return request_ledger.get(self.item_of[did]) if did in self.item_of else None
        except Exception:  # noqa: BLE001
            return None

    def unproven(self, did) -> bool:
        """An action whose promise has no proof (a pid, kanban, cron, commit or file receipt) on its item."""
        it = self.ledger_item(did)
        return bool(it and it["kind"] == "action" and not it["proven"] and it["state"] not in request_ledger.TERMINAL)

    async def ledger_answer(self, did, answer: str) -> str:
        """Record a background agent's answer and keep it honest. A question's answer is its result. An action's
        answer is a claim: PROOF lines are checked (proof_exists) and recorded as receipts; it becomes a result only with
        proof and done words; if it says the work started or finished and the item has no proof, its own words are
        spoken with "I haven't confirmed it started" (the item stays a claim, not started). An answer that says it
        did NOT do the work blocks the item instead of leaving a promise."""
        item = self.item_of.get(did)
        if not item:
            return answer
        text, proofs = split_proof(answer)
        for kind, ref, detail in proofs:
            ok = await asyncio.to_thread(proof_exists, kind, ref, self.profile)
            if not ok:
                self.counts["ledger_proof_rejected"] += 1
                self.trace("ledger.proof_rejected", did=did, item=item, kind=kind)
                continue
            self.ledger_do(request_ledger.receipt, item, "background", kind, ref, detail)
        it = self.ledger_item(did)
        if not it:
            return text or answer
        if it["kind"] != "action":
            # N216: a question's answer may not state that work is running, finished or not picked up unless it
            # cites an id read now (or a PROOF that checked out) and that record agrees.
            text = await self.gate_claims(did, text, "background", extra_ids=[r for _, r, _ in proofs])
            self.ledger_do(request_ledger.result, item, "background", text)
            return text
        if not it["proven"] and self.claims_work(text):
            self.ledger_do(request_ledger.claim, item, "background", text[:500])
            self.counts["unproven_claims"] += 1
            self.trace("ledger.unproven_claim", did=did, item=item)
            return unproven_claim(text)
        if not it["proven"]:  # no claim and no proof: it did not start
            if NEGATED.search(text):  # a refusal, a failure or a decision it needs from the owner
                self.ledger_do(request_ledger.state, item, "blocked", "background", text[:500])
            else:  # an informational answer to a request that only looked like an action
                self.ledger_do(request_ledger.result, item, "background", text)
            return text
        self.ledger_do(request_ledger.claim, item, "background", text[:500])
        if DONE_WORDS.search(text) and not RUNNING_WORDS.search(text):
            self.ledger_do(request_ledger.result, item, "background", text)
        return text

    def tier_claims_work(self, did, text: str) -> bool:
        """A tier lane has no tools: for an action its words must not say work started or finished."""
        return bool((self.ledger_item(did) or {}).get("kind") == "action" and self.claims_work(text))

    @staticmethod
    def claims_work(text: str) -> bool:
        """One of the first two sentences says work started, finished or is about to be started, and does not say it did not."""
        return any((CLAIMS_WORK.search(sent) or PROMISES_WORK.search(sent)) and not NEGATED.search(sent)
                   for sent in re.split(r"(?<=[.!?])\s+|\n+", text.strip())[:2])  # the verdict leads

    def lane_command(self, did, request, *, final=False):
        """A spoken lane command ("add two opus lanes", "drop the sol lanes", "how many lanes do I have") changes or
        reads lane_layout at once and speaks its receipt. One utterance changes the layout once: a second delegation
        of the same words within 20 s is swallowed, not applied again. True when the request was a lane command."""
        user = str(self.protocol.turn_text.get("user") or "") if hasattr(self.protocol, "turn_text") else ""
        # The owner's own words only back up a request that is itself about lanes: a delegation can beat his turn.done,
        # and the previous utterance ("add two opus lanes") must never swallow "what's the weather".
        text = next((t for t in (request, user if not final and re.search(r"\blanes?\b", request or "", re.I) else "")
                     if lane_layout.command(t)), None)
        if not text:
            return False
        cmd, now = lane_layout.command(text), time.monotonic()
        key = json.dumps(cmd, sort_keys=True)  # the command alone: the user text changes when a delegation beats it
        recent = self.__dict__.setdefault("layout_done", {})
        if cmd["action"] != "list" and now - recent.get(key, -99) < 20:
            self.trace("lanes.layout_repeat", did=did, cmd=cmd)
            return True
        recent[key] = now
        result = lane_layout.run(text, by="voice:" + self.id[-8:])
        if result is None:
            return False
        self.counts["lane_layout_" + ("ok" if result.get("ok") else "refused")] += 1
        speech = result.get("receipt") or result.get("say") or ""
        self.trace("lanes.layout", did=did, request=(request or "")[:300], cmd=cmd, ok=result.get("ok"), say=speech[:300],
                   error=result.get("error"))
        if not self.closed:
            self.speak_for(did, speech)
        return True

    def bot_ask(self, did, request, *, final=False):
        """A spoken "ask @pc to list the drive" / "ask the PC bot ..." goes straight to that profile's own Hermes host
        (ask_bot.py; GPT-Live has no tools, so the words are the command). The ask, its delivery id (receipt kind ask)
        and the answer land on this call's ledger item; the answer is spoken when it comes. Never on a guest-profile
        call. One utterance is one ask: the same words again within 20 s are swallowed. True when it was an ask."""
        if self.profile == "guest":
            return False
        user = str(self.protocol.turn_text.get("user") or "") if hasattr(self.protocol, "turn_text") else ""
        names = ask_bot.hosts()
        # The owner's own words back up a paraphrased request only when the request still names that bot.
        named = any(re.search(rf"\b{re.escape(n)}\b", request or "", re.I) for n in names)
        hit = next((h for h in map(ask_bot.parse, (request, user if named and not final else "")) if h), None)
        if not hit:
            return False
        profile, message = hit
        now, key = time.monotonic(), f"{profile}\0{message.casefold()}"
        recent = self.__dict__.setdefault("asks_done", {})
        if now - recent.get(key, -99) < 20:
            self.trace("ask.repeat", did=did, profile=profile)
            return True
        recent[key] = now
        self.trace("ask.start", did=did, profile=profile, message=ask_bot.mask(message)[:500])
        self.ledger_open(did, ask_bot.mask(request))
        self.counts["bot_asks"] += 1
        if not self.closed:
            self.speak_for(did, f"Asking {profile}. I'll read you its answer when it comes.")

        async def run():
            began = time.monotonic()
            hook = self.approval_hook(did, profile) if VOICE_APPROVALS else None
            result = await asyncio.to_thread(ask_bot.ask, profile, message, call=self.ledger_call,
                                             item=self.item_of.get(did, ""), by="voice", via="voice", caller="tek",
                                             on_approval=hook)
            self.counts["bot_ask_" + ("ok" if result.get("ok") else "failed")] += 1
            self.trace("ask.done", did=did, profile=profile, ok=result.get("ok"), delivery=result.get("delivery"),
                       item=result.get("item"), error=result.get("error"), answer=(result.get("answer") or "")[:2000],
                       s=round(time.monotonic() - began, 3))
            if not self.closed:
                if result.get("ok") is True:
                    self.speak_for(did, result.get("say") or "")
                else:
                    self.speak_for(did, f"I couldn't reach {profile}.")
        self.spawn(run())
        return True

    # ---- a second agent joins by voice (guest_join) ----
    def join_words(self, text):
        """("add" | "remove", profile) when the owner's words are a join command this call can act on, else None."""
        if self.profile == "guest" or self.leg or self.engine != "gpt-live":
            return None
        return guest_join.parse(text)

    def join_command(self, did, request) -> bool:
        """The voice delegated "add Tek to the call": act on it here, never in Hermes. The owner's own words back up a
        paraphrased request only when the request still names a profile that can join."""
        user = str(self.protocol.turn_text.get("user") or "") if hasattr(self.protocol, "turn_text") else ""
        named = any(re.search(rf"\b{re.escape(spoken)}\b", request or "", re.I)
                    for spoken, _ in guest_join._spellings())
        hit = next((h for h in map(self.join_words, (request, user if named else "")) if h), None)
        if not hit:
            return False
        self.join(hit, did, request)
        return True

    def join(self, hit, did, said=""):
        """One utterance is one join: the owner's turn and the voice's delegation of it both arrive (20 s window)."""
        action, profile = hit
        now, key = time.monotonic(), f"{action}\0{profile}"
        recent = self.__dict__.setdefault("joins_done", {})
        if now - recent.get(key, -99) < 20:
            self.trace("guest.join_repeat", action=action, profile=profile, did=did)
            return
        recent[key] = now
        self.counts["guest_join_" + action] += 1
        self.trace("guest.join", action=action, profile=profile, did=did, said=(said or "")[:200])
        self.spawn(self.add_guest(did, profile) if action == "add" else self.remove_guest(did, profile))

    async def guest_host_up(self, profile) -> bool:
        port = guest_join.port(profile)
        try:
            key = (Path.home() / ".config/z0live/guest-hosts" / (profile + ".key")).read_text().strip()
            async with self.manager.http.get(f"http://127.0.0.1:{port}/v1/models", allow_redirects=False,
                                             headers={"Authorization": "Bearer " + key},
                                             timeout=ClientTimeout(total=3)) as response:
                return response.status == 200
        except Exception:  # noqa: BLE001 - unreachable is the answer
            return False

    async def add_guest(self, did, profile):
        """the owner asked for a second agent: it joins this call only (voices.json is never written), in its own voice,
        and says so itself once its session is up. One guest at a time."""
        def say(text):
            if not self.closed:
                self.speak_for(did, text)
        guest = self.guest
        if isinstance(guest, guest_voice.GuestVoice):
            return say("Grok is on this call already. One guest at a time.")
        if isinstance(guest, GPTGuest) and not guest.closed and guest.config:
            name = guest.config.name
            return say(f"{name} is already on the call." if guest.config.profile == profile else
                       f"{name} is on the call. One guest at a time: say remove {name} first.")
        config = guest_join.config(profile)
        if config is None or not guest_join.has_key(profile):
            self.trace("guest.join_refused", profile=profile, why="no_host")
            return say(f"{profile} can't join calls yet: it has no voice host.")
        if not await self.guest_host_up(profile):
            self.trace("guest.join_refused", profile=profile, why="host_down", port=guest_join.port(profile))
            return say(f"I can't reach {config.name}'s Hermes, so {config.name} can't join right now.")
        duplex = self.duplex
        if duplex is None:
            duplex = self.duplex = GPTGuest(self, None)
            duplex.set_people(self.people)
        duplex.adopt(config)
        # No line from Hermes on success: its own "One sec." is the ack and the guest's hello, in the guest's voice,
        # is the confirmation. A "Bringing PC in." line was paraphrased ("PC's here now"), resent and dropped, and
        # the hello waited behind it until the owner had moved on (join probe 13:26).
        self.guest, self.guest_config = duplex, config
        self.protocol.own = lambda said: duplex.own_words("primary", said)
        duplex.protocol.own = lambda said: duplex.own_words("guest", said)
        if self.microphone:
            duplex.attach(self.subscribe_mic())
        self.trace("guest.joining", profile=profile, name=config.name, voice=config.voice)
        try:
            await duplex.start()
        except Exception as exc:  # noqa: BLE001 - the call goes on with one voice
            duplex.failed("join_" + type(exc).__name__, line=f"I couldn't bring {config.name} onto the call.")
            if self.guest is duplex:
                self.guest = None
            return
        if self.closed or duplex.closed:
            return
        duplex.note(duplex.persona)
        self.guest_note(config.primary_note)
        self.spawn(self.renew_subscription(duplex))
        # The guest says hello in its own voice once Hermes's "One sec." is off the phone.
        end = time.monotonic() + 8
        while time.monotonic() < end and not self.closed and (
                "primary" in duplex.replies or duplex.playing("primary")):
            await asyncio.sleep(0.1)
        if self.closed or duplex.closed:
            return
        duplex.floor.change("guest", "guest_joined", time.monotonic())
        duplex.floor.enqueue(Answer("guest", GREETING + profile, guest_join.greeting(config)), config.name)
        duplex.flush_answers()

    async def remove_guest(self, did, profile):
        guest = self.guest
        config = guest_join.config(profile)
        name = config.name if config else profile
        if not (isinstance(guest, GPTGuest) and guest.config and guest.config.profile == profile and not guest.closed):
            if not self.closed:
                self.speak_for(did, f"{name} isn't on the call.")
            return
        # Only words the call has heard (the name, "call"): the voice says it its own way ("Tek's off the call"), and
        # a line with no new words counts as said when its reply ends, so a paraphrase is never resent (join probe
        # 13:18: "Tek left the call" was said as "Tek's off the call", resent, and said again).
        guest.leave(f"{name} is off the call now.")
        self.guest = None
        self.guest_note(f"{name} has left the call. You are the only voice again; ignore earlier notes about {name}.")

    # ---- N230: Hermes approval prompts on the call ----
    def phrase_profile(self) -> str:
        """Whose .env holds the pass-phrase: the call's own profile (secret-intake saves it there), tek by default."""
        return self.profile if self.profile and self.profile != "guest" else "tek"

    def arm_scrub(self, phrase: str):
        """Keep the pass-phrase out of this call's trace in any spelling ASR gives it (case, punctuation)."""
        if phrase:
            self.trace.scrub = re.compile(r"[\W_]+".join(map(re.escape, phrase.split())), re.I)

    def approval_hook(self, did, profile):
        """A thread-safe hand-off from the ask's Runs reader to this call's loop. Never blocks the reader."""
        loop = asyncio.get_running_loop()

        def hook(client, run_id, event):
            loop.call_soon_threadsafe(self.approval_event, did, profile, client, run_id, event)
        return hook

    def approval_event(self, did, profile, client, run_id, event):
        kind, rid = voice_approvals.event_kind(event), voice_approvals.request_id(event)
        if kind == "approval.request":
            if self.closed or self.profile == "guest" or not rid:
                self.trace("approval.ignored", did=did, profile=profile, run=run_id, request_id=rid,
                           why="closed" if self.closed else ("guest" if self.profile == "guest" else "no request id"))
                return  # nobody answers it here: Hermes' own timeout denies it
            phrase = voice_approvals.load_phrase(self.phrase_profile())
            self.arm_scrub(phrase)
            first = not self.approvals
            self.approvals.append({"did": did, "profile": profile, "client": client, "run": run_id, "rid": rid,
                                   "gen": event.get("execution_generation"), "event": event, "spoken_at": None})
            self.counts["approval_requests"] += 1
            self.trace("approval.request", did=did, profile=profile, run=run_id, request_id=rid,
                       command=str(event.get("command") or "")[:300], description=str(event.get("description") or "")[:200],
                       phrase_set=bool(phrase), queued=not first)
            if first:
                self.speak_approval()
            return
        # approval.responded / approval.cleared: the request stopped waiting (answered here or elsewhere, or timed out).
        head = self.approvals[0] if self.approvals else None
        left = [a for a in self.approvals if not (a["run"] == run_id and (not rid or a["rid"] == rid))]
        if len(left) == len(self.approvals):
            return
        self.approvals = left
        self.trace("approval.settled", did=did, run=run_id, request_id=rid, by=kind)
        if self.approvals and self.approvals[0] is not head:
            self.speak_approval()

    def speak_approval(self):
        head = self.approvals[0]
        head["spoken_at"] = time.monotonic()
        text = voice_approvals.spoken_prompt(head["event"], head["profile"])
        self.trace("approval.spoken", did=head["did"], request_id=head["rid"], say=text)
        self.speak_for(head["did"], text)

    def approval_mask(self, kind, event):
        """(event with the owner's words replaced, True) for a user-words event while a spoken prompt waits, else (event,
        None). His words come as turn.created/turn.delta of a user turn (the whole text) and input_transcript.added
        (the same words again); all are masked so the app, lanes and trace never get the phrase. The answer is decided
        at the first of: the phrase heard, the user turn's turn.done, or APPROVAL_QUIET_S with no new words. Live
        probe 11-28 04:58: gpt-live held a bare 'copper meadow falcon' turn open with no turn.done for minutes."""
        if not self.approvals or self.approvals[0]["spoken_at"] is None:
            return event, None
        head, masked = self.approvals[0], "[approval answer]"
        if kind == "input_transcript.added" and isinstance(event.get("item"), dict):
            return {**event, "item": {**event["item"], "text": masked}}, True
        turn = event.get("turn") if kind in ("turn.created", "turn.done") else None
        if isinstance(turn, dict) and turn.get("role") == "user":
            head.setdefault("turns", set()).add(turn.get("id"))
            text = str(turn.get("transcript") or "")
            if kind == "turn.done":
                head["heard"] = text or head.get("heard", "") or str(self.protocol.turn_text.get("user") or "")
                self.protocol.turn_text["user"] = ""
                self.approval_decide(head, now=True)
            else:
                head["heard"] = (head.get("heard", "") + " " + text).strip()
                self.approval_decide(head)
            return {**event, "turn": {**turn, "transcript": masked}}, True
        if kind == "turn.delta" and event.get("turn_id") in head.get("turns", ()):
            head["heard"] = head.get("heard", "") + str(event.get("delta") or "")
            self.approval_decide(head)
            return {**event, "delta": " " + masked}, True
        return event, None

    def approval_decide(self, head, now=False):
        """Answer at once on turn.done or a heard phrase; otherwise after APPROVAL_QUIET_S without new words."""
        timer = head.pop("timer", None)
        if timer:
            timer.cancel()
        heard = head.get("heard", "")
        if now or voice_approvals.matches(heard, voice_approvals.load_phrase(self.phrase_profile())):
            self.spawn(self.approval_answer(heard, head))
        elif heard.strip():
            head["timer"] = asyncio.get_running_loop().call_later(
                APPROVAL_QUIET_S, lambda: self.spawn(self.approval_answer(head.get("heard", ""), head)))

    async def approval_answer(self, heard, head=None):
        """Answer the spoken prompt once: the pass-phrase approves it once, anything else denies it (fail closed)."""
        if not self.approvals or (head is not None and self.approvals[0] is not head):
            return  # already answered (a late turn.done after the phrase was heard) or settled elsewhere
        head = self.approvals.pop(0)
        timer = head.pop("timer", None)
        if timer:
            timer.cancel()
        self.approval_quiet_until = time.monotonic() + 30
        phrase = voice_approvals.load_phrase(self.phrase_profile())
        choice = voice_approvals.decide(heard, phrase)
        error = ""
        try:
            await asyncio.to_thread(head["client"].respond, head["run"], head["rid"], choice, head["gen"])
        except (RuntimeError, ValueError) as exc:
            error = str(exc)[:200]
        self.counts["approval_" + choice] += 1
        self.trace("approval.answer", did=head["did"], profile=head["profile"], run=head["run"], request_id=head["rid"],
                   choice=choice, phrase_set=bool(phrase), heard=voice_approvals.mask(heard, phrase), error=error or None)
        if error:
            say = (voice_approvals.SETTLED if "409" in error else
                   "I couldn't send that answer to Hermes, so it will deny the command when its approval times out.")
        elif choice == "once":
            say = voice_approvals.APPROVED
        else:
            say = voice_approvals.DENIED if phrase else voice_approvals.NO_PHRASE
        self.speak_for(head["did"], say)
        if self.approvals:
            self.speak_approval()

    def status_reader(self):
        if self.status_sources is None:
            self.status_sources = live_status.Sources.for_profile(self.profile)
        return self.status_sources

    async def status_read(self, topics, request=""):
        return await asyncio.to_thread(live_status.read, topics, self.status_reader(), self.status_clock(), request)

    async def status_answer(self, did, request) -> bool:
        """N216: a status question (lanes running, hill climbs, blockers, a card or job, latency) is answered from the
        live records read now, with the read time, never by a model or a cached snapshot. Before delegation.start, so
        asking "how many lanes" never starts lanes. The answer is the request's ledger result; the voice is told to say
        it verbatim and its reading is checked once (check_verbatim). True when the request was a status question."""
        topics = live_status.exact_topics(request)
        if not topics or self.profile == "guest" or not self.live_records:
            return False
        if self.background_owns(did, request):  # F2: one voice per ask; the delegation's answer is the answer
            self.trace("status.yield", did=did, topics=topics, why="background_owns")
            return False
        # One utterance is one answer: the owner's turn.done answers it at once, and the voice's delegation of the same
        # question (if it sends one) within 20 s is swallowed. Probe 11-28 03:16: the voice said "Let me check.", sent
        # no delegation, and 70 s later said "Three lanes are active right now." (0 TV lanes, 1 card were running).
        now, key = time.monotonic(), " ".join(sorted(topics)) + "\0" + " ".join(live_status.CARD_ID.findall(request))
        recent = self.__dict__.setdefault("status_done", {})
        if now - recent.get(key, -99) < 20:
            self.trace("status.repeat", did=did, topics=topics)
            return True
        recent[key] = now
        began = time.monotonic()
        self.ledger_open(did, request)
        try:
            snap = await self.status_read(topics, request)
            text = live_status.render(snap)
        except Exception as exc:  # noqa: BLE001 - a broken reader is said, never guessed over
            snap, text = None, f"I couldn't read the live records just now ({type(exc).__name__}), so I can't say."
        self.counts["status_answers"] += 1
        self.trace("status.answer", did=did, topics=topics, text=text[:2000], s=round(time.monotonic() - began, 3),
                   read_at=(snap or {}).get("read_at"), ids=sorted((snap or {}).get("ids") or ())[:60],
                   errors=(snap or {}).get("errors"))
        self.ledger_do(request_ledger.result, self.item_of.get(did), "records", text)
        if self.closed:
            return True
        self.note("The next final message is a live status read from the records. Say it word for word: keep every "
                  "number, id and clock time exactly as written, add nothing and drop nothing.")
        self.verbatim = (did, text, time.monotonic())
        self.speak_for(did, text)
        return True

    def background_owns(self, did, request) -> bool:
        """F2: the ask already has a delegation (active or completed) other than this utterance's own: background
        speaks its one answer and no status read or TV summary races it. Sources: background_asks
        (lane_cards.request_key(ask) -> "active"|"completed", set by the background owner), hermes_done, and a lane
        delegation (ask_dids, the one card_watch maps cards by) recorded for the same request under another did."""
        key = lane_cards.request_key(request)
        if (getattr(self, "background_asks", None) or {}).get(key) in ("active", "completed"):
            return True
        if did in self.__dict__.get("hermes_done", set()):
            return True
        prior = self.ask_dids.get(key)
        return bool(prior and prior != did)

    @staticmethod
    def card_verified(ln) -> bool:
        """F2: only a card whose own metadata says it was verified may state a fact (card.verified true)."""
        return (ln.get("card") or {}).get("verified") is True

    def check_verbatim(self, spoken: str):
        """The voice's reading of the last status answer keeps every count and clock time (live_status.reworded); if it
        changed one, the records' own words follow once as a correction. Turns that do not read it (an ack) are skipped."""
        if not self.verbatim or not str(spoken or "").strip():
            return
        did, source, at = self.verbatim
        if time.monotonic() - at > 120:
            self.verbatim = None
            return
        words = lambda t: {w for w in re.findall(r"[a-z]{4,}", t.lower())}  # noqa: E731
        if not (words(spoken) & words(source)) and not live_status.reworded("", spoken):
            return  # no word of the answer and no number: an ack, not its reading of the status answer
        self.verbatim = None
        problems = live_status.reworded(source, spoken)
        self.trace("status.verbatim", did=did, ok=not problems, problems=problems, spoken=str(spoken)[:500])
        if not problems:
            return
        self.counts["status_reworded"] += 1
        if not self.closed:
            self.speak_for(did, "correction: " + source)

    async def gate_claims(self, did, text: str, source: str, extra_ids=()) -> str:
        """N216 proof gate for model words (a background answer, a tier lane, a lane card): a sentence that says work is
        running, finished, stopped or not picked up keeps only with an id read now that agrees (live_status.gate)."""
        if not text or not getattr(self, "live_records", True) or not live_status.has_claim(text):
            return text
        try:
            snap = await self.status_read(live_status.ALL)
        except Exception as exc:  # noqa: BLE001 - no records: no claim about them stands
            snap = {"ids": set(), "state": {}, "read_at": self.status_clock(), "errors": {"read": type(exc).__name__}}
        out, dropped = live_status.gate(text, snap, extra_ids)
        if dropped:
            self.counts["status_claims_dropped"] += len(dropped)
            self.trace("status.gated", did=did, source=source, dropped=[d[:300] for d in dropped], said=out[:1000],
                       read_at=snap.get("read_at"))
        return out

    async def card_text(self, lanes) -> str:
        """The one line for finished lane cards, through the proof gate; '' when nothing in it can be confirmed."""
        if getattr(self, "require_verified_cards", True):  # F2: unverified cards stay on TV and in the ledger
            held = [ln for ln in lanes if not self.card_verified(ln)]
            if held:
                self.trace("lanes.card_unverified", lanes=[ln.get("id") for ln in held])
            lanes = [ln for ln in lanes if self.card_verified(ln)]
        text = await self.gate_claims(None, lane_cards.line(lanes), "card")
        return "" if text.strip() in ("", live_status.CANT_CONFIRM) else text

    async def card_summary(self, did, lane, source):
        if not self.card_verified(lane):
            return ""
        summary = lane_cards.spoken_summary(lane["card"]["summary"])
        # Never turn a proof warning into a purported result.
        return summary if summary and await self.gate_claims(did, summary, source) == summary else ""

    async def tier_lanes(self, did, request):
        """Tier races are trace-only: an unchecked count or diagnosis must never reach the speaker."""
        began, mode = time.monotonic(), tiers.conductor_mode()
        cfg = tiers.for_request(tiers.load(), mode)  # turbo adds the semantic-router fast lane; normal drops it
        if mode != "normal":
            self.trace("lanes.mode", did=did, mode=mode, fast=[b["model"] for b in cfg.get("fast", [])])

        context = tiers.snapshot(self.context_text, list(self.protocol.exchanges), live=live_picture(),
                                 ledger=self.ledger_brief(did, 2500, proof_rule=False))


        async def one(name):
            return name, await tiers.race(self.manager.http, name, cfg[name], context, request)

        for job in asyncio.as_completed([one(name) for name in tiers.DEADLINE if cfg.get(name)]):
            name, (text, model, first_s, errors) = await job
            self.trace("lanes.tier", did=did, tier=name, model=model, first_s=first_s, text=text[:500], errors=errors,
                       s=round(time.monotonic() - began, 3))
            quiet = (self.closed or getattr(self, "latest_did", did) != did
                     or did in self.__dict__.get("hermes_done", set()))
            if text and not quiet:
                self.trace("lanes.tier_claim_held", did=did, tier=name)

    def watch_lane_cards(self, did, request):
        """The TV conductor runs lanes for this request (it reads our delegation.start): watch for their cards."""
        self.ask_dids[lane_cards.request_key(request)] = did
        self.last_delegation_at = time.monotonic()
        if self.card_watch is None or self.card_watch.done():
            self.card_watch = self.spawn(self.watch_cards())

    @property
    def speak_mode(self):
        return self.speak_requested or not self.tv_present

    def observe_tv_presence(self, tv):
        # serve.py owns the heartbeat freshness threshold (and checks power and manual away).
        self.tv_present = isinstance(tv, dict) and tv.get("present") is True

    def route_readback(self, did, text):
        if lane_cards.away_requested(text):
            self.speak_requested = True  # explicit choice wins over every later heartbeat in this call
        if not lane_cards.readback_requested(text):
            return False
        if self.readback_turn != self.card_turn:
            self.readback_turn = self.card_turn
            self.readback_did = did
            if self.card_watch is None or self.card_watch.done():
                self.card_watch = self.spawn(self.watch_cards())
        elif self.readback_did and did in self.protocol.seen:
            self.readback_did = did  # answer the provider's tool call, if it arrived before the reading
        return True

    def card_did(self, ask):
        return self.ask_dids.get(lane_cards.request_key(ask.get("text"))) or ask.get("did")

    async def earlier_results(self, batch, limit, *, repeat=False):
        """Select spoken, proof-checked lines; leave the backlog beyond this turn's allowance intact."""
        lines, consumed = [], set()
        topics = {did: text for text, did in self.ask_dids.items()}
        for did, ln in batch:
            if len(lines) >= limit:
                break
            lid = ln["id"]
            consumed.add(lid)
            if lid in self.cards_read:
                continue
            summary = ln["card"]["summary"]
            if lane_cards.superseded(summary, did, self.spoken_for):
                self.card_skipped("superseded", did, [ln])
                continue
            if not repeat and lane_cards.said_already(summary, self.spoken_for.get(did, [])):
                self.card_skipped("already_said", did, [ln])
                continue
            if verified := await self.card_summary(did, ln, "earlier_card"):
                lines.append((lid, lane_cards.speak_line(ln, verified, topics.get(did, ""))))
            else:
                self.card_skipped("unconfirmed", did, [ln])
        return lines, consumed

    async def say_readback(self, state, call_key, now):
        if not self.card_floor_free(now):
            return
        turn = self.card_turn
        batch = [(self.card_did(ask), ln) for ask, ln in lane_cards.lanes_of(state or {}, call_key)
                 if ln.get("state") == "done" and (ln.get("card") or {}).get("summary")]
        limit = lane_cards.MAX_EARLIER - self.earlier_this_turn
        lines, _ = await self.earlier_results(batch, limit, repeat=True)
        now = time.monotonic()
        if self.closed or turn != self.card_turn or not self.card_floor_free(now):
            return
        did, self.readback_did = self.readback_did, None
        if not lines:
            text = ("I couldn't read the lane results right now." if state is None else
                    "No more finished lane results to read yet." if batch or self.cards_busy else
                    "No lanes have returned a result on this call yet.")
        else:
            text = " ".join(text for _, text in lines)
        ids = [lid for lid, _ in lines]
        self.cards_read.update(ids)
        self.earlier_this_turn += len(ids)
        self.trace("lanes.readback", did=did, lanes=ids, text=text[:500])
        self.speak_for(did, text, lane=True)

    def card_floor_free(self, now) -> bool:
        """Nobody is talking: the caller has no turn open, the voice no reply open, and both audio legs are quiet.
        The single-voice call has no Floor (prod, 11-27): this is the gate; with two voices speak_for adds the floor's."""
        p = self.protocol
        return not (p.user_open or p.assistant_open or self.audio_loud(now)
                    or now - getattr(self, "handed_at", float("-inf")) < lane_cards.HANDED_SECONDS)

    def audio_loud(self, now) -> bool:
        return now - self.input_loud_at < lane_cards.QUIET_SECONDS or now - self.output_loud_at < lane_cards.QUIET_SECONDS

    def card_ledger(self, did, ln):
        """A finished lane card is the result of its request's ledger item whether or not it is ever spoken (11-27:
        78 of 84 were dropped as stale and the next agent never knew they existed). A card on an action is research
        about it, not its completion; a failed card does not fail an item the agent may still answer."""
        item = self.item_of.get(did)
        if not item:
            return
        ok = ln.get("state") == "done" and bool((ln.get("card") or {}).get("summary"))
        text = (ln.get("card") or {}).get("summary") or f"lane {ln.get('id')} ended {ln.get('state')} with no card"
        final = (ok and self.card_verified(ln) and did not in self.__dict__.get("hermes_done", set())
                 and (self.ledger_item(did) or {}).get("kind") != "action")
        self.ledger_do(request_ledger.receipt, item, "conductor", "lane", str(ln.get("id")), str(ln.get("model") or ""))
        self.ledger_do(request_ledger.result, item, f"lane:{lane_cards.who(ln)}", text, ok, [str(ln.get("id"))], final)

    async def say_earlier_cards(self, now, latest):
        """One batch of up to two actual results per caller turn; hold the remainder for later gaps."""
        limit = lane_cards.MAX_EARLIER - self.earlier_this_turn
        if not self.earlier_cards or not latest or limit <= 0:
            return
        if not self.card_floor_free(now) or not self.status_gap_open(now, acks=False):
            return
        turn = self.card_turn
        lines, consumed = await self.earlier_results(self.earlier_cards, limit)
        now = time.monotonic()
        if (self.closed or turn != self.card_turn or self.readback_did or latest != getattr(self, "latest_did", latest)
                or not self.card_floor_free(now) or not self.status_gap_open(now, acks=False)):
            return
        self.earlier_cards = [(did, ln) for did, ln in self.earlier_cards if ln["id"] not in consumed]
        if not lines:
            return
        ids = [lid for lid, _ in lines]
        text = " ".join(text for _, text in lines)
        self.cards_read.update(ids)
        self.earlier_this_turn += len(ids)
        self.stamp_status(now)
        self.counts["lane_cards_earlier_spoken"] += len(ids)
        self.trace("lanes.card_earlier_spoken", did=latest, lanes=ids, text=text[:500])
        self.speak_for(latest, text, lane=True)

    def card_skipped(self, reason, did, lanes, **data):
        self.counts["lane_cards_skipped"] += 1
        self.trace("lanes.card_skipped", reason=reason, did=did, lanes=[ln.get("id") for ln in lanes],
                   models=[ln.get("model") for ln in lanes], **data)

    async def watch_cards(self):
        """Poll the conductor's state.json (a stat per second, parsed only when its mtime moves). A lane of an ask
        from THIS call that finishes with a card is spoken as one short line once the floor is free: lanes that
        finish together are batched, and older requests enter the delta queue instead of being dropped.
        An explicit read-back takes precedence over automatic results."""
        path, key, mtime = tiers.CONDUCTOR_STATE, lane_cards.call_key(self.id), None
        state = None
        pending: list[tuple[str, dict, float]] = []  # (did, lane, first seen)
        start_seen = self.__dict__.setdefault("conductor_starts_seen", set())
        start_pending = {}
        last_new = float("-inf")
        while not self.closed:
            await self.sleep(lane_cards.POLL_SECONDS)
            if self.closed:
                break
            now = time.monotonic()
            try:
                stamp = path.stat().st_mtime_ns
            except OSError:
                stamp = None
            if stamp is None:
                state, mtime = None, None
            if stamp is not None and stamp != mtime and (loaded := lane_cards.read(path)) is not None:
                state = loaded
                mtime = stamp
                self.cards_busy = lane_cards.busy(state, key)
                for ask, ln in lane_cards.lanes_of(state, key):
                    did = self.ask_dids.get(lane_cards.request_key(ask.get("text") or "")) or ask.get("did")
                    # A poll may first see a completed worker. t0 distinguishes it from a synthetic card.
                    if ln["id"] not in start_seen and (ln.get("state") == "running" or
                                                       (ln.get("state") == "done" and ln.get("t0"))):
                        start_seen.add(ln["id"])
                        start_pending.setdefault((ask.get("id"), did), []).append(ln)
                    if ln["id"] in self.cards_seen or ln.get("state") not in ("done", "failed"):
                        continue
                    self.cards_seen.add(ln["id"])
                    self.card_ledger(did, ln)
                    if ln.get("state") != "done" or not (ln.get("card") or {}).get("summary"):
                        self.card_skipped("failed", did, [ln], ask=ask.get("id"))
                    elif not did:
                        self.card_skipped("unmapped", None, [ln], ask=ask.get("id"))
                    elif self.background_owns(did, ask.get("text")):
                        self.card_skipped("background_owns", did, [ln], ask=ask.get("id"))
                    elif not self.card_verified(ln):
                        self.card_skipped("unverified", did, [ln], ask=ask.get("id"))
                    else:
                        pending.append((did, ln, now))
                        last_new = now
            if start_pending and not self.approvals and self.card_floor_free(now):
                group = next(iter(start_pending))
                ask_id, did = group
                lanes = start_pending.pop(group)
                models = Counter(lane_cards.who(ln) if ln.get("model") not in (None, "-") else "worker"
                                 for ln in lanes)
                names = [f"{lane_layout.spoken_n(n)} {model} lane{'s' if n != 1 else ''}" for model, n in models.items()]
                line = " and ".join(names)
                text = line[0].upper() + line[1:] + " started."
                self.trace("lanes.start_receipt", did=did, ask=ask_id, lanes=[ln["id"] for ln in lanes], text=text)
                self.speak_for(did, text, lane=True)
                continue  # give the receipt the floor before any result cards
            latest = getattr(self, "latest_did", None)
            pending = [p for p in pending if p[1]["id"] not in self.cards_read]
            self.earlier_cards.extend((did, ln) for did, ln, _ in pending if did != latest)
            pending = [p for p in pending if p[0] == latest]
            if self.readback_did:
                await self.say_readback(state, key, now)
                continue
            await self.say_earlier_cards(now, latest)
            now = time.monotonic()
            if self.closed or latest != getattr(self, "latest_did", None):
                continue
            if not pending:
                if not start_pending and not self.cards_busy and not self.earlier_cards and now - self.last_delegation_at > lane_cards.IDLE_SECONDS:
                    break
                continue
            held = now - min(p[2] for p in pending)
            if not self.speak_mode and held > lane_cards.HOLD_SECONDS:
                self.card_skipped("floor_busy", latest, [p[1] for p in pending], held_s=round(held, 1))
                pending = []
                continue
            if now - last_new < lane_cards.BATCH_SECONDS or not self.card_floor_free(now):
                continue
            said, fresh = list(self.spoken_for.get(latest, [])), []
            for _, ln, _ in pending:
                if lane_cards.superseded(ln["card"]["summary"], latest, self.spoken_for):
                    self.card_skipped("superseded", latest, [ln])
                elif not self.speak_mode and lane_cards.said_already(ln["card"]["summary"], said):
                    self.card_skipped("already_said", latest, [ln])
                else:
                    fresh.append(ln)
                    said.append(ln["card"]["summary"])
            mode = self.speak_mode
            if mode:
                verified = [(ln, summary) for ln in fresh if (summary := await self.card_summary(latest, ln, "card"))]
                text = " ".join(lane_cards.speak_line(ln, summary) for ln, summary in verified)
                spoken = [ln for ln, _ in verified]
            else:
                text = await self.card_text(fresh)
                spoken = fresh[:lane_cards.MAX_LANES]
            if (self.closed or latest != getattr(self, "latest_did", None) or self.readback_did
                    or mode != self.speak_mode or not self.card_floor_free(time.monotonic())):
                continue
            pending = []
            if not text:
                continue
            self.cards_read.update(ln["id"] for ln in spoken)
            self.counts["lane_cards_spoken"] += len(spoken)
            self.trace("lanes.card_spoken", did=latest, lanes=[ln["id"] for ln in spoken],
                       models=[ln.get("model") for ln in spoken], held_s=round(held, 1), text=text[:500])
            self.speak_for(latest, text, lane=True)
        self.card_watch = None

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
        and the previous turn's lane answer ("Hey there! Not much...") was spoken for a new question (11-16)."""
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

    def say_first(self, did, text, lane=True):
        """Speak one instant answer per question; a second instant lane (relay vs Haiku/Groq) stays silent."""
        if lane or not text or NOISE.match(text) or self.closed or getattr(self, "latest_did", did) != did:
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
        # 11-25: the fast lane answered "I can't see your screen" about the owner's own TV and lost the thread of the
        # call: give it what the TV shows now and the call's last turns with every question.
        tv = await asyncio.to_thread(tv_screen)
        said = "\n".join(f"{owner_identity.LABEL if role == 'user' else 'Voice'}: {' '.join(text.split())[:300]}"
                         for role, text in list(self.protocol.exchanges)[-6:])
        ask = (f"Said on this call so far:\n{said}\n\n" if said else "") + (f"{tv}\n\n" if tv else "") + \
            f"{owner_identity.LABEL} now asks: {ask}"
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
            text = await self.groq(owner_identity.fill(FAST_SYSTEM.format(who="Qwen on Groq")) + "\n\n" + self.context_text[-6000:], ask,
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
        made a 22 s monologue (11-16 call); the owner wants the most important fact first and little else."""
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
            self.speak_for(did, text, lane=True)

    async def merge_lanes(self, did, request, answers):
        """Two spoken sentences from the lanes, most important first; SKIP if they add nothing to what was said."""
        before = self.__dict__.get("first_said", {}).get(did, "")
        found = "\n".join(f"- {name}: {text}" for name, text in answers)
        text = await self.groq(
            owner_identity.fill("Merge helper findings into what the owner hears on a live voice call: at most two short spoken sentences, "
            "the single most important fact first, no lane names, no plumbing. If the findings add nothing to what "
            "he already heard, reply exactly SKIP."),
            f"He asked: {request}\nAlready said: {before or '(nothing)'}\nFindings:\n{found}", 160)
        return text or answers[0][1][:280]

    async def ask_hermes(self, did, request):
        if self.chat_lookup and not self.chat:
            try:
                self.chat = await asyncio.wait_for(asyncio.shield(self.chat_lookup), 5)
            except Exception:  # noqa: BLE001
                self.chat = ""
            self.trace("chat.resolved", chat=self.chat)
        anchor = self.chat or await self.anchor_chat()
        if anchor:
            async with self.chat_slots.slot(did, self.queued_notice(did)):
                self.dequeued(did)
                try:
                    return await self.ask_chat_slot(did, request, anchor)
                except (OSError, aiohttp.ClientError, chat_continuity.NotStarted) as exc:
                    if isinstance(exc, TimeoutError):  # an OSError too, but the agent ran: never a second run
                        raise
                    # The dashboard is down or refused the run before it began: the CLI is all that is left.
                    self.trace("delegation.dashboard_unreachable", did=did, error=type(exc).__name__,
                               detail=str(exc)[:200])
        self.hermes_path[did] = "host"
        async with self.host_slots.slot(did, self.queued_notice(did)):
            self.dequeued(did)
            context = self.host_context(did)
            return await self.run_host(did, context + "\n\nRequest: " + request if context else request)

    def queued_notice(self, did):
        """What the voice hears when a request has to wait: it has not started, and where it is in line."""
        def notice(position, running):
            self.told_queued[did] = time.monotonic()
            self.counts["delegations_queued"] += 1
            self.trace("delegation.queued", did=did, position=position, running=running)
            self.stamp_status(time.monotonic())  # it says the request is waiting: a status line in the call's budget
            self.speak_for(did, QUEUED.format(running=running, position=position))
        return notice

    def dequeued(self, did):
        if did in self.told_queued:
            self.trace("delegation.dequeued", did=did, waited_s=round(time.monotonic() - self.told_queued[did], 3))

    async def anchor_chat(self):
        """The chat a background agent runs beside when the call matched none (112025, 11-27: chat.resolved "" sent
        three requests to cold CLI runs and none answered before hang-up). Not the newest-started chat: on 11-28 that
        was a 22-message side chat and the call's memory was 170 chars. chat_continuity.anchor_chat: the pin file,
        else the chat the owner typed in most recently in the last 6 h, weighted by size."""
        if not (CHAT_CONTINUITY and self.token):
            return ""
        if "anchor" not in self.__dict__:
            self.anchor, why = await asyncio.to_thread(chat_continuity.anchor_chat, self.profile)
            self.trace("chat.anchor", chat=self.anchor, why=why)
        return self.anchor

    async def ask_chat_slot(self, did, request, sid=None):
        """One admitted request on the chat path: a background agent beside the chat (the fast path: 35k tokens a
        call against the chat's 489k on 11-27), or the chat's own turn when the dashboard says no turn runs in it
        (CHAT_TURNS, or the background run was refused before it began). Never folded into a running turn: 114034's
        request joined a turn the owner had going, got no line of its own and was silent for 184 s."""
        sid = sid or self.chat
        if topic_sessions.ENABLED and not CHAT_TURNS:
            answer = await self.ask_topic(did, request, sid)
            if answer is not None:
                return answer
        if not CHAT_TURNS:
            try:
                return await self.ask_background(did, request, sid)
            except chat_continuity.NotStarted as exc:  # refused before it began (a cold agent build past 30 s)
                if sid != self.chat or self.__dict__.get("chat_turn"):
                    raise
                self.trace("delegation.background_refused", did=did, detail=str(exc)[:200])
        if sid == self.chat and not self.__dict__.get("chat_turn"):
            self.chat_turn = did  # set before the first await: a request a moment later sees the turn running
            try:
                # No fallback once submitted: a lost acknowledgement or final reply does not mean the turn didn't run.
                answer = await asyncio.wait_for(chat_continuity.ask_chat(
                    self.manager.http, self.token, self.profile, sid, request, DELEGATION_TIMEOUT_SECONDS,
                    only_if_idle=True), DELEGATION_TIMEOUT_SECONDS)
            except chat_continuity.ChatBusy:
                self.trace("delegation.chat_busy", did=did, chat=sid)
                answer = False
            finally:
                if self.__dict__.get("chat_turn") == did:
                    self.chat_turn = None
            if answer:
                self.hermes_path[did] = "chat"
                self.counts["chat_turns"] += 1
                self.trace("delegation.chat", did=did, chat=sid)
                return answer
            if answer is None:  # a turn began between the resume and the submit: this request has no answer there
                self.counts["chat_fold_rerun"] += 1
                self.trace("delegation.fold_rerun", did=did, chat=sid)
        return await self.ask_background(did, request, sid)

    async def ask_topic(self, did, request, sid):
        """The request as a turn of its project group's chat (topic_sessions): the chat holds the group's earlier
        requests and answers, so a follow-up starts warm and the call leaves one chat per project, not one session
        per request. None when it did not start there (the group's chat is DEPTH deep, or the dashboard refused before
        the submit): the caller runs the request beside the chat as before. Never a second run once submitted."""
        group, why = self.topics.route(request)
        if not self.topics.admit(group.key):
            self.counts["topic_overflow"] += 1
            self.trace("delegation.topic_overflow", did=did, group=group.key, depth=topic_sessions.DEPTH)
            return None
        self.hermes_path[did] = "topic"
        primed = group.key in self.topic_primed
        self.topic_primed.add(group.key)

        def compose(created: bool) -> str:
            if created:  # a new chat starts empty: the anchored chat's memory and the call's brief, once
                memory = chat_continuity.chat_memory(self.profile, sid)
                self.trace("delegation.memory", did=did, chat=sid, chars=len(memory))
                return stream_rule + self.side_request(request, memory, sid, did)
            if not primed:  # first request of this call in a chat that already knows the project
                context = self.host_context(did)
                return stream_rule + ((context + "\n\nRequest: " + request) if context else request)
            said = "\n".join(f"{'Caller' if role == 'user' else 'Voice'}: {' '.join(text.split())[:400]}"
                             for role, text in list(self.protocol.exchanges)[-4:])
            led = self.ledger_brief(did)
            return (stream_rule + f"the owner asked this on the same live voice call; answer in a few short spoken sentences."
                    + (f"\n\nSaid on the call since:\n{said}" if said else "") + (f"\n\n{led}" if led else "")
                    + f"\n\nRequest: {request}")

        action = request_kind(request) == "action"
        stream_rule = ("For the voice call, do not announce that you are checking. Only after checking the evidence, "
                       "start the final answer with FINDING: followed by one complete verified sentence, then a space. "
                       "If you revise an earlier spoken fact, start with correction: and state the verified fact once. "
                       "Do not stack apologies.\n\n")
        partial = ""

        async def delta(text):
            nonlocal partial
            if not isinstance(text, str):
                partial = ""
            elif len(partial) < 512 and did not in self.findings:
                partial += text[:512]
                await self.offer_finding(did, partial, action=action)

        try:
            answer, chat, created = await asyncio.wait_for(topic_sessions.ask_topic(
                self.manager.http, chat_continuity.WS, self.token, self.profile, group, compose,
                known=self.topics.chats.get(group.key, ""),
                on_submit=lambda chat, created, status: self.trace(
                    "delegation.topic", did=did, group=group.key, why=why, chat=chat, created=created,
                    status=status), on_delta=delta), DELEGATION_TIMEOUT_SECONDS)
        except topic_sessions.NotStarted as exc:
            self.counts["topic_refused"] += 1
            self.trace("delegation.topic_refused", did=did, group=group.key, detail=str(exc)[:200])
            if not primed:
                self.topic_primed.discard(group.key)
            return None
        finally:
            self.topics.release(group.key)
        self.topics.chats[group.key] = chat
        self.counts["topic_turns"] += 1
        self.counts["topic_chats_created"] += int(created)
        return answer

    async def ask_background(self, did, request, sid):
        self.hermes_path[did] = "background"
        # A read-only indexed read, 0.4 ms on tek's 265k-message store (11-27); inline, so the replay clock never
        # advances under it the way a thread hop let it.
        memory = chat_continuity.chat_memory(self.profile, sid)
        self.trace("delegation.memory", did=did, chat=sid, chars=len(memory))
        answer = await asyncio.wait_for(chat_continuity.ask_background(
            self.manager.http, self.token, self.profile, sid, self.side_request(request, memory, sid, did),
            lambda task: self.trace("delegation.background", did=did, task=task)),
            DELEGATION_TIMEOUT_SECONDS)
        self.counts["background_turns"] += 1
        return answer

    def ledger_brief(self, did=None, max_chars: int = 4000, proof_rule: bool = True) -> str:
        """What the next agent must read to know this call: earlier answers, promises, proof and results, the current
        request marked. For an action it also says how to prove the work started."""
        try:
            text = request_ledger.brief(self.ledger_call, max_chars, current=self.item_of.get(did))
        except Exception:  # noqa: BLE001
            return ""
        item = self.item_of.get(did)
        if text and item and proof_rule and (self.ledger_item(did) or {}).get("kind") == "action":
            text += (f"\n\nThis request ({item}) is an action. Do not say it started, is running or is done unless you "
                     f"started it and can prove it: end your answer with one line `PROOF: <pid|kanban|cron|commit|file> "
                     f"<id, sha or path>` (a kanban card id, a cron job id, a process id, a commit, or a file you "
                     f"wrote). With no proof line, say what you could not start and why.")
        return text

    def host_context(self, did=None, ledger: bool = True, chat_snapshot: bool = True) -> str:
        """What a Hermes that starts without the call's chat needs to place a request: the chat the call opened from
        and the call's recent words. Tek's Hermes got only the words to it, so "the glass crash in PR 28" was looked
        up in the wrong repo (11-22 film call, take 7)."""
        led = self.ledger_brief(did) if ledger else ""
        cross = self.__dict__.get("cross_brief") or ""
        if not (self.context_text or self.call_note or cross):
            return led
        # The written brief the primary voice opened with (without its voice-only framing): it can name what the
        # chat's lines do not ("PR 28" is kvnloo/hermes-android#28). Without one, the primary's brief is a digest of
        # the same chat lines, which follow in full.
        note = f"\n\nBrief for this call:\n{self.call_note}" if self.call_note else ""
        chat = f"\n\n{self.context_text[-6000:]}" if self.context_text and chat_snapshot else ""
        chat += f"\n\n{cross}" if cross else ""
        said = "\n".join(f"{'Caller' if role == 'user' else 'Voice'}: {' '.join(text.split())[:400]}"
                         for role, text in list(self.protocol.exchanges)[-6:])
        return (f"{CALL_CONTEXT}{note}{chat}"
                + (f"\n\nSaid on the call so far:\n{said}" if said else "")
                + (f"\n\n{led}" if led else ""))

    async def run_host(self, did, request):
        """One Hermes run on this host, in a slot of host_slots (beside any the call already has going)."""
        self.trace("delegation.hermes", did=did, running=len(self.host_slots.running))
        return await asyncio.to_thread(run_hermes, request, self.profile, True)

    def side_request(self, request, memory: str = "", sid: str = "", did=None):
        """A background agent's prompt: it starts fresh, so it gets the chat's summary and turns since (memory,
        chat_continuity.chat_memory), the call's brief, the chat it opened from and the call's recent words with the
        request (host_context), or at least the call's recent words. The call's request ledger (earlier answers,
        promises, proof) comes last, next to the request: the call's memory of its own work."""
        # With fresh chat memory (read now, timestamped), drop host_context's chat snapshot: it was copied when the call
        # opened and is minutes to an hour stale on a long call (11-28 13:55). The call's own recent words stay.
        context = self.host_context(did, ledger=False, chat_snapshot=not memory)
        led = self.ledger_brief(did)
        if memory:
            look = (f" For anything older or not covered here, search that chat first (session_search with "
                    f"session_id={sid}) before answering; never say you lack context." if sid else "")
            context = (owner_identity.fill("You are answering beside the owner's main chat with his agent;") + f" you share its work and "
                       f"history.{look}\n\n{memory}" + (f"\n\n{context}" if context else ""))
        if context:
            return context + (f"\n\n{led}" if led else "") + "\n\nRequest: " + request
        said = "\n".join(f"{owner_identity.LABEL if role == 'user' else 'Voice'}: {text[:400]}"
                         for role, text in list(self.protocol.exchanges)[-6:])
        return (owner_identity.fill("The owner asked this on a live voice call.") + f" Answer it on its own, in a few short spoken sentences."
                f"\n\nRecent call:\n{said or '(none)'}" + (f"\n\n{led}" if led else "") + f"\n\nRequest: {request}")

    async def sleep(self, seconds):  # the replay runs this on its own clock
        await asyncio.sleep(seconds)

    async def until(self, job, at):
        """Wait until `at` (monotonic) or the job ends; True while the job still runs on an open call."""
        if (wait := at - time.monotonic()) > 0 and not job.done():
            timer = asyncio.ensure_future(self.sleep(wait))
            try:
                await asyncio.wait({job, timer}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                timer.cancel()
        return not job.done() and not self.closed

    def floor_busy(self):
        """The owner is talking, the voice is mid-reply or was just handed a line: the lane cards' gate (card_floor_free).
        The duplex floor queues a status line itself (lane=True)."""
        return not self.duplex and not self.card_floor_free(time.monotonic())

    def status_gap_open(self, now, acks=True) -> bool:
        """No status line (and, with acks, no ack of a request) within STATUS_GAP."""
        return now - (self.status_at if acks else self.line_at) >= STATUS_GAP

    def stamp_status(self, now):
        self.status_at = self.line_at = now

    def take_status(self, source, now=None) -> bool:
        """The call's one status budget, for a line from outside the request loop (Hermes's progress ingress): True
        (and the gap restarts) when the floor is free and no status line or ack played within STATUS_GAP."""
        now = time.monotonic() if now is None else now
        if self.closed or self.floor_busy() or not self.status_gap_open(now):
            self.trace("status.dropped", source=source, reason="floor" if self.floor_busy() else "gap")
            return False
        self.stamp_status(now)
        return True

    async def receipt(self, did, job):
        """Status lines for a Hermes request still running: a receipt at RECEIPT_SECONDS, its elapsed time at
        PROGRESS_AT, then at CAP_SECONDS one honest line saying where the answer will land, and no more. Every line
        draws on the call's one budget (STATUS_GAP after any status line or ack): a receipt or progress line that
        cannot fit before its window closes is dropped, and one line covers every request running (PROGRESS_MANY).
        The cap waits for the gap and a quiet floor instead. None for a request a lane already answered or one not
        yet running (it was told it is queued); its answer is spoken whenever it lands."""
        began = time.monotonic()
        self.status_running[did] = began
        try:
            await self.status_marks(did, job, began)
        finally:
            self.status_running.pop(did, None)
            self.status_covered.pop(did, None)

    def status_window(self, kind, began):
        """When a deferred line stops waiting: a receipt gives way to the progress mark, a progress line must leave
        the cap its gap, the cap waits as long as the request runs."""
        if kind == "receipt":
            return began + UNPROVEN_SECONDS
        return began + DELEGATION_TIMEOUT_SECONDS

    async def status_marks(self, did, job, began):
        marks = [(UNPROVEN_SECONDS, "unproven")]
        for mark, kind in marks:
            if not await self.until(job, began + mark):
                return
            if did in self.__dict__.get("first_said", {}):
                return
            if "running" not in (self.host_slots.state(did), self.chat_slots.state(did)):
                continue  # still queued: "still working on it" would not be true
            if kind == "unproven" and not self.unproven(did):
                continue  # only an action with a promise and no proof hears this
            if kind in self.status_covered.get(did, ()):
                continue  # a line for the whole call already said this one runs
            limit = self.status_window(kind, began)
            # 151433 replayed: a request every 20-60 s, each ack restarting the gap, held one cap to 293 s. Past one
            # gap beyond its mark the cap waits only for the last status line's gap (other requests' acks no longer).
            def gap(now):
                return self.status_gap_open(now)
            while self.floor_busy() or not gap(time.monotonic()):
                if time.monotonic() >= limit or not await self.until(job, min(limit, time.monotonic() + 0.5)):
                    break
            if job.done() or self.closed:
                return
            now = time.monotonic()
            if self.floor_busy() or not gap(now) or kind in self.status_covered.get(did, ()):
                self.trace("delegation.status_dropped", did=did, kind=kind,
                           reason="floor" if self.floor_busy() else "gap")
                continue
            others = [d for d, t in self.status_running.items()
                      if d != did and now - t >= RECEIPT_SECONDS and d not in self.__dict__.get("first_said", {})
                      and "running" in (self.host_slots.state(d), self.chat_slots.state(d))]
            text = UNPROVEN if kind == "unproven" else RECEIPT
            if kind == "receipt":
                for d in others:  # this line told the owner they run too: no receipt line of their own
                    self.status_covered.setdefault(d, set()).add("receipt")
            self.stamp_status(now)
            self.counts["receipts" if kind == "receipt" else f"status_{kind}"] += 1
            self.trace("delegation.receipt" if kind == "receipt" else f"delegation.{kind}", did=did,
                       s=round(now - began, 3), path=self.hermes_path.get(did), covers=len(others) + 1)
            self.speak_for(did, text, lane=True)  # a status line: the answer supersedes it unsaid, as a lane guess

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

    @staticmethod
    def brief_events(text, channel="developer"):
        return [{"type": "session.context.append", "channel": channel,
                 "content": [{"type": "input_text", "text": part}]}
                for part in chunks(text, int(os.environ.get("HERMES_VOICE_BRIEF_CHUNK", "1800")))]

    async def renew_subscription(self, guest=None):
        owner = guest or self
        await owner.subscription.ready.wait()
        lead = min(45, PROVIDER_CAP_SECONDS / 3)
        trace = guest.trace if guest else self.trace
        prefix = "" if guest else "call."
        while not self.closed and not owner.closed:
            deadline = owner.subscription.deadline(PROVIDER_CAP_SECONDS)
            renew_at = deadline - lead
            if self.created + MAX_CALL_SECONDS <= renew_at:
                return
            await asyncio.sleep(max(0, renew_at - time.monotonic()))
            if owner.closed:
                return
            try:
                async with asyncio.timeout_at(deadline):
                    await self.replace_subscription(deadline, guest)
            except Exception as exc:
                trace(prefix + "renewal_failed", error=type(exc).__name__)
                if time.monotonic() >= deadline:
                    return  # The old transport owns closure if it actually expires.
                await asyncio.sleep(min(2, deadline - time.monotonic()))

    async def replace_subscription(self, deadline, guest=None):
        owner = guest or self
        old = owner.subscription
        if not self.microphone or not self.primary_audio:
            raise RuntimeError("Call audio is not connected")
        candidate = owner.new_subscription()
        protocol = owner.protocol
        duplex = self.duplex
        source = "guest" if guest else "primary"
        trace = guest.trace if guest else self.trace
        try:
            candidate.attach(self.subscribe_mic())
            await start_subscription(self.manager.http, candidate.peer, guest.persona if guest else PERSONA,
                                     guest.config.voice if guest else "sol", trace)
            await candidate.ready.wait()
            # Prefer a quiet boundary. A job that outlasts the deadline can still enqueue its answer.
            while time.monotonic() < deadline - 2 and not owner.closed and (
                self.microphone[1].level.on is not None or time.monotonic() - self.output_loud_at < 0.75
                or (guest.floor.guest_jobs if guest else self.pending_delegations)
                or (duplex and duplex.ledger.pending(source))
            ):
                await asyncio.sleep(0.15)
            if (self.closed or owner.closed or not candidate.output or candidate.output.readyState != "live"
                    or candidate.peer.connectionState != "connected" or candidate.channel.readyState != "open"):
                raise RuntimeError("Replacement voice transport disconnected")
            grok = self.guest if not guest and isinstance(self.guest, guest_voice.GuestVoice) else None
            recent = self.call_recap(protocol)
            summary = (owner_identity.fill("Continuing the same call. This is a recap, not a new request. Stay silent until the owner "
                       "speaks again; do not repeat prior actions or answers.\n") + recent)
            if grok:
                summary = self.guest_context()
            opening = self.brief_events(guest.persona) if guest else self.opening_context
            if not guest and duplex and duplex.floor.available:
                opening = opening + self.brief_events(duplex.config.primary_note)
            for event in opening + self.brief_events(summary):
                candidate.channel.send(json.dumps(event))
                if not guest:
                    context_sent(self.trace, event, 0)

            # The provider's turn IDs and clocks belong to this transport, not to the call.
            old.active = False
            owner.subscription = candidate
            protocol.seen.clear()
            protocol.turn_text.update(user="", assistant="")
            protocol.last_user = protocol.last_delegation = ""
            protocol.user_open = False  # the retired session never sends turn.done for a turn it had open
            while protocol.deferred:
                protocol.release(*protocol.deferred.pop(0))
            candidate.activate()
            if guest:
                guest.audio.clear()
                guest.on_track(candidate.output)
            else:
                self.primary_audio.source = candidate.output
                self.flush(self.provider_dc, self.to_provider)
            if duplex:
                duplex.transport_replaced(source)
            if grok:
                grok.share(summary, "renewal")
            trace("renewed" if guest else "call.renewed", seconds=round(time.monotonic() - self.created, 3),
                  recap_turns=len(protocol.exchanges), mobile=self.mobile.connectionState)
        finally:
            await asyncio.shield((old if candidate is owner.subscription else candidate).close())

    async def watchdog(self):
        await asyncio.sleep(min(35, MAX_CALL_SECONDS))
        voice_up = self.leg.ready if self.leg else self.provider.connectionState == "connected"
        if MAX_CALL_SECONDS > 35 and (self.mobile.connectionState != "connected" or not voice_up):
            await self.close("connection_timeout")
            return
        await asyncio.sleep(max(0, self.created + MAX_CALL_SECONDS - time.monotonic()))
        await self.close("duration_limit")

    async def maybe_handoff(self, reason):
        """The owner left the call screen: hand the conversation to z0live on the phone (opt-out: HERMES_VOICE_HANDOFF=off)."""
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
        current = asyncio.current_task()
        for task in list(self.tasks):
            if task is not current:
                task.cancel()
        await asyncio.gather(self.mobile.close(), self.subscription.close(), *([self.leg.close()] if self.leg else []),
                             *(voice.close() for voice in {self.guest, self.duplex} if voice), return_exceptions=True)
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


async def health(request):
    # Direct loopback startup check. This does not authenticate to a provider.
    ready = not request.app["manager"].draining
    return web.json_response({"ready": ready, "pid": os.getpid()}, status=200 if ready else 503)


async def version(request):
    # The deployed commit (the release directory) and this file's hash; /_z0live/ready stays exact for rolling_restart.
    return web.json_response({"release": RELEASE, "rev": ADAPTER_REV, "pid": os.getpid()})


def make_app():
    from live_progress import PATH, progress

    app = web.Application(client_max_size=512 * 1024)
    app.cleanup_ctx.append(resources)
    app.router.add_post(PATH, progress)
    app.router.add_post(passthru.PATH, passthru.control)
    app.router.add_get("/_z0live/ready", health)
    app.router.add_get("/health", health)
    app.router.add_get("/_z0live/version", version)
    app.router.add_get("/api/audio/voice-live/status", status)
    app.router.add_post("/api/audio/voice-live/session", session)
    app.router.add_get("/api/audio/voice-live/diagnostics", diagnostics)
    return app


async def serve(port, stop, *, app=None, sock=None):
    runner = web.AppRunner(make_app() if app is None else app, access_log=None, shutdown_timeout=70)
    await runner.setup()
    try:
        site = web.TCPSite(runner, "127.0.0.1", port) if sock is None else web.SockSite(runner, sock)
        await site.start()
        await stop.wait()
    finally:
        await runner.cleanup()


async def main():
    stop = asyncio.Event()
    # Reissuing SIGTERM after an interrupted rollout must not interrupt the drain.
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    await serve(int(os.environ.get("HERMES_MOBILE_VOICE_PORT", "9168")), stop)


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(name)s %(levelname)s %(message)s")
    LOG.setLevel(logging.INFO)
    # aioice addresses and SDP payloads are deliberately not logged.
    asyncio.run(main())
