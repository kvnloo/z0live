from __future__ import annotations

import asyncio
import base64
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class EventKind(str, Enum):
    SESSION_STARTING = "session.starting"
    SESSION_READY = "session.ready"
    SESSION_STOPPED = "session.stopped"
    USER_SPEECH_STARTED = "user.speech.started"
    USER_SPEECH_STOPPED = "user.speech.stopped"
    INPUT_TRANSCRIPT_DELTA = "user.transcript.delta"
    INPUT_TRANSCRIPT_FINAL = "user.transcript.final"
    ASSISTANT_SPEECH_STARTED = "assistant.speech.started"
    ASSISTANT_SPEECH_STOPPED = "assistant.speech.stopped"
    ASSISTANT_TRANSCRIPT_DELTA = "assistant.transcript.delta"
    ASSISTANT_TRANSCRIPT_FINAL = "assistant.transcript.final"
    INTERRUPT = "conversation.interrupt"
    PLAYBACK_SILENCE = "playback.silence"
    QUIET_CONTEXT = "context.quiet"
    SPEAKABLE_COMMENTARY = "context.commentary"
    HARNESS_PROGRESS = "harness.progress"
    HARNESS_RESULT = "harness.result"
    HARNESS_APPROVAL = "harness.approval"
    HARNESS_ERROR = "harness.error"
    ACTOR_ERROR = "actor.error"
    RESOURCE_SAMPLE = "resource.sample"
    MARKER = "marker"


@dataclass(slots=True)
class TimelineEvent:
    kind: EventKind
    source: str
    at_ms: int = 0
    payload: dict[str, Any] = field(default_factory=dict)
    trace_id: str | None = None
    task_id: str | None = None
    event_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    supersedes: str | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "event_id": self.event_id,
            "kind": self.kind.value,
            "source": self.source,
            "at_ms": int(self.at_ms),
            "payload": self.payload,
        }
        if self.trace_id is not None:
            out["trace_id"] = self.trace_id
        if self.task_id is not None:
            out["task_id"] = self.task_id
        if self.supersedes is not None:
            out["supersedes"] = self.supersedes
        return out

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "TimelineEvent":
        return cls(
            event_id=str(raw.get("event_id") or uuid.uuid4().hex),
            kind=EventKind(str(raw["kind"])),
            source=str(raw.get("source") or "unknown"),
            at_ms=int(raw.get("at_ms") or 0),
            payload=dict(raw.get("payload") or {}),
            trace_id=raw.get("trace_id"),
            task_id=raw.get("task_id"),
            supersedes=raw.get("supersedes"),
        )


@dataclass(slots=True)
class AudioFrame:
    data: bytes
    codec: str
    sample_rate_hz: int
    channels: int = 1
    duration_ms: float | None = None

    def to_dict(self, *, include_data: bool = False) -> dict[str, Any]:
        out = {
            "codec": self.codec,
            "sample_rate_hz": self.sample_rate_hz,
            "channels": self.channels,
            "bytes": len(self.data),
            "duration_ms": self.duration_ms,
        }
        if include_data:
            out["data_base64"] = base64.b64encode(self.data).decode("ascii")
        return out


@dataclass(frozen=True, slots=True)
class ActorCapabilities:
    full_duplex: bool
    native_audio_in: bool
    native_audio_out: bool
    input_codec: str
    output_codec: str
    input_sample_rate_hz: int
    output_sample_rate_hz: int
    input_transcripts: bool = False
    input_partial_transcripts: bool = False
    output_transcripts: bool = False
    output_partial_transcripts: bool = False
    server_vad: bool = False
    native_barge_in: bool = False
    cancel_response: bool = False
    quiet_context: bool = False
    speakable_commentary: bool = False
    tool_delegation: bool = False
    local_transport: bool = False

    def to_dict(self) -> dict[str, Any]:
        out = {name: getattr(self, name) for name in self.__dataclass_fields__}
        out["partial_transcripts"] = (
            self.input_partial_transcripts or self.output_partial_transcripts
        )
        return out


@dataclass(frozen=True, slots=True)
class TranscriberCapabilities:
    input_codec: str
    input_sample_rate_hz: int
    channels: int = 1
    partial_transcripts: bool = True
    end_of_utterance: bool = False
    end_of_backchannel: bool = False
    local_transport: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {name: getattr(self, name) for name in self.__dataclass_fields__}


class InputTranscriber(ABC):
    @property
    @abstractmethod
    def transcriber_id(self) -> str: ...

    @property
    @abstractmethod
    def capabilities(self) -> TranscriberCapabilities: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def send_audio(self, frame: AudioFrame) -> None: ...

    async def user_speech_started(self) -> None:
        return None

    async def user_speech_stopped(self) -> None:
        return None

    async def assistant_speech_changed(self, speaking: bool) -> None:
        del speaking

    @abstractmethod
    async def recv(self) -> TimelineEvent: ...

    async def observe(self, event: TimelineEvent) -> None:
        del event

    @abstractmethod
    async def close(self) -> None: ...


@dataclass(slots=True)
class ActorMessage:
    event: TimelineEvent | None = None
    audio: AudioFrame | None = None

    def __post_init__(self) -> None:
        if (self.event is None) == (self.audio is None):
            raise ValueError("ActorMessage must contain exactly one of event/audio")


@dataclass(slots=True)
class ContextInjection:
    text: str
    speakable: bool = False
    trace_id: str | None = None
    task_id: str | None = None


class ConversationActor(ABC):
    @property
    @abstractmethod
    def actor_id(self) -> str: ...

    @property
    @abstractmethod
    def capabilities(self) -> ActorCapabilities: ...

    @abstractmethod
    async def start(self) -> None: ...

    @abstractmethod
    async def send_audio(self, frame: AudioFrame) -> None: ...

    async def user_speech_started(self, *, backchannel: bool = False) -> None:
        del backchannel

    async def user_speech_stopped(self) -> None:
        return None

    @abstractmethod
    async def interrupt(self) -> None: ...

    @abstractmethod
    async def inject_context(self, injection: ContextInjection) -> None: ...

    @abstractmethod
    async def recv(self) -> ActorMessage: ...

    @abstractmethod
    async def close(self) -> None: ...


class QueueActor(ConversationActor):
    def __init__(self) -> None:
        self._messages: asyncio.Queue[ActorMessage] = asyncio.Queue()
        self._started_at = time.monotonic()

    def _now_ms(self) -> int:
        return int((time.monotonic() - self._started_at) * 1000)

    async def _emit_event(
        self,
        kind: EventKind,
        *,
        source: str | None = None,
        payload: dict[str, Any] | None = None,
        trace_id: str | None = None,
        task_id: str | None = None,
    ) -> None:
        await self._messages.put(
            ActorMessage(
                event=TimelineEvent(
                    kind=kind,
                    source=source or self.actor_id,
                    at_ms=self._now_ms(),
                    payload=payload or {},
                    trace_id=trace_id,
                    task_id=task_id,
                )
            )
        )

    async def _emit_audio(self, frame: AudioFrame) -> None:
        await self._messages.put(ActorMessage(audio=frame))

    async def recv(self) -> ActorMessage:
        return await self._messages.get()


class HarnessCommandKind(str, Enum):
    SUBMIT = "submit"
    STEER = "steer"
    REDIRECT = "redirect"
    CANCEL = "cancel"
    APPROVE = "approve"


@dataclass(slots=True)
class HarnessCommand:
    kind: HarnessCommandKind
    trace_id: str
    task_id: str | None = None
    text: str | None = None
    payload: dict[str, Any] = field(default_factory=dict)
    command_id: str = field(default_factory=lambda: uuid.uuid4().hex)

    def to_dict(self) -> dict[str, Any]:
        return {
            "command_id": self.command_id,
            "kind": self.kind.value,
            "trace_id": self.trace_id,
            "task_id": self.task_id,
            "text": self.text,
            "payload": self.payload,
        }


@dataclass(frozen=True, slots=True)
class HarnessCapabilities:
    submit: bool = True
    steer: bool = False
    redirect: bool = False
    cancel: bool = False
    approvals: bool = False
    progress_events: bool = True
    verified_results: bool = True
    observations: bool = False

    def to_dict(self) -> dict[str, bool]:
        return {name: bool(getattr(self, name)) for name in self.__dataclass_fields__}


class HarnessAdapter(ABC):
    @property
    @abstractmethod
    def capabilities(self) -> HarnessCapabilities: ...

    @abstractmethod
    async def attach(self) -> None: ...

    @abstractmethod
    async def command(self, command: HarnessCommand) -> dict[str, Any]: ...

    @abstractmethod
    async def recv(self) -> TimelineEvent: ...

    @abstractmethod
    async def close(self) -> None: ...
