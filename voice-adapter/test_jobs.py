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
    with open(P, "a") as f:
        f.write(json.dumps(a) + "\\n")
def open_item(call, text, kind, owner): _w("open_item", call, text, kind, owner); return "r-1"
def claim(id, by, note): _w("claim", id, by, note)
def receipt(id, by, kind, ref, detail=""): _w("receipt", id, by, kind, ref)
def result(id, by, text, ok=True, refs=()): _w("result", id, text, ok)
def state(id, s, by, why=""): _w("state", id, s, why)
def get(item):
    p = os.environ.get("STUB_LEDGER_ITEMS")
    rows = json.load(open(p)) if p and os.path.exists(p) else []
    return next((r for r in rows if r["id"] == item), {"id": item} if item.startswith("r-") else None)
def items(call=None, open_only=False):
    p = os.environ.get("STUB_LEDGER_ITEMS")
    rows = json.load(open(p)) if p and os.path.exists(p) else []
    return [r for r in rows if call is None or r["call"] == call]
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
        for k in ("Z0LIVE_JOBS", "Z0LIVE_HERMES", "FAKE_BOARD", "Z0LIVE_LEDGER_PY", "STUB_LEDGER_LOG", "Z0LIVE_JOB_HERMES_HOME", "STUB_LEDGER_ITEMS"):
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

    def test_bot_job_goes_to_that_profiles_board_queue(self):
        prof = self.tmp / "home" / ".hermes/profiles"
        for name in ("pc", "guest"):
            (prof / name).mkdir(parents=True)
            (prof / name / "config.yaml").write_text("x: 1\n")
        self.jobs.HOME = self.tmp / "home"
        r = self.jobs.start("List the made-up drive's top folders", bot="@PC", call="c1")
        self.assertTrue(r["ok"], r)
        self.assertEqual(r["bot"], "pc")
        create = [c for c in json.loads(self.board.read_text())["calls"] if c[1] == "create"][0]
        self.assertEqual(create[create.index("--assignee") + 1], "pc")
        body = create[create.index("--body") + 1]
        self.assertIn("Requested from z0live by tek", body)
        self.assertIn("on call c1", body)
        self.assertIn("explicit go", body)
        same_on_tek = self.jobs.start("List the made-up drive's top folders", call="c1")
        self.assertNotEqual(same_on_tek["id"], r["id"])  # another owner is another job
        self.assertFalse(self.jobs.start("anything", bot="guest")["ok"])
        self.assertIn("no Hermes profile", self.jobs.start("anything", bot="nobody")["error"])
        self.assertFalse(self.jobs.start("anything", bot="../tek")["ok"])
        self.assertFalse(self.jobs.start("x", bot="pc", engine="pid", cmd="true")["ok"])

    def test_same_ask_twice_is_one_job_without_an_item(self):
        a = self.jobs.start("Improve the made-up gadget", call="c2")
        b = self.jobs.start("Improve the made-up gadget", call="c2")
        self.assertEqual((a["id"], b["id"], b.get("again")), ("t_fake1", "t_fake1", True))
        self.assertEqual([x[0] for x in self.led()].count("open_item"), 1)
        self.assertEqual(len(json.loads(self.board.read_text())["tasks"]), 1)

    def test_proof_goes_on_the_item_the_adapter_opened(self):
        items = self.tmp / "items.json"
        items.write_text(json.dumps([{"id": "rA", "call": "c3", "kind": "action", "proven": False},
                                     {"id": "rQ", "call": "c3", "kind": "question", "proven": False}]))
        os.environ["STUB_LEDGER_ITEMS"] = str(items)
        r = self.jobs.start("Run the made-up sweep", call="c3")
        self.assertEqual(r["item"], "rA")
        self.assertNotIn("open_item", [x[0] for x in self.led()])
        self.assertIn(["receipt", "rA", "job", "kanban", r["id"]], self.led())

    def test_unknown_item_id_is_refused(self):
        r = self.jobs.start("widget five", item="made-up-id")
        self.assertFalse(r["ok"])
        self.assertIn("no ledger item", r["error"])
        self.assertFalse(self.board.exists())  # nothing was created on the board

    def test_again_reports_state(self):
        a = self.jobs.start("widget seven", item="r-7")
        self.set_board(a["id"], status="done", summary="ok")
        self.jobs.status(a["id"])
        self.assertEqual(self.jobs.start("widget seven", item="r-7")["state"], "done")

    def test_unclaimed_job_goes_blocked_on_the_ledger_then_recovers(self):
        r = self.jobs.start("widget six", item="r-6")
        job = self.jobs._read(r["id"])
        job["created"] -= 400
        self.jobs._write(job)
        self.jobs.status(r["id"])
        self.assertIn(["state", "r-6", "blocked", "no worker claimed the job in 5 min (ready)"], self.led())
        n = len(self.led())
        self.jobs.status(r["id"])
        self.assertEqual(len(self.led()), n)  # said once
        self.set_board(r["id"], status="running")
        self.assertEqual(self.jobs.status(r["id"])["state"], "running")

    def test_cli_every_verb(self):
        import contextlib
        import io

        def run(*argv):
            buf = io.StringIO()
            with contextlib.redirect_stdout(buf):
                rc = self.jobs.main(list(argv))
            return rc, json.loads(buf.getvalue())
        rc, out = run("start", "Made-up brief", "--title", "t", "--call", "c5", "--no-goal", "--max-runtime", "10m")
        self.assertEqual((rc, out["ok"], out["id"]), (0, True, "t_fake1"))
        self.assertEqual(run("status", "t_fake1")[1]["state"], "waiting")
        self.assertEqual(len(run("list")[1]["jobs"]), 1)
        self.assertEqual(len(run("list", "--open")[1]["jobs"]), 1)
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            self.assertEqual(self.jobs.main(["sync"]), 0)
        self.assertEqual(buf.getvalue(), "")  # nothing changed since the status call: silent
        self.set_board("t_fake1", status="done", summary="finished")
        rc, out = run("sync")
        self.assertEqual([j["state"] for j in out["jobs"]], ["done"])
        rc, out = run("start", "pid one", "--engine", "pid", "--cmd", "echo hi", "--workspace", str(self.tmp))
        self.assertEqual((rc, out["receipt"]["kind"]), (0, "pid"))
        self.assertEqual(run("stop", "t_fake1")[1]["state"], "dropped")
        self.assertEqual(run("status", "nope")[0], 1)

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
