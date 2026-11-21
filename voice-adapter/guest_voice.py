"""Per-call guest configuration, the Grok guest leg and the shared phone output switch."""
import asyncio
import base64
import json
import os
import re
import time
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import av

import grok_leg

FILE = Path(os.environ.get("Z0LIVE_VOICES_FILE", str(Path.home() / ".config/z0live/voices.json")))
NAME = re.compile(r"\b(grok|groc|grog|grock)\b", re.I)  # how STT hears the name
PERSONA = """You are Grok, the second voice on Kevin's live call. The first voice is Hermes (GPT-Live), his
assistant, which does his real work. Kevin brings you in by name. Answer only what he asked you, in one to three
short spoken sentences, as yourself: direct, a little playful, never long. Do not claim to do tasks or use tools;
if he wants something done, say Hermes can do it. Context notes about the call arrive as system messages."""
PRIMARY_NOTE = ("A second voice, Grok, is on this call. When Kevin talks to Grok by name, say nothing and let Grok "
                "answer. Notes starting 'Grok said:' tell you what Grok told him.")
HOLD_S = 1.2  # floor stays with Grok this long after its audio ends, so the primary's tail does not leak in


def shaped(mono: bytes, like):
    """48 kHz mono s16 PCM as a frame in exactly the primary frame's layout and clock (the encoder expects one shape)."""
    channels = len(like.layout.channels)
    pcm = mono
    if channels == 2:
        import numpy as np
        pcm = np.repeat(np.frombuffer(mono, dtype=np.int16), 2).tobytes()
    frame = av.AudioFrame(format="s16", layout=like.layout.name, samples=like.samples)
    frame.planes[0].update(pcm)
    frame.sample_rate, frame.pts, frame.time_base = like.sample_rate, like.pts, like.time_base or Fraction(1, like.sample_rate)
    return frame


@dataclass(frozen=True)
class GuestConfig:
    engine: str
    name: str
    voice: str
    profile: str = ""

    @property
    def primary_note(self):
        return (f"A second voice, {self.name}, is on this call. When Kevin addresses {self.name} by name"
                f"{' (including tek or teknium)' if self.profile == 'tek' else ''}, say nothing and do not "
                f"delegate; let the guest answer. Notes beginning '{self.name} said' are silent context.")


def configured() -> GuestConfig | None:
    forced = os.environ.get("Z0LIVE_GUEST", "").strip().lower()
    try:
        path = Path(os.environ.get("Z0LIVE_VOICES_FILE", str(FILE)))
        raw = json.loads(path.read_text()).get("guest")
    except (OSError, ValueError, AttributeError):
        raw = None
    if forced == "grok" or (not forced and raw == "grok"):
        return GuestConfig("grok", "Grok", os.environ.get("Z0LIVE_GUEST_VOICE", "rex"))
    if forced not in ("", "gpt-live") or not isinstance(raw, dict) or raw.get("engine") != "gpt-live":
        return None
    profile, name, voice = raw.get("profile"), raw.get("name"), raw.get("voice", "ember")
    if not isinstance(profile, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", profile) or profile == "default":
        return None
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9_-]{1,32}", name):
        return None
    if voice not in ("arbor", "breeze", "cove", "ember", "juniper", "maple", "spruce", "vale"):
        return None
    return GuestConfig("gpt-live", name, voice, profile)


def wanted() -> str:
    config = configured()
    return config.engine if config else ""


class GuestVoice(grok_leg.GrokLeg):
    trace_leg = "guest"

    def __init__(self, call):
        super().__init__(call, PERSONA)
        self.addressed = False   # Kevin named Grok this turn: its reply may play
        self.claimed_at = 0.0    # first moment the primary must go quiet
        self.until = 0.0         # floor held until (monotonic)
        self.heard = ""          # this user turn's transcript, as the primary hears it
        self.seen = 0            # chars of the running user transcript that belong to earlier turns
        self.running = ""        # the primary's running user transcript (it may span turns it stayed silent on)
        self.muted = False       # the primary started a reply on Grok's floor: keep it off the phone to its end
        self.notes = []          # "Grok said" notes, sent to the primary once its muted reply is over
        self.asked = False       # response.create sent for this turn
        self.played = 0          # bytes of Grok speech sent to the phone, this reply

    async def start(self):
        secret = await asyncio.to_thread(grok_leg.mint_secret)
        self.ws = await self.call.manager.http.ws_connect(grok_leg.URL, headers={"Authorization": f"Bearer {secret}"},
                                                          max_msg_size=4 * 1024 * 1024, heartbeat=20)
        await self.raw({"type": "session.update", "session": {
            "voice": os.environ.get("Z0LIVE_GUEST_VOICE", "rex"), "instructions": PERSONA,
            "turn_detection": {"type": "server_vad", "create_response": False, "interrupt_response": True},
            "audio": {"input": {"format": {"type": "audio/pcm", "rate": grok_leg.RATE_IN}},
                      "output": {"format": {"type": "audio/pcm", "rate": grok_leg.RATE_IN}}}}})
        self.tasks.append(asyncio.create_task(self.reader()))

    # --- what the primary hears (called from Call.message for provider events) -------------------------------
    def observe(self, event: dict, user_text: str, turn_id=0):
        kind = event.get("type")
        if kind == "input_transcript.added":
            if len(user_text) < self.seen:  # the protocol started a fresh transcript
                self.seen = 0
            self.running = user_text
            self.heard = user_text[self.seen:]
            if NAME.search(self.heard) and not self.addressed:
                self.claim()
        elif kind == "output_transcript.added" and self.floor():
            self.muted = True  # the primary answers the turn that was Grok's (it has no cancel): mute it whole
        elif kind == "turn.done":
            turn = event.get("turn") or {}
            if turn.get("role") == "user":
                self.heard, self.seen = "", len(user_text)
                self.ask()  # Kevin finished the turn that named Grok
            elif turn.get("role") == "assistant":
                was_muted, self.muted = self.muted, False
                if was_muted:
                    self.call.trace("guest.muted_primary", text=(turn.get("transcript") or "")[:200])
                elif (turn.get("transcript") or "").strip():
                    text = "Hermes said: " + turn["transcript"].strip()[:1500]
                    self.tasks.append(asyncio.create_task(self.note(text, turn_id)))
                self.flush_notes()

    def claim(self):
        """Kevin named Grok: the primary goes quiet now, Grok answers once his turn ends."""
        self.addressed, self.asked, self.played, self.claimed_at = True, False, 0, time.monotonic()
        self.until = time.monotonic() + 30  # released when Grok's audio ends (or on barge-in)
        self.call.trace("guest.claimed", heard=self.heard[:300])

    def ask(self):
        """End of the addressed turn (whichever side notices first): Grok replies once."""
        if self.addressed and not self.asked:
            self.asked = True
            self.seen, self.heard = len(self.running), ""  # words after this belong to the next turn
            self.call.trace("guest.asked", after_claim_s=round(time.monotonic() - self.claimed_at, 2))
            self.tasks.append(asyncio.create_task(self.raw({"type": "response.create"})))

    def floor(self) -> bool:
        """Grok holds the floor (or the primary's reply to Grok's turn is still playing): the phone hears Grok."""
        return self.muted or time.monotonic() < self.until

    def flush_notes(self):
        if self.muted or self.asked:
            return
        for text in self.notes:
            self.call.send({"type": "session.context.append", "channel": "developer",
                            "content": [{"type": "input_text", "text": text}]}, True)
        self.notes.clear()

    # --- Grok events ----------------------------------------------------------------------------------------
    def on_event(self, ev: dict):
        ty = ev.get("type", "")
        if ty == "session.updated" and not self.ready:
            self.ready = True
            self.call.trace("guest.ready", voice="grok")
        elif ty == "input_audio_buffer.speech_started" and self.output.buf:
            self.output.clear()  # Kevin barged in on Grok
            self.until = time.monotonic() + 0.2
        elif ty == "input_audio_buffer.committed":
            self.ask()  # Grok's VAD saw the turn end
        elif ty == "conversation.item.input_audio_transcription.completed":
            text = ev.get("transcript") or ""
            if NAME.search(text) and not self.addressed:
                self.heard = text
                self.claim()  # the primary's transcript missed the name; Grok's own caught it
                self.ask()  # this transcript only exists after the turn ended
        elif ty == "response.created" and not self.asked:
            self.tasks.append(asyncio.create_task(self.raw({"type": "response.cancel"})))
        elif ty == "response.output_audio.delta" and self.addressed:
            self.output.feed(base64.b64decode(ev.get("delta") or ""))
        elif ty == "response.output_audio_transcript.done" and self.addressed:
            said = (ev.get("transcript") or "").strip()
            self.call.trace("guest.said", text=said[:500], after_claim_s=round(time.monotonic() - self.claimed_at, 2))
            if said:
                self.notes.append("Grok said (to Kevin, already spoken; do not repeat it): " + said)
        elif ty == "response.done" and self.asked:
            self.addressed = self.asked = False
            self.flush_notes()
            self.tasks.append(asyncio.create_task(self.release()))
        elif ty == "error":
            self.call.trace("guest.error", error=str(ev.get("error") or ev)[:300])

    async def release(self):
        while self.output.buf and not self.call.closed:  # let the queued speech play out
            await asyncio.sleep(0.05)
        self.until = time.monotonic() + HOLD_S
        self.call.trace("guest.played", audio_s=round(self.played / 2 / 48000, 2))

    def take(self, like):
        """One frame of Grok's speech shaped like the primary's frame (same pts and clock), or None."""
        n = like.samples
        if not self.output.buf:
            return None
        chunk = bytes(self.output.buf[: n * 2]).ljust(n * 2, b"\0")
        del self.output.buf[: n * 2]
        self.played += n * 2
        return shaped(chunk, like)


class FloorSwitch(grok_leg.MediaStreamTrack):
    """The phone's one output track: the primary's audio, or the guest's while the guest holds the floor."""
    kind = "audio"

    def __init__(self, source, guest):
        super().__init__()
        self.source, self.guest = source, guest
        self.last_guest = False

    async def recv(self):
        frame = await self.source.recv()  # the primary's clock paces the phone
        selected = self.guest.floor()
        if selected != self.last_guest:
            self.last_guest = selected
            self.guest.call.trace("guest.floor", speaker="guest" if selected else "primary")
        if not selected:
            return frame
        out = self.guest.take(frame)
        if out is not None:
            return out
        return shaped(bytes(frame.samples * 2), frame)  # the primary stays quiet while the floor is the guest's
