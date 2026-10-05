from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contracts import EventKind, TimelineEvent


class AttentionAction(str, Enum):
    DROP = "drop"
    SILENT = "silent"
    SPEAK = "speak"
    INTERRUPT = "interrupt"


@dataclass(slots=True)
class AttentionDecision:
    action: AttentionAction
    text: str | None
    reason: str


class AttentionPolicy:
    def decide(self, event: TimelineEvent) -> AttentionDecision:
        payload = event.payload
        if payload.get("stale") or payload.get("superseded"):
            return AttentionDecision(AttentionAction.DROP, None, "stale_or_superseded")

        if event.kind == EventKind.HARNESS_APPROVAL:
            text = str(payload.get("text") or payload.get("question") or "Approval required")
            return AttentionDecision(AttentionAction.INTERRUPT, text, "blocking_approval")

        if event.kind == EventKind.HARNESS_PROGRESS:
            text = str(payload.get("text") or "") or None
            return AttentionDecision(AttentionAction.SILENT, text, "progress_is_quiet")

        if event.kind == EventKind.HARNESS_RESULT:
            verified = payload.get("verified") is True
            if not verified:
                text = str(payload.get("text") or "") or None
                return AttentionDecision(AttentionAction.SILENT, text, "unverified_result")
            text = str(payload.get("text") or payload.get("result") or "") or None
            if text:
                return AttentionDecision(AttentionAction.SPEAK, text, "verified_result")
            return AttentionDecision(AttentionAction.SILENT, None, "verified_result_without_text")

        if event.kind == EventKind.HARNESS_ERROR:
            text = str(payload.get("text") or payload.get("error") or "Background work failed")
            return AttentionDecision(AttentionAction.SPEAK, text, "harness_error")

        return AttentionDecision(AttentionAction.SILENT, None, "not_attention_worthy")
