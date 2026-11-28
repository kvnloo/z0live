"""F5/F7 input boundaries: which of the owner's speech segments may start a Hermes run.

The provider's VAD cuts a 10-20 s thought into segments, and GPT-Live delegates some of them alone: 41 of 265
delegations (15%) in the cos-1128 L1 audit were <=5 words ("NPC" spawned 3 conductor lanes, "Yeah" in 112025 started
a run of its own). This module is pure (no I/O, injectable clock) and decides at the boundary, before any route
(host run, conductor lanes, guest Hermes, lane relay) sees the words:

- Segments coalesce: Protocol.asked already stitches the caller's words since the voice last answered or delegated.
  A chain of segments stays one request while each follows the last within COALESCE_SECONDS; after a longer quiet
  the old words are stale and never join a new ask. A complete ask is never delayed: the gate only holds fragments.
- A fragment (<= FRAGMENT_WORDS words) never runs alone. It is held in the stitched words (the next segment within
  the window carries it), unless it answers the voice's pending question ("Want me to check the studio?" / "Yes
  please"): then it runs with the question attached, so Hermes knows what "yes" means. A short imperative or
  question ("check disk space", "restart the lanes") is a complete ask and runs at once.
- A pure backchannel from the voice ("Mm-hm.") inside the owner's open thought is not an exchange: it neither ends his
  thought nor enters the call's exchange history (handoff, topic chats).
"""
from __future__ import annotations

from dataclasses import dataclass
import re
import time
from typing import Callable

COALESCE_SECONDS = 30.0  # segments closer than this are one thought (cos-1128 F7: ~30 s window)
FRAGMENT_WORDS = 3  # <= this many words is a fragment unless it is a complete short ask
PENDING_SECONDS = 60.0  # how long the voice's question stays open for a short answer

_WORD = re.compile(r"[\w']+")
# Filler and acknowledgements: never a request on their own, never "a thought".
_FILLER = {"mm", "mhm", "mmhm", "mm-hm", "hmm", "hm", "uh", "um", "uhhuh", "uh-huh", "yeah", "yep", "yes", "yup",
           "no", "nope", "nah", "right", "okay", "ok", "sure", "so", "and", "but", "well", "oh", "ah", "like",
           "hello", "hi", "hey", "thanks", "thank", "you", "please", "cool", "great", "nice", "got", "it", "alright",
           "the", "a", "that", "this", "just", "i", "mean", "wait", "huh", "what"}
_BACKCHANNEL = re.compile(r"^\W*(?:(?:m+|h?m+|mm[ -]?h?mm?|m+[ -]?h+m+|uh[ -]?huh|uh|um|yeah|yep|yes|right|okay|ok|"
                          r"sure|got it|i see|alright|cool|great|nice|thanks|thank you|exactly|totally|true)\W*)+$", re.I)
# A short ask that stands on its own: an imperative or a question lead, with at least one more word.
_SHORT_ASK = re.compile(
    r"^\W*(?:(?:please|hey|okay|ok|so|and|now)\W+)*(?:[\w']+,\s*)?(?:check|show|open|list|find|read|run|restart|retry|rerun|stop|"
    r"cancel|kill|pause|resume|add|remove|ask|call|play|summari[sz]e|explain|deploy|start|fix|build|search|look|"
    r"status|what|what's|whats|who|who's|where|where's|when|why|how|which|is|are|did|does|do|can|could|will|would)"
    r"\W+\w", re.I)
_QUESTION_END = re.compile(r"\?[\"')\]]*\s*$")


def words(text: str) -> list[str]:
    return _WORD.findall((text or "").replace("\u2019", "'").casefold())


def backchannel(text: str) -> bool:
    """Only an acknowledgement ("Mm-hm.", "Yeah, right.", "Okay"): no content of its own."""
    text = (text or "").replace("\u2019", "'").strip()
    return bool(text) and bool(_BACKCHANNEL.match(text))


def fragment(text: str) -> bool:
    """<= FRAGMENT_WORDS words and not a complete short ask (or only filler at any length up to that)."""
    found = words(text)
    if not found:
        return True
    if len(found) > FRAGMENT_WORDS:
        return False
    return not complete_short_ask(text)


def complete_short_ask(text: str) -> bool:
    found = words(text)
    return (2 <= len(found) <= FRAGMENT_WORDS and bool(_SHORT_ASK.match(text or ""))
            and not all(w in _FILLER for w in found))


def asks_question(text: str) -> bool:
    """The voice's turn asks something in its last two sentences, so a short answer may follow (151433 @1427 s:
    "...Want to clear the memory ones first? They're quicker." / "Yeah, go ahead")."""
    sentences = [s for s in re.split(r"(?<=[.!?])[\"')\]]*\s+", (text or "").strip()) if s.strip()]
    return any(_QUESTION_END.search(s) for s in sentences[-2:])


@dataclass(frozen=True)
class Verdict:
    run: bool
    request: str
    reason: str  # "ask" | "short_ask" | "answer" | "fragment"


class Gate:
    """One leg's input boundary state: when the owner last spoke, and the voice's open question."""

    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self.clock = clock
        self.user_at: float | None = None  # last finished segment of the owner's on this leg
        self.question: tuple[float, str] | None = None  # (when, the voice's question) still open
        self.answered: tuple[float, str, str] | None = None  # (when, short answer, its question): idempotent re-check
        self.held = 0  # fragments that did not run
        self.backchannels = 0  # voice backchannels inside the owner's thought, kept out of the exchanges

    def thought_open(self) -> bool:
        """the owner's thought is still going: his last segment is within the coalesce window."""
        return self.user_at is not None and self.clock() - self.user_at < COALESCE_SECONDS

    def user(self, said: str) -> bool:
        """A finished segment of the owner's. True when the stitched words before it are stale (quiet past the window),
        so the caller drops them before adding this one."""
        now = self.clock()
        stale = self.user_at is not None and now - self.user_at >= COALESCE_SECONDS
        self.user_at = now
        if self.question and not fragment(said):  # a full sentence moved on from the question
            self.question = None
        return stale

    def assistant(self, said: str) -> bool:
        """A finished turn of the voice's. True when it is a backchannel inside the owner's open thought (not an
        exchange: it neither answers nor ends his words)."""
        if backchannel(said):
            if self.thought_open():
                self.backchannels += 1
                return True
            return False
        if asks_question(said):  # other turns (a status line, "Pulling them up.") leave an open question open
            self.question = (self.clock(), " ".join(said.split())[-300:])
        return False

    def pending_question(self) -> str:
        if self.question and self.clock() - self.question[0] < PENDING_SECONDS:
            return self.question[1]
        self.question = None
        return ""

    def holds(self, text: str) -> bool:
        """Pure check (no state change): would check() hold these words?"""
        return fragment(text) and not (self.pending_question() and words(text))

    def check(self, request: str) -> Verdict:
        """May this request run now? A fragment runs only as the answer to the voice's open question; any short
        reply to an open question ("do it", "yes please") carries the question, so Hermes knows what it answers."""
        request = " ".join((request or "").split())
        short = len(words(request)) <= FRAGMENT_WORDS
        question = self.pending_question() if short else ""
        if not question and self.answered and self.answered[1] == request and self.clock() - self.answered[0] < 10:
            question = self.answered[2]  # the same answer at a second route (provider delegation, then floor hand-off)
        if question and words(request):
            self.question = None  # answered: the next short words need a new question
            self.answered = (self.clock(), request, question)
            return Verdict(True, f'{request} (the owner\'s answer to the voice\'s question: "{question}")', "answer")
        if not fragment(request):
            return Verdict(True, request, "short_ask" if short else "ask")
        self.held += 1
        return Verdict(False, request, "fragment")
