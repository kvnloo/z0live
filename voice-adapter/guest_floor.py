"""Turn policy for two GPT-Live legs. All events run on the call's asyncio loop."""
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Callable, Literal

Leg = Literal["primary", "guest"]
IDLE_SECONDS = 20.0
_WORD = r"[\w\u0b80-\u0bff]"
_BACKCHANNEL = re.compile(r"(?:m+|h?m+|mm[ -]mm|uh|um|uh[ -]huh|m+[ -]?h+m+|yeah|yep|right|okay|ok|thanks|thank you|got it|sure)(?:[.!?, ]*)$", re.I)


class Address(Enum):
    GUEST = "guest"
    OTHER = "other"


class Addressing:
    def __init__(self, guest_names: tuple[str, ...]):
        others = ("hermes", "primary", "assistant", "gpt", "grok")
        names = "|".join(map(re.escape, (*guest_names, *others)))
        self.guest_names = frozenset(name.casefold() for name in guest_names)
        self.known = self.guest_names | frozenset(others)
        self.vocative = re.compile(
            rf"(?:^\s*|(?:[.!?;,]|[—–])\s*|(?<!{_WORD})(?:hey|hi|hello|ok|okay)\s*,?\s+)"
            rf"(?P<name>{names})(?!{_WORD})(?=\s*[,!:?—–.]|\s+(?:what|why|how|can|could|would|do|did|tell|please)\b|\s*$)",
            re.I,
        )
        self.handoff = re.compile(r"\b(?:over to|back to|let's hear from)\s+([\w\u0b80-\u0bff]+)", re.I)

    def __call__(self, text: str, *, final: bool) -> Address | None:
        matches = [match for match in self.vocative.finditer(text)
                   if final or text[match.end():].strip()]
        # "Back to X" names a voice only when X is a known name ("back to you" and "back to the budget" do not),
        # and it never overrides a direct address to the guest in the same turn ("Tek, back to you").
        if final and not any(match.group(1).casefold() in self.guest_names for match in matches):
            matches.extend(match for match in self.handoff.finditer(text) if match.group(1).casefold() in self.known)
        if matches:
            name = max(matches, key=lambda match: match.start()).group(1).casefold()
            return Address.GUEST if name in self.guest_names else Address.OTHER
        return None

    def own_words(self, text: str, *, guest: bool, lead: bool) -> str:
        """The words of one caller turn that were spoken to this voice. A voice that stays silent keeps one turn
        open across exchanges ("Tek, what did you work on today ... Thanks, Tek. Hermes, back to you"), so each
        named voice owns the words from its name to the next name; `lead` says whether the words before any name
        are this voice's (it held the floor when the turn began)."""
        marks = [(m.start("name"), m.group("name").casefold() in self.guest_names)
                 for m in self.vocative.finditer(text)]
        marks += [(m.start(1), m.group(1).casefold() in self.guest_names) for m in self.handoff.finditer(text)
                  if m.group(1).casefold() in self.known]
        marks.sort()
        bounds = [(0, lead)] + [(at, to_guest == guest) for at, to_guest in marks]
        ends = [at for at, _ in bounds[1:]] + [len(text)]
        return "".join(text[at:end] for (at, mine), end in zip(bounds, ends) if mine).strip()


def substantive(text: str) -> bool:
    text = text.strip(" \t,.!?…—-\"'")
    return bool(text and not _BACKCHANNEL.fullmatch(text))


@dataclass(frozen=True)
class Answer:
    leg: Leg
    delegation_id: str | None
    text: str
    lane: bool = False  # a fast lane's guess; Hermes's own answer to the same delegation supersedes it


@dataclass
class Floor:
    since: float
    trace: Callable[..., None] = field(repr=False)
    owner: Leg = "primary"
    reason: str = "call_start"
    available: bool = False
    replied_at: float | None = None
    reply_began_at: float | None = None
    answers: dict[Leg, deque[Answer]] = field(default_factory=lambda: {"guest": deque(), "primary": deque()})
    guest_jobs: int = 0

    def change(self, owner: Leg, reason: str, now: float) -> None:
        if owner != self.owner:
            previous, self.owner = self.owner, owner
            self.reason, self.since = reason, now
            self.replied_at = self.reply_began_at = None
            self.trace("floor.changed", previous=previous, speaker=owner, reason=reason, since=now)

    def user_turn(self, address: Address | None, text: str, began: float, now: float, *, final: bool,
                  interrupted: bool = False) -> None:
        if address is Address.GUEST and self.available:
            self.change("guest", "guest_addressed", now)
            self.since, self.replied_at, self.reply_began_at = now, None, None  # a new question restarts the idle clock
        elif address is Address.OTHER:
            self.change("primary", "redirected", now)
        elif (final and substantive(text) and (interrupted or (
                self.replied_at is not None and self.reply_began_at is not None
                and began >= self.reply_began_at and not self.guest_jobs))):
            self.change("primary", "new_user_turn", now)

    def reply_finished(self, leg: Leg, now: float, began: float) -> None:
        if leg == self.owner == "guest":
            self.replied_at, self.reply_began_at = now, began

    def expire(self, now: float) -> None:
        if (self.owner == "guest" and not self.guest_jobs and not self.answers["guest"]
                and now - (self.replied_at if self.replied_at is not None else self.since) >= IDLE_SECONDS):
            self.change("primary", "guest_idle", now)

    def failed(self, name: str, now: float) -> None:
        self.available = False
        self.change("primary", "guest_failed", now)
        self.answers["primary"].appendleft(Answer("primary", None, f"{name} disconnected. I can keep going with you."))
        while self.answers["guest"]:
            self.enqueue(self.answers["guest"].popleft(), name)

    def enqueue(self, answer: Answer, name: str) -> None:
        if answer.leg == "guest" and not self.available:
            answer = Answer("primary", None, f"{name}'s Hermes returned this answer: {answer.text}")
        queue = self.answers[answer.leg]
        if not answer.lane:
            for guess in [a for a in queue if a.lane and a.delegation_id == answer.delegation_id]:
                queue.remove(guess)
                self.trace("floor.answer_dropped", speaker=guess.leg, did=guess.delegation_id, reason="superseded")
        queue.append(answer)
        self.trace("floor.answer_queued", speaker=answer.leg, did=answer.delegation_id, owner=self.owner)
