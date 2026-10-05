from __future__ import annotations

import asyncio
import ctypes
import sys
from array import array
from pathlib import Path
from typing import Protocol

from ..contracts import (
    AudioFrame,
    EventKind,
    InputTranscriber,
    TimelineEvent,
    TranscriberCapabilities,
)

PARAKEET_EVENT_EOU = 1
PARAKEET_EVENT_EOB = 2
MIN_EOU_ABI = 5


class _StreamingAPI(Protocol):
    def abi_version(self) -> int: ...
    def load(self, model_path: str) -> object: ...
    def stream_begin(self, ctx: object) -> object: ...
    def stream_feed(self, stream: object, samples: list[float]) -> tuple[str, int]: ...
    def stream_finalize(self, stream: object) -> str: ...
    def stream_free(self, stream: object) -> None: ...
    def free(self, ctx: object) -> None: ...


class ParakeetCppAPI:
    """Tiny ownership-safe wrapper around parakeet.cpp's flat C API."""

    def __init__(self, library_path: str | Path) -> None:
        self.library_path = str(Path(library_path).expanduser())
        self.lib = ctypes.CDLL(self.library_path)

        self.lib.parakeet_capi_abi_version.argtypes = []
        self.lib.parakeet_capi_abi_version.restype = ctypes.c_int

        self.lib.parakeet_capi_load.argtypes = [ctypes.c_char_p]
        self.lib.parakeet_capi_load.restype = ctypes.c_void_p

        self.lib.parakeet_capi_load_error.argtypes = []
        self.lib.parakeet_capi_load_error.restype = ctypes.c_char_p

        self.lib.parakeet_capi_last_error.argtypes = [ctypes.c_void_p]
        self.lib.parakeet_capi_last_error.restype = ctypes.c_char_p

        self.lib.parakeet_capi_free.argtypes = [ctypes.c_void_p]
        self.lib.parakeet_capi_free.restype = None

        self.lib.parakeet_capi_stream_begin.argtypes = [ctypes.c_void_p]
        self.lib.parakeet_capi_stream_begin.restype = ctypes.c_void_p

        self.lib.parakeet_capi_stream_feed.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.c_float),
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_int),
        ]
        # Use c_void_p so the malloc-owned pointer remains available for free().
        self.lib.parakeet_capi_stream_feed.restype = ctypes.c_void_p

        self.lib.parakeet_capi_stream_finalize.argtypes = [ctypes.c_void_p]
        self.lib.parakeet_capi_stream_finalize.restype = ctypes.c_void_p

        self.lib.parakeet_capi_stream_free.argtypes = [ctypes.c_void_p]
        self.lib.parakeet_capi_stream_free.restype = None

        self.lib.parakeet_capi_free_string.argtypes = [ctypes.c_void_p]
        self.lib.parakeet_capi_free_string.restype = None

    def abi_version(self) -> int:
        return int(self.lib.parakeet_capi_abi_version())

    def _load_error(self) -> str:
        raw = self.lib.parakeet_capi_load_error()
        return raw.decode("utf-8", errors="replace") if raw else "unknown load error"

    def _ctx_error(self, ctx: object) -> str:
        raw = self.lib.parakeet_capi_last_error(ctx)
        return raw.decode("utf-8", errors="replace") if raw else "unknown parakeet error"

    def load(self, model_path: str) -> object:
        ctx = self.lib.parakeet_capi_load(str(model_path).encode("utf-8"))
        if not ctx:
            raise RuntimeError(f"parakeet.cpp model load failed: {self._load_error()}")
        return ctx

    def stream_begin(self, ctx: object) -> object:
        stream = self.lib.parakeet_capi_stream_begin(ctx)
        if not stream:
            raise RuntimeError(
                f"parakeet.cpp stream begin failed: {self._ctx_error(ctx)}"
            )
        return stream

    def _take_string(self, ptr: int | None, *, ctx: object | None = None) -> str:
        if not ptr:
            if ctx is not None:
                raise RuntimeError(
                    f"parakeet.cpp streaming call failed: {self._ctx_error(ctx)}"
                )
            return ""
        try:
            return ctypes.string_at(ptr).decode("utf-8", errors="replace")
        finally:
            self.lib.parakeet_capi_free_string(ptr)

    def stream_feed(self, stream: object, samples: list[float]) -> tuple[str, int]:
        if not samples:
            return "", 0
        buf = (ctypes.c_float * len(samples))(*samples)
        mask = ctypes.c_int(0)
        ptr = self.lib.parakeet_capi_stream_feed(
            stream,
            buf,
            len(samples),
            ctypes.byref(mask),
        )
        # stream_feed returns NULL only on error. last_error is context-owned,
        # but the C API does not expose stream -> ctx; treat NULL as a hard error.
        if not ptr:
            raise RuntimeError("parakeet.cpp stream feed failed")
        return self._take_string(ptr), int(mask.value)

    def stream_finalize(self, stream: object) -> str:
        ptr = self.lib.parakeet_capi_stream_finalize(stream)
        if not ptr:
            return ""
        return self._take_string(ptr)

    def stream_free(self, stream: object) -> None:
        self.lib.parakeet_capi_stream_free(stream)

    def free(self, ctx: object) -> None:
        self.lib.parakeet_capi_free(ctx)


def _pcm16le_to_float(data: bytes) -> list[float]:
    if len(data) % 2:
        raise ValueError("pcm16 payload length must be even")
    values = array("h")
    values.frombytes(data)
    if sys.byteorder != "little":
        values.byteswap()
    return [max(-1.0, min(1.0, value / 32768.0)) for value in values]


class ParakeetCppEOUTranscriber(InputTranscriber):
    """CPU-friendly parakeet.cpp EOU/EOB sidecar.

    Audio ingestion is queue-only. Native inference runs in a worker thread so
    PersonaPlex's realtime audio path never waits on ASR compute.
    """

    def __init__(
        self,
        *,
        library_path: str | Path,
        model_path: str | Path,
        max_queue_chunks: int = 64,
        api: _StreamingAPI | None = None,
    ) -> None:
        self.library_path = str(Path(library_path).expanduser())
        self.model_path = str(Path(model_path).expanduser())
        self.max_queue_chunks = max(2, int(max_queue_chunks))
        self._api: _StreamingAPI = api or ParakeetCppAPI(self.library_path)
        self._ctx: object | None = None
        self._stream: object | None = None
        self._audio: asyncio.Queue[bytes | None] = asyncio.Queue(
            maxsize=self.max_queue_chunks
        )
        self._events: asyncio.Queue[TimelineEvent] = asyncio.Queue()
        self._worker: asyncio.Task | None = None
        self._utterance_parts: list[str] = []
        self._closed = False
        self._dropped_chunks = 0

    @property
    def transcriber_id(self) -> str:
        return "parakeet-cpp-realtime-eou-120m-v1"

    @property
    def capabilities(self) -> TranscriberCapabilities:
        return TranscriberCapabilities(
            input_codec="pcm16",
            input_sample_rate_hz=16000,
            channels=1,
            partial_transcripts=True,
            end_of_utterance=True,
            end_of_backchannel=True,
            local_transport=True,
        )

    async def start(self) -> None:
        abi = await asyncio.to_thread(self._api.abi_version)
        if abi < MIN_EOU_ABI:
            raise RuntimeError(
                f"parakeet.cpp ABI {abi} is too old; EOU/EOB requires >= {MIN_EOU_ABI}"
            )
        self._ctx = await asyncio.to_thread(self._api.load, self.model_path)
        try:
            self._stream = await asyncio.to_thread(
                self._api.stream_begin,
                self._ctx,
            )
        except Exception:
            await asyncio.to_thread(self._api.free, self._ctx)
            self._ctx = None
            raise
        self._worker = asyncio.create_task(
            self._run_worker(),
            name="z0live-parakeet-cpp",
        )
        await self._events.put(
            TimelineEvent(
                kind=EventKind.TRANSCRIBER_READY,
                source=self.transcriber_id,
                payload={
                    "backend": "parakeet.cpp",
                    "abi": abi,
                    "model_path": self.model_path,
                },
            )
        )

    async def send_audio(self, frame: AudioFrame) -> None:
        if self._closed:
            return
        if frame.codec.lower() != "pcm16":
            raise ValueError(
                f"parakeet.cpp control lane requires pcm16, got {frame.codec!r}"
            )
        if frame.sample_rate_hz != 16000 or frame.channels != 1:
            raise ValueError(
                "parakeet.cpp control lane requires mono 16 kHz PCM"
            )
        try:
            self._audio.put_nowait(bytes(frame.data))
        except asyncio.QueueFull:
            # Realtime conversation wins over ASR completeness. Drop the oldest
            # control chunk and make the loss explicit in the timeline.
            try:
                self._audio.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._dropped_chunks += 1
            self._audio.put_nowait(bytes(frame.data))
            await self._events.put(
                TimelineEvent(
                    kind=EventKind.TRANSCRIBER_ERROR,
                    source=self.transcriber_id,
                    payload={
                        "error": "control_audio_queue_overrun",
                        "dropped_chunks": self._dropped_chunks,
                    },
                )
            )

    async def recv(self) -> TimelineEvent:
        return await self._events.get()

    async def _run_worker(self) -> None:
        try:
            while True:
                chunk = await self._audio.get()
                if chunk is None:
                    return
                samples = _pcm16le_to_float(chunk)
                text, mask = await asyncio.to_thread(
                    self._api.stream_feed,
                    self._stream,
                    samples,
                )
                await self._handle_feed(text, mask)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._events.put(
                TimelineEvent(
                    kind=EventKind.TRANSCRIBER_ERROR,
                    source=self.transcriber_id,
                    payload={"error": str(exc)},
                )
            )

    async def _handle_feed(self, text: str, mask: int) -> None:
        if text:
            self._utterance_parts.append(text)
            await self._events.put(
                TimelineEvent(
                    kind=EventKind.INPUT_TRANSCRIPT_DELTA,
                    source=self.transcriber_id,
                    payload={
                        "text": "".join(self._utterance_parts),
                        "cumulative": True,
                    },
                )
            )

        eou = bool(mask & PARAKEET_EVENT_EOU)
        eob = bool(mask & PARAKEET_EVENT_EOB)
        if not (eou or eob):
            return

        utterance = "".join(self._utterance_parts).strip()
        self._utterance_parts.clear()

        # A mixed mask can represent multiple events inside one feed block.
        # Without draining per-event timing we cannot safely assign text to the
        # EOU rather than EOB, so fail closed: treat it as non-authoritative.
        mixed = eou and eob
        await self._events.put(
            TimelineEvent(
                kind=EventKind.INPUT_TRANSCRIPT_FINAL,
                source=self.transcriber_id,
                payload={
                    "text": utterance,
                    "backchannel": bool(eob),
                    "boundary": (
                        "mixed_eou_eob"
                        if mixed
                        else "eob"
                        if eob
                        else "eou"
                    ),
                    "mixed_boundary": mixed,
                },
            )
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True

        if self._worker is not None:
            try:
                self._audio.put_nowait(None)
            except asyncio.QueueFull:
                try:
                    self._audio.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                self._audio.put_nowait(None)
            try:
                await self._worker
            except asyncio.CancelledError:
                pass
            self._worker = None

        stream, ctx = self._stream, self._ctx
        self._stream = None
        self._ctx = None
        if stream is not None:
            try:
                await asyncio.to_thread(self._api.stream_finalize, stream)
            finally:
                await asyncio.to_thread(self._api.stream_free, stream)
        if ctx is not None:
            await asyncio.to_thread(self._api.free, ctx)
