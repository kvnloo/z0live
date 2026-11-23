"""Every recorded film take (fixtures/takes, cut by bench/take_fixture.py with its words scrambled) replays through
the real adapter on each change and is judged by film call 1's five checks on what the phone got (replay.checks).
A new take is one more fixture; a check a take is known to fail only because of the recorded voice's own words is
listed in RECORDED, and must keep failing until the fixture is re-cut (a replay speaks the recorded words)."""
import asyncio
import json
import os
from pathlib import Path
import unittest

os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")  # never the owner's lanes config
import replay  # noqa: E402

FIXTURES = Path(__file__).with_name("fixtures") / "takes"
TAKES = sorted(FIXTURES.glob("take*.jsonl"), key=lambda p: int(p.stem[4:]))
# Takes 7, 9, 14 and 15: the voice's own hand-off line promised work ("I'll handle that and keep receipts", "I'm
# taking that on end-to-end", "I'll go step by step and show receipts", "I'll take that"). The prompts are the guard;
# a replay cannot unsay it.
RECORDED = {"promised": {"take7", "take9", "take14", "take15"}}
# Take 15 failures of the adapter's own, each fixed by its own change: an entry here must keep failing until then.
OPEN = {"runs": {"take15"}}


def checked(path):
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    r = replay.Replay(rows)
    asyncio.run(r.run())
    return replay.checks(r)


def take(name):
    rows = [json.loads(line) for line in (FIXTURES / f"{name}.jsonl").read_text().splitlines() if line.strip()]
    r = replay.Replay(rows)
    asyncio.run(r.run())
    return r


def handed(r, since=0.0):
    """(t, text) of each answer the adapter handed the primary voice, its 500-byte pieces joined."""
    out = []
    for t, e in r.sent:
        text = "".join(c.get("text", "") for c in e.get("content", []))
        if e.get("channel") != "speakable" or t < since:
            continue
        if out and out[-1][0] == t and not text.startswith(replay.FINAL):
            out[-1] = (t, out[-1][1] + text)
        else:
            out.append((t, text))
    return out


class Take15(unittest.TestCase):
    """Take 15, line 3 (words scrambled; the scrambled line-3 answer opens "That zqcj is zqck running")."""

    @classmethod
    def setUpClass(cls):
        cls.r = take("take15")

    def test_line_3s_answer_handed_into_a_playing_reply_is_handed_again_whole_without_its_receipt(self):
        again = [text for t, text in handed(self.r, 120) if "zqck running" in text]
        self.assertEqual(len(again), 1, "the floor's return hands the whole answer again")
        self.assertIn("I can only report from this call", again[0], "all three paragraphs")
        self.assertNotIn("Still working on that", "".join(text for _, text in handed(self.r, 62)),
                         "the answer overtook the receipt")


    def test_an_answer_dropped_for_the_next_request_is_never_sent_after(self):
        # 52.62 s: floor.answer_dropped (new_request) for line 2, and 83 ms later line 2's whole answer went out as the
        # reply to line 3.
        floor = self.r.floor
        dropped = {d["did"]: t for t, ev, d in floor if ev == "floor.answer_dropped" and d["reason"] == "new_request"}
        self.assertTrue(dropped, "fixture: line 3 drops line 2's stopped answer")
        sent = [(d["of"], t) for t, ev, d in floor if ev in ("floor.answer_sent", "floor.answer_resent")]
        self.assertEqual([(did, t) for did, t in sent if did in dropped and t >= dropped[did]], [])

    def test_a_reply_that_ran_its_course_is_not_cut_and_a_rest_is_never_resumed_twice(self):
        # 106.0 (Tek), 144.7 and 150.9 s: replies ended on their own, the mic quiet since 94.5 s, and each was logged
        # as a cut and its last sentence re-queued, the second time as "To finish what I was saying: To finish ...".
        cuts = [round(t, 1) for t, ev, _ in self.r.floor if ev == "floor.answer_cut"]
        self.assertEqual([t for t in cuts if t > 100], [])
        self.assertEqual([t for t, text in handed(self.r) if text.count("To finish what I was saying") > 1], [])
        self.assertEqual([t for t, text in self.r.sent_guest if "To finish" in json.dumps(text) and t > 100], [])


class RecordedTakes(unittest.TestCase):
    def test_the_takes_are_here(self):
        self.assertEqual([p.stem for p in TAKES][:9], [f"take{n}" for n in range(7, 16)])

    def test_each_recorded_take_passes_the_five_checks_on_the_phone(self):
        for path in TAKES:
            result = checked(path)
            for check, failures in result.items():
                with self.subTest(take=path.stem, check=check):
                    if path.stem in RECORDED.get(check, ()) or path.stem in OPEN.get(check, ()):
                        self.assertTrue(failures, "now passes: drop it from RECORDED")
                    else:
                        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
