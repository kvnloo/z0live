from __future__ import annotations

import asyncio
import ssl
from urllib.parse import urlencode

from ..contracts import (
    ActorCapabilities,
    AudioFrame,
    ContextInjection,
    EventKind,
    QueueActor,
)
from ..plan import VoicePlan


class PersonaPlexActor(QueueActor):
    """NVIDIA PersonaPlex/Moshi binary WebSocket adapter."""

    def __init__(self, plan: VoicePlan) -> None:
        super().__init__()
        self.plan = plan
        self._ws = None
        self._reader: asyncio.Task | None = None
        self._ready = asyncio.Event()

    @property
    def actor_id(self) -> str:
        return self.plan.actor_id

    @property
    def capabilities(self) -> ActorCapabilities:
        return ActorCapabilities(
            full_duplex=True,
            native_audio_in=True,
            native_audio_out=True,
            input_codec="ogg-opus",
            output_codec="ogg-opus",
            input_sample_rate_hz=24000,
            output_sample_rate_hz=24000,
            input_transcripts=False,
            input_partial_transcripts=False,
            output_transcripts=True,
            output_partial_transcripts=True,
            server_vad=False,
            native_barge_in=True,
            cancel_response=False,
            quiet_context=False,
            speakable_commentary=False,
            tool_delegation=False,
            local_transport=True,
        )

    def _url(self) -> str:
        options = self.plan.actor_options
        query = urlencode(
            {
                "voice_prompt": options.get("voice_prompt", "NATF2.pt"),
                "text_prompt": options.get(
                    "text_prompt", "You are a helpful conversational assistant."
                ),
                "seed": options.get("seed", -1),
            }
        )
        scheme = str(self.plan.endpoint.get("scheme") or "wss")
        if scheme in ("https", "wss"):
            scheme = "wss"
        elif scheme in ("http", "ws"):
            scheme = "ws"
        return (
            f"{scheme}://{self.plan.endpoint_host}:{self.plan.endpoint_port}"
            f"/api/chat?{query}"
        )

    async def start(self) -> None:
        from websockets.asyncio.client import connect

        ssl_ctx = None
        if self._url().startswith("wss://"):
            ssl_ctx = ssl.create_default_context()
            if self.plan.endpoint_host in ("127.0.0.1", "localhost", "::1"):
                ssl_ctx.check_hostname = False
                ssl_ctx.verify_mode = ssl.CERT_NONE
        self._ws = await connect(self._url(), ssl=ssl_ctx, max_size=None).__aenter__()
        self._reader = asyncio.create_task(
            self._read_loop(), name="z0live-personaplex-reader"
        )
        try:
            await asyncio.wait_for(self._ready.wait(), timeout=60)
        except Exception:
            await self.close()
            raise

    async def _read_loop(self) -> None:
        try:
            async for message in self._ws:
                if not isinstance(message, bytes) or not message:
                    continue
                kind, payload = message[0], message[1:]
                if kind == 0:
                    self._ready.set()
                    await self._emit_event(
                        EventKind.SESSION_READY, payload={"protocol": "personaplex"}
                    )
                elif kind == 1:
                    await self._emit_audio(
                        AudioFrame(data=payload, codec="opus", sample_rate_hz=24000)
                    )
                elif kind == 2:
                    await self._emit_event(
                        EventKind.ASSISTANT_TRANSCRIPT_DELTA,
                        payload={"text": payload.decode("utf-8", errors="replace")},
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_event(EventKind.ACTOR_ERROR, payload={"error": str(exc)})
        finally:
            self._ready.set()

    async def send_audio(self, frame: AudioFrame) -> None:
        if frame.codec.lower() != "ogg-opus":
            raise ValueError(f"PersonaPlex requires Ogg/Opus input, got {frame.codec!r}")
        if self._ws is None:
            raise RuntimeError("PersonaPlex actor is not started")
        await self._ws.send(b"\x01" + frame.data)

    async def interrupt(self) -> None:
        # Native full duplex: barge-in is represented by continued user audio.
        return None

    async def inject_context(self, injection: ContextInjection) -> None:
        raise NotImplementedError(
            "PersonaPlex v1 prompt is startup-only; dynamic context injection is unsupported"
        )

    async def close(self) -> None:
        reader = self._reader
        self._reader = None
        if reader is not None:
            reader.cancel()
            try:
                await reader
            except asyncio.CancelledError:
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            finally:
                self._ws = None
        await self._emit_event(
            EventKind.SESSION_STOPPED, payload={"protocol": "personaplex"}
        )
