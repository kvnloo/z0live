import asyncio

from websockets.asyncio.server import serve

from z0live.actors.personaplex import PersonaPlexActor
from z0live.contracts import AudioFrame, EventKind
from z0live.plan import validate_plan


def test_personaplex_binary_protocol_translation():
    async def go():
        received = []

        async def handler(ws):
            await ws.send(b"\x00")
            received.append(await ws.recv())
            await ws.send(b"\x02hello")
            await ws.send(b"\x01out")
            await asyncio.sleep(0.05)

        server = await serve(
            handler,
            "127.0.0.1",
            0,
        )
        port = server.sockets[0].getsockname()[1]
        plan = validate_plan(
            {
                "schema": "z0int.voice_plan.v1",
                "plan_id": "vp_pp",
                "actor_id": "pp",
                "adapter": "personaplex",
                "provider": "local",
                "endpoint": {
                    "scheme": "ws",
                    "host": "127.0.0.1",
                    "port": port,
                },
                "admission": {
                    "admitted": True,
                    "status": "admitted",
                },
                "resource": {
                    "residency": "session",
                },
            }
        )
        actor = PersonaPlexActor(plan)
        await actor.start()

        ready = await actor.recv()
        assert (
            ready.event.kind
            == EventKind.SESSION_READY
        )

        await actor.send_audio(
            AudioFrame(
                b"in",
                "opus",
                24000,
            )
        )
        transcript = await actor.recv()
        audio = await actor.recv()
        assert (
            transcript.event.kind
            == EventKind.ASSISTANT_TRANSCRIPT_DELTA
        )
        assert (
            transcript.event.payload["text"]
            == "hello"
        )
        assert audio.audio.data == b"out"
        assert received == [b"\x01in"]

        await actor.close()
        server.close()
        await server.wait_closed()

    asyncio.run(go())
