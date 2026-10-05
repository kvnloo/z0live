import asyncio
import base64
import json

from websockets.asyncio.client import connect

from z0live.actors.fake import FakeActor
from z0live.contracts import EventKind
from z0live.gateway import GatewayAddress, RuntimeGateway
from z0live.runtime import ConversationRuntime
from z0live.transcribers.fake import FakeTranscriber


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


def test_gateway_exposes_and_forwards_separate_transcriber_audio():
    async def go():
        actor = FakeActor()
        transcriber = FakeTranscriber()
        runtime = ConversationRuntime(actor, transcriber=transcriber)
        await runtime.start()
        gateway = RuntimeGateway(runtime, GatewayAddress("127.0.0.1", 0))
        await gateway.start()
        port = gateway._server.sockets[0].getsockname()[1]

        async with connect(f"ws://127.0.0.1:{port}") as ws:
            hello = json.loads(await ws.recv())
            assert hello["transcriber_id"] == "fake-transcriber"
            assert hello["transcriber_capabilities"]["input_codec"] == "pcm16"

            pcm = b"\x01\x00\x02\x00"
            await ws.send(
                json.dumps(
                    {
                        "type": "transcriber.audio",
                        "audio": {
                            "codec": "pcm16",
                            "sample_rate_hz": 16000,
                            "channels": 1,
                            "data_base64": base64.b64encode(pcm).decode("ascii"),
                        },
                    }
                )
            )
            deadline = asyncio.get_running_loop().time() + 1
            while not transcriber.audio_in and asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.01)
            assert transcriber.audio_in[0].data == pcm

        await gateway.close()
        await runtime.close()

    asyncio.run(go())
