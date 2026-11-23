"""Which answers a voice has actually said. Pure and clock-free: the caller passes the time.

An answer handed to a voice (a speakable context append) can be spoken in the reply already playing, in the next
reply, or not at all, and the provider does not say which. On 11-20 a 15 s resend timer repeated answers the voice
had already folded into its reply (192702 at 735.8 s, 205504 at 53.2 s), and a rule that "any reply starting means
delivered" lost others (205504 at 240.1 s: the voice said "Not yet" and "Checking now" and never the answer).

So delivery is decided from the voice's own transcript: an answer piece is spoken once the voice has said
MIN_HITS (two thirds of a short piece) of the piece's words that had not been said on the call before the piece was
sent (status answers repeat their topic's words: "three fast, two medium"). In 23 answers on five 11-20 calls, a
reply that spoke an answer said 3 to 33 of its new words; a reply that ignored it said none.

Everything one delegation sends to one voice (a lane guess, Hermes's answer, a 500-byte split) is one entry, and a
resend carries only its unspoken pieces. A resend happens only after the voice has been quiet for SETTLE_SECONDS
without saying the entry, at most once.

"Spoken" means the voice began saying it, which is enough while the reply plays on. A reply cut off mid-answer (a
barge-in, the floor moving away, or the provider ending it under the caller's words) is not: take 13's line-3
answer was cut after its first sentence and Tek's after "It was a startup timing bug: the blur". So the ledger keeps
the entries the current or just-finished reply is saying, closed or not, and a cut returns each one's unheard
sentences for the floor to hand back.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

MIN_HITS = 3
# The voice starts replying to an append within 0.7 s (median) and 2.1 s (p90) when it is free (11-20, n=469).
SETTLE_SECONDS = 2.5
MAX_SENDS = 2
STARTED_HITS = 2  # new words said before a barge-in that show the voice had begun this answer (one can be chance)
RESUME_PREFIX = "To finish what I was saying: "

_NUMBERS = dict(enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen "
                          "fifteen sixteen seventeen eighteen nineteen twenty".split()))
_STOP = frozenset("""a an the and or but if then so to of in on at for from by with about as is are was were be been
being am im you your yours we our us it its this that these those there here what which who whom when where why how
do does did done have has had having will would can could should may might must shall just also only very really
too more most some any all each every both either than into out up down over under again once off own same such
let lets got get gets yes yeah okay sure right well now still""".split())


def needed(new_words: int) -> int:
    """How many of a piece's new words the voice must say: MIN_HITS, or two thirds of a short piece's words."""
    return min(MIN_HITS, -(-2 * new_words // 3))


def words(text: str) -> list[str]:
    """Lower-case word tokens, with 0-20 spelled out the way the voice says them."""
    out = []
    for word in re.findall(r"\w+", text.casefold().replace("’", "'")):
        out.append(_NUMBERS[int(word)] if word.isdigit() and int(word) in _NUMBERS else word)
    return out


def content(text: str) -> set[str]:
    return {w for w in words(text) if (len(w) >= 3 or w.isdigit()) and w not in _STOP}


def sentences(text: str) -> list[str]:
    return [s for s in re.split(r"(?<=[.!?])\s+", text.strip()) if s]


@dataclass
class Piece:
    text: str
    new: frozenset[str]  # its content words the call had not heard when it was sent
    lane: bool
    late: bool = False  # handed while the voice's reply was already playing
    hits: set[str] = field(default_factory=set)
    spoken: bool = False

    def hear(self, said: set[str]) -> None:
        self.hits |= self.new & said
        if self.new and len(self.hits) >= needed(len(self.new)) and self.read():
            self.spoken = True

    def read(self) -> bool:
        """Whether the words the voice said were this piece's. A reply already playing when the piece came was made
        for the request before it, and says that request's words: take 15's line-3 answer, handed 9 s into the reply
        to line 3, was credited from "The update run is still going, no new receipts yet", 3 of its 37 new words,
        each from a different sentence. So a late piece is read only once the voice says one of its sentences."""
        return not self.late or any(len(own & self.hits) >= needed(len(own))
                                    for s in sentences(self.text) if (own := content(s) & self.new))

    @property
    def begun(self) -> bool:
        """The voice had begun saying it: a late piece from its start, as a voice merging an answer reads it."""
        if self.spoken or not self.hits or len(self.hits) < min(STARTED_HITS, len(self.new)):
            return self.spoken
        first = next((own for s in sentences(self.text) if (own := content(s) & self.new)), set())
        return not self.late or len(first & self.hits) >= min(STARTED_HITS, len(first)) or self.read()


@dataclass
class Entry:
    leg: str
    did: str | None
    sent_at: float
    pieces: list[Piece] = field(default_factory=list)
    sends: int = 1
    said: set[str] = field(default_factory=set)  # content words the voice said while this entry was out

    @property
    def started(self) -> bool:
        return any(p.begun for p in self.pieces)

    def rest(self) -> str:
        """The sentences after the last one the voice said (half the words it adds to the ones before it). Speech
        runs in order, so what comes before it, or before the piece the voice was saying (take 13), is not owed."""
        begun = [i for i, p in enumerate(self.pieces) if p.hits or p.spoken]
        pieces = self.pieces[begun[-1] if begun else 0:]
        parts, last, before = [s for p in pieces for s in sentences(p.text)], -1, set()
        for i, sentence in enumerate(parts):
            own = content(sentence) - before
            before |= own
            if own and 2 * len(own & self.said) >= len(own):
                last = i
        return " ".join(parts[last + 1:])

    @property
    def unspoken(self) -> list[Piece]:
        return [p for p in self.pieces if not p.spoken]

    @property
    def lane(self) -> bool:
        return all(p.lane for p in self.pieces)

    def text(self) -> str:
        return "\n\n".join(p.text for p in self.unspoken)


class Ledger:
    def __init__(self):
        self.entries: list[Entry] = []
        self.said: set[str] = set()  # content words any voice has said on this call
        self.speaking: dict[str, list[Entry]] = {}  # per leg: entries the last reply has begun, closed ones too

    def pending(self, leg: str) -> list[Entry]:
        return [e for e in self.entries if e.leg == leg]

    def sent(self, leg: str, did: str | None, text: str, now: float, *, lane: bool = False, late: bool = False) -> Entry:
        """A new answer went to the voice: one more piece of its delegation's entry."""
        entry = next((e for e in self.entries if e.leg == leg and e.did == did and did is not None), None)
        if entry is None:
            entry = Entry(leg, did, now)
            self.entries.append(entry)
        else:
            entry.sent_at = now
        if not lane:  # Hermes's own answer supersedes a lane guess the voice has not said
            entry.pieces = [p for p in entry.pieces if p.spoken or not p.lane]
        entry.pieces.append(Piece(text, frozenset(content(text) - self.said), lane, late))
        return entry

    def resent(self, entry: Entry, now: float) -> None:
        entry.sends += 1
        entry.sent_at = now

    def heard(self, leg: str, text: str) -> list[Entry]:
        """The voice on this leg said these words aloud. Returns the entries now fully spoken (and closes them)."""
        said = content(text)
        speaking = self.speaking.setdefault(leg, [])
        for entry in self.pending(leg):
            for piece in entry.unspoken:
                piece.hear(said)
            entry.said |= said
            if entry.started and entry not in speaking:
                speaking.append(entry)
        for entry in speaking:  # closed entries the reply is still saying
            entry.said |= said
        self.said |= said
        return self._close(leg)

    def reply_started(self, leg: str) -> None:
        """A new reply opened on this leg: the last one ran its course, and what it said of an answer is what the
        voice chose to say."""
        self.speaking.pop(leg, None)

    def reply_done(self, leg: str) -> list[Entry]:
        """An audible reply on this leg ended: a piece with no new words to listen for counts as said. The entries it
        was saying stay with it until the next reply: the provider ends a reply itself when the caller talks over
        it, and that is a cut too (take 14)."""
        for entry in self.pending(leg):
            for piece in entry.unspoken:
                if not piece.new:
                    piece.spoken = True
        return self._close(leg)

    def cut(self, leg: str) -> list[tuple[str | None, str]]:
        """Playback on this leg stopped before the phone heard it all. Each answer the reply had begun leaves the
        pending entries; returns (did, unheard rest) for each with a rest. Unbegun answers stay pending."""
        begun = self.speaking.pop(leg, [])
        self.entries = [e for e in self.entries if e not in begun]
        return [(e.did, text) for e in begun if (text := e.rest())]

    def due(self, leg: str, now: float, quiet_since: float | None) -> list[tuple[Entry, str | None]]:
        """Entries the voice had its chance to say and didn't: (entry, text to resend), or (entry, None) once it
        has been sent MAX_SENDS times (dropped). The voice must be quiet now; quiet_since is when it last stopped."""
        out = []
        for entry in self.pending(leg):
            if now - max(entry.sent_at, quiet_since if quiet_since is not None else entry.sent_at) < SETTLE_SECONDS:
                continue
            if entry.sends >= MAX_SENDS:
                self.entries.remove(entry)
                out.append((entry, None))
            else:
                out.append((entry, entry.text()))
        return out

    def next_due(self, leg: str, quiet_since: float | None) -> float | None:
        times = [max(e.sent_at, quiet_since if quiet_since is not None else e.sent_at) + SETTLE_SECONDS
                 for e in self.pending(leg)]
        return min(times, default=None)

    def retire(self, leg: str) -> list[tuple[Entry, str]]:
        """This leg's session is gone: hand back every entry with its unspoken text."""
        gone = self.pending(leg)
        self.entries = [e for e in self.entries if e.leg != leg]
        self.speaking.pop(leg, None)
        return [(e, e.text()) for e in gone]

    def _close(self, leg: str) -> list[Entry]:
        done = [e for e in self.pending(leg) if not e.unspoken]
        self.entries = [e for e in self.entries if e not in done]
        return done
