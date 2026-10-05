from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .speculation import SpeculationAction, SpeculationGate


@dataclass(frozen=True, slots=True)
class SpeculationFixtureCase:
    fixture_id: str
    trace_id: str
    partials: tuple[str, ...]
    final: str
    expected_actions: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SpeculationFixtureCorpus:
    revision: str
    min_chars: int
    cases: tuple[SpeculationFixtureCase, ...]
    sha256: str


def load_speculation_corpus(path: str | Path) -> SpeculationFixtureCorpus:
    p = Path(path)
    data = p.read_bytes()
    raw = json.loads(data)
    if raw.get("schema") != "z0live.speculation_fixtures.v1":
        raise ValueError("unsupported speculation fixture schema")
    cases = tuple(
        SpeculationFixtureCase(
            fixture_id=str(case["id"]),
            trace_id=str(case.get("trace_id") or case["id"]),
            partials=tuple(str(x) for x in case.get("partials") or []),
            final=str(case["final"]),
            expected_actions=tuple(str(x) for x in case.get("expected_actions") or []),
        )
        for case in raw.get("cases") or []
    )
    return SpeculationFixtureCorpus(
        revision=str(raw.get("revision") or "unknown"),
        min_chars=int(raw.get("min_chars") or 12),
        cases=cases,
        sha256=hashlib.sha256(data).hexdigest(),
    )


def replay_speculation_case(
    case: SpeculationFixtureCase,
    *,
    min_chars: int,
) -> dict[str, Any]:
    gate = SpeculationGate(min_chars=min_chars)
    observed: list[str] = []
    unauthorized_mutations = 0

    for partial in case.partials:
        decision = gate.partial(case.trace_id, partial)
        observed.append(decision.action.value)
        if decision.action != SpeculationAction.AUTHORITY and decision.mutation_allowed:
            unauthorized_mutations += 1

    for decision in gate.final(case.trace_id, case.final):
        observed.append(decision.action.value)
        if decision.action != SpeculationAction.AUTHORITY and decision.mutation_allowed:
            unauthorized_mutations += 1

    expected = list(case.expected_actions)
    return {
        "fixture_id": case.fixture_id,
        "passed": observed == expected and unauthorized_mutations == 0,
        "expected": expected,
        "observed": observed,
        "unauthorized_mutations": unauthorized_mutations,
    }


def replay_speculation_corpus(corpus: SpeculationFixtureCorpus) -> dict[str, Any]:
    cases = [
        replay_speculation_case(case, min_chars=corpus.min_chars)
        for case in corpus.cases
    ]
    unauthorized = sum(int(case["unauthorized_mutations"]) for case in cases)
    return {
        "schema": "z0live.speculation_replay_result.v1",
        "revision": corpus.revision,
        "fixture_sha256": corpus.sha256,
        "passed": bool(cases) and all(bool(case["passed"]) for case in cases) and unauthorized == 0,
        "cases_total": len(cases),
        "cases_passed": sum(1 for case in cases if case["passed"]),
        "unauthorized_mutations": unauthorized,
        "cases": cases,
    }
