from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from .attention import AttentionAction, AttentionPolicy
from .contracts import (
    AudioFrame,
    ContextInjection,
    ConversationActor,
    EventKind,
    HarnessAdapter,
    HarnessCommand,
    TimelineEvent,
)
from .floor import FloorAction, FloorController


@dataclass(slots=True)
class RuntimeHooks:
    on_audio: Callable[[AudioFrame], Awaitable[None]] | None = None
    on_event: Callable[[TimelineEvent], Awaitable[None]] | None = None
    on_activity: Callable[[], None] | None = None


class ConversationRuntime:
    def __init__(
        self,
        actor: ConversationActor,
        *,
        harness: HarnessAdapter | None = None,
        hooks: RuntimeHooks | None = None,
        floor: FloorController | None = None,
        attention: AttentionPolicy | None = None,
    ) -> None:
        self.actor = actor
        self.harness = harness
        self.hooks = hooks or RuntimeHooks()
        self.floor = floor or FloorController()
        self.attention = attention or AttentionPolicy()
        self._tasks: list[asyncio.Task] = []
        self._closed = False
        self._started_at = time.monotonic()

    def _touch(self) -> None:
        if self.hooks.on_activity is not None:
            self.hooks.on_activity()

    def _normalize_event(self, event: TimelineEvent) -> TimelineEvent:
        source_at = int(event.at_ms)
        event.at_ms = int((time.monotonic() - self._started_at) * 1000)
        if source_at > 0:
            event.payload = {**event.payload, "source_at_ms": source_at}
        return event

    async def _publish_event(self, event: TimelineEvent) -> None:
        self._touch()
        event = self._normalize_event(event)
        if self.hooks.on_event is not None:
            await self.hooks.on_event(event)

    async def start(self) -> None:
        if self.harness is not None:
            await self.harness.attach()
        await self.actor.start()
        self._tasks.append(asyncio.create_task(self._actor_loop(), name="z0live-actor-loop"))
        if self.harness is not None:
            self._tasks.append(asyncio.create_task(self._harness_loop(), name="z0live-harness-loop"))

    async def _actor_loop(self) -> None:
        while not self._closed:
            message = await self.actor.recv()
            if message.audio is not None:
                self._touch()
                if self.hooks.on_audio is not None:
                    await self.hooks.on_audio(message.audio)
                continue
            event = message.event
            assert event is not None
            decision = self.floor.observe(event)
            await self._publish_event(event)
            if (
                decision.action == FloorAction.INTERRUPT_ASSISTANT
                and not self.actor.capabilities.native_barge_in
                and self.actor.capabilities.cancel_response
            ):
                await self.actor.interrupt()

    async def _harness_loop(self) -> None:
        assert self.harness is not None
        while not self._closed:
            event = await self.harness.recv()
            await self._publish_event(event)
            decision = self.attention.decide(event)
            if decision.action == AttentionAction.DROP or decision.text is None:
                continue
            if decision.action == AttentionAction.INTERRUPT and self.actor.capabilities.cancel_response:
                await self.actor.interrupt()
            if decision.action == AttentionAction.SILENT:
                if self.actor.capabilities.quiet_context:
                    await self.actor.inject_context(
                        ContextInjection(
                            decision.text,
                            speakable=False,
                            trace_id=event.trace_id,
                            task_id=event.task_id,
                        )
                    )
            elif decision.action in (AttentionAction.SPEAK, AttentionAction.INTERRUPT):
                if self.actor.capabilities.speakable_commentary:
                    await self.actor.inject_context(
                        ContextInjection(
                            decision.text,
                            speakable=True,
                            trace_id=event.trace_id,
                            task_id=event.task_id,
                        )
                    )

    async def send_audio(self, frame: AudioFrame) -> None:
        self._touch()
        await self.actor.send_audio(frame)

    async def client_event(self, event: TimelineEvent) -> None:
        self._touch()
        decision = self.floor.observe(event)
        await self._publish_event(event)
        if event.kind == EventKind.USER_SPEECH_STARTED:
            await self.actor.user_speech_started(
                backchannel=bool(event.payload.get("backchannel"))
            )
        elif event.kind == EventKind.USER_SPEECH_STOPPED:
            await self.actor.user_speech_stopped()
        if (
            decision.action == FloorAction.INTERRUPT_ASSISTANT
            and not self.actor.capabilities.native_barge_in
            and self.actor.capabilities.cancel_response
        ):
            await self.actor.interrupt()

    def dispatch_harness(self, command: HarnessCommand) -> asyncio.Task:
        if self.harness is None:
            raise RuntimeError("no harness attached")
        return asyncio.create_task(
            self.harness.command(command),
            name=f"z0live-harness-{command.kind.value}",
        )

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()
        if self.harness is not None:
            await self.harness.close()
        await self.actor.close()
