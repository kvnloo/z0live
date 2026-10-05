import asyncio
import json

from websockets.asyncio.client import connect

from z0live.actors.fake import FakeActor
from z0live.contracts import EventKind
from z0live.gateway import GatewayAddress, RuntimeGateway
from z0live.runtime import ConversationRuntime


def test_gateway_forwards_audio_and_floor_events():
    async def go():
        actor = FakeActor()
        runtime = ConversationRuntime(actor)
        await runtime.start()
        gateway = RuntimeGateway(
            runtime,
            GatewayAddress("127.0.0.1", 0),
        )
        await gateway.start()
        port = gateway._server.sockets[0].getsockname()[1]

        async with connect(
            f"ws://127.0.0.1:{port}"
        ) as ws:
            hello = json.loads(await ws.recv())
            assert hello["type"] == "hello"

            await ws.send(b"abc")
            await asyncio.sleep(0.01)
            assert actor.audio_in[0].data == b"abc"

            await ws.send(
                json.dumps(
                    {
                        "type": "event",
                        "event": {
                            "kind": "user.speech.started",
                            "source": "omp",
                            "payload": {},
                        },
                    }
                )
            )
            seen = json.loads(await ws.recv())
            assert (
                seen["event"]["kind"]
                == EventKind.USER_SPEECH_STARTED.value
            )

        await gateway.close()
        await runtime.close()

    asyncio.run(go())
