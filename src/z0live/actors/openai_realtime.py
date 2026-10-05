from __future__ import annotations

import asyncio
import base64
from typing import Any

from ..contracts import (
    ActorCapabilities,
    AudioFrame,
    ContextInjection,
    EventKind,
    QueueActor,
)
from ..plan import VoicePlan


class OpenAIRealtimeActor(QueueActor):
    """Optional OpenAI Realtime adapter selected only by VoicePlan."""

    def __init__(self, plan: VoicePlan, *, client: Any | None = None) -> None:
        super().__init__()
        self.plan = plan
        self._client = client
        self._cm = None
        self._connection = None
        self._reader: asyncio.Task | None = None
        self._quiet_context: list[str] = []
        self._assistant_audio_active = False

    @property
    def actor_id(self) -> str:
        return self.plan.actor_id

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
            partial_transcripts=True,
            server_vad=True,
            native_barge_in=True,
            cancel_response=True,
            quiet_context=True,
            speakable_commentary=True,
            tool_delegation=True,
            local_transport=False,
        )

    async def start(self) -> None:
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:
                raise RuntimeError(
                    "OpenAI adapter requires pip install 'z0live[openai]'"
                ) from exc
            self._client = AsyncOpenAI()
        model = self.plan.model or "gpt-realtime-2.1"
        self._cm = self._client.realtime.connect(model=model)
        self._connection = await self._cm.__aenter__()
        options = self.plan.actor_options
        session: dict[str, Any] = {"type": "realtime"}
        if options.get("instructions"):
            session["instructions"] = str(options["instructions"])
        if options.get("session") and isinstance(options["session"], dict):
            session.update(options["session"])
        await self._connection.session.update(session=session)
        self._reader = asyncio.create_task(
            self._read_loop(), name="z0live-openai-reader"
        )

    async def _read_loop(self) -> None:
        try:
            async for event in self._connection:
                et = str(getattr(event, "type", ""))
                if et in ("session.created", "session.updated"):
                    await self._emit_event(
                        EventKind.SESSION_READY, payload={"provider_event": et}
                    )
                elif et == "input_audio_buffer.speech_started":
                    await self._emit_event(EventKind.USER_SPEECH_STARTED)
                elif et == "input_audio_buffer.speech_stopped":
                    await self._emit_event(EventKind.USER_SPEECH_STOPPED)
                elif et.endswith("input_audio_transcription.delta"):
                    await self._emit_event(
                        EventKind.INPUT_TRANSCRIPT_DELTA,
                        payload={"text": str(getattr(event, "delta", ""))},
                    )
                elif et.endswith("input_audio_transcription.completed"):
                    await self._emit_event(
                        EventKind.INPUT_TRANSCRIPT_FINAL,
                        payload={"text": str(getattr(event, "transcript", ""))},
                    )
                elif et == "response.output_audio.delta":
                    if not self._assistant_audio_active:
                        self._assistant_audio_active = True
                        await self._emit_event(EventKind.ASSISTANT_SPEECH_STARTED)
                    raw = base64.b64decode(str(getattr(event, "delta", "")))
                    await self._emit_audio(AudioFrame(raw, "pcm16", 24000))
                elif et == "response.output_audio.done":
                    self._assistant_audio_active = False
                    await self._emit_event(EventKind.ASSISTANT_SPEECH_STOPPED)
                elif et == "response.output_audio_transcript.delta":
                    await self._emit_event(
                        EventKind.ASSISTANT_TRANSCRIPT_DELTA,
                        payload={"text": str(getattr(event, "delta", ""))},
                    )
                elif et == "response.output_audio_transcript.done":
                    await self._emit_event(
                        EventKind.ASSISTANT_TRANSCRIPT_FINAL,
                        payload={"text": str(getattr(event, "transcript", ""))},
                    )
                elif et == "error":
                    await self._emit_event(
                        EventKind.ACTOR_ERROR,
                        payload={"error": str(getattr(event, "error", None))},
                    )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await self._emit_event(EventKind.ACTOR_ERROR, payload={"error": str(exc)})

    async def send_audio(self, frame: AudioFrame) -> None:
        if frame.codec.lower() != "pcm16" or frame.sample_rate_hz != 24000:
            raise ValueError(
                "OpenAI Realtime adapter expects pcm16 mono 24kHz audio"
            )
        if self._connection is None:
            raise RuntimeError("OpenAI actor is not started")
        audio = base64.b64encode(frame.data).decode("ascii")
        await self._connection.input_audio_buffer.append(audio=audio)

    async def interrupt(self) -> None:
        if self._connection is not None:
            await self._connection.response.cancel()

    async def inject_context(self, injection: ContextInjection) -> None:
        if self._connection is None:
            raise RuntimeError("OpenAI actor is not started")
        if injection.speakable:
            await self._connection.conversation.item.create(
                item={
                    "type": "message",
                    "role": "user",
                    "content": [
                        {
                            "type": "input_text",
                            "text": (
                                "Background result from the execution harness. "
                                "Tell the user only if useful, without claiming anything "
                                "beyond this result:\n" + injection.text
                            ),
                        }
                    ],
                }
            )
            await self._connection.response.create()
            return

        self._quiet_context.append(injection.text)
        base = str(self.plan.actor_options.get("instructions") or "")
        extra = "\n\n".join(self._quiet_context[-8:])
        instructions = (
            base
            + "\n\nQuiet execution context (do not announce unless relevant):\n"
            + extra
        ).strip()
        await self._connection.session.update(
            session={"type": "realtime", "instructions": instructions}
        )

    async def close(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                pass
            self._reader = None
        if self._cm is not None:
            await self._cm.__aexit__(None, None, None)
            self._cm = None
            self._connection = None
        await self._emit_event(
            EventKind.SESSION_STOPPED, payload={"provider": "openai"}
        )
