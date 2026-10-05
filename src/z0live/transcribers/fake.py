from __future__ import annotations

import asyncio

from ..contracts import (
    AudioFrame,
    EventKind,
    InputTranscriber,
    TimelineEvent,
    TranscriberCapabilities,
)


class FakeTranscriber(InputTranscriber):
    def __init__(self, transcriber_id: str = "fake-transcriber") -> None:
        self._id = transcriber_id
        self.audio_in: list[AudioFrame] = []
        self._events: asyncio.Queue[TimelineEvent] = asyncio.Queue()
        self.started = False
        self.closed = False
        self.assistant_speaking = False

    @property
    def transcriber_id(self) -> str:
        return self._id

    @property
    def capabilities(self) -> TranscriberCapabilities:
        return TranscriberCapabilities(
            input_codec="pcm16",
            input_sample_rate_hz=16000,
            partial_transcripts=True,
            end_of_utterance=True,
            end_of_backchannel=True,
        )

    async def start(self) -> None:
        self.started = True
        await self._events.put(
            TimelineEvent(
                EventKind.TRANSCRIBER_READY,
                source=self.transcriber_id,
                payload={"fake": True},
            )
        )

    async def send_audio(self, frame: AudioFrame) -> None:
        self.audio_in.append(frame)

    async def assistant_speech_changed(self, speaking: bool) -> None:
        self.assistant_speaking = speaking

    async def recv(self) -> TimelineEvent:
        return await self._events.get()

    async def close(self) -> None:
        self.closed = True

    async def emit_partial(self, text: str, *, trace_id: str | None = None) -> None:
        await self._events.put(
            TimelineEvent(
                EventKind.INPUT_TRANSCRIPT_DELTA,
                source=self.transcriber_id,
                payload={"text": text, "cumulative": True},
                trace_id=trace_id,
            )
        )

    async def emit_final(
        self,
        text: str,
        *,
        trace_id: str | None = None,
        backchannel: bool = False,
    ) -> None:
        await self._events.put(
            TimelineEvent(
                EventKind.INPUT_TRANSCRIPT_FINAL,
                source=self.transcriber_id,
                payload={
                    "text": text,
                    "backchannel": backchannel,
                    "authority": not backchannel,
                },
                trace_id=trace_id,
            )
        )
