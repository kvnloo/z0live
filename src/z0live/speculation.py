from __future__ import annotations

import hashlib
import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Iterable


class SpeculationAction(str, Enum):
    NONE = "none"
    PREWARM = "prewarm"
    CANCEL = "cancel"
    AUTHORITY = "authority"


@dataclass(frozen=True, slots=True)
class SpeculationDecision:
    action: SpeculationAction
    trace_id: str
    text: str
    mutation_allowed: bool
    reason: str
    ticket_id: str | None = None


@dataclass(slots=True)
class _Ticket:
    ticket_id: str
    trace_id: str
    partial: str


def _norm(text: str) -> str:
    return " ".join(text.lower().strip().split())


class SpeculationGate:
    """Side-effect-free prewarm gate for partial voice transcripts.

    z0live may identify that preparation is useful, but partial speech never
    grants mutation authority. The final transcript is a separate authority
    event consumed by the harness/control plane.
    """

    def __init__(self, *, min_chars: int = 12) -> None:
        self.min_chars = max(1, int(min_chars))
        self._tickets: dict[str, _Ticket] = {}

    def partial(self, trace_id: str, text: str) -> SpeculationDecision:
        normalized = _norm(text)
        if len(normalized) < self.min_chars:
            return SpeculationDecision(
                SpeculationAction.NONE,
                trace_id,
                text,
                False,
                "partial_too_short",
            )
        previous = self._tickets.get(trace_id)
        if previous is not None and _norm(previous.partial) == normalized:
            return SpeculationDecision(
                SpeculationAction.NONE,
                trace_id,
                text,
                False,
                "duplicate_partial",
                previous.ticket_id,
            )
        digest = hashlib.sha256(
            f"{trace_id}\0{normalized}".encode("utf-8")
        ).hexdigest()[:16]
        ticket = _Ticket(f"pw_{digest}", trace_id, text)
        self._tickets[trace_id] = ticket
        return SpeculationDecision(
            SpeculationAction.PREWARM,
            trace_id,
            text,
            False,
            "eligible_partial",
            ticket.ticket_id,
        )

    def final(self, trace_id: str, text: str) -> tuple[SpeculationDecision, ...]:
        ticket = self._tickets.pop(trace_id, None)
        out: list[SpeculationDecision] = []
        normalized = _norm(text)
        if ticket is not None:
            partial = _norm(ticket.partial)
            # A large revision invalidates prepared work. Prefix-compatible
            # completions may reuse it, but authority still arrives only now.
            if not normalized.startswith(partial):
                out.append(
                    SpeculationDecision(
                        SpeculationAction.CANCEL,
                        trace_id,
                        text,
                        False,
                        "final_diverged_from_partial",
                        ticket.ticket_id,
                    )
                )
        out.append(
            SpeculationDecision(
                SpeculationAction.AUTHORITY,
                trace_id,
                text,
                True,
                "final_transcript",
                ticket.ticket_id if ticket is not None else None,
            )
        )
        return tuple(out)

    def cancel(self, trace_id: str, reason: str = "cancelled") -> SpeculationDecision:
        ticket = self._tickets.pop(trace_id, None)
        return SpeculationDecision(
            SpeculationAction.CANCEL,
            trace_id,
            ticket.partial if ticket is not None else "",
            False,
            reason,
            ticket.ticket_id if ticket is not None else None,
        )


@dataclass(frozen=True, slots=True)
class SpeculationMeasurement:
    trace_id: str
    authority_ms: float
    baseline_worker_ready_ms: float
    prewarm_worker_ready_ms: float
    prewarm_used: bool
    speculative_mutations: int = 0
    peak_memory_mb: float | None = None

    @property
    def useful_gain_ms(self) -> float:
        return self.baseline_worker_ready_ms - self.prewarm_worker_ready_ms


def evaluate_speculation(
    rows: Iterable[SpeculationMeasurement],
    *,
    min_p50_gain_ms: float = 50.0,
) -> dict:
    samples = list(rows)
    gains = [row.useful_gain_ms for row in samples if row.prewarm_used]
    mutations = sum(max(0, row.speculative_mutations) for row in samples)
    wasted = sum(1 for row in samples if not row.prewarm_used)
    p50 = statistics.median(gains) if gains else None
    peak_memory = max(
        (row.peak_memory_mb for row in samples if row.peak_memory_mb is not None),
        default=None,
    )
    return {
        "schema": "z0live.speculation_eval.v1",
        "n": len(samples),
        "used_n": len(gains),
        "wasted_n": wasted,
        "waste_rate": (wasted / len(samples)) if samples else None,
        "p50_useful_gain_ms": p50,
        "speculative_mutations": mutations,
        "peak_memory_mb": peak_memory,
        "pass": bool(gains) and mutations == 0 and p50 is not None and p50 >= min_p50_gain_ms,
        "gate": {
            "min_p50_useful_gain_ms": min_p50_gain_ms,
            "required_speculative_mutations": 0,
        },
    }
