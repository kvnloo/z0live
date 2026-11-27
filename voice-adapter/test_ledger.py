import importlib.util
import json
import multiprocessing
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("ledger", HERE / "ledger.py")
ledger = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ledger)


def _writer(args):
    directory, n = args
    os.environ["Z0LIVE_LEDGER"] = directory
    for i in range(25):
        ledger.open_item("c1", f"item {n}-{i} " + "x" * 300, "question", "voice")


class LedgerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["Z0LIVE_LEDGER"] = self.tmp.name

    def tearDown(self):
        os.environ.pop("Z0LIVE_LEDGER", None)
        self.tmp.cleanup()

    def test_open_and_fold(self):
        a = ledger.open_item("c1", "water the ferns", "action", "voice")
        b = ledger.open_item("c2", "what is a ferrule", "question", "background")
        self.assertEqual([i["id"] for i in ledger.items("c1")], [a])
        self.assertEqual(len(ledger.items()), 2)
        self.assertEqual(ledger.items("c1")[0]["state"], "open")
        self.assertNotEqual(a, b)

    def test_receipt_runs_result_finishes(self):
        a = ledger.open_item("c1", "water the ferns", "action", "voice")
        ledger.claim(a, "voice", "I will water them")
        ledger.receipt(a, "job", "kanban", "t_42")
        it = ledger.items("c1")[0]
        self.assertEqual((it["state"], it["proven"]), ("running", True))
        ledger.result(a, "job", "watered", True, ["/tmp/x"])
        self.assertEqual(ledger.items("c1")[0]["state"], "done")
        ledger.receipt(a, "job", "pid", "9")  # a late receipt never reopens a finished item
        self.assertEqual(ledger.items("c1")[0]["state"], "done")
        self.assertEqual(ledger.items("c1", open_only=True), [])

    def test_failed_result_and_explicit_state(self):
        a = ledger.open_item("c1", "x", "action", "job")
        ledger.result(a, "job", "boom", ok=False)
        self.assertEqual(ledger.items("c1")[0]["state"], "failed")
        b = ledger.open_item("c1", "y", "action", "job")
        ledger.state(b, "blocked", "job", "needs Kevin")
        self.assertEqual(ledger.items("c1", open_only=True)[0]["state"], "blocked")

    def test_claim_without_receipt_is_unstarted_after_120s(self):
        a = ledger.open_item("c1", "water the ferns", "action", "voice")
        ledger.claim(a, "background", "kicked it off")
        t = ledger.items("c1")[0]["claims"][0]["t"]
        fresh = ledger.items("c1", now=t + 60)[0]
        late = ledger.items("c1", now=t + 121)[0]
        self.assertFalse(fresh["unstarted"])
        self.assertTrue(fresh["claimed_not_started"])
        self.assertTrue(late["unstarted"])
        self.assertIn("NOT STARTED", ledger.brief("c1", now=t + 121))
        self.assertNotIn("NOT STARTED", ledger.brief("c1", now=t + 60))

    def test_blocked_is_not_promised_and_a_non_final_result_keeps_the_state(self):
        a = ledger.open_item("c1", "eject it", "action", "voice")
        ledger.claim(a, "background", "asked")
        ledger.state(a, "blocked", "background", "the drive is busy")
        t = ledger.items("c1")[0]["claims"][0]["t"]
        it = ledger.items("c1", now=t + 500)[0]
        self.assertEqual((it["state"], it["unstarted"], it["claimed_not_started"]), ("blocked", False, False))
        self.assertIn("BLOCKED", ledger.brief("c1", now=t + 500))
        ledger.result(a, "lane:Sol", "research note", True, [], final=False)
        self.assertEqual(ledger.items("c1")[0]["state"], "blocked")
        self.assertEqual(len(ledger.items("c1")[0]["results"]), 1)

    def test_reads_are_cached_until_the_file_grows(self):
        a = ledger.open_item("c1", "one", "question", "voice")
        self.assertEqual(len(ledger.items("c1")), 1)
        ledger.result(a, "x", "ok")
        self.assertEqual(ledger.items("c1")[0]["state"], "done")

    def test_lane_receipt_is_not_proof_for_an_action(self):
        a = ledger.open_item("c1", "water the ferns", "action", "voice")
        q = ledger.open_item("c1", "why are ferns green", "research", "voice")
        for i in (a, q):
            ledger.claim(i, "voice", "on it")
            ledger.receipt(i, "conductor", "lane", "L1")
        by = {i["id"]: i for i in ledger.items("c1")}
        self.assertFalse(by[a]["proven"])
        self.assertTrue(by[q]["proven"])

    def test_brief_orders_open_first_and_truncates(self):
        done = ledger.open_item("c1", "old thing", "question", "voice")
        ledger.result(done, "background", "the answer is blue")
        live = ledger.open_item("c1", "new thing", "action", "voice")
        ledger.receipt(live, "job", "cron", "job-7")
        text = ledger.brief("c1", current=live)
        self.assertLess(text.index("new thing"), text.index("old thing"))
        self.assertIn("this request", text)
        self.assertIn("proof cron job-7", text)
        self.assertIn("the answer is blue", text)
        for n in range(40):
            ledger.open_item("c1", f"filler {n} " + "z" * 250, "question", "voice")
        short = ledger.brief("c1", max_chars=1500)
        self.assertLessEqual(len(short), 1500 + 60)
        self.assertIn("older items not shown", short)
        self.assertEqual(ledger.brief("nobody"), "")

    def test_garbled_and_orphan_lines_are_skipped(self):
        a = ledger.open_item("c1", "ok", "question", "voice")
        with open(ledger.path(), "ab") as f:
            f.write(b'{"op":"claim","id":"nope","by":"x"}\n{broken\n\n{"op":"receipt","id":"' + a.encode() + b'"')
        ledger.result(a, "x", "fine")
        self.assertEqual(ledger.items("c1")[0]["state"], "done")

    def test_validation(self):
        with self.assertRaises(ValueError):
            ledger.open_item("c", "t", "wish", "voice")
        a = ledger.open_item("c", "t", "action", "voice")
        with self.assertRaises(ValueError):
            ledger.receipt(a, "x", "lane", "")
        with self.assertRaises(ValueError):
            ledger.receipt(a, "x", "vibes", "1")
        with self.assertRaises(ValueError):
            ledger.state(a, "nearly", "x")

    def test_env_is_read_at_call_time(self):
        a = ledger.open_item("c1", "one", "question", "voice")
        with tempfile.TemporaryDirectory() as other:
            os.environ["Z0LIVE_LEDGER"] = other
            self.assertEqual(ledger.items(), [])
        os.environ["Z0LIVE_LEDGER"] = self.tmp.name
        self.assertEqual(ledger.items()[0]["id"], a)

    def test_concurrent_appends_stay_whole_lines(self):
        with multiprocessing.get_context("spawn").Pool(4) as pool:
            pool.map(_writer, [(self.tmp.name, n) for n in range(4)])
        lines = ledger.path().read_text().splitlines()
        self.assertEqual(len(lines), 100)
        self.assertTrue(all(json.loads(line) for line in lines))
        self.assertEqual(len(ledger.items("c1")), 100)

    def test_cli_records_proof(self):
        a = ledger.open_item("c1", "water the ferns", "action", "voice")
        env = {**os.environ, "Z0LIVE_LEDGER": self.tmp.name}
        out = subprocess.run([sys.executable, "-I", str(HERE / "ledger.py"), "receipt", a, "--by", "bg", "--kind", "pid",
                              "--ref", "123"], env=env, capture_output=True, text=True)
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertTrue(ledger.items("c1")[0]["proven"])


if __name__ == "__main__":
    unittest.main()
