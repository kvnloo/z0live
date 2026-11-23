"""Cut a public regression fixture from one recorded film take: replay.fixture's rows with every word scrambled.

    python bench/take_fixture.py TAKE_DIR OUT.jsonl

A take dir holds the call trace (traces/*/*.jsonl) and the script it played (turns.json: who said each line and when,
on caller.wav's clock). Call transcripts stay off GitHub, so each word the floor never reads becomes a made-up word,
the same one everywhere in the take: what the phone heard still matches the answer it was handed, a caller's words
still echo the voice's, and names, promises, requests and backchannels read as they did on the call. Rows of kind
`line` place each script line on the trace clock (the caller's first sound is line 0) and say who it was for.
"""
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import answer_delivery  # noqa: E402
import replay  # noqa: E402

# Who each line of film call 1 is for (z0film BRIEF): 0, 1 and 5 are the people talking to each other.
FOR = ("people", "people", "hermes", "hermes", "tek", "people")
TEXT_KEYS = {"answer", "delta", "text", "transcript", "truncated"}
_WORD = re.compile(r"[A-Za-z0-9\u0b80-\u0bff]+")
NUMBERS = set(answer_delivery._NUMBERS.values()) | {"thirty", "forty", "fifty", "sixty", "hundred", "thousand"}
# Words the floor, the replay or the voice's guards read (guest_floor, mobile_live.PROMISE/QUEUED/RECEIPT/FULL,
# answer_delivery), plus everyday function words. Everything else is scrambled.
KEEP = (answer_delivery._STOP | NUMBERS | set(re.findall(r"[a-z]+", " ".join((
    "hermes herms tek tech tec teck teknium primary assistant gpt grok kevin jules s",
    "hey hi hello ok okay mm mmm hmm uh um huh yep thanks thank sure right nice wait no not",
    "please need want get make put install check show tell find send give run start stop look pull open fix prove try",
    "ship merge deploy update keep move add remove delete write read over back hear",
    "i me my mine i'm i'll ll m t re ve d isn don can won haven couldn wasn didn doesn aren wouldn shouldn",
    "taking handling handle take care go going step posted updated receipts receipt report follow circle end consider",
    "handled taken finish saying working tell soon done meanwhile else anything started queued number earlier requests",
    "know",
    "requests running waiting haven't one them finishes again line", "phone call while")).lower())))


class Scrambler:
    def __init__(self):
        self.made: dict[str, str] = {}

    def word(self, w: str) -> str:
        low = w.lower()
        # An identifier (letters and digits: S25, the 0a96e2de of card t_0a96e2de) stays, and so do letters and
        # two-letter words: the floor spells an id and matches the voice's spelling or split transcript (" z", "0") to
        # it (answer_delivery.anchors), and a made-up word for "PR" or "CI" would count as content where theirs does not
        # (answer_delivery.content). Take 19's voice said "t, zero a nine six e two d e": a scrambled id is never heard.
        if low in KEEP or len(low) <= 2 or (low.isdigit() and int(low) <= 20) or answer_delivery._named(low):
            return w
        if low not in self.made:
            n, out = len(self.made), ""
            while True:
                out, n = chr(97 + n % 26) + out, n // 26
                if not n:
                    break
            self.made[low] = ("9" * len(low)) if low.isdigit() else "zq" + out
        made = self.made[low]
        return made.capitalize() if w[:1].isupper() else made

    def text(self, s: str) -> str:
        return _WORD.sub(lambda m: self.word(m.group()), s)

    def row(self, value, key=""):
        if isinstance(value, dict):
            return {k: self.row(v, k) for k, v in value.items()}
        if isinstance(value, list):
            return [self.row(v, key) for v in value]
        if isinstance(value, str) and key in TEXT_KEYS:
            return self.text(value)
        if isinstance(value, str) and key == "id" and value.startswith(("sess", "conv")):
            return "s"
        return value


def cut(take: Path) -> list[dict]:
    trace = sorted(take.glob("traces/*/*.jsonl"))[-1]
    rows = replay.fixture(trace)
    script = json.loads((take / "turns.json").read_text())
    first = min(r["t"] for r in rows if r.get("input_loud"))
    offset = first - script[0]["t"]  # the caller's first sound is line 0's first word
    lines = [{"t": round(line["t"] + offset, 3), "line": i, "end": round(line["end"] + offset, 3), "to": FOR[i]}
             for i, line in enumerate(script)]
    s = Scrambler()
    if not any("people" in r for r in rows):  # take 7 predates floor.people: its chat said two people share the phone
        rows.insert(0, {"t": 0.0, "people": 2})
    return sorted([s.row(r) for r in rows] + lines, key=lambda r: r["t"])


if __name__ == "__main__":
    out = cut(Path(sys.argv[1]))
    Path(sys.argv[2]).write_text("".join(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n" for r in out))
