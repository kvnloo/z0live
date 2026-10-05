from __future__ import annotations

from dataclasses import dataclass

from .attention import AttentionPolicy
from .contracts import EventKind
from .fixtures import FixtureCase, FixtureCorpus
from .floor import FloorAction, FloorController


@dataclass(slots=True)
class ReplayCaseResult:
    fixture_id: str
    family: str
    passed: bool
    expected: list[str]
    observed: list[str]


@dataclass(slots=True)
class ReplayResult:
    revision: str
    fixture_sha256: str
    passed: bool
    cases: list[ReplayCaseResult]

    def to_dict(self) -> dict:
        return {
            "schema": "z0live.replay_result.v1",
            "revision": self.revision,
            "fixture_sha256": self.fixture_sha256,
            "passed": self.passed,
            "cases": [
                {
                    "fixture_id": c.fixture_id,
                    "family": c.family,
                    "passed": c.passed,
                    "expected": c.expected,
                    "observed": c.observed,
                }
                for c in self.cases
            ],
        }


def replay_case(case: FixtureCase) -> ReplayCaseResult:
    floor = FloorController()
    attention = AttentionPolicy()
    observed: list[str] = []
    for event in sorted(case.events, key=lambda e: e.at_ms):
        if event.kind in {
            EventKind.USER_SPEECH_STARTED,
            EventKind.USER_SPEECH_STOPPED,
            EventKind.ASSISTANT_SPEECH_STARTED,
            EventKind.ASSISTANT_SPEECH_STOPPED,
            EventKind.PLAYBACK_SILENCE,
        }:
            action = floor.observe(event).action
            if action != FloorAction.NONE:
                observed.append(action.value)
        if event.kind in {
            EventKind.HARNESS_PROGRESS,
            EventKind.HARNESS_RESULT,
            EventKind.HARNESS_APPROVAL,
            EventKind.HARNESS_ERROR,
        }:
            observed.append(attention.decide(event).action.value)
    expected = list(case.expected_actions)
    return ReplayCaseResult(
        case.fixture_id,
        case.family,
        observed == expected,
        expected,
        observed,
    )


def replay_corpus(corpus: FixtureCorpus) -> ReplayResult:
    cases = [replay_case(case) for case in corpus.cases]
    return ReplayResult(
        revision=corpus.revision,
        fixture_sha256=corpus.sha256,
        passed=all(c.passed for c in cases),
        cases=cases,
    )
