from collections import deque
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import unittest

spec = importlib.util.spec_from_file_location(
    "call_scorecard", Path(__file__).with_name("bench") / "call_scorecard.py")
scorecard = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = scorecard
spec.loader.exec_module(scorecard)


def event(ev, t, **fields):
    return dict(ev=ev, t=t, wall=1000 + t, **fields)


def turn(t, kind, transcript, role="assistant"):
    return event("rx", t, type=f"turn.{kind}",
                 event={"turn": {"role": role, "id": transcript, "transcript": transcript}})


def handoff(t, did, text):
    return event("tx", t, type="delegation.context.append", event={
        "channel": "speakable", "delegation_item_id": did, "content": [{"text": text}]})


class CallScorecardTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.call = self.root / "120000-engine-f00dcafe123456.jsonl"
        self.events = self.root / "conductor.jsonl"

    def write(self, path, *rows):
        path.write_text("".join(json.dumps(row) + "\n" for row in rows) + "{\n")

    def analyze(self, *events, profile="tek", ledger=None):
        self.write(self.call, event("call.created", 0, profile=profile), *events)
        return scorecard.analyze(self.call, scorecard.Conductor(self.events), self.root,
                                 deque(), deque(), ledger)

    def test_spoken_answer_excludes_status_and_unspoken_handoffs(self):
        text = "The orchid is in greenhouse seven."
        status = "The helper is still checking the greenhouse."
        result = self.analyze(
            event("delegation.start", 1, did="a", request="Locate an orchid"),
            event("delegation.memory", 2, did="a"),
            event("lanes.tier", 3, errors={"quick": "SKIP", "other": "empty"}, text=""),
            event("delegation.receipt", 4, did="a"),
            handoff(4, "a", status), turn(5, "created", status), turn(7, "done", status),
            event("delegation.done", 8, did="a", answer=text, s=7),
            handoff(8, "wire-a", text), event("floor.answer_sent", 8, did="wire-a", of="a"),
            turn(10, "created", text), turn(12, "done", text),
            event("delegation.start", 15, did="b", request="Polish the vase"),
            handoff(16, "b", "The vase polish is in the cupboard."),
            event("call.closed", 60, seconds=60)).render()
        self.assertEqual(result["requests"], 2)
        self.assertEqual(result["requests_without_observed_answer"], 1)
        self.assertEqual(result["first_spoken_answer_s"], {"n": 1, "p50": 9, "p90": 9})
        self.assertEqual(result["tier_skip_rate"], .5)
        self.assertEqual(result["hermes_paths"]["background"]["terminal_s"]["p50"], 7)
        self.assertEqual(result["status"]["receipt"]["spoken_per_min"], 1)
        self.assertEqual(result["invalid_trace_lines"], 1)

    def test_counts_cards_and_only_this_calls_lanes(self):
        call = "f00dcafe1234"
        self.write(self.events, dict(ev="ask.received", call=call, ask="a", t=1001),
                   dict(ev="ask.received", call="different", ask="b", t=1001),
                   dict(ev="lane.done", ask="a", lane="a-1", t=1010, rc="0"),
                   dict(ev="lane.failed", ask="a", lane="a-2", t=1011, rc="timeout"),
                   dict(ev="lane.failed", ask="a", lane="a-3", t=1012, rc="0"),
                   dict(ev="lane.start", ask="a", lane="a-4", t=1013),
                   dict(ev="lane.done", ask="b", lane="b-1", t=1010, rc="0"))
        result = self.analyze(
            event("lanes.card_spoken", 12, lanes=["a-1", "a-5"]),
            event("lanes.card_skipped", 14, reason="stale", lanes=["a-2"]),
            event("call.closed", 60)).render()
        self.assertEqual(result["lane_cards_spoken"], 2)
        self.assertEqual(result["lane_card_batches_spoken"], 1)
        self.assertEqual(result["lane_cards_skipped"], {"stale": 1})
        self.assertEqual(result["lane_outcomes"],
                         dict(evidence=1, artifact_only=0, died=1, empty=1, unfinished=1))

    def test_claim_requires_matching_timely_receipt_and_repeats_have_a_window(self):
        request = "Launch the greenhouse irrigation job"
        answer = "I started the irrigation job."
        events = [event("delegation.start", 1, did="a", request=request),
                  event("delegation.done", 4, did="a", answer=answer),
                  event("delegation.start", 15, did="b", request=request),
                  event("delegation.start", 616, did="c", request=request)]
        result = self.analyze(*events).render()
        self.assertEqual(result["unproven_action_claims"], 1)
        self.assertEqual(result["repeat_requests_10m"], 1)

        def ledger(call):
            if call == "engine-f00dcafe123456":
                return [{"text": request, "kind": "action", "receipts": [{"t": 1003, "kind": "kanban", "ref": "task-1"}]}]
            return []

        self.assertEqual(self.analyze(*events, ledger=ledger).render()["unproven_action_claims"], 0)
        self.assertFalse(scorecard.claims("Once I started the dryer, I would check the timer."))
        self.assertFalse(scorecard.claims("I'll start the irrigation job."))

    def test_completion_requires_result_and_lane_receipt_does_not_prove_a_job(self):
        item = {"text": "Launch the greenhouse irrigation job", "kind": "action", "opened": 1000,
                "receipts": [{"t": 1002, "kind": "lane", "ref": "lane-1"}]}
        self.assertFalse(scorecard.ledger_proof([item], item["text"], 1000, 1005, False))
        item["receipts"].append({"t": 1003, "kind": "pid", "ref": "123"})
        self.assertTrue(scorecard.ledger_proof([item], item["text"], 1000, 1005, False))
        self.assertFalse(scorecard.ledger_proof([item], item["text"], 1000, 1005, True))
        item["results"] = [{"t": 1006, "ok": True}]
        self.assertFalse(scorecard.ledger_proof([item], item["text"], 1000, 1005, True))
        self.assertTrue(scorecard.ledger_proof([item], item["text"], 1000, 1007, True))

    def test_filters_non_real_profiles(self):
        for profile in ("", "main"):
            with self.subTest(profile=profile):
                self.assertIsNone(self.analyze(profile=profile))
