from __future__ import annotations

import asyncio
import json
from typing import Any

from ..contracts import (
    HarnessAdapter,
    HarnessCapabilities,
    HarnessCommand,
    TimelineEvent,
)


class JsonLineHarnessAdapter(HarnessAdapter):
    """Generic harness bridge over newline-delimited JSON/TCP."""

    def __init__(self, host: str, port: int) -> None:
        self.host = host
        self.port = int(port)
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._caps = HarnessCapabilities()
        self._events: asyncio.Queue[TimelineEvent] = asyncio.Queue()
        self._pending: dict[str, asyncio.Future] = {}
        self._reader_task: asyncio.Task | None = None

    @property
    def capabilities(self) -> HarnessCapabilities:
        return self._caps

    async def attach(self) -> None:
        self._reader, self._writer = await asyncio.open_connection(
            self.host, self.port
        )
        line = await asyncio.wait_for(self._reader.readline(), timeout=5)
        hello = json.loads(line)
        if hello.get("type") != "hello":
            raise ValueError("harness bridge did not send hello")
        raw = dict(hello.get("capabilities") or {})
        allowed = self._caps.__dataclass_fields__.keys()
        self._caps = HarnessCapabilities(
            **{k: bool(v) for k, v in raw.items() if k in allowed}
        )
        self._reader_task = asyncio.create_task(
            self._read_loop(), name="z0live-harness-jsonl"
        )

    async def _read_loop(self) -> None:
        assert self._reader is not None
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break
                raw = json.loads(line)
                typ = raw.get("type")
                if typ == "event":
                    await self._events.put(
                        TimelineEvent.from_dict(dict(raw["event"]))
                    )
                elif typ == "result":
                    cid = str(raw.get("command_id") or "")
                    fut = self._pending.pop(cid, None)
                    if fut is not None and not fut.done():
                        fut.set_result(dict(raw.get("result") or {}))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(exc)
        finally:
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(
                        ConnectionError("harness bridge disconnected")
                    )
            self._pending.clear()

    async def command(self, command: HarnessCommand) -> dict[str, Any]:
        if self._writer is None:
            raise RuntimeError("harness bridge not attached")
        loop = asyncio.get_running_loop()
        fut = loop.create_future()
        self._pending[command.command_id] = fut
        payload = {"type": "command", "command": command.to_dict()}
        self._writer.write(
            (json.dumps(payload) + "\n").encode("utf-8")
        )
        await self._writer.drain()
        try:
            return await asyncio.wait_for(fut, timeout=30)
        finally:
            self._pending.pop(command.command_id, None)

    async def recv(self) -> TimelineEvent:
        return await self._events.get()

    async def observe(self, event: TimelineEvent) -> None:
        if not self._caps.observations:
            return
        if self._writer is None:
            raise RuntimeError("harness bridge not attached")
        self._writer.write(
            (
                json.dumps(
                    {"type": "observation", "event": event.to_dict()},
                    ensure_ascii=False,
                )
                + "\n"
            ).encode("utf-8")
        )
        await self._writer.drain()

    async def close(self) -> None:
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
        if self._writer is not None:
            self._writer.close()
            await self._writer.wait_closed()
            self._writer = None
