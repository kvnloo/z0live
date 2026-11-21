import runpy
import unittest
from array import array
from pathlib import Path


BENCH = runpy.run_path(str(Path(__file__).parent / "bench" / "oracle_inject.py"))


def row(*chunks):
    trace = [
        {"ev": "oracle.caller_end", "t": 10.0},
        {"ev": "audio.on", "dir": "output", "t": 10.8},
        {"ev": "context.sent", "type": "session.context.append", "channel": "speakable", "t": 10.0},
        *({"ev": "tx", "to": "mobile", "type": "session.output_transcript.delta", "t": t,
           "event": {"delta": text}} for t, text in chunks),
    ]
    return BENCH["score"](BENCH["QUESTIONS"][4], "oracle", trace, {"heard": "", "delegated": []})


class OracleInjectTests(unittest.TestCase):
    def test_end_ignores_trailing_padding(self):
        pcm = bytes(1920) + array("h", [-1600]).tobytes() + bytes(3838)
        self.assertEqual(BENCH["speech_end"](pcm), 1920)

    def test_delayed_first_answer_is_not_first_audio(self):
        result = row((10.7, "Sure, let me check. "), (13.1, "Ursula K. Le"), (13.3, " Guin wrote it."))
        self.assertEqual(result["first_audio_s"], 0.8)
        self.assertEqual(result["oracle_wire_s"], 0.0)
        self.assertEqual(result["first_answer_transcript_s"], 3.3)
        self.assertTrue(result["filler"])
        self.assertFalse(result["answer_in_first_sentence"])

    def test_stall_after_an_acknowledgment(self):
        result = row((10.7, "Mm-hmm. Right, I'll have it checked."))
        self.assertTrue(result["filler"])
        self.assertFalse(result["answer_in_first_sentence"])

    def test_middle_initial_does_not_end_the_sentence(self):
        result = row((10.7, "Ursula K. Le Guin wrote it."))
        self.assertFalse(result["filler"])
        self.assertTrue(result["answer_in_first_sentence"])

    def test_missing_transcript_is_not_no_filler(self):
        result = row()
        self.assertIsNone(result["first_answer_transcript_s"])
        self.assertIsNone(result["filler"])
        self.assertFalse(result["answer_in_first_sentence"])
