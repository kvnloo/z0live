from __future__ import annotations

import json
from dataclasses import dataclass

from .contracts import (
    AudioFrame,
    HarnessCommand,
    HarnessCommandKind,
    TimelineEvent,
)
from .runtime import ConversationRuntime, RuntimeHooks


@dataclass(slots=True)
class GatewayAddress:
    host: str = "127.0.0.1"
    port: int = 8765


class RuntimeGateway:
    """Local WebSocket gateway for harness plugins and UI clients."""

    def __init__(
        self,
        runtime: ConversationRuntime,
        address: GatewayAddress,
    ) -> None:
        self.runtime = runtime
        self.address = address
        self._server = None
        self._clients: set = set()
        previous = runtime.hooks

        async def on_audio(frame: AudioFrame) -> None:
            if previous.on_audio:
                await previous.on_audio(frame)
            await self._broadcast_binary(frame.data)

        async def on_event(event: TimelineEvent) -> None:
            if previous.on_event:
                await previous.on_event(event)
            await self._broadcast_json(
                {"type": "event", "event": event.to_dict()}
            )

        runtime.hooks = RuntimeHooks(
            on_audio=on_audio,
            on_event=on_event,
            on_activity=previous.on_activity,
        )

    async def start(self) -> None:
        from websockets.asyncio.server import serve

        self._server = await serve(
            self._handle,
            self.address.host,
            self.address.port,
            max_size=None,
        )

    async def _handle(self, ws) -> None:
        self._clients.add(ws)
        caps = self.runtime.actor.capabilities
        await ws.send(
            json.dumps(
                {
                    "type": "hello",
                    "schema": "z0live.gateway.v1",
                    "actor_id": self.runtime.actor.actor_id,
                    "actor_capabilities": caps.to_dict(),
                    "harness_capabilities": (
                        self.runtime.harness.capabilities.to_dict()
                        if self.runtime.harness
                        else None
                    ),
                }
            )
        )
        try:
            async for message in ws:
                if isinstance(message, bytes):
                    await self.runtime.send_audio(
                        AudioFrame(
                            data=message,
                            codec=caps.input_codec,
                            sample_rate_hz=caps.input_sample_rate_hz,
                        )
                    )
                    continue
                raw = json.loads(message)
                typ = raw.get("type")
                if typ == "event":
                    await self.runtime.client_event(
                        TimelineEvent.from_dict(
                            dict(raw.get("event") or {})
                        )
                    )
                elif typ == "harness.command":
                    c = dict(raw.get("command") or {})
                    command = HarnessCommand(
                        kind=HarnessCommandKind(str(c["kind"])),
                        trace_id=str(c.get("trace_id") or "gateway"),
                        task_id=c.get("task_id"),
                        text=c.get("text"),
                        payload=dict(c.get("payload") or {}),
                        command_id=str(c.get("command_id") or "")
                        or __import__("uuid").uuid4().hex,
                    )
                    self.runtime.dispatch_harness(command)
                elif typ == "interrupt":
                    await self.runtime.actor.interrupt()
                elif typ == "ping":
                    await ws.send(json.dumps({"type": "pong"}))
                elif typ == "close":
                    break
        finally:
            self._clients.discard(ws)

    async def _broadcast_binary(self, data: bytes) -> None:
        dead = []
        for client in tuple(self._clients):
            try:
                await client.send(data)
            except Exception:
                dead.append(client)
        for client in dead:
            self._clients.discard(client)

    async def _broadcast_json(self, payload: dict) -> None:
        raw = json.dumps(payload, ensure_ascii=False)
        dead = []
        for client in tuple(self._clients):
            try:
                await client.send(raw)
            except Exception:
                dead.append(client)
        for client in dead:
            self._clients.discard(client)

    async def close(self) -> None:
        clients = tuple(self._clients)
        self._clients.clear()
        for client in clients:
            try:
                await client.close()
            except Exception:
                pass
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
