import asyncio
import time

from z0live.actors.fake import FakeActor
from z0live.contracts import (
    ActorMessage,
    EventKind,
    HarnessCommand,
    HarnessCommandKind,
    TimelineEvent,
)
from z0live.harnesses.fake import FakeHarness
from z0live.runtime import ConversationRuntime, RuntimeHooks
from z0live.transcribers.fake import FakeTranscriber


def test_background_harness_command_does_not_block_audio_loop():
    async def go():
        actor = FakeActor()
        harness = FakeHarness(delay_seconds=0.2)
        audio = []

        async def on_audio(frame):
            audio.append(frame.data)

        runtime = ConversationRuntime(
            actor,
            harness=harness,
            hooks=RuntimeHooks(on_audio=on_audio),
        )
        await runtime.start()
        task = runtime.dispatch_harness(
            HarnessCommand(
                HarnessCommandKind.SUBMIT,
                trace_id="t1",
                text="work",
            )
        )
        await actor.emit_audio(b"now")
        await asyncio.sleep(0.01)
        assert audio == [b"now"]
        assert not task.done()
        await task
        await runtime.close()

    asyncio.run(go())


def test_verified_result_is_injected_as_speakable_commentary():
    async def go():
        actor = FakeActor()
        harness = FakeHarness()
        runtime = ConversationRuntime(actor, harness=harness)
        await runtime.start()
        await harness.emit(
            EventKind.HARNESS_RESULT,
            trace_id="t",
            text="tests passed",
            verified=True,
        )
        deadline = time.monotonic() + 1
        while not actor.context and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert actor.context
        assert actor.context[0].speakable is True
        assert actor.context[0].text == "tests passed"
        await runtime.close()

    asyncio.run(go())


def test_runtime_normalizes_actor_and_harness_events_to_one_clock():
    async def go():
        actor = FakeActor()
        seen = []

        async def on_event(event):
            seen.append(event)

        runtime = ConversationRuntime(
            actor,
            hooks=RuntimeHooks(on_event=on_event),
        )
        await runtime.start()
        await actor._messages.put(
            ActorMessage(
                event=TimelineEvent(
                    kind=EventKind.USER_SPEECH_STARTED,
                    source="actor",
                    at_ms=999999,
                    payload={},
                )
            )
        )
        deadline = time.monotonic() + 1
        while not any(e.kind == EventKind.USER_SPEECH_STARTED for e in seen) and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        event = next(e for e in seen if e.kind == EventKind.USER_SPEECH_STARTED)
        assert event.at_ms < 1000
        assert event.payload["source_at_ms"] == 999999
        await runtime.close()

    asyncio.run(go())


def test_transcriber_final_reaches_harness_as_authority_observation():
    async def go():
        actor = FakeActor()
        transcriber = FakeTranscriber()
        harness = FakeHarness()
        runtime = ConversationRuntime(
            actor,
            transcriber=transcriber,
            harness=harness,
        )
        await runtime.start()

        await runtime.client_event(
            TimelineEvent(
                kind=EventKind.USER_SPEECH_STARTED,
                source="web",
                trace_id="voice-1",
            )
        )
        await transcriber.emit_partial("OMP run")
        await transcriber.emit_final("OMP run tests")

        deadline = time.monotonic() + 1
        while (
            not any(
                e.kind == EventKind.INPUT_TRANSCRIPT_FINAL
                for e in harness.observations
            )
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)

        final = next(
            e
            for e in harness.observations
            if e.kind == EventKind.INPUT_TRANSCRIPT_FINAL
        )
        assert final.trace_id == "voice-1"
        assert final.payload["authority"] is True

        authority = [
            e
            for e in harness.observations
            if e.kind == EventKind.MARKER
            and e.payload.get("action") == "authority"
        ]
        assert authority
        assert authority[0].payload["mutation_allowed"] is True

        await runtime.close()

    asyncio.run(go())


def test_backchannel_never_grants_harness_authority():
    async def go():
        actor = FakeActor()
        transcriber = FakeTranscriber()
        harness = FakeHarness()
        runtime = ConversationRuntime(
            actor,
            transcriber=transcriber,
            harness=harness,
        )
        await runtime.start()
        await transcriber.emit_final(
            "uh huh",
            trace_id="voice-bc",
            backchannel=True,
        )

        deadline = time.monotonic() + 1
        while (
            not any(
                e.kind == EventKind.INPUT_TRANSCRIPT_FINAL
                for e in harness.observations
            )
            and time.monotonic() < deadline
        ):
            await asyncio.sleep(0.01)

        final = next(
            e
            for e in harness.observations
            if e.kind == EventKind.INPUT_TRANSCRIPT_FINAL
        )
        assert final.payload["authority"] is False
        assert not any(
            e.kind == EventKind.MARKER
            and e.payload.get("mutation_allowed") is True
            for e in harness.observations
        )
        await runtime.close()

    asyncio.run(go())
