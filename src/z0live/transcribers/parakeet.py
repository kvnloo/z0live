from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Any, Callable

from ..contracts import (
    AudioFrame,
    EventKind,
    InputTranscriber,
    TimelineEvent,
    TranscriberCapabilities,
)


@dataclass(frozen=True, slots=True)
class _Control:
    kind: str


class ParakeetEOUTranscriber(InputTranscriber):
    """Optional NVIDIA Parakeet realtime EOU sidecar.

    The dependency is imported lazily so the base z0live runtime remains small.
    Audio inference happens on a worker task and never blocks the actor audio loop.
    """

    def __init__(
        self,
        *,
        model: str = "nvidia/parakeet_realtime_eou_120m-v1",
        device: str = "cuda",
        sample_rate_hz: int = 16000,
        chunk_ms: int = 80,
        max_queue_chunks: int = 64,
        service: Any | None = None,
        service_factory: Callable[..., Any] | None = None,
    ) -> None:
        self.model_name = model
        self.device = device
        self.sample_rate_hz = int(sample_rate_hz)
        self.chunk_ms = int(chunk_ms)
        self.chunk_bytes = max(
            2,
            int(self.sample_rate_hz * (self.chunk_ms / 1000.0) * 2),
        )
        self._queue: asyncio.Queue[bytes | _Control] = asyncio.Queue(
            maxsize=max(4, int(max_queue_chunks))
        )
        self._events: asyncio.Queue[TimelineEvent] = asyncio.Queue()
        self._service = service
        self._service_factory = service_factory
        self._worker: asyncio.Task | None = None
        self._byte_buffer = bytearray()
        self._utterance = ""
        self._assistant_speaking = False
        self._started_at = time.monotonic()
        self.dropped_chunks = 0

    @property
    def transcriber_id(self) -> str:
        return "parakeet-realtime-eou-120m"

    @property
    def capabilities(self) -> TranscriberCapabilities:
        return TranscriberCapabilities(
            input_codec="pcm16",
            input_sample_rate_hz=self.sample_rate_hz,
            channels=1,
            partial_transcripts=True,
            end_of_utterance=True,
            end_of_backchannel=True,
            local_transport=True,
        )

    def _now_ms(self) -> int:
        return int((time.monotonic() - self._started_at) * 1000)

    async def _emit(
        self,
        kind: EventKind,
        payload: dict[str, Any],
    ) -> None:
        await self._events.put(
            TimelineEvent(
                kind=kind,
                source=self.transcriber_id,
                at_ms=self._now_ms(),
                payload=payload,
            )
        )

    async def start(self) -> None:
        if self._service is None:
            factory = self._service_factory
            if factory is None:
                try:
                    from nemo_voice_agent.pipecat.services.nemo.streaming_asr import (
                        NemoStreamingASRService,
                    )
                except ImportError as exc:
                    raise RuntimeError(
                        "Parakeet transcriber requires NVIDIA NeMo Voice Agent on Python 3.12+"
                    ) from exc
                factory = NemoStreamingASRService
            self._service = await asyncio.to_thread(
                factory,
                self.model_name,
                device=self.device,
                sample_rate=self.sample_rate_hz,
                chunk_size_in_secs=self.chunk_ms / 1000.0,
            )
        self._worker = asyncio.create_task(
            self._run(),
            name="z0live-parakeet-transcriber",
        )
        await self._emit(
            EventKind.TRANSCRIBER_READY,
            {
                "model": self.model_name,
                "device": self.device,
                "chunk_ms": self.chunk_ms,
            },
        )

    async def send_audio(self, frame: AudioFrame) -> None:
        if frame.codec.lower() != "pcm16":
            raise ValueError(
                f"Parakeet sidecar requires PCM16 input, got {frame.codec!r}"
            )
        if frame.sample_rate_hz != self.sample_rate_hz or frame.channels != 1:
            raise ValueError(
                "Parakeet sidecar requires mono "
                f"{self.sample_rate_hz} Hz PCM16 audio"
            )
        try:
            self._queue.put_nowait(bytes(frame.data))
        except asyncio.QueueFull:
            self.dropped_chunks += 1
            if self.dropped_chunks in (1, 10, 100):
                await self._emit(
                    EventKind.TRANSCRIBER_ERROR,
                    {
                        "error": "audio_queue_overflow",
                        "dropped_chunks": self.dropped_chunks,
                    },
                )

    async def user_speech_stopped(self) -> None:
        try:
            self._queue.put_nowait(_Control("vad_stop"))
        except asyncio.QueueFull:
            await self._queue.put(_Control("vad_stop"))

    async def assistant_speech_changed(self, speaking: bool) -> None:
        self._assistant_speaking = bool(speaking)

    @staticmethod
    def _clean(text: str) -> str:
        return " ".join(
            text.replace("<EOU>", " ")
            .replace("<EOB>", " ")
            .split()
        ).strip()

    def _append(self, text: str) -> str:
        clean = self._clean(text)
        if clean:
            self._utterance = " ".join(
                part for part in (self._utterance, clean) if part
            ).strip()
        return self._utterance

    async def _handle_result(self, result: Any) -> None:
        raw = str(getattr(result, "text", "") or "")
        if not raw.strip():
            return
        has_eou = "<EOU>" in raw
        has_eob = "<EOB>" in raw
        text = self._append(raw)

        metadata = {
            "processing_time_ms": (
                None
                if getattr(result, "processing_time", None) is None
                else float(result.processing_time) * 1000.0
            ),
            "eou_probability": getattr(result, "eou_prob", None),
            "eob_probability": getattr(result, "eob_prob", None),
            "cumulative": True,
        }

        if has_eou:
            await self._emit(
                EventKind.INPUT_TRANSCRIPT_FINAL,
                {
                    **metadata,
                    "text": text,
                    "eou": True,
                    "backchannel": False,
                    "authority": True,
                },
            )
            self._utterance = ""
            return

        if has_eob and self._assistant_speaking:
            await self._emit(
                EventKind.INPUT_TRANSCRIPT_FINAL,
                {
                    **metadata,
                    "text": text,
                    "eob": True,
                    "backchannel": True,
                    "authority": False,
                },
            )
            self._utterance = ""
            return

        if text:
            await self._emit(
                EventKind.INPUT_TRANSCRIPT_DELTA,
                {
                    **metadata,
                    "text": text,
                    "eob_ignored": bool(has_eob),
                },
            )

    async def _flush_vad(self) -> None:
        text = self._utterance.strip()
        if text:
            await self._emit(
                EventKind.INPUT_TRANSCRIPT_FINAL,
                {
                    "text": text,
                    "eou": False,
                    "backchannel": False,
                    "authority": True,
                    "vad_fallback": True,
                },
            )
        self._utterance = ""
        if self._service is not None and hasattr(self._service, "reset_state"):
            await asyncio.to_thread(self._service.reset_state)

    async def _run(self) -> None:
        try:
            while True:
                item = await self._queue.get()
                if isinstance(item, _Control):
                    if item.kind == "stop":
                        return
                    if item.kind == "vad_stop":
                        while len(self._byte_buffer) >= self.chunk_bytes:
                            chunk = bytes(self._byte_buffer[: self.chunk_bytes])
                            del self._byte_buffer[: self.chunk_bytes]
                            result = await asyncio.to_thread(
                                self._service.transcribe,
                                chunk,
                            )
                            await self._handle_result(result)
                        self._byte_buffer.clear()
                        await self._flush_vad()
                    continue

                self._byte_buffer.extend(item)
                while len(self._byte_buffer) >= self.chunk_bytes:
                    chunk = bytes(self._byte_buffer[: self.chunk_bytes])
                    del self._byte_buffer[: self.chunk_bytes]
                    result = await asyncio.to_thread(
                        self._service.transcribe,
                        chunk,
                    )
                    await self._handle_result(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit(
                EventKind.TRANSCRIBER_ERROR,
                {"error": str(exc)},
            )

    async def recv(self) -> TimelineEvent:
        return await self._events.get()

    async def close(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is not None:
            try:
                self._queue.put_nowait(_Control("stop"))
            except asyncio.QueueFull:
                worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
