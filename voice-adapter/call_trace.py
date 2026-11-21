"""Local JSONL and optional caller PCM for a single voice call."""
from collections import deque
import hashlib
import json
import logging
import os
import re
import time
import unicodedata
import wave

import av

LOG = logging.getLogger(__name__)
_TAMIL_NO = "(?:இல்லை|இல்ல|illai|illa)"
_REACTION_MARKERS = {
    "correction": (
        # "No" corrects only turn-initially or before a comma; inside a sentence it is plain negation.
        r"no(?= *,)", "i meant", "wait", "that's not",
        rf"^{_TAMIL_NO}", rf"{_TAMIL_NO}(?= *,)", "நான் சொன்னது", "naan sonnadhu", "naan sonnathu",
        "காத்திரு", "kaathiru", "kathiru",
    ),
    "frustration": (
        "bro", "already told", r"what(?= *\?)",
        "டேய்", "dei", "daei", "ஏற்கனவே சொன்னேன்", "erkanave sonnen", "yerkanave sonnen",
    ),
}
# Python's \w excludes Tamil combining marks, including vowel signs and the pulli.
_REACTION_WORD_CHAR = r"[\w\u0b82\u0bbe-\u0bcd\u0bd7]"
_REACTION_PATTERNS = {
    label: re.compile(rf"(?<!{_REACTION_WORD_CHAR})(?:{'|'.join(markers)})(?!{_REACTION_WORD_CHAR})")
    for label, markers in _REACTION_MARKERS.items()
}
TOKENS = re.compile(r"\b\w+(?:'\w+)*\b")
RATE = 16000


def private_file(path, flags):
    return os.open(path, flags, 0o600)


def context_sent(trace, event, turn_id, leg="primary", did=None):
    """Record the actual wire dialect, without credentials, SDP, or audio."""
    kind = event.get("type")
    fields = {"type": kind, "turn_id": turn_id, "leg": leg}
    if kind in ("session.update", "signaling.session"):
        instructions = event["session"].get("instructions")
        if isinstance(instructions, str):
            encoded = instructions.encode("utf-8")
            trace("context.sent", **fields, instructions_sha256=hashlib.sha256(encoded).hexdigest(),
                  instructions_bytes=len(encoded))
    elif kind in ("session.context.append", "delegation.context.append", "conversation.item.create"):
        item = event.get("item") or event
        texts = [c["text"] for c in item.get("content", [])
                 if isinstance(c, dict) and isinstance(c.get("text"), str)]
        if isinstance(item.get("output"), str):
            texts.append(item["output"])
        if texts:
            trace("context.sent", **fields, texts=texts, channel=event.get("channel"),
                  did=did or event.get("delegation_item_id") or item.get("call_id"))


class Trace:
    def __init__(self, call_id, directory):
        self.t0, self.f = time.monotonic(), None
        self.turn_id = 0  # setup before the first caller turn
        self.turn_open = False
        self.barge_in = False
        self.previous = deque(maxlen=3)
        self.delegations = {}
        self.audio = None
        if os.environ.get("HERMES_MOBILE_VOICE_TRACE", "1") == "0":
            return
        try:
            day = directory / time.strftime("%Y-%m-%d")
            day.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.path = day / f"{time.strftime('%H%M%S')}-{call_id}.jsonl"
            self.f = open(self.path, "a", buffering=1, encoding="utf-8", opener=private_file)
            if os.environ.get("Z0LIVE_TRACE_AUDIO") == "1":
                self.audio = CallerAudio(self.path.with_suffix(".wav"))
        except OSError as exc:
            LOG.warning("voice trace unavailable: %s", type(exc).__name__)

    def __call__(self, ev, **data):
        if self.f:
            try:
                self.f.write(json.dumps({"t": round(time.monotonic() - self.t0, 3), "wall": round(time.time(), 3),
                                         "ev": ev, **data}, ensure_ascii=False, default=str) + "\n")
            except (OSError, ValueError):
                pass

    def begin_turn(self, barge_in=False):
        if not self.turn_open:
            self.turn_id += 1
            self.turn_open = True
            self.barge_in = barge_in

    def finish_turn(self, text):
        self.begin_turn()
        normalized = " ".join(text.casefold().replace("’", "'").split())
        tokens = set(TOKENS.findall(normalized))
        repeat = any(len(tokens & old) / len(tokens | old) >= 0.6 for old in self.previous if tokens or old)
        self.previous.append(tokens)
        reaction_text = unicodedata.normalize("NFC", normalized)
        self("user.reaction", turn_id=self.turn_id, barge_in=self.barge_in, repeat=repeat,
             **{label: bool(pattern.search(reaction_text)) for label, pattern in _REACTION_PATTERNS.items()})
        self.turn_open = self.barge_in = False

    def bind_delegation(self, did):
        self.delegations[did] = self.turn_id
        if len(self.delegations) > 256:
            del self.delegations[next(iter(self.delegations))]

    def record_audio(self, frame):
        if self.audio and not self.audio.write(frame):
            self.audio.close(flush=False)
            self.audio = None

    def close(self):
        if self.audio:
            self.audio.close()
            self.audio = None
        if self.f:
            self.f.close()
            self.f = None


class CallerAudio:
    def __init__(self, path):
        self.path = path
        self.file = self.wav = None
        self.resampler = av.AudioResampler(format="s16", layout="mono", rate=RATE)

    def write(self, frame):
        try:
            self._write(self.resampler.resample(frame))
        except (OSError, ValueError, av.FFmpegError, wave.Error) as exc:
            LOG.warning("caller audio write failed: %s", type(exc).__name__)
            return False
        return True

    def _write(self, frames):
        for frame in frames:
            if self.wav is None:
                self.file = open(self.path, "xb", opener=private_file)
                self.wav = wave.open(self.file, "wb")
                self.wav.setparams((1, 2, RATE, 0, "NONE", "not compressed"))
            self.wav.writeframesraw(bytes(frame.planes[0])[:frame.samples * 2])

    def close(self, *, flush=True):
        try:
            if flush:
                self._write(self.resampler.resample(None))
        except (OSError, ValueError, av.FFmpegError, wave.Error) as exc:
            LOG.warning("caller audio flush failed: %s", type(exc).__name__)
        try:
            if self.wav:
                self.wav.close()
        except (OSError, wave.Error) as exc:
            LOG.warning("caller WAV header write failed: %s", type(exc).__name__)
        finally:
            if self.file:
                try:
                    self.file.close()
                except OSError as exc:
                    LOG.warning("caller WAV close failed: %s", type(exc).__name__)
