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
"""
from __future__ import annotations

from dataclasses import dataclass, field
import re

MIN_HITS = 3
# The voice starts replying to an append within 0.7 s (median) and 2.1 s (p90) when it is free (11-20, n=469).
SETTLE_SECONDS = 2.5
MAX_SENDS = 2
STARTED_HITS = 2  # new words said before a barge-in that show the voice had begun this answer (one can be chance)

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


@dataclass
class Piece:
    text: str
    new: frozenset[str]  # its content words the call had not heard when it was sent
    lane: bool
    hits: set[str] = field(default_factory=set)
    spoken: bool = False

    def hear(self, said: set[str]) -> None:
        self.hits |= self.new & said
        if self.new and len(self.hits) >= needed(len(self.new)):
            self.spoken = True


@dataclass
class Entry:
    leg: str
    did: str | None
    sent_at: float
    pieces: list[Piece] = field(default_factory=list)
    sends: int = 1

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

    def pending(self, leg: str) -> list[Entry]:
        return [e for e in self.entries if e.leg == leg]

    def sent(self, leg: str, did: str | None, text: str, now: float, *, lane: bool = False) -> Entry:
        """A new answer went to the voice: one more piece of its delegation's entry."""
        entry = next((e for e in self.entries if e.leg == leg and e.did == did and did is not None), None)
        if entry is None:
            entry = Entry(leg, did, now)
            self.entries.append(entry)
        else:
            entry.sent_at = now
        if not lane:  # Hermes's own answer supersedes a lane guess the voice has not said
            entry.pieces = [p for p in entry.pieces if p.spoken or not p.lane]
        entry.pieces.append(Piece(text, frozenset(content(text) - self.said), lane))
        return entry

    def resent(self, entry: Entry, now: float) -> None:
        entry.sends += 1
        entry.sent_at = now

    def heard(self, leg: str, text: str) -> list[Entry]:
        """The voice on this leg said these words aloud. Returns the entries now fully spoken (and closes them)."""
        said = content(text)
        for entry in self.pending(leg):
            for piece in entry.unspoken:
                piece.hear(said)
        self.said |= said
        return self._close(leg)

    def reply_done(self, leg: str) -> list[Entry]:
        """An audible reply on this leg ended: a piece with no new words to listen for counts as said."""
        for entry in self.pending(leg):
            for piece in entry.unspoken:
                if not piece.new:
                    piece.spoken = True
        return self._close(leg)

    def barged_in(self, leg: str) -> list[Entry]:
        """The caller cut this leg off. What the voice had started saying, he chose not to hear out."""
        cut = [e for e in self.pending(leg)
               if any(len(p.hits) >= min(STARTED_HITS, len(p.new)) and p.hits for p in e.pieces)]
        self.entries = [e for e in self.entries if e not in cut]
        return cut

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
        return [(e, e.text()) for e in gone]

    def _close(self, leg: str) -> list[Entry]:
        done = [e for e in self.pending(leg) if not e.unspoken]
        self.entries = [e for e in self.entries if e not in done]
        return done
