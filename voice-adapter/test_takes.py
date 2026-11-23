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
import answer_delivery  # noqa: E402
import replay  # noqa: E402

FIXTURES = Path(__file__).with_name("fixtures") / "takes"
TAKES = sorted(FIXTURES.glob("take*.jsonl"), key=lambda p: int(p.stem[4:]))
# None now: takes 7, 9, 14 and 15 failed `promised` on the voice's own hand-off line ("I'll handle that and keep
# receipts", "I'll take that"); the floor mutes a hand-off line at its promise, and `promised` is judged on what
# reached the phone (take 16).
RECORDED: dict[str, set[str]] = {}


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


def sent_after_dropped(test, r):
    """(did, t) of each answer sent or resent after the floor dropped it for the caller's next request."""
    dropped = {d["did"]: t for t, ev, d in r.floor if ev == "floor.answer_dropped" and d["reason"] == "new_request"}
    test.assertTrue(dropped, "fixture: line 3 drops line 2's stopped answer")
    sent = [(d["of"], t) for t, ev, d in r.floor if ev in ("floor.answer_sent", "floor.answer_resent")]
    return [(did, t) for did, t in sent if did in dropped and t >= dropped[did]]


class Take15(unittest.TestCase):
    """Take 15, line 3 (words scrambled; the scrambled line-3 answer opens "That zqcj is zqck running")."""

    @classmethod
    def setUpClass(cls):
        cls.r = take("take15")

    def test_line_3s_answer_handed_into_a_playing_reply_is_handed_again_whole_without_its_receipt(self):
        again = [text for t, text in handed(self.r, 62) if "zqck running" in text]
        self.assertEqual(len(again), 1, "the floor's return hands the whole answer again, once")
        self.assertIn("I can only report from this call", again[0], "all three paragraphs")
        self.assertNotIn("Still working on that", "".join(text for _, text in handed(self.r, 62)),
                         "the answer overtook the receipt")


    def test_an_answer_dropped_for_the_next_request_is_never_sent_after(self):
        # 52.62 s: floor.answer_dropped (new_request) for line 2, and 83 ms later line 2's whole answer went out as the
        # reply to line 3.
        self.assertEqual(sent_after_dropped(self, self.r), [])

    def test_a_reply_that_ran_its_course_is_not_cut_and_a_rest_is_never_resumed_twice(self):
        # 106.0 (Tek), 144.7 and 150.9 s: replies ended on their own, the mic quiet since 94.5 s, and each was logged
        # as a cut and its last sentence re-queued, the second time as "To finish what I was saying: To finish ...".
        # (Tek's answer that line 5 talked over at 93.6 s is a cut, and comes back once.)
        cuts = [round(t, 1) for t, ev, _ in self.r.floor if ev == "floor.answer_cut"]
        self.assertEqual([t for t in cuts if t > 100], [])
        for leg in (self.r.sent, self.r.sent_guest):
            self.assertEqual([t for t, e in leg if json.dumps(e).count("To finish what I was saying") > 1], [])


class Take16(unittest.TestCase):
    """Take 16, line 3 (words scrambled; the scrambled line-3 answer names card t_zqcx)."""

    @classmethod
    def setUpClass(cls):
        cls.r = take("take16")

    def test_the_paragraph_the_resumed_reply_skipped_is_handed_back_once_with_one_cue(self):
        # 98.8-121.2 s: the voice said paragraphs 2 and 3 of the resumed rest and ran its course; paragraph 1 (card
        # t_0a96e2de, "won't start a second run") was credited at 100.365 and never resent.
        again = [(t, text) for t, text in handed(self.r, 121) if "t_zqcx" in text]
        self.assertTrue(again, "paragraph 1 goes back to the voice")
        self.assertNotIn("\n\n", again[0][1].split(replay.FINAL)[-1].strip(), "only paragraph 1")
        self.assertEqual([t for t, text in handed(self.r) if text.count("To finish what I was saying") > 1], [])

    def test_a_rest_is_not_sent_while_the_callers_turn_is_open(self):
        # 97.969 s: the rest went out 0.8 s before the caller's open turn closed (98.778).
        rested = [t for t, ev, d in self.r.floor if ev == "floor.answer_resumed" and d["speaker"] == "primary"]
        self.assertEqual([t for t in rested if 97 < t < 98.778], [])

    def test_an_answer_dropped_for_the_next_request_is_never_sent_after(self):
        # 51.974 s: answer_dropped (new_request) for line 2's answer, and 16 ms later floor.answer_resent of it.
        self.assertEqual(sent_after_dropped(self, self.r), [])

    def test_the_last_word_of_line_3_ends_line_3(self):
        # 52.836 s: "receipts" became its own utterance and gave the floor to the people, muting the reply.
        utterances = [d["text"] for t, ev, d in self.r.floor if ev == "floor.utterance" and 51 < t < 60]
        self.assertEqual(len(utterances), 1, utterances)
        self.assertEqual([t for t, ev, d in self.r.floor if ev == "floor.changed" and 51 < t < 60], [])

    def test_the_hand_off_line_is_muted_at_its_promise(self):
        # 52.998 s: the reply to line 3 said "Okay, I'll take that".
        self.assertIn("floor.promise_muted", [ev for t, ev, _ in self.r.floor if 52 < t < 56])
        self.assertNotIn("take that", "".join(text for t, text in self.r.heard if 52 < t < 56))


class Take17(unittest.TestCase):
    """Take 17, line 3 (words scrambled; the scrambled line-3 answer names card t_zqby)."""

    @classmethod
    def setUpClass(cls):
        cls.r = take("take17")

    def test_the_sentence_a_paraphrase_jumped_comes_back_with_the_cuts_rest(self):
        # 59.7-66.9 s: the voice said sentences 1 and 3 in its own words ("I won't start another one"); "Hey Tek" cut
        # it, and the rest resumed at 99.561 from sentence 4. Sentence 2 (card t_0a96e2de) was never said.
        rested = [t for t, ev, d in self.r.floor if ev == "floor.answer_resumed" and d["speaker"] == "primary"]
        self.assertTrue(rested, "line 3's rest is resumed")
        card = [t for t, text in handed(self.r, 67) if "t_zqby" in text and answer_delivery.RESUME_PREFIX in text]
        self.assertEqual(card[0], rested[0], "with the cut's rest")
        self.assertEqual(len(card), answer_delivery.MAX_RETELLS, "and once more: the recorded reply skipped it again")


class Take18(unittest.TestCase):
    """Take 18, line 3 (words scrambled; the scrambled line-3 answer names card t_zqct)."""

    @classmethod
    def setUpClass(cls):
        cls.r = take("take18")

    def test_an_id_the_voice_dropped_twice_goes_back_once_more_spelled(self):
        # 61.2-66.7 s: "covered by a card Poteto has been running"; "Hey Tek" cut it at 66.326. The verbatim hand-back
        # at 98.553 was said as "there's already a card for this" and ran its course at 123.177: t_0a96e2de unheard.
        card = [(t, text) for t, text in handed(self.r, 67) if "t_zqct" in text and answer_delivery.RESUME_PREFIX in text]
        self.assertEqual([round(t) for t, _ in card], [99, 150], "the cut's rest, then after Tek's answer")
        for _, text in card:
            self.assertIn('t_zqct (say: "t, z q c t")', text)
            self.assertIn(answer_delivery.SAY_EXACTLY + answer_delivery.RESUME_PREFIX, text)
        self.assertEqual(len(answer_delivery.sentences(card[1][1].split(answer_delivery.RESUME_PREFIX)[1])), 1,
                         "only the sentence naming the card")


class Take19(unittest.TestCase):
    """Take 19, line 3 (words scrambled; ids and short words kept)."""

    @classmethod
    def setUpClass(cls):
        cls.r = take("take19")

    def test_a_hand_off_line_that_began_its_promise_muted_stays_muted(self):
        # 53.599 s: " I'll" came while the utterance was open; the floor unmuted the reply at 53.627 and the phone heard
        # "get the full picture" until 55.335.
        self.assertIn("floor.promise_muted", [ev for t, ev, _ in self.r.floor if 53.5 < t < 54])
        self.assertNotIn("floor.reply_unmuted", [ev for t, ev, _ in self.r.floor if 53.5 < t < 60])
        self.assertEqual("".join(text for t, text in self.r.heard if 52 < t < 60), "")


class RecordedTakes(unittest.TestCase):
    def test_the_takes_are_here(self):
        self.assertEqual([p.stem for p in TAKES][:13], [f"take{n}" for n in range(7, 20)])

    def test_each_recorded_take_passes_the_five_checks_on_the_phone(self):
        for path in TAKES:
            result = checked(path)
            for check, failures in result.items():
                with self.subTest(take=path.stem, check=check):
                    if path.stem in RECORDED.get(check, ()):
                        self.assertTrue(failures, "now passes: drop it from RECORDED")
                    else:
                        self.assertEqual(failures, [])


if __name__ == "__main__":
    unittest.main()
