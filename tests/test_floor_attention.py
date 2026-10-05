from z0live.attention import AttentionAction, AttentionPolicy
from z0live.contracts import EventKind, TimelineEvent
from z0live.floor import FloorAction, FloorController


def ev(kind, **payload):
    return TimelineEvent(kind=kind, source="test", payload=payload)


def test_barge_interrupts_but_backchannel_does_not():
    floor = FloorController()
    floor.observe(ev(EventKind.ASSISTANT_SPEECH_STARTED))
    assert (
        floor.observe(ev(EventKind.USER_SPEECH_STARTED)).action
        == FloorAction.INTERRUPT_ASSISTANT
    )

    floor = FloorController()
    floor.observe(ev(EventKind.ASSISTANT_SPEECH_STARTED))
    assert (
        floor.observe(
            ev(EventKind.USER_SPEECH_STARTED, backchannel=True)
        ).action
        == FloorAction.KEEP_ASSISTANT
    )


def test_attention_only_speaks_verified_current_results():
    policy = AttentionPolicy()
    assert (
        policy.decide(
            ev(EventKind.HARNESS_PROGRESS, text="halfway")
        ).action
        == AttentionAction.SILENT
    )
    assert (
        policy.decide(
            ev(
                EventKind.HARNESS_RESULT,
                text="done",
                verified=False,
            )
        ).action
        == AttentionAction.SILENT
    )
    assert (
        policy.decide(
            ev(
                EventKind.HARNESS_RESULT,
                text="done",
                verified=True,
            )
        ).action
        == AttentionAction.SPEAK
    )
    assert (
        policy.decide(
            ev(
                EventKind.HARNESS_RESULT,
                text="old",
                verified=True,
                stale=True,
            )
        ).action
        == AttentionAction.DROP
    )
    assert (
        policy.decide(
            ev(EventKind.HARNESS_APPROVAL, text="approve?")
        ).action
        == AttentionAction.INTERRUPT
    )
