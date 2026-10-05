from __future__ import annotations

import asyncio

from ..contracts import (
    EventKind,
    HarnessAdapter,
    HarnessCapabilities,
    HarnessCommand,
    TimelineEvent,
)


class FakeHarness(HarnessAdapter):
    def __init__(self, delay_seconds: float = 0.0) -> None:
        self.delay_seconds = delay_seconds
        self.commands: list[HarnessCommand] = []
        self.observations: list[TimelineEvent] = []
        self._events: asyncio.Queue[TimelineEvent] = asyncio.Queue()
        self.attached = False
        self.closed = False

    @property
    def capabilities(self) -> HarnessCapabilities:
        return HarnessCapabilities(
            submit=True,
            steer=True,
            redirect=True,
            cancel=True,
            approvals=True,
            progress_events=True,
            verified_results=True,
            observations=True,
        )

    async def attach(self) -> None:
        self.attached = True

    async def command(self, command: HarnessCommand) -> dict:
        self.commands.append(command)
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        return {"ok": True, "command_id": command.command_id}

    async def recv(self) -> TimelineEvent:
        return await self._events.get()

    async def observe(self, event: TimelineEvent) -> None:
        self.observations.append(event)

    async def close(self) -> None:
        self.closed = True

    async def emit(
        self,
        kind: EventKind,
        *,
        trace_id: str = "trace",
        task_id: str | None = None,
        **payload,
    ) -> None:
        await self._events.put(
            TimelineEvent(
                kind=kind,
                source="fake-harness",
                trace_id=trace_id,
                task_id=task_id,
                payload=payload,
            )
        )
