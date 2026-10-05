import asyncio
import struct

import pytest

from z0live.contracts import AudioFrame, EventKind
from z0live.transcribers.parakeet_cpp import (
    PARAKEET_EVENT_EOB,
    PARAKEET_EVENT_EOU,
    ParakeetCppEOUTranscriber,
)


class FakeAPI:
    def __init__(self, feeds=None, abi=10):
        self.abi = abi
        self.feeds = list(feeds or [])
        self.loaded = False
        self.streamed = False
        self.freed_stream = False
        self.freed_ctx = False

    def abi_version(self):
        return self.abi

    def load(self, model_path):
        self.loaded = True
        return object()

    def stream_begin(self, ctx):
        self.streamed = True
        return object()

    def stream_feed(self, stream, samples):
        assert all(-1.0 <= x <= 1.0 for x in samples)
        return self.feeds.pop(0)

    def stream_finalize(self, stream):
        return ""

    def stream_free(self, stream):
        self.freed_stream = True

    def free(self, ctx):
        self.freed_ctx = True


def pcm16(*values):
    return struct.pack("<" + "h" * len(values), *values)


def test_parakeet_cpp_eou_and_eob_boundaries():
    async def go():
        api = FakeAPI(
            [
                ("OMP run", 0),
                (" tests", PARAKEET_EVENT_EOU),
                ("uh huh", PARAKEET_EVENT_EOB),
            ]
        )
        t = ParakeetCppEOUTranscriber(
            library_path="/fake/libparakeet.so",
            model_path="/fake/eou.gguf",
            api=api,
        )
        await t.start()
        ready = await t.recv()
        assert ready.kind == EventKind.TRANSCRIBER_READY

        frame = AudioFrame(
            pcm16(0, 1000, -1000, 32767, -32768),
            "pcm16",
            16000,
        )
        await t.send_audio(frame)
        delta1 = await asyncio.wait_for(t.recv(), 1)
        assert delta1.kind == EventKind.INPUT_TRANSCRIPT_DELTA
        assert delta1.payload["text"] == "OMP run"

        await t.send_audio(frame)
        delta2 = await asyncio.wait_for(t.recv(), 1)
        final = await asyncio.wait_for(t.recv(), 1)
        assert delta2.payload["text"] == "OMP run tests"
        assert final.kind == EventKind.INPUT_TRANSCRIPT_FINAL
        assert final.payload["text"] == "OMP run tests"
        assert final.payload["backchannel"] is False
        assert final.payload["boundary"] == "eou"

        await t.send_audio(frame)
        await asyncio.wait_for(t.recv(), 1)  # delta
        backchannel = await asyncio.wait_for(t.recv(), 1)
        assert backchannel.payload["backchannel"] is True
        assert backchannel.payload["boundary"] == "eob"

        await t.close()
        assert api.freed_stream is True
        assert api.freed_ctx is True

    asyncio.run(go())


def test_parakeet_cpp_mixed_boundary_fails_closed_as_backchannel():
    async def go():
        api = FakeAPI([("maybe", PARAKEET_EVENT_EOU | PARAKEET_EVENT_EOB)])
        t = ParakeetCppEOUTranscriber(
            library_path="/fake/lib.so",
            model_path="/fake/model.gguf",
            api=api,
        )
        await t.start()
        await t.recv()
        await t.send_audio(AudioFrame(pcm16(1, 2), "pcm16", 16000))
        await t.recv()
        final = await t.recv()
        assert final.payload["mixed_boundary"] is True
        assert final.payload["backchannel"] is True
        await t.close()

    asyncio.run(go())


def test_parakeet_cpp_rejects_pre_eou_abi():
    async def go():
        t = ParakeetCppEOUTranscriber(
            library_path="/fake/lib.so",
            model_path="/fake/model.gguf",
            api=FakeAPI(abi=4),
        )
        with pytest.raises(RuntimeError, match="requires >= 5"):
            await t.start()

    asyncio.run(go())
