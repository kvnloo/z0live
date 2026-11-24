"""Bounded admission for one call's Hermes runs.

A call may have up to `run_max` Hermes runs going at once. A request past that waits in a FIFO line of at most
`queue_max`; past that it is refused at once. Nothing waits without the caller being told: `acquire` reports the
line position through `on_queued` before it waits, and raises `QueueFull` instead of waiting past the bound.

A freed slot is handed straight to the next waiter (no notify-and-race), so a waiter cancelled at the head of the
line (the call closed, the request was dropped) never stalls the requests behind it. Take 7 of the 11-22 film call
is why this exists: a request queued silently behind a running one while the voice said it had been handed off.
"""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager


class QueueFull(Exception):
    def __init__(self, running: int, waiting: int):
        super().__init__(f"{running} running, {waiting} waiting")
        self.running, self.waiting = running, waiting


class TurnSlots:
    def __init__(self, run_max: int, queue_max: int):
        self.run_max, self.queue_max = max(1, run_max), max(0, queue_max)
        self.running: set[str] = set()
        self.waiting: list[tuple[str, asyncio.Future]] = []

    def state(self, did: str) -> str | None:
        if did in self.running:
            return "running"
        return "queued" if any(d == did for d, _ in self.waiting) else None

    def position(self, did: str) -> int | None:
        """1-based place in line, or None when it is not waiting."""
        return next((i for i, (d, _) in enumerate(self.waiting, 1) if d == did), None)

    async def acquire(self, did: str, on_queued=None) -> int:
        """Take a run slot. Returns the place in line it waited at (0: it started at once)."""
        if len(self.running) < self.run_max and not self.waiting:
            self.running.add(did)
            return 0
        if len(self.waiting) >= self.queue_max:
            raise QueueFull(len(self.running), len(self.waiting))
        granted = asyncio.get_running_loop().create_future()
        self.waiting.append((did, granted))
        position = len(self.waiting)
        if on_queued:
            on_queued(position, len(self.running))
        try:
            await granted
        except asyncio.CancelledError:
            if granted.done() and not granted.cancelled():
                self.release(did)  # handed a slot, cancelled before it ran: pass the slot on
            else:
                self.waiting = [w for w in self.waiting if w[1] is not granted]
            raise
        return position

    def release(self, did: str) -> None:
        self.running.discard(did)
        while self.waiting and len(self.running) < self.run_max:
            nxt, granted = self.waiting.pop(0)
            if granted.done():  # cancelled while it waited
                continue
            self.running.add(nxt)
            granted.set_result(None)

    @asynccontextmanager
    async def slot(self, did: str, on_queued=None):
        await self.acquire(did, on_queued)
        try:
            yield
        finally:
            self.release(did)
