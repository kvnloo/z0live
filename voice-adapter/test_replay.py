"""Replays of tonight's failure shapes through the real adapter (replay.py). Each fixture copies the event order and
timing of a real call; the words are made up, because call transcripts stay off GitHub."""
import os
import unittest

os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")  # never the owner's lanes config
import replay  # noqa: E402


def turn(t0, t1, role, text, tid):
    return [{"t": t0, "src": "provider", "rx": {"type": "turn.created", "turn": {"id": tid, "role": role,
                                                                                    "transcript": text.split()[0]}}},
            {"t": t1, "src": "provider", "rx": {"type": "turn.done", "turn": {"id": tid, "role": role,
                                                                                 "transcript": text}}}]


def delegation(t, did, text):
    return [{"t": t, "src": "provider", "rx": {"type": "delegation.created", "item": {
        "id": did, "type": "delegation", "target": "client", "content": [{"type": "input_text", "text": text}]}}}]


def hermes(did, work, answer):
    return [{"t": 0, "cost": "hermes", "did": did, "work": work, "answer": answer}]


def lane(did, tier, s, text):
    return [{"t": 0, "cost": "lane", "did": did, "tier": tier, "s": s, "text": text}]


def run(rows):
    import asyncio
    rows = [{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}] + rows
    return asyncio.run(replay.Replay(rows).run())


class ReplayContract(unittest.TestCase):
    def test_requests_made_while_a_chat_turn_runs_get_one_answer_each(self):
        # 11-20 09:43 call: 11 requests landed while one chat turn ran. Each socket took that turn's one
        # message.complete as its own answer, so one final went to the voice 11 times. Now each runs beside the
        # turn as a background agent and is answered once, by its own answer.
        rows = (turn(5, 10, "user", "What is in the call traces we keep?", "u1") + delegation(9.8, "d1", "call traces")
                + turn(10, 11, "assistant", "On it.", "a1") + hermes("d1", 60, "Each call writes one trace file.")
                + turn(15, 20, "user", "And do we keep the audio too?", "u2") + delegation(19.8, "d2", "audio too")
                + turn(20, 21, "assistant", "Checking.", "a2") + hermes("d2", 30, "The audio is not kept.")
                + turn(28, 30, "user", "Hello, are you there?", "u3") + delegation(29.8, "d3", "Hello")
                + turn(30, 31, "assistant", "Still on it.", "a3") + hermes("d3", 5, "Yes, still here."))
        report = run(rows)
        self.assertEqual(report["duplicate_finals"], {})
        self.assertEqual(report["finals"], 3)
        self.assertEqual(report["backgrounded"], 2)
        self.assertEqual(report["held"], [])
        self.assertEqual(report["unanswered"], [])

    def test_side_by_side_requests_with_the_same_answer_are_spoken_once(self):
        rows = (turn(5, 10, "user", "How much disk is free on the studio?", "u1") + delegation(9.8, "d1", "disk")
                + turn(10, 11, "assistant", "On it.", "a1") + hermes("d1", 20, "The Studio has 23 gigabytes free.")
                + turn(15, 20, "user", "Sorry, I mean free space on the studio", "u2") + delegation(19.8, "d2", "free")
                + turn(20, 21, "assistant", "Checking.", "a2") + hermes("d2", 15, "The Studio has 23 gigabytes free."))
        report = run(rows)
        self.assertEqual(report["finals"], 1)
        self.assertEqual(report["duplicate_finals"], {})

    def test_the_request_is_the_callers_whole_utterance_not_the_last_speech_segment(self):
        # 11-20 20:55 call: the provider's delegation carries only its last segment ("Lane", "requirements, and
        # how far we got") and can beat the caller's turn.done by 0.2-0.4 s.
        rows = (turn(258.4, 268.0, "user", "Can you bring up the garden plan and tell me the progress on each one", "u1")
                + delegation(267.8, "d1", "the progress on each one") + hermes("d1", 10, "Two beds are planted.")
                + turn(268.0, 270.6, "assistant", "I'll lay it out.", "a1")
                + turn(300.0, 309.9, "user", "Um, can you give me an update on the bike repair, my major", "u2")
                + turn(309.9, 310.1, "assistant", "Mm-hm.", "a2")
                + turn(310.1, 312.6, "user", "requirements, and how far we got", "u3")
                + delegation(312.2, "d2", "requirements, and how far we got") + hermes("d2", 5, "The chain is fixed.")
                + turn(312.6, 315.4, "assistant", "Sure, pulling it up.", "a3"))
        report = run(rows)
        self.assertEqual(report["fragments"], [])
        sent = [text for _, _, text in self.last_submitted(rows)]
        self.assertIn("bring up the garden plan", sent[0])
        self.assertIn("update on the bike repair", sent[1])
        self.assertIn("how far we got", sent[1])

    def test_two_delegations_inside_one_utterance_are_one_request(self):
        # 11-21 real GPT-Live probe: delegations at 17.2 s and 20.7 s inside one caller turn that ended at 21.4 s.
        rows = (turn(15.5, 21.4, "user", "Can you look at my studio and tell me how much disk space is free right now",
                     "u1")
                + delegation(17.2, "d1", "Can you look at my studio")
                + delegation(20.7, "d2", "and tell me how much disk space is free right now")
                + hermes("d1", 10, "The Studio has 23 gigabytes free.") + hermes("d2", 10, "The Studio has 23 gigabytes free.")
                + turn(21.4, 24.7, "assistant", "Sure, I'll check that quickly.", "a1"))
        sent = self.last_submitted(rows)
        self.assertEqual([text for _, _, text in sent],
                         ["Can you look at my studio and tell me how much disk space is free right now"])

    def test_one_lane_answer_per_request_then_hermes(self):
        # 11-20 15:17 call: three lane answers and Hermes landed within 4.4 s; the voice spoke a garbled merge
        # and the listener asked "can you repeat the question, you kinda cut off".
        rows = (turn(25.0, 33.6, "user", "He is an intern, ask him an easy first question about the dashboard", "u1")
                + turn(33.6, 33.8, "assistant", "Mm-hm.", "a1") + delegation(34.4, "d1", "ask him an easy question")
                + lane("d1", "medium", 1.9, "Vinay, how would you summarize a week of usage?")
                + lane("d1", "slow", 2.3, "Hey Vinay, start easy: what would you show first on the dashboard?")
                + lane("d1", "fast", 2.9, "Vinay, which number on the dashboard matters most to you?")
                + hermes("d1", 6.4, "Sure. Vinay, what is the first thing you would change in the summary page?")
                + turn(36.6, 56.85, "assistant", "Okay, hey Vinay, easy start.", "a2")
                + turn(60.0, 63.4, "user", "Thanks, that works", "u2"))
        report = run(rows)
        self.assertEqual(report["max_finals_per_delegation"], 2)  # the first lane answer, then Hermes (it adds)

    def test_a_long_hermes_turn_does_not_hold_a_later_request(self):
        # 11-20 10:42 call: a usage check ran 95 s in the chat. Requests made meanwhile joined that turn
        # (receipt "redirected") and heard nothing until it ended; the redirect also turned the long work aside.
        rows = (turn(5, 10, "user", "Check how much of my usage I have left across every account", "u1")
                + delegation(9.8, "d1", "usage left") + turn(10, 11, "assistant", "On it.", "a1")
                + hermes("d1", 95, "You have about forty percent left on the main account.")
                + turn(30, 34, "user", "Meanwhile, what time is the dentist tomorrow?", "u2")
                + delegation(33.8, "d2", "dentist tomorrow") + turn(34, 35, "assistant", "Checking.", "a2")
                + hermes("d2", 6, "The dentist is at nine thirty tomorrow morning."))
        report = run(rows)
        self.assertEqual(report["held"], [])
        self.assertLess(report["own_answer_s"]["d2"], 10)  # its own work (6 s), not the long turn's 95 s
        self.assertLess(report["own_answer_s"]["d1"], 100)  # the long work still lands, spoken when it does

    def test_a_long_hermes_request_gets_a_spoken_receipt_within_seconds(self):
        # A0190: "On it", then minutes of silence (8 of 23 delegations ran over 60 s on 11-20).
        rows = (turn(5, 10, "user", "Check how much of my usage I have left across every account", "u1")
                + delegation(9.8, "d1", "usage left") + turn(10, 11, "assistant", "On it.", "a1")
                + hermes("d1", 95, "You have about forty percent left on the main account."))
        report = run(rows)
        self.assertLessEqual(report["receipt_s"].get("d1", 999), 12)
        self.assertEqual(report["duplicate_finals"], {})

    def test_a_failed_background_agent_is_spoken_not_silent(self):
        rows = (turn(5, 10, "user", "Check every server we run", "u1") + delegation(9.8, "d1", "servers")
                + turn(10, 11, "assistant", "On it.", "a1") + hermes("d1", 60, "All servers are up.")
                + turn(15, 20, "user", "And the phone battery?", "u2") + delegation(19.8, "d2", "battery")
                + turn(20, 21, "assistant", "Checking.", "a2") + hermes("d2", 3, "error: provider timed out"))
        r = replay.Replay([{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}] + rows)
        import asyncio
        report = asyncio.run(r.run())
        said = [(e.get("delegation_item_id"), "".join(c.get("text", "") for c in e.get("content", []))) for _, e in r.sent]
        self.assertTrue(any(did == "d2" and "couldn't finish" in text for did, text in said))
        self.assertEqual(report["held"], [])

    def test_a_quick_hermes_answer_gets_no_receipt(self):
        rows = (turn(5, 10, "user", "What is on my calendar today?", "u1") + delegation(9.8, "d1", "calendar")
                + turn(10, 11, "assistant", "One sec.", "a1") + hermes("d1", 4, "Just the dentist at nine thirty."))
        self.assertEqual(run(rows)["receipt_s"], {})

    def last_submitted(self, rows):
        import asyncio
        r = replay.Replay([{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}] + rows)
        asyncio.run(r.run())
        return r.submitted


def speech(t0, t1, text, tid):
    """A voice reply as the provider streams it: turn.created, one output_transcript.added per word, turn.done."""
    parts = text.split()
    step = (t1 - t0) / max(len(parts), 1)
    rows = [{"t": t0, "src": "provider", "rx": {"type": "turn.created", "turn": {"id": tid, "role": "assistant"}}}]
    rows += [{"t": round(t0 + i * step, 3), "src": "provider", "rx": {"type": "output_transcript.added",
                                                                     "item": {"text": " " + word}}}
             for i, word in enumerate(parts)]
    return rows + [{"t": t1, "src": "provider", "rx": {"type": "turn.done", "turn": {
        "id": tid, "role": "assistant", "transcript": text}}}]


GUEST = [{"t": 1.4, "src": "guest", "rx": {"type": "session.started"}}]  # a second voice (Tek) on the call


class TwoVoiceDelivery(unittest.TestCase):
    """The guest's floor path decides when an answer reached the caller (11-20 night calls 192702, 181920, 205504)."""

    def test_answer_folded_into_the_reply_playing_is_not_sent_again(self):
        # 192702: Hermes's answer landed 2 s into a 24 s reply, which spoke it; 15 s after the reply ended it was
        # sent again and the voice said "Quick correction, actually nothing new has started".
        rows = (GUEST + turn(670, 681.5, "user", "Did the garden jobs finish and did the shed get built?", "u1")
                + delegation(681.4, "d1", "garden jobs")
                + hermes("d1", 17.3, "Nothing new has started. I need your lumber budget and the shed permit first.")
                + speech(681.6, 682.4, "Checking.", "a0")
                + turn(690, 696.5, "user", "And how are the tomatoes?", "u2")
                + speech(696.8, 720.8, "Four of the five garden jobs are done. The tomatoes are fine. I need your "
                                       "lumber budget and the shed permit before the shed.", "a1")
                + turn(780, 782, "user", "Thanks.", "u3"))
        report = run(rows)
        self.assertEqual(report["repeats"], [])
        self.assertEqual(report["lost"], [])

    def test_answer_merged_mid_reply_is_not_resent_into_the_callers_next_turn(self):
        # 181920: the Android answer arrived while the OpenROAD reply played and that reply spoke it; the 463 s
        # resend went out as the caller started a new request.
        rows = (GUEST + turn(255, 260, "user", "Where are the bike and the boat repairs at?", "u1")
                + delegation(259.8, "d1", "bike and boat")
                + hermes("d1", 169.6, "The boat has a new hull patch, not painted yet, and the motor isn't fixed.")
                + speech(260.1, 261, "On it.", "a0")
                + turn(405, 413.7, "user", "So what happened with the bike?", "u2")
                + speech(413.8, 448.1, "The bike chain is fixed and the brakes need pads. The boat has a new hull "
                                       "patch, but it's not painted, and the motor isn't fixed yet.", "a1")
                + turn(453.2, 464.3, "user", "Can you start on all the high priority repairs now", "u3"))
        report = run(rows)
        self.assertEqual(report["repeats"], [])

    def test_answer_no_reply_said_is_sent_again_once_the_voice_is_quiet(self):
        # 205504 at 240.1 s: the answer went out as the caller spoke; the voice said "Not yet" (cut off) and
        # "Checking now" and never the answer, and nothing sent it again.
        rows = (GUEST + turn(170, 174, "user", "Can you install the new kettle firmware?", "u1")
                + delegation(173.8, "d1", "kettle firmware")
                + hermes("d1", 66.3, "I couldn't install it because the kettle is unplugged from the wifi.")
                + speech(174.1, 175, "Sure.", "a0")
                + turn(237.7, 241.3, "user", "So you have no firmware after all those changes", "u2")
                + speech(241.4, 242.1, "Not yet—since", "a1")
                + turn(242.1, 244.4, "user", "or even a photo", "u3")
                + speech(244.4, 246.6, "Checking now.", "a2")
                + turn(270, 272, "user", "Okay.", "u4"))
        report = run(rows)
        self.assertEqual(report["lost"], [])
        self.assertEqual(report["repeats"], [])


def heard(t0, t1, text, tid, src="provider", start_ms=None):
    """A caller turn as one leg hears it: turn.created, the words as one turn.delta, turn.done."""
    start = int((t0 - 0.6) * 1000) if start_ms is None else start_ms
    end = start + int((t1 - t0) * 1000)
    done = {"id": tid, "role": "user", "transcript": text, "start_ms": start, "end_ms": end}
    return [{"t": t0, "src": src, "rx": {"type": "turn.created", "turn": {**done, "transcript": "", "end_ms": start + 200}}},
            {"t": round(t1 - 0.3, 3), "src": src, "rx": {"type": "turn.delta", "turn_id": tid, "delta": text,
                                                       "start_ms": start + 200, "end_ms": end}},
            {"t": t1, "src": src, "rx": {"type": "turn.done", "turn": done}}]


def guest_delegation(t, did, text):
    return [{**row, "src": "guest"} for row in delegation(t, did, text)]


def guest_speech(t0, t1, text, tid):
    return [{**row, "src": "guest"} for row in speech(t0, t1, text, tid)]


# Two people at one phone (Kevin and a friend) and two bots (Hermes, Tek). Take 7 of the 11-22 film call: timing and
# event order from its trace, words made up.
PEOPLE = [{"role": "user", "content": [{"type": "input_text", "text": "status on the laptop beta?"}]},
          {"role": "assistant", "content": [{"type": "output_text", "text": "This call has two people at the desk, "
                                             "Kevin and his friend Sam, and two bots: Hermes and Tek. When Kevin and "
                                             "Sam talk to each other, stay quiet."}]}]


class PeopleOnTheCall(unittest.TestCase):
    def test_bots_stay_quiet_while_the_people_talk_to_each_other(self):
        # Take 7: the friend's "Is the glass theme on your phone yet?" (to Kevin) ran a Hermes job that checked the
        # phone, Tek drafted a reply, and Kevin's "No, I think it's still last night's build" got bot speech too.
        rows = (GUEST
                # 0. friend -> Kevin
                + heard(6.2, 12.3, "Hey, wait, is the dark mode on your laptop yet? I don't see it on mine", "g0", "guest")
                + heard(6.8, 12.9, "Hey, wait, is the dark mode on your laptop yet? I don't see it on mine", "p0")
                + guest_delegation(11.75, "gd0", "Hey, wait, is the dark mode on your laptop yet?")
                + delegation(12.8, "d0", "Hey, wait, is the dark mode on your laptop yet?")
                + hermes("d0", 40, "Your laptop is on last night's build.")
                + guest_speech(12.0, 14.6, "Yeah, let me check that quickly.", "ga0")
                + speech(12.95, 14.7, "I'll check your laptop quickly.", "a0")
                # 1. Kevin -> friend
                + heard(13.4, 17.8, "No. I think it's still last night's build", "p1")
                + heard(13.0, 19.0, "No. I think it's still last night's build", "g1", "guest")
                + guest_delegation(18.7, "gd1", ". I think it's still last night's build")
                + speech(17.9, 23.6, "Mm-hm. Yeah, it's still on the older build.", "a1")
                + guest_speech(19.1, 22.0, "Thanks, checking that now.", "ga1")
                # 2. Kevin -> Hermes, by name
                + heard(23.2, 27.8, "Hermes, what's actually on my laptop right now versus the beta", "p2")
                + heard(22.6, 27.9, "Hermes, what's actually on my laptop right now versus the beta", "g2", "guest")
                + delegation(27.0, "d2", "Hermes, what's actually on my laptop right now versus the beta")
                + guest_delegation(27.5, "gd2", "Hermes, what's actually on my laptop right now versus the beta")
                + hermes("d2", 12, "Your laptop runs build 9. The beta has twenty newer changes.")
                + speech(28.0, 40.9, "I'll take a quick look. Your laptop runs build 9. The beta has twenty newer "
                                     "changes.", "a2")
                # 3. Kevin -> Hermes, the next request in that exchange (no name)
                + heard(40.2, 52.6, "Yeah. Shipped isn't done. Get all of it onto my laptop, prove each one, and "
                                    "make it update itself. Take as long as you need, just send receipts", "p3")
                + heard(41.6, 52.7, "Yeah. Shipped isn't done. Get all of it onto my laptop, prove each one, and "
                                    "make it update itself. Take as long as you need, just send receipts", "g3", "guest")
                + guest_delegation(51.8, "gd3", "Yeah. Shipped isn't done. Get all of it onto my laptop")
                + delegation(52.2, "d3", "Yeah. Shipped isn't done. Get all of it onto my laptop")
                + hermes("d3", 30, "That run is already going as card 7; the last receipt is the signed build.")
                + speech(52.8, 56.0, "Got it, I'll handle that and keep receipts.", "a3")
                # 4. friend -> Tek, by name
                + heard(65.0, 71.7, "Hey, Tek. While that runs, what actually caused the dark mode crash in PR twelve",
                        "g4", "guest")
                + heard(65.4, 72.0, "Hey, Tek. While that runs, what actually caused the dark mode crash in PR twelve",
                        "p4")
                + guest_delegation(70.8, "gd4", "Hey Tek, while that runs, what actually caused the crash in PR 12")
                + guest_speech(72.0, 75.2, "I'll pull that up.", "ga4")
                + guest_speech(87.2, 91.8, "PR twelve fixed a theme lookup that crashed on launch.", "ga5")
                # 5. friend -> Tek's answer, a reaction (no request)
                + heard(92.2, 96.7, "Okay, nice. That's the one I kept hitting", "g5", "guest")
                + heard(92.6, 97.2, "Okay, nice. That's the one I kept hitting", "p5")
                + guest_delegation(96.5, "gd5", "Okay, nice. That's the one I kept hitting")
                + delegation(97.0, "d5", ", nice. That's the one I kept hitting")
                + speech(97.3, 106.4, "Thanks, I'll check. Yeah, the crash is fixed in the beta.", "a5")
                + guest_speech(97.5, 100.6, "Thanks, I'll recheck with that in mind.", "ga6"))
        r = replay.Replay([{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}] + rows, history=PEOPLE)
        import asyncio
        asyncio.run(r.run())
        self.assertEqual([did for did, _ in r.requests], ["d2", "d3"], "Hermes runs only the two requests to it")
        self.assertEqual(len(r.guest_asked), 1, "Tek's Hermes runs only the question to Tek")
        self.assertIn("crash in PR twelve", r.guest_asked[0][1])
        before = "".join(text for t, text in r.heard if t < 27.8)
        self.assertEqual(before, "", "nothing the bots said while the people talked reached the phone")
        after_tek = "".join(text for t, text in r.heard if 96.5 <= t < 120 and "PR twelve" not in text)
        self.assertNotIn("I'll check", after_tek)

    def test_a_question_named_to_tek_on_one_leg_stays_with_tek_when_the_other_leg_mishears_the_name(self):
        # Take 9: Hermes's leg heard "Hey Tek, while" and gave Tek the floor mid-line; Tek's own leg heard "Hey Tech,
        # while that runs ..." and its delegation (that leg's final) found no name in it, so the same utterance was
        # taken for people talk: the floor went to the people, Tek's Hermes was never asked and Tek stayed silent.
        rows = (GUEST
                + heard(23.2, 27.8, "Hermes, what's actually on my laptop right now versus the beta", "p2")
                + heard(22.6, 27.9, "Hermes, what's actually on my laptop right now versus the beta", "g2", "guest")
                + delegation(27.0, "d2", "Hermes, what's actually on my laptop right now versus the beta")
                + hermes("d2", 12, "Your laptop runs build 9. The beta has twenty newer changes.")
                + speech(28.0, 40.9, "Your laptop runs build 9. The beta has twenty newer changes.", "a2")
                + heard(65.0, 71.7, "Hey Tek, while that runs, what actually caused the dark mode crash in PR "
                                    "twelve", "p4")
                # Tek's leg kept the turn open: no turn.done, no reply of its own after its delegation (take 9).
                + heard(65.4, 72.0, "Hey Tech, while that runs, what actually caused the dark mode crash in PR "
                                    "twelve", "g4", "guest")[:2]
                + guest_delegation(71.6, "gd4", "Tech, while that runs, what actually caused the dark mode crash "
                                                "in PR twelve")
                + [{"t": 90.0, "src": "guest", "rx": {"type": "session.usage.updated"}}])
        r = replay.Replay([{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}] + rows, history=PEOPLE)
        import asyncio
        asyncio.run(r.run())
        self.assertEqual([did for did, _ in r.requests], ["d2"], "Hermes runs only the request to it")
        self.assertEqual(len(r.guest_asked), 1, "Tek's Hermes runs the question to Tek")
        self.assertIn("crash in PR twelve", r.guest_asked[0][1])


if __name__ == "__main__":
    unittest.main()
