import asyncio

from z0live.actors.openai_realtime import OpenAIRealtimeActor
from z0live.contracts import AudioFrame, ContextInjection
from z0live.plan import validate_plan


class Node:
    def __init__(self, connection, prefix):
        self.connection = connection
        self.prefix = prefix

    async def append(self, **kw):
        self.connection.calls.append(
            (self.prefix + ".append", kw)
        )

    async def cancel(self, **kw):
        self.connection.calls.append(
            (self.prefix + ".cancel", kw)
        )

    async def create(self, **kw):
        self.connection.calls.append(
            (self.prefix + ".create", kw)
        )

    async def update(self, **kw):
        self.connection.calls.append(
            (self.prefix + ".update", kw)
        )

    @property
    def item(self):
        return Node(
            self.connection,
            self.prefix + ".item",
        )


class Connection:
    def __init__(self):
        self.calls = []
        self.events = asyncio.Queue()
        self.session = Node(self, "session")
        self.input_audio_buffer = Node(
            self,
            "input_audio_buffer",
        )
        self.response = Node(self, "response")
        self.conversation = Node(
            self,
            "conversation",
        )

    def __aiter__(self):
        return self

    async def __anext__(self):
        item = await self.events.get()
        if item is StopAsyncIteration:
            raise StopAsyncIteration
        return item


class CM:
    def __init__(self, conn):
        self.conn = conn

    async def __aenter__(self):
        return self.conn

    async def __aexit__(self, *args):
        return None


class Realtime:
    def __init__(self, conn):
        self.conn = conn

    def connect(self, model):
        return CM(self.conn)


class Client:
    def __init__(self, conn):
        self.realtime = Realtime(conn)


def plan():
    return validate_plan(
        {
            "schema": "z0int.voice_plan.v1",
            "plan_id": "vp_openai",
            "actor_id": "oa",
            "adapter": "openai_realtime",
            "provider": "openai",
            "model": "gpt-realtime-2.1",
            "admission": {
                "admitted": True,
                "status": "admitted",
            },
            "resource": {
                "residency": "session",
            },
        }
    )


def test_openai_adapter_uses_selected_connection_contract():
    async def go():
        conn = Connection()
        actor = OpenAIRealtimeActor(
            plan(),
            client=Client(conn),
        )
        await actor.start()
        await actor.send_audio(
            AudioFrame(
                b"12",
                "pcm16",
                24000,
            )
        )
        await actor.interrupt()
        await actor.inject_context(
            ContextInjection(
                "quiet status",
                speakable=False,
            )
        )
        await actor.inject_context(
            ContextInjection(
                "tests passed",
                speakable=True,
            )
        )

        names = [
            name for name, _ in conn.calls
        ]
        assert "session.update" in names
        assert "input_audio_buffer.append" in names
        assert "response.cancel" in names
        assert "conversation.item.create" in names
        assert "response.create" in names
        await actor.close()

    asyncio.run(go())
