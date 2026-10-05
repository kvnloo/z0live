from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .contracts import TimelineEvent


@dataclass(frozen=True, slots=True)
class FixtureCase:
    fixture_id: str
    family: str
    events: tuple[TimelineEvent, ...]
    expected_actions: tuple[str, ...]
    metadata: dict[str, Any]


@dataclass(frozen=True, slots=True)
class FixtureCorpus:
    schema: str
    revision: str
    cases: tuple[FixtureCase, ...]
    sha256: str


def load_corpus(path: str | Path) -> FixtureCorpus:
    p = Path(path)
    data = p.read_bytes()
    raw = json.loads(data)
    cases = []
    for case in raw.get("cases") or []:
        cases.append(
            FixtureCase(
                fixture_id=str(case["id"]),
                family=str(case["family"]),
                events=tuple(
                    TimelineEvent.from_dict(e)
                    for e in case.get("events") or []
                ),
                expected_actions=tuple(
                    str(x) for x in case.get("expected_actions") or []
                ),
                metadata=dict(case.get("metadata") or {}),
            )
        )
    return FixtureCorpus(
        schema=str(raw.get("schema") or "z0live.fixtures.v1"),
        revision=str(raw.get("revision") or "unknown"),
        cases=tuple(cases),
        sha256=hashlib.sha256(data).hexdigest(),
    )
