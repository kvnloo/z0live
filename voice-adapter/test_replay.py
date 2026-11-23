"""Replays of tonight's failure shapes through the real adapter (replay.py). Each fixture copies the event order and
timing of a real call; the words are made up, because call transcripts stay off GitHub."""
import json
import os
from pathlib import Path
import tempfile
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

    def test_a_request_past_the_side_bound_is_queued_not_folded_into_the_running_turn(self):
        # Take 7 of the 11-22 film call, chat path: Kevin's long-run ask waited behind a running turn while the voice
        # said "Got it, I'll handle that". With the chat turn and SIDE_MAX background agents busy, the next request
        # was still submitted into the busy chat: the dashboard redirected the running turn, so that request had no
        # answer of its own and the first request was answered with the redirect's words. Now it waits for a free
        # agent, the voice says at once that it is queued and its place in line, and it gets its own answer.
        import mobile_live as live
        rows = (turn(5, 10, "user", "Install every merged build on my phone", "u1") + delegation(9.8, "d1", "install")
                + turn(10, 11, "assistant", "On it.", "a1") + hermes("d1", 120, "Every merged build is on the phone."))
        for i, t in enumerate((20, 30, 40, 50), start=2):
            rows += (turn(t - 5, t, "user", f"Question number {i} about the studio", f"u{i}")
                     + delegation(t - 0.2, f"d{i}", f"question {i}") + turn(t, t + 1, "assistant", "Checking.", f"a{i}")
                     + hermes(f"d{i}", 60 if i < 5 else 5, f"Answer number {i}."))
        self.assertEqual(live.SIDE_MAX, 3, "fixture: d2-d4 fill the background agents, d5 is past the bound")
        report = run(rows)
        self.assertEqual(report["held"], [], "a request past the bound was folded into the running turn")
        self.assertEqual(report["unanswered"], [])
        self.assertEqual(report["backgrounded"], 4, "d5 runs as its own background agent once one is free")
        queued = report["queued_s"]
        self.assertIn("d5", queued, "the voice was never told d5 is queued")
        self.assertLess(queued["d5"], 1.0, "the queued notice is prompt")
        self.assertNotIn("d5", report["receipt_s"], "'still working on it' is not true of a queued request")
        # d5 starts when the first background agent (d2, 60 s from 19.8 s) finishes, and gets its own answer.
        self.assertGreaterEqual(report["own_answer_s"]["d5"], 79.8 - 49.8)
        self.assertEqual(report["answers"]["d1"], "Every merged build is on the phone.")
        self.assertEqual(report["answers"]["d5"], "Answer number 5.")

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


def speech(t0, t1, text, tid, *, span=False):
    """A voice reply as the provider streams it: turn.created, one output_transcript.added per word, turn.done.
    `span`: turn.done carries the reply's start_ms/end_ms on the provider clock (heard()'s: t - 0.6 s)."""
    parts = text.split()
    step = (t1 - t0) / max(len(parts), 1)
    rows = [{"t": t0, "src": "provider", "rx": {"type": "turn.created", "turn": {"id": tid, "role": "assistant"}}}]
    rows += [{"t": round(t0 + i * step, 3), "src": "provider", "rx": {"type": "output_transcript.added",
                                                                     "item": {"text": " " + word}}}
             for i, word in enumerate(parts)]
    clock = {"start_ms": int((t0 - 0.6) * 1000), "end_ms": int((t1 - 0.6) * 1000)} if span else {}
    return rows + [{"t": t1, "src": "provider", "rx": {"type": "turn.done", "turn": {
        "id": tid, "role": "assistant", "transcript": text, **clock}}}]


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


def take7(handoff="Got it, I'll handle that and keep receipts."):
    """Take 7 of the 11-22 film call, both legs; `handoff` is the voice's own line as it hands line 3 to Hermes. Lines 0, 1 and 5 are the people talking to each other; 2 and 3 are
    to Hermes (by name, then the next request in that exchange); 4 is to Tek by name."""
    return (GUEST
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
                + speech(52.8, 56.0, handoff, "a3")
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


def replayed(rows, history=PEOPLE):
    import asyncio
    r = replay.Replay([{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}] + rows, history=history)
    asyncio.run(r.run())
    return r


class PeopleOnTheCall(unittest.TestCase):
    def test_bots_stay_quiet_while_the_people_talk_to_each_other(self):
        # Take 7: the friend's "Is the glass theme on your phone yet?" (to Kevin) ran a Hermes job that checked the
        # phone, Tek drafted a reply, and Kevin's "No, I think it's still last night's build" got bot speech too.
        r = replayed(take7())
        self.assertEqual([did for did, _ in r.requests], ["d2", "d3"], "Hermes runs only the two requests to it")
        self.assertEqual(len(r.guest_asked), 1, "Tek's Hermes runs only the question to Tek")
        self.assertIn("crash in PR twelve", r.guest_asked[0][1])
        before = "".join(text for t, text in r.heard if t < 27.8)
        self.assertEqual(before, "", "nothing the bots said while the people talked reached the phone")
        after_tek = "".join(text for t, text in r.heard if 96.5 <= t < 120 and "PR twelve" not in text)
        self.assertNotIn("I'll check", after_tek)

    def test_with_hermes_the_only_bot_it_stays_quiet_while_the_people_talk_to_each_other(self):
        # Take 7's lines with no second voice on the call (Kevin, a friend and Hermes). The people gate lived only in
        # the two-voice path, so here Hermes still ran the friend's question to Kevin and answered Kevin's reply.
        r = replayed([row for row in take7() if row.get("src") != "guest"])
        self.assertEqual([did for did, _ in r.requests], ["d2", "d3"],
                         "Hermes runs its named request and the next request in that exchange, nothing else")
        self.assertNotIn("last night's build", r.requests[0][1], "words the people said to each other never join "
                         "Hermes's next request")
        before = "".join(text for t, text in r.heard if t < 27.8)
        self.assertEqual(before, "", "nothing Hermes said while the people talked reached the phone")
        after = "".join(text for t, text in r.heard if t >= 92.2)
        self.assertEqual(after, "", "the friend's reaction to Tek gets no reply from Hermes")

    def test_with_one_person_on_the_call_an_unnamed_question_still_goes_to_hermes(self):
        # Control: the gate is for calls with more than one person. Alone with Hermes, Kevin never says its name.
        r = replayed([row for row in take7() if row.get("src") != "guest"], history=None)
        self.assertIn("d0", [did for did, _ in r.requests])
        self.assertTrue("".join(text for t, text in r.heard if t < 27.8), "Hermes answers out loud")

    def test_the_voice_hands_off_neutrally_and_hermes_says_what_happened(self):
        # Takes 7 and 9: handing line 3 to Hermes the voice said "Got it, I'll handle that and keep receipts" / "I'm
        # taking that on end-to-end. I'll be back with receipts", and Hermes answered with a report on work already
        # running. The replay flags a promise made before the request's own answer reached the voice.
        import mobile_live
        promised = replayed(take7("Got it, I'm taking that on end-to-end. I'll be back with receipts once it's all "
                                  "buttoned up."))
        self.assertEqual([p["did"] for p in replay.report(promised)["promised"]], ["d3"])
        neutral = replayed(take7("Let me check."))
        report = replay.report(neutral)
        self.assertEqual(report["promised"], [])
        self.assertEqual([did for did, _ in neutral.requests], ["d2", "d3"], "line 3 still reaches Hermes")
        self.assertEqual("".join(text for t, text in neutral.heard if t < 27.8), "", "lines 0-1 stay quiet")
        # What happened reaches the voice from Hermes, as d3's own Agent Final Message, and the adapter's own words
        # about d3 never promise new work either.
        self.assertIn("d3", report["own_answer_s"])
        about = ["".join(c.get("text", "") for c in e.get("content", []))
                 for _, e in neutral.sent if e.get("delegation_item_id") == "d3" and e.get("channel") == "speakable"]
        answers = [text for text in about if mobile_live.RECEIPT not in text]  # the receipt: the run really runs
        self.assertTrue(answers[0].startswith(replay.FINAL) and "already going as card 7" in answers[0], about)
        self.assertFalse([text for text in about if mobile_live.promises_new_work(text)])

    def test_a_request_the_bot_talks_over_is_judged_on_the_whole_utterance(self):
        # Take 10: Kevin began his long-run ask 12 ms after Hermes's answer ended; 1.4 s in, Hermes's voice said
        # "Right" over him. That output committed his turn as final at "Yeah, merged isn't" (no request in it yet), so
        # it was taken for people talk and the ask never reached Hermes. His turn.done came 8 s later with all of it.
        ask = "Yeah, merged isn't done. Get all of it onto my laptop, prove each one, and make it update itself"
        rows = (GUEST
                + heard(23.2, 27.8, "Hermes, what's actually on my laptop right now versus the beta", "p2")
                + heard(22.6, 27.9, "Hermes, what's actually on my laptop right now versus the beta", "g2", "guest")
                + delegation(27.0, "d2", "Hermes, what's actually on my laptop right now versus the beta")
                + hermes("d2", 12, "Your laptop runs build 9. The beta has twenty newer changes.")
                + speech(27.8, 42.6, "Comparing those now. Your laptop runs build 9. The beta has twenty newer "
                                     "changes.", "a2")
                + [{"t": 42.63, "src": "provider", "rx": {"type": "turn.created", "turn": {
                    "id": "p3", "role": "user", "transcript": " Yeah", "start_ms": 41800, "end_ms": 42000}}},
                   {"t": 43.35, "src": "provider", "rx": {"type": "turn.delta", "turn_id": "p3",
                                                          "delta": ", merged isn't", "start_ms": 42000,
                                                          "end_ms": 42700}}]
                + speech(44.0, 45.0, "Right.", "a3")
                + [{"t": 47.0, "src": "provider", "rx": {"type": "turn.delta", "turn_id": "p3",
                                                         "delta": " done. Get all of it onto my laptop",
                                                         "start_ms": 42700, "end_ms": 46000}}]
                + delegation(52.1, "d3", ask)
                + [{"t": 52.3, "src": "provider", "rx": {"type": "turn.done", "turn": {
                    "id": "p3", "role": "user", "transcript": ask, "start_ms": 41800, "end_ms": 51400}}}]
                + hermes("d3", 20, "That run is already going as card 7.")
                # Kevin's mic stayed loud through his ask while its words trailed (the trace's input spans)
                + [{"t": 42.4, "input_loud": True}, {"t": 51.4, "input_loud": False}])
        r = replay.Replay([{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}] + rows, history=PEOPLE)
        import asyncio
        asyncio.run(r.run())
        # On the call the voice obeyed the people note sent at 44.0 s and never delegated; the replay's recorded voice
        # delegates anyway, so the contract is that no such note goes out while the ask is still being spoken.
        notes = [t for t, event in r.sent if "talking to each other" in json.dumps(event)]
        self.assertEqual(notes, [], "no people-talk note while Kevin's request to Hermes is still being spoken")
        self.assertEqual([did for did, _ in r.requests], ["d2", "d3"], "the long-run ask reaches Hermes")

    def test_an_answer_waits_until_the_caller_has_finished_the_next_request(self):
        # Take 11: Hermes's answer to line 2 reached the voice at 51.0 s, 8 s into Kevin's long-run ask (turn.done at
        # 52.1). The voice answered the ask from it and never handed the ask to Hermes.
        ask = "Yeah, merged isn't done. Get all of it onto my laptop, prove each one, and make it update itself"
        words = ask.split()
        rows = (GUEST
                + heard(23.2, 27.8, "Hermes, what's actually on my laptop right now versus the beta", "p2")
                + heard(22.6, 27.9, "Hermes, what's actually on my laptop right now versus the beta", "g2", "guest")
                + delegation(27.0, "d2", "Hermes, what's actually on my laptop right now versus the beta")
                + hermes("d2", 24, "Your laptop is on build 30812 from 3:15 this morning, per the card's receipts.")
                + speech(27.8, 40.9, "Comparing those now. Your laptop is still on last night's build.", "a2")
                + [{"t": 41.0, "src": "provider", "rx": {"type": "turn.created", "turn": {
                    "id": "p3", "role": "user", "transcript": "", "start_ms": 40400, "end_ms": 40600}}}]
                + [{"t": round(41.2 + i * 0.6, 3), "src": "provider", "rx": {
                    "type": "turn.delta", "turn_id": "p3", "delta": " " + word,
                    "start_ms": 40600 + i * 600, "end_ms": 41200 + i * 600}} for i, word in enumerate(words)]
                + delegation(52.0, "d3", ask)
                + [{"t": 52.1, "src": "provider", "rx": {"type": "turn.done", "turn": {
                    "id": "p3", "role": "user", "transcript": ask, "start_ms": 40400, "end_ms": 51400}}}]
                + hermes("d3", 20, "That run is already going as card 7."))
        r = replay.Replay([{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}] + rows, history=PEOPLE)
        import asyncio
        asyncio.run(r.run())
        handed = [t for t, event in r.sent if event.get("type") == "delegation.context.append"]
        self.assertTrue(handed, "the line-2 answer still reaches the voice")
        # Until the voice hands the ask on (its delegation at 52.0 s ends the caller's turn), nothing reaches it.
        self.assertEqual([t for t in handed if 41.0 <= t < 52.0], [], "nothing handed to the voice mid-request")
        self.assertEqual([did for did, _ in r.requests], ["d2", "d3"])

    def test_an_answer_handed_back_after_people_talk_reaches_the_phone(self):
        # Take 12, line 3: Hermes's answer came back while Tek spoke, and the friend's reaction to Tek gave the floor
        # to the people. The primary voice began reading the answer before answer_ready gave it the floor back, and
        # that reply stayed muted: the phone was silent from 95 s to the end and the answer was dropped as unspoken.
        answer = "That run is already going as card seven. Your laptop has been on build 30812 since 3:15 this morning."
        rows = (GUEST
                + heard(40.2, 52.6, "Hermes, get all of it onto my laptop, prove each one, and send receipts", "p3")
                + heard(41.6, 52.7, "Hermes, get all of it onto my laptop, prove each one, and send receipts", "g3",
                        "guest")
                + delegation(52.2, "d3", "Hermes, get all of it onto my laptop, prove each one, and send receipts")
                + hermes("d3", 40, answer)
                + speech(52.8, 56.0, "Let me look into that.", "a3")
                + heard(65.0, 71.7, "Hey, Tek. While that runs, what actually caused the crash in PR twelve", "g4",
                        "guest")
                + heard(65.4, 72.0, "Hey, Tek. While that runs, what actually caused the crash in PR twelve", "p4")
                + guest_delegation(70.8, "gd4", "Hey Tek, while that runs, what actually caused the crash in PR 12")
                + guest_speech(72.0, 75.2, "I'll pull that up.", "ga4")
                + guest_speech(87.2, 93.4, "PR twelve fixed a theme lookup that crashed on launch.", "ga5")
                # the friend's reaction to Tek; the primary leg's final for it trails by 2 s (take 12: 96.2, 99.0)
                + heard(93.5, 96.2, "Okay, nice. That's the one I kept hitting", "g5", "guest")
                + heard(93.6, 99.0, "Okay, nice. That's the one I kept hitting", "p5")
                + speech(98.98, 112.0, "That's already going as card seven. One correction: your laptop has been on "
                                       "build 30812 since 3:15 this morning.", "a6"))
        r = replay.Replay([{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}] + rows, history=PEOPLE)
        import asyncio
        report = asyncio.run(r.run())
        heard_after = "".join(text for t, text in r.heard if t >= 98.9)
        self.assertIn("30812", heard_after, "the answer the voice reads reaches the phone")
        self.assertEqual(report["lost"], [])

    def cut_line_3(self, *after):
        """Take 13, line 3 (words changed): Hermes's answer had played one sentence when the friend's "Hey, Tek.
        While that runs, ..." talked over it; Tek answered, the friend reacted, and the primary voice said nothing
        more. `after` is what the primary voice says once the floor comes back."""
        answer = ("That run is already going as card seven, so I won't start a second one. The receipts so far are a "
                  "signed build and passing screenshot tests. I can't install anything from this call.")
        rows = (GUEST
                + heard(40.2, 52.6, "Hermes, get all of it onto my laptop, prove each one, and send receipts", "p3")
                + heard(41.6, 52.7, "Hermes, get all of it onto my laptop, prove each one, and send receipts", "g3",
                        "guest")
                + delegation(52.2, "d3", "Hermes, get all of it onto my laptop, prove each one, and send receipts")
                + hermes("d3", 8.9, answer)
                + speech(52.8, 54.0, "Let me check.", "a3")
                + [{"t": 61.9, "output_loud": True}, {"t": 66.7, "output_loud": False}]
                + speech(61.3, 66.6, "It's still going as card seven, so I won't start a second one.", "a4")
                + heard(64.4, 66.5, "Hey, Tek. While that runs, what caused the crash in PR twelve", "g4", "guest")
                + heard(64.8, 66.9, "Hey, Tek. While that runs, what caused the crash in PR twelve", "p4")
                + guest_delegation(66.8, "gd4", "Hey Tek, while that runs, what caused the crash in PR twelve")
                + guest_speech(72.0, 75.2, "I'll pull that up.", "ga4")
                + guest_speech(87.2, 93.4, "PR twelve fixed a theme lookup that crashed on launch.", "ga5")
                + heard(93.5, 96.2, "Okay, nice. That's the one I kept hitting", "g5", "guest")
                + heard(93.6, 99.0, "Okay, nice. That's the one I kept hitting", "p5")
                + list(after)
                + [{"t": 130.0, "src": "guest", "rx": {"type": "session.usage.updated"}}])
        r = replay.Replay([{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}] + rows, history=PEOPLE)
        import asyncio
        return r, asyncio.run(r.run())

    def test_an_answer_a_barge_in_cut_is_lost_unless_its_rest_comes_back(self):
        from unittest.mock import patch
        import mobile_live
        r, report = self.cut_line_3()
        self.assertIn("floor.barge_in", [ev for _, ev, _ in r.floor], "fixture: the friend's words cut the answer")
        resumed = [t for t, event in r.sent if "anything from this call" in json.dumps(event) and t > 99]
        self.assertTrue(resumed, "the rest goes back to the voice once the floor does")
        self.assertEqual(report["lost"], [], "handed back: the recorded voice never had that hand-back to say")
        with patch.object(mobile_live.GPTGuest, "cut", lambda g, leg: g.ledger.cut(leg)):  # drops the rest
            r, report = self.cut_line_3()
        self.assertEqual([(x["did"], x.get("cut")) for x in report["lost"]], [("d3", True)])
        r, report = self.cut_line_3(*speech(101.5, 108.0, "The receipts so far are a signed build and passing "
                                                          "screenshot tests. I can't install anything from this "
                                                          "call.", "a6"))
        self.assertIn("anything from this call", "".join(text for t, text in r.heard if t > 100))
        self.assertEqual(report["lost"], [])
        self.assertEqual(report["repeats"], [])

    def test_an_answer_the_provider_cut_under_the_callers_words_is_lost_until_handed_back(self):
        # Take 14, line 5 (words changed): the provider ended the reply reading line 3's answer when the friend said
        # "Okay, nice". No barge-in fired, that turn never closed, the rest was never handed back, and the replay credited
        # the answer as spoken (floor.answer_spoken) and reported lost: [].
        answer = ("That run is already going as card seven, so I won't start a second one. The receipts so far are a "
                  "signed build and passing screenshot tests. I can't install anything from this call.")
        ask = "Hermes, get all of it onto my laptop, prove each one, and send receipts"
        okay = {"id": "p5", "role": "user", "transcript": "Okay", "start_ms": 65800, "end_ms": 66000}
        rows = (GUEST + heard(40.2, 52.6, ask, "p3") + heard(41.6, 52.7, ask, "g3", "guest")
                + delegation(52.2, "d3", ask) + hermes("d3", 8.9, answer) + speech(52.8, 54.0, "Let me check.", "a3")
                + speech(61.3, 66.6, "It's still going as card seven, so I won't start a second one.", "a4", span=True)
                + [{"t": 66.65, "src": "provider", "rx": {"type": "turn.created", "turn": okay}},
                   {"t": 90.0, "src": "guest", "rx": {"type": "session.usage.updated"}}])
        r = replay.Replay([{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}] + rows, history=PEOPLE)
        import asyncio
        report = asyncio.run(r.run())
        self.assertNotIn("floor.barge_in", [ev for _, ev, d in r.floor if d.get("by") != "provider"],
                         "fixture: no barge-in of the adapter's own")
        resumed = [t for t, event in r.sent if "anything from this call" in json.dumps(event) and t > 66.6]
        self.assertTrue(resumed, "the rest goes back to the voice in the caller's pause")
        self.assertEqual(report["lost"], [])
        from unittest.mock import patch
        import mobile_live
        with patch.object(mobile_live.GPTGuest, "cut", lambda g, leg: g.ledger.cut(leg)):  # drops the rest
            report = asyncio.run(replay.Replay([{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}]
                                               + rows, history=PEOPLE).run())
        self.assertEqual([(x["did"], x.get("cut")) for x in report["lost"]], [("d3", True)])

    def test_a_question_to_tek_heard_as_tech_reaches_teks_hermes(self):
        # Takes 9 and 14: both legs transcribed "Hey, Tek" as "Hey, Tech", the floor took line 4 for people talk,
        # and Tek's Hermes was never asked about PR 28.
        rows = [json.loads(json.dumps(row).replace("Hey, Tek.", "Hey, Tech.").replace("Hey Tek,", "Hey Tech,"))
                for row in take7()]
        r = replayed(rows)
        report = replay.report(r)
        self.assertTrue(any("dark mode crash" in request for _, request in r.guest_asked), r.guest_asked)
        self.assertEqual(report["unasked"], [])

    def test_a_fixture_keeps_when_the_phone_heard_output_and_teks_answers(self):
        # Without the loud output a replay never sees a barge-in, and without Tek's answer it hears a made-up one.
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "call.jsonl"
            trace.write_text("".join(json.dumps(row) + "\n" for row in [
                {"t": 61.919, "ev": "audio.on", "dir": "output", "peak": 3628},
                {"t": 62.2, "ev": "audio.on", "dir": "input", "peak": 4138},
                {"t": 67.005, "ev": "audio.off", "dir": "output", "at": 66.704},
                {"t": 71.9, "ev": "guest.delegation.start", "did": "gd4"},
                {"t": 90.6, "ev": "guest.delegation.done", "did": "gd4", "answer": "A startup timing bug."}]))
            rows = replay.fixture(trace)
        self.assertEqual(rows, [{"t": 61.919, "output_loud": True}, {"t": 62.2, "input_loud": True},
                                {"t": 66.704, "output_loud": False},
                                {"t": 90.6, "cost": "guest", "work": 18.7, "answer": "A startup timing bug."}])

    def test_a_fixture_keeps_the_calls_people_count(self):
        # Take 12's people count came from the chat the call opened on; a fixture cut from its trace replayed it as a
        # call with one person, so the floor never went to the people and the muted answer was never seen.
        with tempfile.TemporaryDirectory() as tmp:
            trace = Path(tmp) / "call.jsonl"
            trace.write_text(json.dumps({"t": 0.7, "ev": "floor.people", "people": 2}) + "\n")
            rows = replay.fixture(trace)
        self.assertEqual(rows, [{"t": 0.7, "people": 2}])
        r = replay.Replay(rows + [{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}])
        import call_brief
        self.assertEqual(call_brief.people(r.history), 2)

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
