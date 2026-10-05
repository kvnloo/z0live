from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from .contracts import EventKind, TimelineEvent


class FloorState(str, Enum):
    IDLE = "idle"
    USER = "user"
    ASSISTANT = "assistant"
    OVERLAP = "overlap"


class FloorAction(str, Enum):
    NONE = "none"
    INTERRUPT_ASSISTANT = "interrupt_assistant"
    KEEP_ASSISTANT = "keep_assistant"
    USER_TAKES_FLOOR = "user_takes_floor"
    ASSISTANT_TAKES_FLOOR = "assistant_takes_floor"
    FLOOR_IDLE = "floor_idle"


@dataclass(slots=True)
class FloorDecision:
    state: FloorState
    action: FloorAction
    reason: str


class FloorController:
    def __init__(self) -> None:
        self.user_speaking = False
        self.assistant_speaking = False

    @property
    def state(self) -> FloorState:
        if self.user_speaking and self.assistant_speaking:
            return FloorState.OVERLAP
        if self.user_speaking:
            return FloorState.USER
        if self.assistant_speaking:
            return FloorState.ASSISTANT
        return FloorState.IDLE

    def observe(self, event: TimelineEvent) -> FloorDecision:
        action = FloorAction.NONE
        reason = "no_floor_change"

        if event.kind == EventKind.USER_SPEECH_STARTED:
            was_assistant = self.assistant_speaking
            self.user_speaking = True
            if was_assistant:
                if bool(event.payload.get("backchannel")):
                    action = FloorAction.KEEP_ASSISTANT
                    reason = "user_backchannel"
                else:
                    action = FloorAction.INTERRUPT_ASSISTANT
                    reason = "user_barge_in"
            else:
                action = FloorAction.USER_TAKES_FLOOR
                reason = "user_started"

        elif event.kind == EventKind.USER_SPEECH_STOPPED:
            self.user_speaking = False
            if not self.assistant_speaking:
                action = FloorAction.FLOOR_IDLE
                reason = "user_stopped"

        elif event.kind == EventKind.ASSISTANT_SPEECH_STARTED:
            self.assistant_speaking = True
            action = FloorAction.ASSISTANT_TAKES_FLOOR
            reason = "assistant_started"

        elif event.kind in (EventKind.ASSISTANT_SPEECH_STOPPED, EventKind.PLAYBACK_SILENCE):
            self.assistant_speaking = False
            if not self.user_speaking:
                action = FloorAction.FLOOR_IDLE
                reason = "assistant_stopped"

        return FloorDecision(self.state, action, reason)
