"""The on_device (`chained`) voice engine slice: STT -> model -> TTS with pluggable async stages.

z0live.lanes.v1 `lanes.voice.actor = on_device` maps to engine "chained" (mobile_live.ENGINES), but Call still
falls back to gpt-live; this module is the contract that wiring will use, built and benched first.

Stages (all injectable, so local kernels like MLX/Lure or fakes plug in without touching the engine):
  stt(pcm: bytes) -> str                         awaited once per turn, at end of speech (EOS)
  model(text: str) -> async iterator of str      streamed tokens
  tts(clause: str) -> async iterator of bytes    PCM chunks for one clause

One turn: EOS -> stt -> model tokens grouped into clauses -> tts per clause -> on_audio(pcm) per chunk.
Model and TTS overlap (a clause is spoken while the model keeps writing). The engine speaks the Call's
event dialect (input_transcript.added, output_transcript.added, turn.done) through `emit`, and traces
chained.eos / stt_done / first_token / first_audio / turn_done (plus chained.empty / error / interrupted).
Times are ns from EOS on an injectable monotonic clock.
"""
from __future__ import annotations

import asyncio
import inspect
import re
import time
from dataclasses import dataclass
from typing import AsyncIterator, Awaitable, Callable

STT = Callable[[bytes], Awaitable[str]]
Model = Callable[[str], AsyncIterator[str]]
TTS = Callable[[str], AsyncIterator[bytes]]

CLAUSE_END = re.compile(r"(?<=[.!?;:])\s+")


@dataclass
class TurnResult:
    turn_id: int = 0
    ok: bool = True
    interrupted: bool = False
    error_stage: str = ""
    error: str = ""
    text: str = ""
    reply: str = ""
    chunks: int = 0
    bytes: int = 0
    stt_ns: int | None = None
    first_token_ns: int | None = None
    first_audio_ns: int | None = None
    done_ns: int | None = None


class StageError(Exception):
    def __init__(self, stage: str, error: BaseException):
        super().__init__(f"{stage}: {error!r}")
        self.stage, self.error = stage, error


def _noop(*_args, **_kw):
    return None


class ChainedEngine:
    def __init__(self, stt: STT, model: Model, tts: TTS, *, trace=None, emit=None, on_audio=None,
                 clock: Callable[[], int] = time.perf_counter_ns):
        self.stt, self.model, self.tts = stt, model, tts
        self.trace = trace or _noop
        self.emit = emit or _noop
        self.on_audio = on_audio or _noop
        self.clock = clock
        self._task: asyncio.Task | None = None
        self._interrupted = False

    def interrupt(self) -> bool:
        """Barge-in: stop the running turn (no more audio). True if a turn was running."""
        if self._task and not self._task.done():
            self._interrupted = True
            self._task.cancel()
            return True
        return False

    async def turn(self, pcm: bytes, turn_id: int = 0) -> TurnResult:
        result = TurnResult(turn_id=turn_id)
        t0 = self.clock()
        self.trace("chained.eos", turn_id=turn_id, pcm_bytes=len(pcm))
        self._interrupted = False
        self._task = asyncio.ensure_future(self._run(pcm, result, t0))
        status = "ok"
        try:
            await self._task
        except asyncio.CancelledError:
            if not self._interrupted:
                raise
            result.ok, result.interrupted, status = False, True, "interrupted"
            self.trace("chained.interrupted", turn_id=turn_id, ms=self._ms(t0))
        except StageError as exc:
            result.ok, result.error_stage, result.error, status = False, exc.stage, repr(exc.error), "error"
            self.trace("chained.error", turn_id=turn_id, stage=exc.stage, error=repr(exc.error), ms=self._ms(t0))
        finally:
            self._task = None
        result.done_ns = self.clock() - t0
        self.trace("chained.turn_done", turn_id=turn_id, status=status, ms=result.done_ns / 1e6,
                   chunks=result.chunks, bytes=result.bytes)
        self.emit({"type": "turn.done", "turn_id": turn_id, "status": status})
        return result

    def _ms(self, t0: int) -> float:
        return (self.clock() - t0) / 1e6

    async def _run(self, pcm: bytes, result: TurnResult, t0: int):
        turn_id = result.turn_id
        try:
            text = (await self.stt(pcm) or "").strip()
        except Exception as exc:
            raise StageError("stt", exc) from exc
        result.stt_ns = self.clock() - t0
        result.text = text
        self.trace("chained.stt_done", turn_id=turn_id, ms=result.stt_ns / 1e6, chars=len(text))
        if not text:
            self.trace("chained.empty", turn_id=turn_id)
            return
        self.emit({"type": "input_transcript.added", "turn_id": turn_id, "text": text})

        clauses: asyncio.Queue = asyncio.Queue()
        speaker = asyncio.ensure_future(self._speak(clauses, result, t0))
        try:
            await self._think(text, clauses, result, t0, speaker)
            clauses.put_nowait(None)
            await speaker
        finally:
            if not speaker.done():
                speaker.cancel()
                await asyncio.gather(speaker, return_exceptions=True)

    async def _think(self, text, clauses, result, t0, speaker):
        buf, reply = "", []
        try:
            async for token in self.model(text):
                if result.first_token_ns is None:
                    result.first_token_ns = self.clock() - t0
                    self.trace("chained.first_token", turn_id=result.turn_id, ms=result.first_token_ns / 1e6)
                if speaker.done():  # TTS failed: surface its error instead of writing more
                    break
                buf += token
                *done, buf = CLAUSE_END.split(buf)
                for clause in done:
                    if clause.strip():
                        reply.append(clause.strip())
                        self._say(clause.strip(), clauses, result)
        except Exception as exc:
            raise StageError("model", exc) from exc
        if buf.strip() and not speaker.done():
            reply.append(buf.strip())
            self._say(buf.strip(), clauses, result)
        result.reply = " ".join(reply)

    def _say(self, clause, clauses, result):
        self.emit({"type": "output_transcript.added", "turn_id": result.turn_id, "text": clause})
        clauses.put_nowait(clause)

    async def _speak(self, clauses, result, t0):
        while (clause := await clauses.get()) is not None:
            try:
                async for pcm in self.tts(clause):
                    if not pcm:
                        continue
                    if result.first_audio_ns is None:
                        result.first_audio_ns = self.clock() - t0
                        self.trace("chained.first_audio", turn_id=result.turn_id, ms=result.first_audio_ns / 1e6)
                    result.chunks += 1
                    result.bytes += len(pcm)
                    out = self.on_audio(pcm)
                    if inspect.isawaitable(out):
                        await out
            except Exception as exc:
                raise StageError("tts", exc) from exc
