"""No "started" without a receipt; failed lane cards are spoken as failed; the voice's promises are kept in a ledger.
All phrases are invented."""
import json
import tempfile
import time
import unittest
from pathlib import Path

import lane_cards
import receipts


class GuardTests(unittest.TestCase):
    def test_start_claim_without_receipt_is_replaced(self):
        text, changed = receipts.guard("I've started both climbs. They report after each round.", receipt=False)
        self.assertTrue(changed)
        self.assertEqual(text, receipts.HONEST + " They report after each round.")

    def test_claim_with_a_receipt_or_a_job_id_stays(self):
        line = "I've started the climb. It reports after each round."
        self.assertEqual(receipts.guard(line, receipt=True), (line, False))
        with_id = "I've started it as kanban task t_1a2b3c4d."
        self.assertEqual(receipts.guard(with_id, receipt=False), (with_id, False))

    def test_reports_about_other_jobs_are_not_claims(self):
        for line in ("Round 14 is running now.", "The loop started on the 19th and stalled.",
                     "I haven't started it yet, though.", "I've started on the design doc.", "I've started looking."):
            self.assertFalse(receipts.claims_started(line), line)
        for line in ("I'll start it now.", "I just kicked them off.", "I've launched two workers."):
            self.assertTrue(receipts.claims_started(line), line)

    def test_promises_are_found_checks_are_not(self):
        self.assertEqual(receipts.promises("Let me check. I'll add it as its own item after this call. Okay."),
                         ["I'll add it as its own item after this call."])
        self.assertEqual(receipts.promises("I'll check that now."), [])

    def test_every_promise_goes_to_the_ledger_with_what_followed(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "promises.jsonl"
            made = [{"wall": 100.0, "did": "a", "text": "I'll add it."}, {"wall": 200.0, "did": "b", "text": "I'll fix it."}]
            out = receipts.write_ledger("call1", made, lambda p: ["a-1"] if p["did"] == "a" else [], path)
            self.assertEqual([p["did"] for p in out], ["b"])
            rows = [json.loads(x) for x in path.read_text().splitlines()]
            self.assertEqual([(r["promise"], r["owner"], r["followed_by"]) for r in rows],
                             [("I'll add it.", "lanes", ["a-1"]), ("I'll fix it.", None, [])])


class KanbanReceiptTests(unittest.TestCase):
    def test_a_claimed_task_since_the_request_is_a_receipt(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as d:
            db = Path(d) / "kanban.db"
            con = sqlite3.connect(db)
            con.execute("create table tasks (id text, status text, created_at integer, worker_started_at integer, "
                        "last_heartbeat_at integer, started_at integer)")
            con.execute("insert into tasks values ('t_1', 'ready', 1000, null, null, null)")
            con.commit()
            self.assertFalse(receipts.kanban_started(900, db))  # queued, nobody claimed it
            con.execute("update tasks set status='running', started_at=1001")
            con.commit(); con.close()
            self.assertTrue(receipts.kanban_started(900, db))
            self.assertFalse(receipts.kanban_started(1100, db))  # older than the request
            self.assertFalse(receipts.kanban_started(0, Path(d) / "missing.db"))


class CardTests(unittest.TestCase):
    def lane(self, state, summary, **kw):
        return {"id": "x-1", "state": state, "model": "sol 6.2", "card": {"summary": summary, **kw}}

    def test_failed_lane_is_spoken_as_failed(self):
        self.assertEqual(lane_cards.line([self.lane("failed", "The shell was blocked before it could run.")]),
                         "Sol lane failed. The shell was blocked before it could run.")
        two = lane_cards.line([self.lane("done", "Three views scored 9."), self.lane("failed", "No run started.")])
        self.assertEqual(two, "2 lanes back. Sol: Three views scored 9. Sol: failed. No run started.")

    def test_receipt_needs_running_work_of_this_call(self):
        now = time.time()

        def state(*lanes):
            return {"asks": [{"call": "abc", "lanes": list(lanes)}]}
        self.assertTrue(lane_cards.receipt(state({"id": "1", "state": "running"}), "abc", now))
        self.assertFalse(lane_cards.receipt(state({"id": "1", "state": "running"}), "other", now))
        self.assertFalse(lane_cards.receipt(state({"id": "1", "state": "failed", "t0": now}), "abc", now - 1))
        self.assertFalse(lane_cards.receipt(state({"id": "1", "state": "done", "t0": now - 900}), "abc", now - 60))
        self.assertTrue(lane_cards.receipt(state({"id": "1", "state": "done", "t0": now}), "abc", now - 60))
        queued = {"id": "1", "state": "done", "t0": now - 900, "card": {"receipt": {"task": "t_1", "state": "queued"}}}
        self.assertFalse(lane_cards.receipt(state(queued), "abc", now - 60))
        running = {**queued, "launch": True, "card": {"receipt": {"task": "t_1", "state": "running"}}}
        self.assertTrue(lane_cards.receipt(state(running), "abc", now - 60))
        # a launch lane counts only by its receipt: waiting for a worker (running) or done-but-queued is not started
        waiting = {"id": "1", "state": "running", "launch": True, "engine": "kanban", "t0": now}
        self.assertFalse(lane_cards.receipt(state(waiting), "abc", now - 60))
        self.assertFalse(lane_cards.receipt(state({**queued, "launch": True, "t0": now}), "abc", now - 60))

    def test_receipt_and_followers_are_scoped_to_the_request(self):
        now = time.time()
        st = {"asks": [{"call": "abc", "did": "other", "lanes": [{"id": "o-1", "state": "running", "t0": now}]},
                       {"call": "abc", "did": "mine", "lanes": [{"id": "m-1", "state": "failed", "t0": now}]}]}
        self.assertFalse(lane_cards.receipt(st, "abc", now - 60, did="mine"))  # another request's lane is no receipt
        self.assertTrue(lane_cards.receipt(st, "abc", now - 60, did="other"))
        self.assertEqual(lane_cards.followed(st, "abc", "other", now - 1), ["o-1"])
        self.assertEqual(lane_cards.followed(st, "abc", "mine", now - 1), [])


if __name__ == "__main__":
    unittest.main()
