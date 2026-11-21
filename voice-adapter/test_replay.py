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
    def test_requests_that_join_a_running_chat_turn_get_one_answer_not_one_each(self):
        # 11-20 09:43 call: 11 requests landed while one chat turn ran. Each socket took that turn's one
        # message.complete as its own answer, so one final went to the voice 11 times.
        rows = (turn(5, 10, "user", "What is in the call traces we keep?", "u1") + delegation(9.8, "d1", "call traces")
                + turn(10, 11, "assistant", "On it.", "a1") + hermes("d1", 60, "Each call writes one trace file.")
                + turn(15, 20, "user", "And do we keep the audio too?", "u2") + delegation(19.8, "d2", "audio too")
                + turn(20, 21, "assistant", "Checking.", "a2") + hermes("d2", 30, "Each call writes one trace file.")
                + turn(28, 30, "user", "Hello, are you there?", "u3") + delegation(29.8, "d3", "Hello")
                + turn(30, 31, "assistant", "Still on it.", "a3") + hermes("d3", 5, "Each call writes one trace file."))
        report = run(rows)
        self.assertEqual(report["duplicate_finals"], {})
        self.assertEqual(report["finals"], 1)
        self.assertEqual(report["unanswered"], [])

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

    def last_submitted(self, rows):
        import asyncio
        r = replay.Replay([{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}] + rows)
        asyncio.run(r.run())
        return r.submitted


if __name__ == "__main__":
    unittest.main()
