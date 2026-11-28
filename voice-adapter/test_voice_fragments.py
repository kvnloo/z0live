"""F5/F7 input boundaries (cos-1128 L1): fragments never start a run, segments coalesce in a ~30 s window, a short
answer to the voice's open question keeps that question, and the voice's "Mm-hm" inside a long thought is no exchange."""
import asyncio
import json
import os
import unittest


os.environ.setdefault("HERMES_MOBILE_VOICE_LIAISON", "0")
os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")
import mobile_live as live  # noqa: E402
import voice_fragments as vf  # noqa: E402
from test_gpt_guest import PrimaryCall, turn, utterance_ends  # noqa: E402


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def delegation(did, text):
    return {"type": "delegation.created", "item": {"target": "client", "id": did,
                                                   "content": [{"type": "input_text", "text": text}]}}


def user_done(text, tid="u"):
    return turn("done", text, tid=tid)


def voice_done(text, tid="a"):
    return turn("done", text, role="assistant", tid=tid)


class ClassifyTest(unittest.TestCase):
    def test_audit_fragments_are_fragments(self):
        # cos-1128 L1 F7: "NPC" spawned 3 conductor lanes; "Yeah" (112025) ran on its own.
        for text in ("NPC", "Yeah", "Yes please", "Hello", "Mm-hm.", "Okay so", "the dialogue", "What?", ""):
            with self.subTest(text=text):
                self.assertTrue(vf.fragment(text))

    def test_complete_asks_are_not_fragments(self):
        for text in ("check disk space", "restart the lanes", "what's running?", "You can't do that", "Tek, what shipped?",
                     "Can you look at my studio and tell me how much disk space is free"):
            with self.subTest(text=text):
                self.assertFalse(vf.fragment(text))

    def test_backchannel_and_question(self):
        for text in ("Mm-hm.", "Mhm", "Yeah, right.", "Okay.", "Uh-huh", "Got it.", "Mm-hmm"):
            self.assertTrue(vf.backchannel(text), text)
        for text in ("Mm-hm. I'll check.", "Right, the studio has 40 GB free.", ""):
            self.assertFalse(vf.backchannel(text), text)
        self.assertTrue(vf.asks_question("I can look. Want me to check the studio's disk?"))
        # 151433 @1427 s: the question is the second-to-last sentence.
        self.assertTrue(vf.asks_question("I just counted 28 skill changes. Want to clear the memory ones first? "
                                         "They're quicker."))
        self.assertFalse(vf.asks_question("The studio has 40 GB free."))


class GateTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.gate = vf.Gate(self.clock)

    def test_fragment_is_held_and_complete_ask_runs_now(self):
        self.assertEqual(self.gate.check("NPC"), vf.Verdict(False, "NPC", "fragment"))
        self.assertTrue(self.gate.check("check disk space").run)
        self.assertEqual(self.gate.held, 1)

    def test_short_answer_runs_with_the_open_question_once(self):
        self.gate.assistant("Want me to check the studio's disk?")
        verdict = self.gate.check("Yes please")
        self.assertTrue(verdict.run)
        self.assertEqual(verdict.reason, "answer")
        self.assertIn("Want me to check the studio's disk?", verdict.request)
        self.assertTrue(self.gate.check("Yes please").run, "a second route re-checks the same answer")
        self.clock.t += 11
        self.assertFalse(self.gate.check("Yes please").run, "answered: the question is closed")

    def test_a_status_line_after_the_question_leaves_it_open(self):
        self.gate.assistant("Want to clear the memory ones first? They're quicker.")
        self.gate.assistant("Still on that in Hermes, about a minute so far.")
        self.assertEqual(self.gate.check("Yeah, go ahead").reason, "answer")

    def test_open_question_expires_and_full_sentence_closes_it(self):
        self.gate.assistant("Want me to check it?")
        self.clock.t += vf.PENDING_SECONDS + 1
        self.assertFalse(self.gate.check("Yeah").run)
        self.gate.assistant("Should I restart it?")
        self.gate.user("Actually tell me about the boat inspection first")
        self.assertFalse(self.gate.check("Yeah").run)

    def test_backchannel_counts_only_inside_an_open_thought(self):
        self.assertFalse(self.gate.assistant("Mm-hm."), "no thought open yet")
        self.gate.user("So the NPC dialogue in the quest log")
        self.clock.t += 5
        self.assertTrue(self.gate.assistant("Mm-hm."))
        self.clock.t += vf.COALESCE_SECONDS
        self.assertFalse(self.gate.assistant("Mm-hm."), "the thought ended with the window")

    def test_window_marks_stale_words(self):
        self.assertFalse(self.gate.user("NPC"))
        self.clock.t += 29
        self.assertFalse(self.gate.user("dialogue is broken"))
        self.clock.t += vf.COALESCE_SECONDS + 1
        self.assertTrue(self.gate.user("new topic"))


class ProtocolBoundaryTest(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.p = live.Protocol(host_delegation=True)
        self.p.fragments = vf.Gate(self.clock)

    def feed(self, *events):
        for event in events:
            self.p.from_provider(event)

    def test_fragment_delegation_has_no_run_and_coalesces_into_the_next_segment(self):
        self.feed(user_done("NPC", "u1"), delegation("d1", "NPC"))
        self.assertEqual(self.p.pending_host, [("d1", None)])
        self.assertEqual(self.p.held, {"d1": "NPC"})
        self.p.pending_host.clear()
        self.clock.t += 6
        self.feed(user_done("the dialogue in the quest log is broken again", "u2"),
                  delegation("d2", "the dialogue in the quest log is broken again"))
        self.assertEqual(self.p.pending_host, [("d2", "NPC the dialogue in the quest log is broken again")])

    def test_a_distinct_complete_ask_runs_at_once(self):
        self.feed(user_done("How much disk space is free on the studio?"),
                  delegation("d1", "How much disk space is free on the studio?"))
        self.assertEqual(self.p.pending_host, [("d1", "How much disk space is free on the studio?")])
        self.assertEqual(self.p.held, {})

    def test_words_older_than_the_window_never_join_a_new_ask(self):
        self.feed(user_done("NPC", "u1"), delegation("d1", "NPC"))
        self.p.pending_host.clear()
        self.clock.t += vf.COALESCE_SECONDS + 1
        self.feed(user_done("What time is the boat inspection tomorrow", "u2"),
                  delegation("d2", "What time is the boat inspection tomorrow"))
        self.assertEqual(self.p.pending_host, [("d2", "What time is the boat inspection tomorrow")])

    def test_yes_to_the_voices_question_runs_with_the_question(self):
        # 112025: "Yeah" started a run of its own with no idea what it agreed to.
        self.feed(user_done("The studio feels slow today", "u1"), voice_done("Want me to check the studio's disk?"))
        self.clock.t += 2
        self.feed(user_done("Yeah", "u2"), delegation("d1", "Yeah"))
        (did, request), = self.p.pending_host
        self.assertEqual(did, "d1")
        self.assertTrue(request.startswith("Yeah"))
        self.assertIn("Want me to check the studio's disk?", request)

    def test_voice_backchannels_inside_a_long_thought_are_not_exchanges(self):
        # 151433: "Mm-hm" after nearly every segment of Kevin's 10-20 s thoughts.
        self.feed(user_done("So I was thinking about the lanes", "u1"), voice_done("Mm-hm.", "a1"))
        self.clock.t += 8
        self.feed(user_done("and the conductor keeps spawning three of them", "u2"), voice_done("Mm-hm.", "a2"))
        self.clock.t += 8
        self.feed(user_done("for one word fragments, can you find out why", "u3"),
                  delegation("d1", "can you find out why"))
        self.assertEqual([role for role, _ in self.p.exchanges], ["user", "user", "user"])
        self.assertEqual(self.p.fragments.backchannels, 2)
        (_, request), = self.p.pending_host
        self.assertEqual(request, "So I was thinking about the lanes and the conductor keeps spawning three of them "
                                  "for one word fragments, can you find out why")


class CallRouteTest(unittest.IsolatedAsyncioTestCase):
    async def test_fragment_never_reaches_host_delegate_conductor_or_lanes(self):
        call = live.Call(live.Manager(None), profile="")
        started = []

        async def host_delegate(did, request):
            started.append((did, request))
        call.host_delegate = host_delegate
        finals = []

        class Relay:
            async def transcript(self, text, final):
                finals.append((text, final))
        call.relay = Relay()
        call.lane_final = lambda text, turn_id: Relay().transcript(text, True)
        call.message(json.dumps(user_done("NPC", "u1")), True)
        call.message(json.dumps(delegation("d1", "NPC")), True)
        await asyncio.sleep(0)
        self.assertEqual(started, [], "no host run, so no delegation.start for the TV conductor")
        self.assertEqual(call.counts["host_delegation_fragment"], 1)
        self.assertEqual(call.counts["lane_final_fragment"], 1)
        self.assertEqual(finals, [])
        call.message(json.dumps(user_done("the quest log dialogue is broken again", "u2")), True)
        call.message(json.dumps(delegation("d2", "the quest log dialogue is broken again")), True)
        await asyncio.sleep(0)
        self.assertEqual(started, [("d2", "NPC the quest log dialogue is broken again")])
        self.assertEqual(len(finals), 1)
        call.relay = None
        await call.close("test")


class FloorHandoffTest(unittest.IsolatedAsyncioTestCase):
    async def test_the_floor_handoff_holds_a_fragment_and_runs_an_ask(self):
        call = PrimaryCall()
        g = live.GPTGuest(call, None)
        call.guest = call.duplex = g
        g.set_people(2)
        g.observe("primary", turn("done", "Hermes, NPC", 1000, 1600, tid="p1"))
        utterance_ends(g)
        await asyncio.sleep(0)
        self.assertEqual(call.delegated, [], "a fragment never reaches Kevin's Hermes")
        self.assertIn("floor.fragment_held", str(call.traces))
        g.observe("primary", turn("done", "Hermes, how much disk space is free on the studio?", 9000, 11000,
                                  tid="p2"))
        utterance_ends(g)
        await asyncio.sleep(0)
        self.assertEqual(len(call.delegated), 1)
        self.assertIn("disk space", call.delegated[0][1])


if __name__ == "__main__":
    unittest.main()
