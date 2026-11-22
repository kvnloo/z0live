"""Turn policy for two GPT-Live legs. All events run on the call's asyncio loop."""
from collections import deque
from dataclasses import dataclass, field
from enum import Enum
import re
from typing import Callable, Literal

Leg = Literal["primary", "guest"]
Owner = Literal["primary", "guest", "people"]  # "people": the people on the call are talking to each other
IDLE_SECONDS = 20.0
FOLLOW_UP_SECONDS = 20.0  # with people on the call, an unnamed request this soon after a bot's reply is to that bot
_WORD = r"[\w\u0b80-\u0bff]"
# A request or question in the words, not only a remark ("Get all of it onto my S25", "can you check"). Used only to keep
# an unnamed follow-up with the bot that just answered, when more than one person is on the call.
_REQUEST = re.compile(
    r"\?|(?:^|[.!;,]\s*|\b(?:and|so|now|then|also)\s+)(?:please\s+)?(?:get|make|put|install|check|show|tell|find|"
    r"send|give|run|start|stop|look|pull|open|fix|prove|try|ship|merge|deploy|update|keep|move|add|remove|delete|"
    r"write|read|can you|could you|would you|will you|i need you|i want you)\b", re.I)
_BACKCHANNEL = re.compile(r"(?:m+|h?m+|mm[ -]mm|uh|um|uh[ -]huh|m+[ -]?h+m+|yeah|yep|right|okay|ok|thanks|thank you|got it|sure)(?:[.!?, ]*)$", re.I)


class Address(Enum):
    GUEST = "guest"
    OTHER = "other"


class Addressing:
    def __init__(self, guest_names: tuple[str, ...]):
        others = ("hermes", "herms", "primary", "assistant", "gpt", "grok")
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


def request(text: str) -> bool:
    return bool(_REQUEST.search(text))


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
    owner: Owner = "primary"
    reason: str = "call_start"
    available: bool = False
    replied_at: float | None = None
    reply_began_at: float | None = None
    answers: dict[Leg, deque[Answer]] = field(default_factory=lambda: {"guest": deque(), "primary": deque()})
    guest_jobs: int = 0
    # How many people share the phone (call_brief.people). With more than one, a bot gets the floor only by name or
    # by a request right after its own reply; everything else is the people talking to each other ("people").
    people: int = 1
    # The floor owner's audible replies since it took the floor: [began, ended] (ended None while it plays).
    heard: list[list] = field(default_factory=list)
    addressed_at: float = float("-inf")  # when a caller turn last named a voice

    def change(self, owner: Owner, reason: str, now: float) -> None:
        if owner != self.owner:
            previous, self.owner = self.owner, owner
            self.reason, self.since = reason, now
            self.replied_at = self.reply_began_at = None
            self.heard = []
            self.trace("floor.changed", previous=previous, speaker=owner, reason=reason, since=now)

    def user_turn(self, address: Address | None, text: str, began: float, now: float, *, final: bool,
                  interrupted: bool = False) -> None:
        if address is Address.GUEST and self.available:
            self.change("guest", "guest_addressed", now)
            self.since, self.replied_at, self.reply_began_at = now, None, None  # a new question restarts the idle clock
            self.addressed_at = now
        elif address is Address.OTHER:
            self.change("primary", "redirected", now)
            self.addressed_at = now
        elif self.people > 1:
            # Both legs transcribe the same mic. A final that began before the latest address is the same utterance
            # heard by the other leg, which may miss the name ("Hey Tech", take 9): it never undoes that address.
            if final and substantive(text) and began > self.addressed_at and not self.follow_up(text, began):
                self.change("people", "people_talk", now)
        elif (final and substantive(text) and (interrupted or (
                self.replied_at is not None and self.reply_began_at is not None
                and began >= self.reply_began_at and not self.guest_jobs))):
            self.change("primary", "new_user_turn", now)

    def follow_up(self, text: str, began: float) -> bool:
        """With people on the call: an unnamed turn is the next request to the bot that holds the floor when it is a
        request that began while one of that bot's replies played or within FOLLOW_UP_SECONDS after one ended. A
        reply the bot began after the caller started (and the caller cut off) does not count against it (take 8)."""
        return self.owner != "people" and request(text) and any(
            start <= began and (end is None or began - end <= FOLLOW_UP_SECONDS) for start, end in self.heard)

    def reply_started(self, leg: Leg, now: float) -> None:
        if leg == self.owner:
            self.heard.append([now, None])

    def reply_finished(self, leg: Leg, now: float, began: float) -> None:
        if leg == self.owner:
            open_reply = next((r for r in reversed(self.heard) if r[1] is None), None)
            if open_reply:
                open_reply[1] = now
            else:
                self.heard.append([began, now])
            del self.heard[:-8]
        if leg == self.owner == "guest":
            self.replied_at, self.reply_began_at = now, began

    def expire(self, now: float) -> None:
        if (self.owner == "guest" and not self.guest_jobs and not self.answers["guest"]
                and now - (self.replied_at if self.replied_at is not None else self.since) >= IDLE_SECONDS):
            self.change("people" if self.people > 1 else "primary", "guest_idle", now)

    def answer_ready(self, pending: Callable[[Leg], bool], now: float) -> bool:
        """While the people talk, a bot with an answer to give takes the floor in their next pause (the caller asked
        it earlier). Returns whether the floor moved."""
        for leg in ("primary", "guest"):
            if self.answers[leg] or pending(leg):
                self.change(leg, "answer_ready", now)
                return True
        return False

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
