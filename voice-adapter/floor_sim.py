"""Multi-party floor simulator: film call 1 with a scripted caller and scripted voices, through the real adapter.

Each seed is one call (replay.Replay on a simulated clock), judged by replay.checks on what the phone got:
  caller   the film's six lines (Jules and the owner; 0, 1 and 5 to each other, 2-3 to Hermes, 4 to Tek), each start
           jittered so lines overlap the bots, Tek's name heard as Tek/Tech/Teck/Tec, the guest leg's ASR hearing
           its own variant, words trailing the mic by 0.3-1.5 s, the mic loud while the caller talks
  voices   both legs answer every caller turn with a short draft of their own (self-started, as GPT-Live does) and
           make a delegation for it or not; a voice reads what the adapter hands it, merged into the reply it has
           open, and ends a reply itself when the caller talks over it or at a random word
  Hermes   each request takes 2-30 s (seeded) and answers with the line's facts
    python floor_sim.py [N] [FIRST_SEED]   # per-seed check failures, then failures by check
"""
from __future__ import annotations

import asyncio
import json
import os
import random
import re
import sys
from collections import Counter

os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")  # never the owner's lanes config
import replay  # noqa: E402

LINES = [  # (start s, who it is for, words); {tek} is how the transcriber hears Tek
    (6.0, "people", "Hey, wait. Is the glass theme on your phone yet? I don't see it on mine."),
    (12.5, "people", "No, I think it's still last night's build."),
    (22.5, "hermes", "Hermes, what's actually on my phone right now, versus staging?"),
    (38.0, "hermes", "Yeah. Merged isn't done. Get all of it onto my phone, prove each one on the phone, and make it "
                     "update itself. Take as long as you need, just send receipts."),
    (64.0, "tek", "Hey, {tek}. While that runs, what actually caused the glass crash in PR twenty eight?"),
    (91.0, "people", "Okay, nice. That's the one I kept hitting."),
]
ANSWERS = {  # request words -> what that bot's Hermes answers
    "staging": "Your phone runs build four oh nine from last night. Staging is twenty four merges ahead, including "
               "the glass theme.",
    "merged": "That run is already going as card seven, so nothing new was started. The receipts so far are a signed "
              "build and passing screenshot tests. The phone updates itself from every green staging build. I can't "
              "install anything from this call.",
    "crash": "PR twenty eight fixed a startup ordering bug: the blur ran before the window existed, which crashed "
             "every launch.",
}
DRAFTS = ("Let me check.", "Okay.", "Mm-hm.", "Checking that now.", "Right.", "Sure, one moment.")
TEK = ("Tek", "Tech", "Teck", "Tec")
WORD_S = 0.3  # a voice says a word this often; the caller too


def answer_for(request: str) -> str:
    return next((a for key, a in ANSWERS.items() if key in request.lower()), "Nothing more on that.")


class Sim(replay.Replay):
    def __init__(self, seed: int):
        self.rng = rng = random.Random(seed)
        self.seed, self.tick, self.last_audible = seed, 0, -9.0
        self.spoken: list[dict] = []  # every voice row, scripted or scheduled (a cut clears the ones it drops)
        self.open: dict[str, str] = {}  # the reply each voice has open
        rows = [{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}},
                {"t": 0.5, "src": "guest", "rx": {"type": "session.started"}}, {"t": 0.6, "people": 2}]
        for i, (start, to, words) in enumerate(LINES):
            start += rng.uniform(-2.5, 2.5) if i else 0
            end = start + WORD_S * len(words.split())
            rows += [{"t": start, "line": i, "end": end, "to": to},
                     {"t": start, "input_loud": True}, {"t": end, "input_loud": False}]
            for leg in ("provider", "guest"):  # each leg's own ASR
                rows += self.caller(i, leg, start, end, words.replace("{tek}", rng.choice(TEK)))
        rows.append({"t": 220.0})  # the call runs on until every answer has had its chance
        super().__init__(rows)
        self.hermes = Seeded(self)
        self.guest_answers = [(rng.uniform(2, 30), ANSWERS["crash"]) for _ in range(4)]

    def caller(self, i, leg, start, end, words):
        """One caller line as one leg hears it, then that leg's own draft reply and delegation."""
        rng, tid, lag = self.rng, f"{leg[0]}u{i}", self.rng.uniform(0.3, 1.5)
        tokens = words.split()
        turn = {"id": tid, "role": "user", "start_ms": int(start * 1000)}
        rows = [{"t": start + lag, "src": leg, "rx": {"type": "turn.created", "turn": {
            **turn, "transcript": tokens[0], "end_ms": int(start * 1000) + 300}}}]
        for k in range(1, len(tokens), 3):  # the transcript arrives a few words at a time
            at = start + lag + WORD_S * (k + 2)
            rows.append({"t": at, "src": leg, "rx": {"type": "turn.delta", "turn_id": tid,
                                                     "delta": " " + " ".join(tokens[k:k + 3]),
                                                     "start_ms": int((at - lag - 0.9) * 1000),
                                                     "end_ms": int((at - lag) * 1000)}})
        done = end + lag
        rows.append({"t": done, "src": leg, "rx": {"type": "turn.done", "turn": {
            **turn, "transcript": words, "end_ms": int(end * 1000)}}})
        if rng.random() < 0.7:  # the voice delegates the turn itself, addressed or not
            rows.append({"t": done + rng.uniform(-0.4, 0.4), "src": leg, "rx": {"type": "delegation.created", "item": {
                "id": f"{leg[0]}d{i}", "target": "client", "content": [{"type": "input_text", "text": words}]}}})
        if rng.random() < 0.85:  # and drafts a reply of its own
            rows += self.reply(leg, done + rng.uniform(0.2, 0.8), rng.choice(DRAFTS), pending=False)
        return rows

    def reply(self, leg, at, text, *, pending=True, rid=None):
        """A reply saying `text` from `at`, a word every WORD_S; it ends at the last word unless cut first. With `rid`
        the open reply `rid` goes on with these words."""
        self.tick += 1
        rows = [] if rid else [{"t": at, "src": leg, "rx": {"type": "turn.created", "turn": {
            "id": f"{leg[0]}r{self.tick}", "role": "assistant"}}}]
        rid = rid or rows[0]["rx"]["turn"]["id"]
        for k, word in enumerate(text.split()):
            rows.append({"t": at + WORD_S * (k + 1), "src": leg, "say": rid,
                         "rx": {"type": "output_transcript.added", "item": {"text": " " + word}}})
        end = at + WORD_S * (len(text.split()) + 1)
        rows.append({"t": end, "src": leg, "say": rid, "rx": {"type": "turn.done", "turn": {
            "id": rid, "role": "assistant", "transcript": text, "start_ms": int(at * 1000), "end_ms": int(end * 1000)}}})
        self.spoken += rows
        if pending:
            for row in rows:
                self.schedule(row)
        return rows

    def schedule(self, row):
        """Feed a row the voice decided on mid-call at its time (the replay clock's own timers)."""
        self.call_later(max(0.0, row["t"] - self.now), lambda: self.feed(row))

    def feed(self, row):
        if "rx" in row:
            self.extra.append(row)
            (self.call.guest.message if row["src"] == "guest" else
             lambda raw: self.call.message(raw, True))(json.dumps(row["rx"]))
        self.after(row)

    def react(self, leg, event):
        """A voice reads an answer the adapter hands it: into the reply it has open, or a reply of its own."""
        text = "".join(c.get("text", "") for c in event.get("content", []))
        if event.get("channel") != "speakable" or not text.startswith(replay.FINAL):
            return
        text = " ".join(text[len(replay.FINAL):].split())
        src = "provider" if leg == "primary" else "guest"
        rid = self.open.get(src)
        live = [r for r in self.spoken if rid and r.get("say") == rid and r["t"] > self.now]
        rid = rid if live else None
        at = max([r["t"] for r in live] + [self.now + self.rng.uniform(0.3, 1.0)])
        if rid:  # merged: the open reply goes on with the answer
            for r in [r for r in live if r["say"] == rid and r["rx"]["type"] == "turn.done"]:
                r.clear(), r.update(t=at)
            at -= WORD_S
        self.open[src] = self.reply(src, at, text, rid=rid)[-1]["say"]
        if self.rng.random() < 0.2:  # the provider stops at a random word on its own
            self.cut(src, at + WORD_S * self.rng.randrange(1, len(text.split()) + 1))

    def cut(self, src, at):
        """The provider ends its reply on `src` at `at`: the words after it are never said."""
        rid = self.open.get(src)
        if not rid or not [r for r in self.spoken if r.get("say") == rid and r["t"] >= at]:
            return
        rows = [r for r in self.spoken if r.get("say") == rid and r["t"] >= at]
        for r in [r for r in rows if r["say"] == rid]:
            r.clear(), r.update(t=at)
        opened = max((r["t"] for r in self.spoken if r.get("src") == src and (r.get("rx") or {}).get("turn", {}).get(
            "id") == rid and r["rx"]["type"] == "turn.created"), default=at)
        self.schedule({"t": at, "src": src, "rx": {"type": "turn.done", "turn": {
            "id": rid, "role": "assistant", "transcript": "", "start_ms": int(opened * 1000),
            "end_ms": int(at * 1000) + 1}}})

    def after(self, row):
        e = row.get("rx") or {}
        if (e.get("turn") or {}).get("role") == "assistant":
            if e["type"] == "turn.created":
                self.open[row["src"]] = e["turn"]["id"]
            elif self.open.get(row["src"]) == e["turn"]["id"]:
                del self.open[row["src"]]
        if e.get("type") == "turn.created" and e["turn"]["role"] == "user" and self.rng.random() < 0.6:
            self.cut(row["src"], self.now + self.rng.uniform(0.2, 0.6))  # the voice stops under the caller's words
        if self.heard and self.heard[-1][0] >= self.now - 0.01:
            self.last_audible = self.now
        self.keep_loud()

    def keep_loud(self):
        """The phone's output is loud for 0.5 s after the last word it heard, between rows too: with no row after a
        reply, the call's output stayed loud and the floor never saw the voice quiet (seed 9)."""
        self.loud = self.now - getattr(self, "last_audible", float("-inf")) < 0.5
        super().keep_loud()


class Seeded(dict):
    """Hermes on the simulated call: each request's work time is seeded, its answer the line's facts."""

    def __init__(self, sim):
        super().__init__()
        self.sim = sim

    def get(self, did, default=None):
        if did not in self:
            request = next((text for d, text in self.sim.requests if d == did), "")
            self[did] = (self.sim.rng.uniform(2, 30), answer_for(request))
        return self[did]


def run(seed: int) -> dict[str, list]:
    sim = Sim(seed)
    asyncio.run(sim.run())
    return replay.checks(sim)


def main(argv):
    n, first = (int(argv[0]) if argv else 20), (int(argv[1]) if len(argv) > 1 else 0)
    failed = Counter()
    for seed in range(first, first + n):
        result = {k: v for k, v in run(seed).items() if v}
        failed.update(result.keys())
        print(seed, json.dumps(result, ensure_ascii=False)[:300] if result else "ok")
    print("failures by check:", dict(failed) or "none", f"({n} seeds)")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
