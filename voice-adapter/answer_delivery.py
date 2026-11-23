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

"Spoken" means the voice began saying it, which is enough while the reply plays on: it is not resent. Heard is
decided when the reply ends, from what that reply's transcript said. A reply cut off mid-answer (a
barge-in, the floor moving away, or the provider ending it under the caller's words) leaves its unheard sentences:
take 13's line-3 answer was cut after its first sentence and Tek's after "It was a startup timing bug: the blur".
A reply that ran its course can leave some out too: take 16's resumed line-3 rest was read as paragraph 2 and a
paraphrase of paragraph 3, and paragraph 1 (card t_0a96e2de, "won't start a second run") was never said. So the
ledger keeps the entries the current or just-finished reply is saying, closed or not; a cut returns each one's
unheard sentences, and a reply that ran its course the paragraphs it skipped, for the floor to hand back.
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


def paragraphs(text: str) -> list[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]


_TENS = "twenty thirty forty fifty sixty seventy eighty ninety".split()


def spoken(text: str) -> set[str]:
    """Word tokens the way a voice says them, letters apart from digits: up to two digits as a number, longer runs
    digit by digit (\"S25\" and \"S twenty-five\", \"30652\" and \"three zero six five two\" match)."""
    out = set()
    for part in re.findall(r"[^\W\d_]+|\d+", text.casefold().replace("’", "'")):
        digits = part.lstrip("0") or "0"
        if not part.isdigit():
            out.add(part)
        elif len(digits) > 2:
            out |= {_NUMBERS[int(d)] for d in part}
        elif int(digits) <= 20:
            out.add(_NUMBERS[int(digits)])
        else:
            out |= {_TENS[int(digits) // 10 - 2], *([_NUMBERS[int(digits) % 10]] if int(digits) % 10 else [])}
    return out


def anchors(text: str) -> list[set[str]]:
    """What a text names that a voice cannot put in other words: each identifier in it (a token with an underscore,
    or with letters and digits: card t_0a96e2de, S25), as spoken(). Not a bare number: a voice says \"12:30\" as
    \"twelve thirty\" or \"half past twelve\"."""
    return [spoken(token) for token in re.findall(r"\w+", text)
            if "_" in token or (re.search(r"\d", token) and re.search(r"[^\W\d_]", token))]


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
    spoken: set[str] = field(default_factory=set)  # spoken() of the same words

    @property
    def started(self) -> bool:
        return any(p.begun for p in self.pieces)

    def rest(self) -> str:
        """The sentences after the last one the voice said (half the words it adds to the ones before it). Speech
        runs in order, so what comes before it, or before the piece the voice was saying (take 13), is not owed."""
        begun = [i for i, p in enumerate(self.pieces) if p.hits or p.spoken]
        pieces = self.pieces[begun[-1] if begun else 0:]
        parts = [((i, j), s) for i, p in enumerate(pieces) for j, para in enumerate(paragraphs(p.text))
                 for s in sentences(para)]
        last, before = -1, set()
        for i, (_, sentence) in enumerate(parts):
            own = content(sentence) - before
            before |= own
            if own and 2 * len(own & self.said) >= len(own):
                last = i
        out = []  # its paragraphs stay paragraphs: a reply that skips one is judged by them (skipped)
        for i, (para, sentence) in enumerate(parts[last + 1:]):
            out.append(sentence if not i or para == parts[last + i][0] else "\n\n" + sentence)
        return " ".join(out).replace(" \n\n", "\n\n")

    def skipped(self) -> str:
        """The paragraphs of its answer a reply that ran its course left out: each names something (an identifier,
        anchors()) the voice never said while this entry was out. A voice may put an answer in its own
        words and leave out how it works (take 16: Tek's \"In short: blur ran too early and crashed\"), not what
        it names (Hermes's resumed reply skipped paragraph 1 and card t_0a96e2de). Lane guesses and status lines
        are never owed: Hermes's own answer supersedes them."""
        begun = [i for i, p in enumerate(self.pieces) if p.hits or p.spoken]
        pieces = [p for p in self.pieces[begun[-1] if begun else 0:] if not p.lane]

        def unheard(text):
            return any(not anchor <= self.spoken for anchor in anchors(text))

        out = []
        for para in (para for p in pieces for para in paragraphs(p.text) if unheard(para)):
            # Without the sentences of it the voice did say (half their words), unless they hold what it named.
            out.append(" ".join(s for s in sentences(para)
                                if unheard(s) or 2 * len(content(s) & self.said) < len(content(s))))
        return "\n\n".join(o for o in out if o)

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
        self.retold: set[tuple[str, str | None]] = set()  # (leg, did) whose skipped paragraphs went back once

    def pending(self, leg: str) -> list[Entry]:
        return [e for e in self.entries if e.leg == leg]

    def sent(self, leg: str, did: str | None, text: str, now: float, *, lane: bool = False, late: bool = False) -> Entry:
        """A new answer went to the voice: one more piece of its delegation's entry."""
        entry = next((e for e in self.entries if e.leg == leg and e.did == did and did is not None), None)
        if entry is None:
            entry = Entry(leg, did, now)
            self.entries.append(entry)
        else:  # new words: the voice gets its own chances at them (a resent receipt cost Hermes's answer its resend)
            entry.sent_at, entry.sends = now, 1
        if not lane:  # Hermes's own answer supersedes a lane guess the voice has not said
            entry.pieces = [p for p in entry.pieces if p.spoken or not p.lane]
        entry.pieces.append(Piece(text, frozenset(content(text) - self.said), lane, late))
        return entry

    def resent(self, entry: Entry, now: float) -> None:
        entry.sends += 1
        entry.sent_at = now

    def heard(self, leg: str, text: str) -> list[Entry]:
        """The voice on this leg said these words aloud. Returns the entries now fully spoken (and closes them)."""
        said, voiced = content(text), spoken(text)
        speaking = self.speaking.setdefault(leg, [])
        for entry in self.pending(leg):
            for piece in entry.unspoken:
                piece.hear(said)
            entry.said |= said
            entry.spoken |= voiced
            if entry.started and entry not in speaking:
                speaking.append(entry)
        for entry in speaking:  # closed entries the reply is still saying
            entry.said |= said
            entry.spoken |= voiced
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
        pending entries; returns (did, unheard rest) for each with a rest. Unbegun answers stay pending, and a
        stopped reply was not their chance: one send comes back (floor_sim seed 33: Tek's resumed answer, all words
        the call had heard, was stopped at \"PR twenty\" and again at \"PR twenty eight\", then dropped)."""
        begun = self.speaking.pop(leg, [])
        self.entries = [e for e in self.entries if e not in begun]
        self.retold -= {(leg, e.did) for e in begun}  # its rest takes the place of a retelling
        for entry in self.pending(leg):
            entry.sends = max(1, entry.sends - 1)
        return [(e.did, text) for e in begun if (text := e.rest())]

    def finished(self, leg: str) -> list[tuple[Entry, str]]:
        """The reply on this leg ran its course: (entry, the paragraphs it skipped) for each answer it had begun, ""
        when it left none out. Skipped paragraphs are owed once per answer (a voice that skips them again chose
        to), and are not resent as well. The entries stay with the reply until the next one, as after reply_done:
        a caller turn that began inside it, or the floor leaving while it drains, is still a cut, and the cut's
        rest takes the place of the skipped paragraphs (take 14; a barge-in in the same tick as turn.done)."""
        out = []
        for entry in self.speaking.get(leg, []):
            again = (leg, entry.did) in self.retold
            rest = "" if again else entry.skipped()
            if rest:
                self.retold.add((leg, entry.did))
            if rest or again:  # handed back, or its one retelling had its chance
                self.entries = [e for e in self.entries if e is not entry]
            out.append((entry, rest))
        return out

    def drop(self, leg: str, dids: set[str]) -> None:
        """The caller moved on from these answers: nothing of them is resent."""
        self.entries = [e for e in self.entries if e.leg != leg or e.did not in dids]

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
