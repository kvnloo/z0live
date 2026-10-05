from __future__ import annotations

from ..contracts import (
    ActorCapabilities,
    AudioFrame,
    ContextInjection,
    EventKind,
    QueueActor,
)


class FakeActor(QueueActor):
    def __init__(self, actor_id: str = "fake") -> None:
        super().__init__()
        self._actor_id = actor_id
        self.started = False
        self.closed = False
        self.interrupt_count = 0
        self.audio_in: list[AudioFrame] = []
        self.context: list[ContextInjection] = []

    @property
    def actor_id(self) -> str:
        return self._actor_id

    @property
    def capabilities(self) -> ActorCapabilities:
        return ActorCapabilities(
            full_duplex=True,
            native_audio_in=True,
            native_audio_out=True,
            input_codec="pcm16",
            output_codec="pcm16",
            input_sample_rate_hz=24000,
            output_sample_rate_hz=24000,
            input_transcripts=True,
            input_partial_transcripts=True,
            output_transcripts=True,
            output_partial_transcripts=True,
            server_vad=False,
            native_barge_in=False,
            cancel_response=True,
            quiet_context=True,
            speakable_commentary=True,
            tool_delegation=False,
            local_transport=True,
        )

    async def start(self) -> None:
        self.started = True
        await self._emit_event(EventKind.SESSION_READY, payload={"fake": True})

    async def send_audio(self, frame: AudioFrame) -> None:
        self.audio_in.append(frame)

    async def interrupt(self) -> None:
        self.interrupt_count += 1
        await self._emit_event(EventKind.INTERRUPT, payload={"fake": True})

    async def inject_context(self, injection: ContextInjection) -> None:
        self.context.append(injection)
        await self._emit_event(
            EventKind.SPEAKABLE_COMMENTARY if injection.speakable else EventKind.QUIET_CONTEXT,
            payload={"text": injection.text},
            trace_id=injection.trace_id,
            task_id=injection.task_id,
        )

    async def close(self) -> None:
        self.closed = True
        await self._emit_event(EventKind.SESSION_STOPPED, payload={"fake": True})

    async def emit_event(self, kind: EventKind, **payload) -> None:
        await self._emit_event(kind, payload=payload)

    async def emit_audio(self, data: bytes = b"audio") -> None:
        await self._emit_audio(AudioFrame(data=data, codec="pcm16", sample_rate_hz=24000))
