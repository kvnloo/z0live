"""jobs.py with a fake `hermes` (a script that reads a state file) and a stub ledger module. Made-up briefs only."""
import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent

FAKE_HERMES = r'''#!/usr/bin/env python3
import json, os, sys
st = os.environ["FAKE_BOARD"]
board = json.load(open(st)) if os.path.exists(st) else {"n": 0, "tasks": {}, "calls": []}
a = sys.argv[1:]
board["calls"].append(a)
if a[:2] == ["kanban", "create"]:
    key = a[a.index("--idempotency-key") + 1]
    tid = next((t for t, v in board["tasks"].items() if v["key"] == key), None)
    if not tid:
        board["n"] += 1
        tid = "t_fake%d" % board["n"]
        board["tasks"][tid] = {"key": key, "status": "ready", "summary": "", "comments": []}
    print(json.dumps({"id": tid, "status": "ready"}))
elif a[:2] == ["kanban", "show"]:
    t = board["tasks"][a[2]]
    print(json.dumps({"task": {"id": a[2], "status": t["status"]}, "latest_summary": t["summary"], "comments": t["comments"]}))
elif a[:2] == ["kanban", "block"]:
    board["tasks"][a[2]]["status"] = "blocked"
elif a[:2] == ["kanban", "reclaim"]:
    pass
json.dump(board, open(st, "w"))
'''

STUB_LEDGER = '''
import json, os
P = os.environ["STUB_LEDGER_LOG"]
def _w(*a):
    open(P, "a").write(json.dumps(a) + "\\n")
def open_item(call, text, kind, owner): _w("open_item", call, text, kind, owner); return "r-1"
def claim(id, by, note): _w("claim", id, by, note)
def receipt(id, by, kind, ref, detail=""): _w("receipt", id, by, kind, ref)
def result(id, by, text, ok=True, refs=()): _w("result", id, text, ok)
def state(id, s, by, why=""): _w("state", id, s, why)
'''


class JobsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        hermes = self.tmp / "hermes"
        hermes.write_text(FAKE_HERMES)
        hermes.chmod(hermes.stat().st_mode | stat.S_IEXEC)
        (self.tmp / "ledger.py").write_text(STUB_LEDGER)
        self.board = self.tmp / "board.json"
        self.log = self.tmp / "ledger.log"
        os.environ.update({"Z0LIVE_JOBS": str(self.tmp / "jobs"), "Z0LIVE_HERMES": str(hermes),
                           "FAKE_BOARD": str(self.board), "Z0LIVE_LEDGER_PY": str(self.tmp / "ledger.py"),
                           "STUB_LEDGER_LOG": str(self.log), "Z0LIVE_JOB_HERMES_HOME": str(self.tmp / "h")})
        sys.modules.pop("jobs", None)
        sys.path.insert(0, str(HERE))
        import jobs
        jobs._ledger = None
        self.jobs = jobs

    def tearDown(self):
        sys.path.remove(str(HERE))
        for k in ("Z0LIVE_JOBS", "Z0LIVE_HERMES", "FAKE_BOARD", "Z0LIVE_LEDGER_PY", "STUB_LEDGER_LOG", "Z0LIVE_JOB_HERMES_HOME"):
            os.environ.pop(k, None)

    def led(self):
        return [json.loads(x) for x in self.log.read_text().splitlines()] if self.log.exists() else []

    def set_board(self, tid, **kw):
        b = json.loads(self.board.read_text())
        b["tasks"][tid].update(kw)
        self.board.write_text(json.dumps(b))

    def test_start_writes_receipt_after_task_id_and_is_idempotent(self):
        r = self.jobs.start("Improve the made-up widget score to 9.5", title="widget climb", call="c1")
        self.assertTrue(r["ok"])
        self.assertEqual((r["id"], r["item"], r["ledger"]), ("t_fake1", "r-1", True))
        self.assertEqual(r["receipt"], {"kind": "kanban", "ref": "t_fake1"})
        kinds = [x[0] for x in self.led()]
        self.assertEqual(kinds, ["open_item", "claim", "receipt"])
        self.assertEqual(self.led()[0][3:], ["action", "job"])
        again = self.jobs.start("Improve the made-up widget score to 9.5", item="r-1")
        self.assertEqual((again["id"], again.get("again")), ("t_fake1", True))
        self.assertEqual(len(json.loads(self.board.read_text())["tasks"]), 1)
        create = [c for c in json.loads(self.board.read_text())["calls"] if c[1] == "create"][0]
        self.assertIn("--goal", create)
        self.assertIn("never service_tier", create[create.index("--body") + 1])

    def test_sync_waits_then_runs_then_done_once(self):
        r = self.jobs.start("widget", item="r-9")
        self.assertEqual(self.jobs.status(r["id"])["state"], "waiting")
        self.assertNotIn("running", [x[2] for x in self.led() if x[0] == "state"])  # no claim on the board yet
        self.set_board("t_fake1", status="running")
        self.assertEqual(self.jobs.status("t_fake1")["state"], "running")
        self.set_board("t_fake1", status="done", summary="Score 7.1 -> 9.6, branch feat/x")
        v = self.jobs.status("t_fake1")
        self.assertEqual(v["state"], "done")
        self.assertIn("9.6", v["result"])
        n = len(self.led())
        self.jobs.status("t_fake1")
        self.jobs.sync_all()
        self.assertEqual(len(self.led()), n)  # idempotent
        res = [x for x in self.led() if x[0] == "result"]
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0][3])

    def test_blocked_and_stop(self):
        r = self.jobs.start("widget two", item="r-2")
        self.set_board(r["id"], status="blocked", comments=[{"body": "needs a decision"}])
        v = self.jobs.status(r["id"])
        self.assertEqual(v["state"], "blocked")
        self.assertIn(["state", "r-2", "blocked", "needs a decision"], self.led())
        s = self.jobs.stop(r["id"], "not wanted")
        self.assertEqual(s["state"], "dropped")
        calls = [c[1] for c in json.loads(self.board.read_text())["calls"]]
        self.assertIn("reclaim", calls)
        self.assertIn("block", calls)

    def test_ledger_missing_still_starts(self):
        os.environ["Z0LIVE_LEDGER_PY"] = str(self.tmp / "nope.py")
        self.jobs._ledger = None
        orig = self.jobs.HERE
        self.jobs.HERE = self.tmp / "empty"
        try:
            r = self.jobs.start("widget three")
        finally:
            self.jobs.HERE = orig
        self.assertTrue(r["ok"])
        self.assertFalse(r["ledger"])
        self.assertEqual(self.jobs.status(r["id"])["state"], "waiting")

    def test_create_failure_marks_claim_failed_and_returns_error(self):
        os.environ["Z0LIVE_HERMES"] = "/nonexistent/hermes"
        r = self.jobs.start("widget four")
        self.assertFalse(r["ok"])
        self.assertIn(["state", "r-1", "failed", "job did not start: " + r["error"]], self.led())
        self.assertNotIn("receipt", [x[0] for x in self.led()])

    def test_bad_input(self):
        self.assertFalse(self.jobs.start("")["ok"])
        self.assertFalse(self.jobs.start("x", model="gpt-6.2-sol", provider="codex-ultrafast")["ok"])
        self.assertFalse(self.jobs.start("x", engine="pid")["ok"])
        self.assertFalse(self.jobs.sync("nope")["ok"])
        self.assertFalse(self.jobs.sync("../etc")["ok"])

    def test_pid_engine_runs_and_finishes(self):
        r = self.jobs.start("count to three", engine="pid", cmd="echo one; echo two", item="r-3")
        self.assertTrue(r["ok"])
        self.assertEqual(r["receipt"]["kind"], "pid")
        for _ in range(40):
            v = self.jobs.status(r["id"])
            if v["state"] == "done":
                break
            time.sleep(0.1)
        self.assertEqual(v["state"], "done")
        self.assertIn("two", v["result"])
        self.assertEqual(len([x for x in self.led() if x[0] == "result"]), 1)

    def test_pid_stop(self):
        r = self.jobs.start("sleepy", engine="pid", cmd="sleep 30", item="r-4")
        self.assertEqual(self.jobs.status(r["id"])["state"], "running")
        self.assertEqual(self.jobs.stop(r["id"])["state"], "dropped")
        time.sleep(0.3)
        self.assertEqual(self.jobs._read(r["id"])["synced"], "dropped")


if __name__ == "__main__":
    unittest.main()
