import asyncio
import json

from z0live.contracts import (
    EventKind,
    HarnessCommand,
    HarnessCommandKind,
    TimelineEvent,
)
from z0live.harnesses.jsonl import JsonLineHarnessAdapter


def test_jsonl_harness_bridge_command_and_event():
    async def go():
        async def handler(reader, writer):
            writer.write(
                (
                    json.dumps(
                        {
                            "type": "hello",
                            "capabilities": {
                                "submit": True,
                                "steer": True,
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()

            raw = json.loads(await reader.readline())
            cid = raw["command"]["command_id"]
            writer.write(
                (
                    json.dumps(
                        {
                            "type": "result",
                            "command_id": cid,
                            "result": {"ok": True},
                        }
                    )
                    + "\n"
                ).encode()
            )
            writer.write(
                (
                    json.dumps(
                        {
                            "type": "event",
                            "event": {
                                "kind": "harness.progress",
                                "source": "omp",
                                "payload": {"text": "running"},
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            await asyncio.sleep(0.05)
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(
            handler,
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        adapter = JsonLineHarnessAdapter(
            "127.0.0.1",
            port,
        )
        await adapter.attach()
        assert adapter.capabilities.steer is True
        result = await adapter.command(
            HarnessCommand(
                HarnessCommandKind.SUBMIT,
                trace_id="t",
                text="x",
            )
        )
        assert result["ok"] is True
        event = await adapter.recv()
        assert event.kind == EventKind.HARNESS_PROGRESS
        await adapter.close()
        server.close()
        await server.wait_closed()

    asyncio.run(go())


def test_jsonl_harness_bridge_streams_observations_when_advertised():
    async def go():
        seen = []

        async def handler(reader, writer):
            writer.write(
                (
                    json.dumps(
                        {
                            "type": "hello",
                            "capabilities": {
                                "submit": True,
                                "observations": True,
                            },
                        }
                    )
                    + "\n"
                ).encode()
            )
            await writer.drain()
            seen.append(json.loads(await reader.readline()))
            writer.close()
            await writer.wait_closed()

        server = await asyncio.start_server(handler, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        adapter = JsonLineHarnessAdapter("127.0.0.1", port)
        await adapter.attach()
        assert adapter.capabilities.observations is True

        await adapter.observe(
            TimelineEvent(
                kind=EventKind.INPUT_TRANSCRIPT_FINAL,
                source="parakeet",
                trace_id="voice-1",
                payload={"text": "OMP run tests", "authority": True},
            )
        )
        deadline = asyncio.get_running_loop().time() + 1
        while not seen and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        assert seen[0]["type"] == "observation"
        assert seen[0]["event"]["payload"]["authority"] is True

        await adapter.close()
        server.close()
        await server.wait_closed()

    asyncio.run(go())
