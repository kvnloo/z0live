"""Grok Voice as the call's voice model (z0live.lanes.v1 `lanes.voice.actor = grok_voice`).

The Call speaks one event dialect to its voice model, the ChatGPT-subscription one: it receives
input/output_transcript.added, turn.done, delegation.created, session.started and sends
session.context.append / delegation.context.append. GrokLeg converts Grok's Realtime API (OpenAI-shaped,
wss://api.x.ai/v1/realtime) to and from that dialect, so lanes, the call brief, host delegation and the
handoff run unchanged. Hermes work goes through one `ask_hermes` tool, the way GPT-Live delegates.

Audio: phone frames (48 kHz) -> 24 kHz mono PCM16 -> input_audio_buffer.append; Grok's
response.output_audio.delta -> 48 kHz 20 ms frames on GrokOutput. Barge-in (speech_started) drops
queued output. Measured 11-19: speech end -> first audio ~0.5 s; tool call + spoken result verified.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import subprocess
import time
from fractions import Fraction
from pathlib import Path

import aiohttp
import av
from aiortc import MediaStreamTrack

LOG = logging.getLogger("hermes.mobile.grok")
URL = os.environ.get("GROK_VOICE_URL", "wss://api.x.ai/v1/realtime?model=grok-voice-latest")
VOICE = os.environ.get("GROK_VOICE", "eve")
RATE_IN, RATE_OUT, FRAME = 24000, 48000, 960  # Grok PCM rate; phone rate; 20 ms at 48 kHz
SECRET_SCRIPT = Path(__file__).with_name("xai_secret.py")
HERMES_BIN = os.environ.get("HERMES_BIN", str(Path.home() / ".hermes/hermes-agent/venv/bin/hermes"))
TOOL = {"type": "function", "name": "ask_hermes",
        "description": "Hand real work to Hermes, Kevin's agent: anything about his data, files, sessions, phone "
                       "or screen, or anything you can't answer from the conversation. Say a few words first.",
        "parameters": {"type": "object", "properties": {"request": {"type": "string"}}, "required": ["request"]}}


def mint_secret() -> str:
    """A short-lived client secret from Hermes's xAI login (xai_secret.py under the Hermes runtime)."""
    runtime = json.loads(subprocess.check_output([HERMES_BIN, "--print-runtime-command"], text=True, timeout=20))
    out = subprocess.run([runtime[0], "-I", str(SECRET_SCRIPT)], capture_output=True, text=True, timeout=30)
    if out.returncode != 0:
        raise RuntimeError("Grok Voice login unavailable: " + (out.stderr.strip().splitlines() or ["?"])[-1][:200])
    return json.loads(out.stdout)["value"]


class GrokOutput(MediaStreamTrack):
    """Grok's speech to the phone: 48 kHz mono s16 frames, silence when nothing is queued."""
    kind = "audio"

    def __init__(self):
        super().__init__()
        self.buf = bytearray()
        self.pts = 0
        self.started = None
        self.resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE_OUT)

    def feed(self, pcm24: bytes):
        frame = av.AudioFrame(format="s16", layout="mono", samples=len(pcm24) // 2)
        frame.planes[0].update(pcm24[: frame.samples * 2])
        frame.sample_rate = RATE_IN
        for out in self.resampler.resample(frame):
            self.buf += bytes(out.planes[0])[: out.samples * 2]

    def clear(self):
        self.buf.clear()

    async def recv(self):
        if self.started is None:
            self.started = time.monotonic()
        wait = self.started + self.pts / RATE_OUT - time.monotonic()
        if wait > 0:
            await asyncio.sleep(wait)
        chunk = bytes(self.buf[: FRAME * 2]).ljust(FRAME * 2, b"\0")
        del self.buf[: FRAME * 2]
        frame = av.AudioFrame(format="s16", layout="mono", samples=FRAME)
        frame.planes[0].update(chunk)
        frame.sample_rate, frame.pts, frame.time_base = RATE_OUT, self.pts, Fraction(1, RATE_OUT)
        self.pts += FRAME
        return frame


class GrokLeg:
    def __init__(self, call, persona: str):
        self.call, self.persona = call, persona
        self.ws = None
        self.output = GrokOutput()
        self.tasks: list[asyncio.Task] = []
        self.answered: set[str] = set()  # call_ids that already got their function_call_output
        self.pending: dict[str, list[str]] = {}  # delegation id -> speakable chunks of one answer
        self.ready = False

    def emit(self, event: dict):
        """Hand a subscription-dialect event to the Call as if the provider sent it."""
        self.call.message(json.dumps(event), True)

    async def start(self):
        secret = await asyncio.to_thread(mint_secret)
        self.ws = await self.call.manager.http.ws_connect(URL, headers={"Authorization": f"Bearer {secret}"},
                                                          max_msg_size=4 * 1024 * 1024, heartbeat=20)
        await self.raw({"type": "session.update", "session": {
            "voice": VOICE, "instructions": self.persona, "turn_detection": {"type": "server_vad"}, "tools": [TOOL],
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": RATE_IN}},
                      "output": {"format": {"type": "audio/pcm", "rate": RATE_IN}}}}})
        self.tasks.append(asyncio.create_task(self.reader()))

    def attach(self, track):
        """The phone's microphone track: resample and stream to Grok."""
        self.tasks.append(asyncio.create_task(self.pump(track)))

    async def raw(self, event: dict):
        if self.ws is not None and not self.ws.closed:
            await self.ws.send_str(json.dumps(event))

    async def pump(self, track):
        resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE_IN)
        buf = bytearray()
        try:
            while True:
                frame = await track.recv()
                if self.call.muted:
                    continue
                for out in resampler.resample(frame):
                    buf += bytes(out.planes[0])[: out.samples * 2]
                if len(buf) >= RATE_IN // 10 * 2 and self.ready:  # ~100 ms per append
                    await self.raw({"type": "input_audio_buffer.append", "audio": base64.b64encode(bytes(buf)).decode()})
                    buf.clear()
        except Exception as exc:  # noqa: BLE001 - track ended or socket closed; the call closes separately
            LOG.info("grok input ended: %s", type(exc).__name__)

    async def reader(self):
        try:
            async for msg in self.ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    self.on_event(json.loads(msg.data))
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR):
                    break
        except Exception as exc:  # noqa: BLE001
            LOG.warning("grok socket ended: %s", type(exc).__name__)
        if not self.call.closed:
            self.emit({"type": "session.closed"})

    def on_event(self, ev: dict):
        ty = ev.get("type", "")
        if ty == "session.updated" and not self.ready:
            self.ready = True
            self.emit({"type": "session.started"})
        elif ty == "input_audio_buffer.speech_started":
            self.output.clear()  # barge-in: stop speaking over Kevin
        elif ty == "conversation.item.input_audio_transcription.completed":
            text = ev.get("transcript") or ""
            if text:
                self.emit({"type": "input_transcript.added", "item": {"text": text}})
                self.emit({"type": "turn.done", "turn": {"role": "user", "transcript": text}})
        elif ty == "response.output_audio.delta":
            self.output.feed(base64.b64decode(ev.get("delta") or ""))
        elif ty == "response.output_audio_transcript.delta":
            if ev.get("delta"):
                self.emit({"type": "output_transcript.added", "item": {"text": ev["delta"]}})
        elif ty == "response.output_audio_transcript.done":
            self.emit({"type": "turn.done", "turn": {"role": "assistant", "transcript": ev.get("transcript") or ""}})
        elif ty == "response.function_call_arguments.done" and ev.get("name") == "ask_hermes":
            try:
                request = json.loads(ev.get("arguments") or "{}").get("request", "")
            except ValueError:
                request = ev.get("arguments") or ""
            self.emit({"type": "delegation.created", "item": {"id": ev.get("call_id"), "target": "client",
                                                              "content": [{"type": "text", "text": request}]}})
        elif ty == "error":
            err = ev.get("error") if isinstance(ev.get("error"), dict) else {"message": str(ev.get("message"))}
            self.emit({"type": "error", "error": {"message": str(err.get("message", "Grok Voice error"))[:500]}})

    def outbound(self, event: dict):
        """Subscription-dialect events from the Call, as Grok conversation items."""
        ty = event.get("type")
        text = "".join(c.get("text", "") for c in event.get("content") or [] if isinstance(c, dict))
        if ty == "session.close":
            self.tasks.append(asyncio.create_task(self.close()))
        elif ty == "session.context.append" and text:
            self.tasks.append(asyncio.create_task(self.note(text)))
        elif ty == "delegation.context.append" and text:
            did = event.get("delegation_item_id") or ""
            if event.get("channel") == "speakable":
                if did not in self.pending:  # chunks of one answer arrive in the same tick
                    self.pending[did] = []
                    asyncio.get_running_loop().call_soon(lambda: self.tasks.append(asyncio.create_task(self.answer(did))))
                self.pending[did].append(text)
            else:
                self.tasks.append(asyncio.create_task(self.note(text)))

    async def note(self, text: str):
        await self.raw({"type": "conversation.item.create", "item": {
            "type": "message", "role": "system", "content": [{"type": "input_text", "text": text}]}})

    async def answer(self, did: str):
        text = "".join(self.pending.pop(did, [])).replace('"Agent Final Message":\n\n', "", 1)
        if did and did not in self.answered:
            self.answered.add(did)
            await self.raw({"type": "conversation.item.create", "item": {
                "type": "function_call_output", "call_id": did, "output": text}})
        else:  # a second answer for the same request (lane first, Hermes later): speak it as an update
            await self.note("Update from Hermes on Kevin's earlier request; say it briefly: " + text)
        await self.raw({"type": "response.create"})

    async def close(self):
        for task in self.tasks:
            if task is not asyncio.current_task():
                task.cancel()
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
