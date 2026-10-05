from z0live.contracts import EventKind, TimelineEvent
from z0live.metrics import TimelineMetrics


def event(kind, at, *, trace=None, **payload):
    return TimelineEvent(
        kind=kind,
        source="test",
        at_ms=at,
        trace_id=trace,
        payload=payload,
    )


def test_latency_dimensions_stay_separate():
    m = TimelineMetrics()
    for ev in [
        event(EventKind.USER_SPEECH_STOPPED, 100),
        event(EventKind.ASSISTANT_SPEECH_STARTED, 180),
        event(EventKind.USER_SPEECH_STARTED, 250),
        event(EventKind.PLAYBACK_SILENCE, 290),
        event(EventKind.INPUT_TRANSCRIPT_FINAL, 400, trace="t"),
        event(EventKind.HARNESS_PROGRESS, 520, trace="t", phase="worker_ready"),
    ]:
        m.observe(ev)

    out = m.summary()["series"]
    assert out["turn_gap"]["p50_ms"] == 80
    assert out["interrupt_to_silence"]["p50_ms"] == 40
    assert out["authority_to_worker_ready"]["p50_ms"] == 120


def test_reference_bands_are_not_reported_as_pass_fail_gates():
    out = TimelineMetrics().summary()
    assert "reference_bands_ms" in out
    assert "pass" not in out
