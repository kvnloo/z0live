"""Hermes mobile app Live -> z0live lanes relay (stdlib; used by the subscription-voice adapter).

The Hermes app's Live voice is GPT-Live in our adapter (hermes-mobile/subscription-voice/mobile_live.py).
GPT-Live has no tool slot, so it cannot call read_context: this relay is its lane path instead.
- `transcript(text, final)`: STT partials and finals go to the broker (POST /v1/lanes/turn), so the
  blackboard fills while Kevin talks (preset D: heartbeat speculation on partials).
- `answer(request)`: when GPT-Live delegates, the latest final's lane answer is returned at once if it
  may be spoken (not held by the pre-speak gate, or already verified); a held answer is awaited until a
  large lane releases it.
- `updates()`: verified / corrected entries after the answer, pushed into GPT-Live as silent context
  (push stands in for the voice's read_context pull).
- `covered(lane_answer, hermes_answer)`: the slow Hermes run is spoken only if it changes the answer.
A spoken "start live with ..." line or "switch to preset B" is handled by the broker (Fanout.on_final).
No token file -> the relay is off and the adapter behaves exactly as before.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
import urllib.request
from pathlib import Path

from .lane_fanout import _NUM, _STOP, _WORD

LOG = logging.getLogger("z0live.mobile_relay")
BASE = os.environ.get("Z0LIVE_RELAY_URL", "http://127.0.0.1:" + os.environ.get("Z0LIVE_PORT", "8791"))
TOKEN_FILE = Path(os.environ.get("Z0LIVE_RELAY_TOKEN_FILE",
                                 str(Path.home() / ".config/z0live/hermes-mobile-relay.token")))


def covered(lane, hermes, question=""):
    """True when the Hermes answer adds nothing the lane answer did not already say: every number
    and most content words of the (short) lane answer appear in it (question words excluded)."""
    lane, hermes, q = (str(x or "").lower() for x in (lane, hermes, question))
    qn, qw = set(_NUM.findall(q)), set(_WORD.findall(q)) | _STOP
    if not (set(_NUM.findall(lane)) - qn) <= set(_NUM.findall(hermes)):
        return False
    words = set(_WORD.findall(lane)) - qw
    return bool(words) and len(words & set(_WORD.findall(hermes))) / len(words) >= 0.6


class LaneRelay:
    def __init__(self, token, base=BASE, choice=None, timeout=20):
        self.token, self.base, self.choice, self.timeout = token, base.rstrip("/"), choice, timeout
        self.sid = None
        self.turn = None  # {"text", "receipt", "event": asyncio.Event, "at"}
        self.pushed: dict[int, str] = {}  # entry id -> status already pushed to the voice
        self.counts: dict[str, int] = {}
        self.partial_at, self.partial_busy = 0.0, False

    @classmethod
    def from_env(cls, choice=None):
        try:
            token = TOKEN_FILE.read_text().strip()
        except OSError:
            return None
        return cls(token, choice=choice or os.environ.get("Z0LIVE_RELAY_LANES") or None) if token else None

    def _call(self, path, body=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": "Bearer " + self.token,
                                              "Content-Type": "application/json"},
                                     method="POST" if body is not None else "GET")
        with urllib.request.urlopen(req, timeout=self.timeout) as r:
            return json.loads(r.read())

    async def call(self, path, body=None):
        return await asyncio.to_thread(self._call, path, body)

    def count(self, key):
        self.counts[key] = self.counts.get(key, 0) + 1

    async def start(self, profile=""):
        body = {"client": "web", "transports": ["bidi"], "context": {"app": "hermes-mobile-live", "profile": profile}}
        if self.choice:
            body["lanes"] = self.choice
        result = await self.call("/v1/realtime/session", body)
        self.sid = result["session_id"]
        LOG.info("relay session sid=%s lanes=%s", self.sid[:8], result.get("lanes"))
        return result

    async def transcript(self, text, final, context=""):
        """Returns the broker receipt for a final (None for partials or on error)."""
        if not self.sid or not text.strip():
            return None
        if not final:
            now = time.monotonic()
            if self.partial_busy or now - self.partial_at < 0.4:  # the broker's heartbeat is 400 ms anyway
                return None
            self.partial_busy, self.partial_at = True, now
            try:
                await self.call("/v1/lanes/turn", {"session_id": self.sid, "text": text, "final": False})
                self.count("partials")
            except Exception as exc:  # noqa: BLE001 - lanes are advisory; the call never fails on them
                self.count("errors")
                LOG.warning("relay partial failed: %s", type(exc).__name__)
            finally:
                self.partial_busy = False
            return None
        turn = self.turn = {"text": text, "receipt": None, "event": asyncio.Event(), "at": time.monotonic()}
        try:
            result = await self.call("/v1/lanes/turn", {"session_id": self.sid, "text": text, "context": context[-2000:]})
            turn["receipt"] = {**result["receipt"], "checking": result.get("checking", False)}
            self.count("finals")
        except Exception as exc:  # noqa: BLE001
            self.count("errors")
            LOG.warning("relay final failed: %s", type(exc).__name__)
            turn["receipt"] = {}
        turn["event"].set()
        return turn["receipt"]

    async def entry(self, entry_id):
        view = await self.call("/v1/tools/read_context", {"session_id": self.sid, "arguments": {}})
        for e in view.get("entries", []):
            if e["id"] == entry_id or e.get("supersedes") == entry_id:
                return e
        return None

    async def answer(self, request, wait_final=2.5, wait_gate=8.0, wait_gather=45.0):
        """Speakable lane answer for the delegated request, or None (Hermes answers alone). The caller
        races this against the driver, so the waits here never delay Hermes."""
        turn = self.turn
        if not self.sid or not turn:
            return None
        try:
            await asyncio.wait_for(turn["event"].wait(), wait_final)
        except asyncio.TimeoutError:
            return None
        r = turn["receipt"] or {}
        if r.get("configured"):
            return r["configured"].get("say")
        if not r.get("answer") and r.get("gathering"):
            return await self.gathered(turn["text"], wait_gather)
        if not r.get("answer"):
            return None
        if not r.get("gated"):
            self.count("lane_answers")
            return r["answer"]
        deadline = time.monotonic() + wait_gate
        while time.monotonic() < deadline:  # held by the pre-speak gate: wait for a large lane
            e = await self.entry(r["entry"])
            if e and e["status"] in {"verified", "corrected"}:
                self.pushed[e["id"]] = e["status"]
                self.count("lane_answers_gated")
                return e["text"]
            await asyncio.sleep(0.25)
        self.count("gate_timeouts")
        return None

    async def gathered(self, text, window):
        """The gather lane's (tool-using) answer for this turn, once it lands on the blackboard."""
        deadline = time.monotonic() + window
        while time.monotonic() < deadline and self.turn and self.turn["text"] == text:
            view = await self.call("/v1/tools/read_context", {"session_id": self.sid, "arguments": {}})
            for e in view.get("entries", []):
                if str(e.get("lane", "")).startswith("gather:") and e.get("turn") == text[:200]:
                    self.count("lane_answers_gathered")
                    self.turn["source"] = "gather"  # the adapter frames it as a first look
                    return e["text"]
            await asyncio.sleep(0.5)
        self.count("gather_timeouts")
        return None

    async def updates(self, entry_id, window=30.0):
        """Yield (status, text) once when a large lane verifies or corrects `entry_id`."""
        deadline = time.monotonic() + window
        while time.monotonic() < deadline:
            e = await self.entry(entry_id)
            if e and e["status"] in {"verified", "corrected"}:
                if self.pushed.get(e["id"]) != e["status"]:
                    self.pushed[e["id"]] = e["status"]
                    return e["status"], e["text"]
                return None
            await asyncio.sleep(0.5)
        return None

    async def close(self):
        if self.sid:
            try:
                await self.call(f"/v1/sessions/{self.sid}/end", {})
            except Exception:  # noqa: BLE001
                pass
