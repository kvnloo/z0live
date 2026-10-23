"""Opt-in transcript/control L0 using llama.cpp and Pocket TTS HTTP endpoints.

Capture, ASR, AEC and immediate playback stop belong to the client. Native work,
its cancellation receipts and the speech outbox remain in the existing liaison.
"""

from __future__ import annotations

import base64
import http.client
import io
import json
import os
import socket
import threading
import time
from urllib.parse import urlencode, urlsplit
import wave

from .core import dumps, text

ADAPTER = "desktop-hybrid-http"
VERSION = "z0live.hybrid.v1"
# A local model may choose silence or one acknowledgement, never facts or tools.
BACKCHANNELS = ("", "Okay.", "On it.", "Got it.")


def provider_setting():
    value = os.environ.get("Z0LIVE_L0_PROVIDER", "openai").strip().lower()
    if value not in {"openai", "local"}:
        raise RuntimeError("Z0LIVE_L0_PROVIDER must be openai or local")
    return value


def loopback_url(value):
    if not isinstance(value, str):
        raise RuntimeError("local HTTP endpoint must be a loopback URL")
    parsed = urlsplit(value)
    if (parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path not in {"", "/"}):
        raise RuntimeError("local HTTP endpoint must be an HTTP loopback origin without credentials")
    try:
        parsed.port
    except ValueError:
        raise RuntimeError("local HTTP endpoint has an invalid port") from None
    return value.rstrip("/")


class Interrupted(RuntimeError):
    """The current speech generation lost its epoch or floor."""


class Cancellation:
    def __init__(self):
        self.lock = threading.Lock()
        self.stopped = False
        self.sockets = set()

    def check(self):
        with self.lock:
            if self.stopped:
                raise Interrupted("local speech interrupted")

    def attach(self, sock):
        with self.lock:
            if self.stopped:
                sock.close()
                raise Interrupted("local speech interrupted")
            self.sockets.add(sock)

    def detach(self, sock):
        with self.lock:
            self.sockets.discard(sock)

    def cancel(self):
        with self.lock:
            self.stopped = True
            for sock in self.sockets:
                try:
                    sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                sock.close()
            self.sockets.clear()


class HTTPTransport:
    """Real bounded HTTP transport. No proxy, redirects, auth store or retries."""

    def request(self, method, url, body=None, headers=None, cancel=None):
        parsed = urlsplit(url)
        connection = http.client.HTTPConnection(parsed.hostname, parsed.port, timeout=15)
        token = cancel or Cancellation()
        sock = None
        try:
            token.check()
            connection.connect()
            sock = connection.sock
            token.attach(sock)
            connection.request(method, parsed.path or "/", body=body, headers=headers or {})
            with connection.getresponse() as response:
                if response.status != 200:
                    raise RuntimeError(f"local backend HTTP {response.status}")
                data = response.read(2_000_001)
                token.check()
                if len(data) > 2_000_000:
                    raise RuntimeError("local backend response exceeds 2000000 bytes")
                return data
        except (OSError, http.client.HTTPException):
            token.check()
            raise RuntimeError("local backend unavailable") from None
        finally:
            if sock is not None:
                token.detach(sock)
            connection.close()


class LocalHTTPBackend:
    """Benchmark seam: probe(), backchannel(text, cancel), synthesize(text, cancel).

    The supplied plan is the selector's already pinned binding. transport is injectable
    only for offline conformance. Normal use performs real loopback HTTP requests.
    """

    def __init__(self, plan, transport=None):
        config = plan.get("local_http")
        if not isinstance(config, dict) or set(config) != {"llm", "tts"}:
            raise RuntimeError("local VoicePlan requires local_http llm and tts origins")
        self.llm = loopback_url(config["llm"])
        self.tts = loopback_url(config["tts"])
        self.model, self.voice = plan["model"], plan["voice"]
        self.transport = transport or HTTPTransport()

    def json(self, method, url, payload=None, cancel=None):
        raw = self.transport.request(
            method, url, dumps(payload).encode() if payload is not None else None,
            {"Content-Type": "application/json"}, cancel,
        )
        try:
            value = json.loads(raw)
        except (ValueError, UnicodeError):
            raise RuntimeError("local backend returned invalid JSON") from None
        if not isinstance(value, dict):
            raise RuntimeError("local backend returned invalid JSON object")
        return value

    def probe(self):
        models = self.json("GET", self.llm + "/v1/models")
        if not any(isinstance(m, dict) and m.get("id") == self.model
                   for m in models.get("data", [])):
            raise RuntimeError("pinned local model unavailable")
        if self.json("GET", self.tts + "/health").get("status") != "healthy":
            raise RuntimeError("local TTS unavailable")

    def backchannel(self, utterance, cancel):
        cancel.check()
        result = self.json("POST", self.llm + "/v1/chat/completions", {
            "model": self.model,
            "stream": False,
            "temperature": 0,
            "max_tokens": 24,
            "messages": [
                {"role": "system", "content": "The user's final request is already admitted to the existing agent. Choose silence or one short acknowledgement. Never answer the request, claim success, ask a question, call tools or describe waiting. Return only JSON with a say field."},
                {"role": "user", "content": text(utterance)},
            ],
            "response_format": {"type": "json_schema", "json_schema": {
                "name": "backchannel", "strict": True, "schema": {
                    "type": "object", "properties": {"say": {"type": "string", "enum": list(BACKCHANNELS)}},
                    "required": ["say"], "additionalProperties": False,
                },
            }},
            "chat_template_kwargs": {"enable_thinking": False},
        }, cancel)
        cancel.check()
        try:
            choice = result["choices"][0]
            value = json.loads(choice["message"]["content"])
            if (choice.get("finish_reason") != "stop" or set(value) != {"say"}
                    or value["say"] not in BACKCHANNELS):
                raise ValueError()
            return value["say"]
        except (ValueError, KeyError, TypeError, IndexError):
            raise RuntimeError("local backend returned an invalid backchannel") from None

    def synthesize(self, say, cancel):
        cancel.check()
        raw = self.transport.request(
            "POST", self.tts + "/tts",
            urlencode({"text": text(say, 700), "voice_url": self.voice}).encode(),
            {"Content-Type": "application/x-www-form-urlencoded"}, cancel,
        )
        cancel.check()
        try:
            with wave.open(io.BytesIO(raw), "rb") as audio:
                if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, 24000, "NONE"):
                    raise ValueError()
                pcm = audio.readframes(24000 * 40 + 1)
                if not pcm or len(pcm) > 24000 * 40 * 2 or len(pcm) % 2:
                    raise ValueError()
        except (wave.Error, EOFError, ValueError):
            raise RuntimeError("local TTS must return mono PCM16 24000 Hz WAV, at most 40 seconds") from None
        return {"format": "pcm_s16le", "sample_rate": 24000, "channels": 1,
                "pcm": base64.b64encode(pcm).decode()}


class HybridSession:
    def __init__(self, store, sid, device, backend):
        self.store, self.sid, self.device, self.backend = store, sid, device, backend
        self.lock = threading.RLock()
        self.epoch = 0
        self.token = Cancellation()
        self.closed = False
        self.audio_cache = {}
        self.synthesizing = set()

    def current(self, epoch):
        if self.closed or epoch != self.epoch:
            raise Interrupted("local speech epoch is stale; clear playback")

    def advance(self, epoch):
        if isinstance(epoch, bool) or not isinstance(epoch, int) or not 0 <= epoch <= 2**31:
            raise ValueError("turn_epoch must be a nonnegative integer")
        if epoch < self.epoch or epoch > self.epoch + 1:
            raise ValueError("turn_epoch must be current or next")
        if epoch > self.epoch:
            self.token.cancel()
            self.epoch, self.token = epoch, Cancellation()
            self.audio_cache.clear()

    def quiet(self):
        with self.store.connect() as db:
            floor = db.execute("SELECT state FROM voice_floor WHERE sid=?", (self.sid,)).fetchone()
            session = db.execute("SELECT ended,updated FROM sessions WHERE id=?", (self.sid,)).fetchone()
            latest = db.execute("SELECT id FROM sessions WHERE device=? ORDER BY created DESC,rowid DESC LIMIT 1", (self.device,)).fetchone()
        return (session and not session["ended"] and latest[0] == self.sid
                and time.time() - session["updated"] <= 120
                and (not floor or floor[0] == "quiet"))

    def claim(self, key, kind, value="", **data):
        return self.store.add_events(self.device, [{
            "session_id": self.sid, "type": kind, "text": value,
            "data": {"event_id": "hybrid:" + self.sid + ":" + key, **data},
        }])["accepted"] == 1

    def played(self, ident):
        with self.store.connect() as db:
            return db.execute(
                "SELECT 1 FROM events WHERE session_id=? AND event_key=?",
                (self.sid, f"hybrid:{self.sid}:played:{ident}"),
            ).fetchone() is not None

    def speech_id(self, epoch, ident):
        return f"hybrid:{self.sid}:{epoch}:inbox:{ident}"

    def speech(self, epoch, say, token, pending_turn=None):
        if not say:
            return {"say": "", "speech": "silent"}
        with self.lock:
            self.current(epoch)
            token.check()
            if not self.quiet() or (pending_turn and not self.pending(pending_turn)):
                return {"say": "", "speech": "silent", "suppressed": "floor"}
        audio = self.backend.synthesize(say, token)
        with self.lock:
            self.current(epoch)
            token.check()
            if not self.quiet() or (pending_turn and not self.pending(pending_turn)):
                return {"say": "", "speech": "silent", "suppressed": "floor"}
            return {"say": say, "speech": "audio", "audio": audio,
                    "session_id": self.sid, "turn_epoch": epoch}

    def pending(self, turn):
        with self.store.connect() as db:
            row = db.execute("SELECT status FROM voice_turns WHERE id=?", (turn,)).fetchone()
        return row and row[0] not in {"completed", "cancelled", "cancelling", "failed", "interrupted", "timeout"}

    def event(self, body):
        epoch, kind = body.get("turn_epoch"), body.get("kind")
        if kind not in {"stt.partial", "stt.final", "floor", "barge_in", "cancel", "playback.ack"}:
            raise ValueError("invalid hybrid event kind")
        value = text(body.get("text")) if kind in {"stt.partial", "stt.final"} else ""
        state = body.get("state")
        if kind == "floor" and state not in {"user", "assistant", "quiet"}:
            raise ValueError("invalid floor state")
        seq = body.get("seq")
        if isinstance(seq, bool) or not isinstance(seq, int) or not 0 <= seq <= 2**31:
            raise ValueError("seq must be a nonnegative integer")
        with self.lock:
            if kind == "playback.ack" and epoch != self.epoch:
                raise ValueError("playback acknowledgement cannot advance the epoch")
            self.advance(epoch)
            self.current(epoch)
            token = self.token
            if kind == "playback.ack":
                ident = body.get("inbox_id")
                if (isinstance(ident, bool) or not isinstance(ident, int) or ident < 1
                        or body.get("speech_id") != self.speech_id(epoch, ident)):
                    raise ValueError("playback acknowledgement requires its exact speech ID")
                if ident not in self.audio_cache and not self.played(ident):
                    raise ValueError("no generated audio for playback acknowledgement")
                fresh = self.claim(f"played:{ident}", "tool_result",
                                   source="hybrid_client_playback_ack", inbox_id=ident)
                self.audio_cache.pop(ident, None)
                return {"ok": True, "speech": "silent", "duplicate": not fresh}
            if kind == "stt.partial":
                return {"ok": True, "speech": "silent", "status": "partial"}
            if kind in {"floor", "barge_in", "cancel"}:
                self.claim(f"{epoch}:{kind}:{seq}", kind, state=state)
                return {"ok": True, "speech": "silent", "clear_playback": kind != "floor" or state == "user",
                        "turn_epoch": self.epoch}
            # The canonical admission key survives retries and broker restarts. Partials
            # cannot start work. No model may choose a different L1/L2/L3 binding here.
            result = self.store.voice.admit(self.sid, value, f"hybrid:{epoch}")
            fresh = self.claim(f"{epoch}:final", "user_transcript", value)
            if not fresh:
                return {"ok": True, "handoff": {**result, "say": ""}, "speech": "silent", "duplicate": True}
            # Cached facts/cancellation are already authoritative. Do not add an ack.
            if result.get("status") != "queued" or result.get("say"):
                say = result.get("say", "")
            else:
                say = None
        acknowledgement = say is None
        try:
            if acknowledgement:
                say = self.backend.backchannel(value, token)
            output = self.speech(epoch, say, token, result.get("turn_id") if acknowledgement else None)
            return {"ok": True, "handoff": {**result, "say": ""},
                    "speech_id": f"hybrid:{self.sid}:{epoch}:ack", **output}
        except Interrupted:
            return {"ok": True, "handoff": {**result, "say": ""}, "speech": "silent", "clear_playback": True}
        except RuntimeError as exc:
            # Work may already be admitted. Return that receipt with a visible provider
            # error, never fabricate speech or ask the caller to repeat the work.
            return {"ok": False, "handoff": {**result, "say": ""}, "speech": "silent", "error": str(exc)}

    def inbox_speech(self, body):
        epoch, ident = body.get("turn_epoch"), body.get("inbox_id")
        if isinstance(ident, bool) or not isinstance(ident, int) or ident < 1:
            raise ValueError("inbox_id must be a positive integer")
        with self.lock:
            if isinstance(epoch, bool) or not isinstance(epoch, int):
                raise ValueError("turn_epoch must be an integer")
            self.current(epoch)
            token = self.token
            items = self.store.inbox(self.device, ident - 1)["messages"]
            item = next((item for item in items if item["id"] == ident), None)
            if not item or item["session_id"] != self.sid:
                raise LookupError("inbox item not found")
            with self.store.connect() as db:
                turn = db.execute("SELECT call_key FROM voice_turns WHERE id=?", (item["params"].get("turn_id"),)).fetchone()
            if not turn or turn[0] != f"{self.sid}:hybrid:{epoch}":
                return {"ok": True, "speech": "silent", "suppressed": "stale_epoch"}
            if (item["params"].get("silent") or not item["say"] or not self.quiet()
                    or item["params"].get("event") not in {"result", "clarification"}
                    or not str(item.get("source", "")).startswith("voice:")):
                return {"ok": True, "speech": "silent"}
            if self.played(ident):
                return {"ok": True, "speech": "silent", "duplicate": True}
            if ident in self.audio_cache:
                return {**self.audio_cache[ident], "replayed": True}
            if ident in self.synthesizing:
                return {"ok": True, "speech": "silent", "status": "synthesizing"}
            self.synthesizing.add(ident)
            # A final result supersedes any acknowledgement still synthesizing.
            self.token.cancel()
            self.token = token = Cancellation()
        try:
            output = {"ok": True, "inbox_id": ident, **self.speech(epoch, item["say"], token)}
            with self.lock:
                self.current(epoch)
                token.check()
                if output.get("speech") == "audio":
                    if not self.quiet():
                        return {"ok": True, "speech": "silent", "suppressed": "floor"}
                    output["speech_id"] = self.speech_id(epoch, ident)
                    self.audio_cache[ident] = output
                return output
        except Interrupted:
            return {"ok": True, "speech": "silent", "clear_playback": True}
        except RuntimeError as exc:
            return {"ok": False, "speech": "silent", "error": str(exc)}
        finally:
            with self.lock:
                self.synthesizing.discard(ident)

    def interrupt(self, advance=False):
        with self.lock:
            self.token.cancel()
            if advance:
                self.epoch += 1
                self.audio_cache.clear()
            self.token = Cancellation()

    def close(self):
        with self.lock:
            self.closed = True
            self.token.cancel()
            self.audio_cache.clear()


class HybridCalls:
    def __init__(self, store):
        self.store = store
        self.lock = threading.Lock()
        self.calls = {}

    def prepare(self, plan):
        backend = LocalHTTPBackend(plan)
        backend.probe()
        return backend

    def bind(self, sid, device, backend):
        with self.lock:
            previous = [call for call in self.calls.values() if call.device == device]
            for call in previous:
                del self.calls[call.sid]
            self.calls[sid] = HybridSession(self.store, sid, device, backend)
        for call in previous:
            call.close()

    def request(self, device, body, speech=False):
        if not isinstance(body, dict):
            raise ValueError("expected object")
        sid = body.get("session_id")
        with self.store.connect() as db:
            row = self.store.owned(db, sid, device)
            latest = db.execute("SELECT id FROM sessions WHERE device=? ORDER BY created DESC,rowid DESC LIMIT 1", (device,)).fetchone()
            if row["ended"] or row["transport"] != "hybrid" or latest[0] != sid:
                raise ValueError("hybrid session ended, superseded or not selected")
        with self.lock:
            call = self.calls.get(sid)
        if not call:
            raise RuntimeError("local session unavailable after restart; mint a new session")
        return call.inbox_speech(body) if speech else call.event(body)

    def observe(self, sid, kind, data):
        with self.lock:
            call = self.calls.get(sid)
        if call and (kind in {"barge_in", "cancel"} or data.get("barge_in") is True
                     or data.get("topic_changed") is True
                     or (kind == "floor" and data.get("state") == "user")):
            local_event = str(data.get("event_id", "")).startswith("hybrid:" + sid + ":")
            call.interrupt(advance=not local_event)

    def close(self, sid):
        with self.lock:
            call = self.calls.pop(sid, None)
        if call:
            call.close()
