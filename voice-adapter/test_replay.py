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

    def last_submitted(self, rows):
        import asyncio
        r = replay.Replay([{"t": 0.5, "src": "provider", "rx": {"type": "session.started"}}] + rows)
        asyncio.run(r.run())
        return r.submitted


if __name__ == "__main__":
    unittest.main()
