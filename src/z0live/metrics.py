from __future__ import annotations

import math
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Iterable

from .contracts import EventKind, TimelineEvent


def _percentile(values: list[float], q: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if len(ordered) == 1:
        return ordered[0]
    pos = (len(ordered) - 1) * q
    lo = math.floor(pos)
    hi = math.ceil(pos)
    if lo == hi:
        return ordered[lo]
    weight = pos - lo
    return ordered[lo] * (1.0 - weight) + ordered[hi] * weight


@dataclass(slots=True)
class MetricSeries:
    values: list[float] = field(default_factory=list)

    def add(self, value: float) -> None:
        if value >= 0:
            self.values.append(float(value))

    def summary(self) -> dict:
        return {
            "n": len(self.values),
            "p50_ms": statistics.median(self.values) if self.values else None,
            "p95_ms": _percentile(self.values, 0.95),
            "p99_ms": _percentile(self.values, 0.99),
            "max_ms": max(self.values) if self.values else None,
        }


class TimelineMetrics:
    """Reduce canonical timeline events into independent latency dimensions."""

    def __init__(self) -> None:
        self.series: dict[str, MetricSeries] = defaultdict(MetricSeries)
        self._user_stop_ms: int | None = None
        self._interrupt_ms: int | None = None
        self._authority_ms: dict[str, int] = {}
        self._assistant_speaking = False

    def observe(self, event: TimelineEvent) -> None:
        at = int(event.at_ms)
        playback_delay = event.payload.get("playback_delay_ms", 0)
        if (
            event.kind in (
                EventKind.ASSISTANT_SPEECH_STARTED,
                EventKind.ASSISTANT_SPEECH_STOPPED,
                EventKind.PLAYBACK_SILENCE,
            )
            and isinstance(playback_delay, (int, float))
        ):
            at += max(0, int(playback_delay))

        if event.kind == EventKind.USER_SPEECH_STOPPED:
            self._user_stop_ms = at

        elif event.kind == EventKind.ASSISTANT_SPEECH_STARTED:
            self._assistant_speaking = True
            if self._user_stop_ms is not None:
                self.series["turn_gap"].add(at - self._user_stop_ms)
                self._user_stop_ms = None

        elif event.kind == EventKind.USER_SPEECH_STARTED:
            if self._assistant_speaking and not bool(event.payload.get("backchannel")):
                self._interrupt_ms = at

        elif event.kind in (EventKind.ASSISTANT_SPEECH_STOPPED, EventKind.PLAYBACK_SILENCE):
            self._assistant_speaking = False
            if self._interrupt_ms is not None:
                self.series["interrupt_to_silence"].add(at - self._interrupt_ms)
                self._interrupt_ms = None

        elif event.kind == EventKind.INPUT_TRANSCRIPT_FINAL:
            key = event.trace_id or event.task_id or "_default"
            self._authority_ms[key] = at

        elif event.kind == EventKind.HARNESS_PROGRESS:
            if event.payload.get("phase") == "worker_ready":
                key = event.trace_id or event.task_id or "_default"
                authority = self._authority_ms.pop(key, None)
                if authority is not None:
                    self.series["authority_to_worker_ready"].add(at - authority)

        if event.kind == EventKind.MARKER:
            metric = event.payload.get("metric")
            value = event.payload.get("value_ms")
            if isinstance(metric, str) and isinstance(value, (int, float)):
                self.series[metric].add(float(value))

    def summary(self) -> dict:
        return {
            "schema": "z0live.timeline_metrics.v1",
            "series": {
                name: values.summary()
                for name, values in sorted(self.series.items())
            },
            "reference_bands_ms": {
                "media_reflex": [10, 80],
                "interaction_policy": [80, 500],
                "cognition_agency": [200, None],
            },
            "note": (
                "Reference bands are research targets, not certification gates. "
                "z0evals must freeze any pass/fail threshold before measured comparison."
            ),
        }


def summarize_events(events: Iterable[TimelineEvent]) -> dict:
    tracker = TimelineMetrics()
    for event in sorted(events, key=lambda e: e.at_ms):
        tracker.observe(event)
    return tracker.summary()
