import asyncio
import json

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from z0live.actors.fake import FakeActor
from z0live.contracts import EventKind
from z0live.gateway import GatewayAddress, RuntimeGateway
from z0live.harnesses.fake import FakeHarness
from z0live.runtime import ConversationRuntime


def test_android_loopback_browser_origin_and_harness_receipt():
    async def go():
        actor = FakeActor()
        harness = FakeHarness(delay_seconds=0.05)
        runtime = ConversationRuntime(actor, harness=harness)
        await runtime.start()
        gateway = RuntimeGateway(runtime, GatewayAddress("127.0.0.1", 0))
        await gateway.start()
        port = gateway._server.sockets[0].getsockname()[1]
        uri = f"ws://127.0.0.1:{port}"
        try:
            # A malicious remote webpage must not be allowed to drive the
            # existing Wi-Fi ADB tools through a cross-site WebSocket.
            with pytest.raises(InvalidStatus):
                async with connect(uri, origin="https://other.example"):
                    pass
            async with connect(uri, origin="http://127.0.0.1:8780") as ws:
                hello = json.loads(await ws.recv())
                assert hello["harness_capabilities"]["submit"] is True
                assert hello["actor_capabilities"]["input_codec"] == "pcm16"
                await ws.send(json.dumps({
                    "type": "harness.command",
                    "command": {
                        "kind": "submit",
                        "text": "inspect the screen",
                        "trace_id": "phone-test",
                        "command_id": "test-request-1",
                    },
                }))
                # Command execution is async and never blocks the audio lane.
                await ws.send(b"\x00\x00")
                seen_result = None
                deadline = asyncio.get_running_loop().time() + 2
                while asyncio.get_running_loop().time() < deadline:
                    message = await asyncio.wait_for(ws.recv(), timeout=2)
                    if isinstance(message, str):
                        parsed = json.loads(message)
                        if parsed["type"] == "harness.result":
                            seen_result = parsed
                            break
                assert seen_result is not None
                assert seen_result["command_id"] == "test-request-1"
                assert seen_result["result"]["ok"] is True
                assert seen_result["result"]["command_id"] == "test-request-1"
                assert len(harness.commands) == 1
                assert harness.commands[0].text == "inspect the screen"
                assert actor.audio_in[0].data == b"\x00\x00"

            # OMP's local Node WebSocket client doesn't set Origin.
            async with connect(uri) as ws:
                assert json.loads(await ws.recv())["type"] == "hello"
        finally:
            await gateway.close()
            await runtime.close()

    asyncio.run(go())


def test_gateway_never_binds_an_unauthenticated_lan_port():
    async def go():
        runtime = ConversationRuntime(FakeActor())
        gateway = RuntimeGateway(runtime, GatewayAddress("0.0.0.0", 8765))
        with pytest.raises(ValueError, match="loopback"):
            await gateway.start()
    asyncio.run(go())


def test_actor_final_transcript_is_one_authoritative_harness_observation():
    async def go():
        actor = FakeActor()
        harness = FakeHarness()
        runtime = ConversationRuntime(actor, harness=harness)
        await runtime.start()
        try:
            await actor.emit_event(EventKind.INPUT_TRANSCRIPT_DELTA, text="omp op")
            await actor.emit_event(EventKind.INPUT_TRANSCRIPT_FINAL, text="omp open camera")
            await actor.emit_event(EventKind.INPUT_TRANSCRIPT_FINAL,
                                   text="yeah", backchannel=True)
            deadline = asyncio.get_running_loop().time() + 2
            while len(harness.observations) < 1 and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            assert len(harness.observations) == 1
            final = harness.observations[0]
            assert final.kind == EventKind.INPUT_TRANSCRIPT_FINAL
            assert final.payload["text"] == "omp open camera"
            assert final.payload["authority"] is True
            await asyncio.sleep(0.02)
            assert len(harness.observations) == 1
        finally:
            await runtime.close()
    asyncio.run(go())
