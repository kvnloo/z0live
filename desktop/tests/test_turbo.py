"""Turbo contract tests: fake CLI only; never live provider or owner delivery."""

import concurrent.futures
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop.core import INSTRUCTIONS, Store, builtin_tools


class TurboTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.device = self.store.authenticate(self.store.pair("fake turbo"))
        self.sid = self.session()
        self.turbo = self.store.turbo
        self.tick = patch.object(self.turbo, "tick")
        self.tick.start()
        self.addCleanup(self.tick.stop)

    def session(self, device=None):
        with patch("z0live_desktop.core.mint", return_value={}), patch("z0live_desktop.voice_runtime.load_plan", return_value=None):
            return self.store.new_session(device or self.device, {"client": "web"})["session_id"]

    def tool(self, name="turbo_run", args=None, call="call-1", sid=None, device=None):
        return self.store.tool(device or self.device, name, {"session_id": sid or self.sid, "call_id": call,
                                                           "arguments": args if args is not None else {"task": "fake test"}})

    def admit(self, **args):
        result = self.tool(args={"task": "fake test", **args})
        return self.turbo.find(self.sid, task_id=result["task_id"])

    def emit(self, row, event="progress", summary="Verified fake progress"):
        return self.turbo.event(row["task_id"], {"run_id": "fake-run", "event": event, "summary": summary})

    def test_catalog_and_explicit_voice_policy(self):
        names = {t["name"] for t in builtin_tools()}
        self.assertTrue({"turbo_run", "turbo_status", "turbo_stop"} <= names)
        for phrase in ("turbo: <task>", "go turbo on this", "stop turbo", "z0intelligence owns"):
            self.assertIn(phrase, INSTRUCTIONS)

    def test_concurrent_admission_idempotency_and_argument_conflict(self):
        with concurrent.futures.ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(lambda _: self.tool(), range(12)))
        self.assertEqual(len({r["task_id"] for r in results}), 1)
        with self.assertRaisesRegex(ValueError, "different arguments"):
            self.tool(args={"task": "changed"})
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM turbo_runs").fetchone()[0], 1)

    def test_validation_dangerous_ids_and_extra_model_arguments(self):
        for args in ({"task": "x", "model": "forbidden"}, {"task": "x", "deadline": "10m; touch x"},
                     {"task": "x", "repo": "--help"}, {"task": "x\0y"},
                     {"task": "x", "deadline": "61m"}, {"task": "x", "deadline": "2h"},
                     {"task": "x", "deadline": "3601s"}):
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.tool(args=args)
        self.assertEqual(self.tool(args={"task": "cap", "deadline": "60m"})["status"], "queued")
        for name in ("turbo_status", "turbo_stop"):
            with self.assertRaises(ValueError):
                self.tool(name, {"run_id": "--all; kill"})

    def test_subprocess_argv_literal_no_shell_or_model_selection(self):
        task = 'audit "quotes"; $(touch PWNED)\n--model danger'
        repo = '/fake/repo; $(touch BAD)'
        row = self.admit(task=task, repo=repo)
        process = Mock(stdout=io.StringIO(json.dumps({"run_id": "fake-run", "event": "completed", "summary": "fake done"}) + "\n"))
        process.poll.return_value = 0
        with patch.dict(os.environ, {"Z0LIVE_Z0INT": "/fake/tool with spaces"}), patch("z0live_desktop.turbo.subprocess.Popen", return_value=process) as popen:
            self.turbo.launch(row)
        self.assertEqual(popen.call_args.args[0], ["/fake/tool with spaces", "turbo", "run", "--task", task, "--repo", repo, "--no-code-workers", "--deadline", "10m", "--jsonl"])
        self.assertIs(popen.call_args.kwargs["shell"], False)
        self.assertEqual(self.turbo.find(self.sid)["status"], "completed")
        process.kill.assert_not_called()
        process.terminate.assert_not_called()

    def test_floor_progress_final_dedupe_and_cursor_no_replay(self):
        row = self.admit()
        self.store.voice.floor(self.sid, "user")
        self.emit(row)
        self.emit(row)
        self.emit(row, "completed", "Fake final")
        self.assertEqual(self.store.inbox(self.device)["messages"], [])
        self.store.voice.floor(self.sid, "quiet")
        inbox = self.store.inbox(self.device)
        self.assertEqual([r["say"] for r in inbox["messages"]], ["Verified fake progress", "Fake final"])
        self.emit(row, "completed", "Fake final")
        self.assertEqual(self.store.inbox(self.device, inbox["after"])["messages"], [])

    def test_session_isolation_supersession_and_stale_silence(self):
        row = self.admit()
        self.emit(row)
        other = self.store.authenticate(self.store.pair("other"))
        other_sid = self.session(other)
        with patch.object(self.turbo, "command") as cmd:
            with self.assertRaises(LookupError):
                self.tool("turbo_stop", {"run_id": "fake-run"}, sid=other_sid, device=other)
            cmd.assert_not_called()
        self.session()
        self.emit(row, "completed", "Old final")
        messages = self.store.inbox(self.device)["messages"]
        self.assertTrue(all(m["say"] == "" and m["params"]["silent"] for m in messages))
        with self.assertRaises(ValueError):
            self.tool(call="new-call")

    def test_stale_and_ended_admission_rejected(self):
        with self.store.connect() as db:
            db.execute("UPDATE sessions SET updated=? WHERE id=?", (time.time()-121, self.sid))
        with self.assertRaises(ValueError):
            self.admit()
        with self.store.connect() as db:
            db.execute("UPDATE sessions SET updated=?,ended=1 WHERE id=?", (time.time(), self.sid))
        with self.assertRaises(ValueError):
            self.admit()

    def test_barge_in_suppresses_speech_and_uses_stop_marker_not_kill(self):
        row = self.admit()
        self.store.voice.floor(self.sid, "assistant")
        self.emit(row)
        with patch.object(self.turbo, "command", return_value={"run_id": "fake-run", "status": "running"}) as cmd:
            self.store.add_events(self.device, [{"session_id": self.sid, "type": "barge_in"}])
        self.assertIn((("stop", "fake-run", "--json"),), [(c.args,) for c in cmd.call_args_list])
        self.assertEqual(self.turbo.find(self.sid)["status"], "cancelling")
        self.store.voice.reconcile_cancels()
        self.assertEqual(self.turbo.find(self.sid)["status"], "cancelling")
        self.emit(row, "completed", "Racing result")
        self.store.voice.floor(self.sid, "quiet")
        self.assertTrue(all(not m["say"] for m in self.store.inbox(self.device)["messages"]))

    def test_stop_before_first_run_id_and_cancel_failure_pending(self):
        row = self.admit()
        with self.store.connect() as db:
            db.execute("UPDATE turbo_runs SET status='starting'")
        with patch.object(self.turbo, "command", side_effect=RuntimeError("down")) as cmd:
            self.tool("turbo_stop", {})
            self.assertTrue(self.emit(row, "started", ""))
            self.turbo.request_stop(self.turbo.find(self.sid))
            self.assertEqual(cmd.call_args.args, ("stop", "fake-run", "--json"))
        self.assertEqual(self.turbo.find(self.sid)["status"], "cancelling")
        self.emit(row, "cancelled", "Fake cancellation confirmed")
        self.assertEqual(self.turbo.find(self.sid)["status"], "cancelled")

    def test_restart_never_reexecutes_unknown_admission_and_no_repeated_final(self):
        row = self.admit()
        with self.store.connect() as db:
            db.execute("UPDATE turbo_runs SET status='starting'")
        with patch("z0live_desktop.turbo.subprocess.Popen") as popen:
            self.turbo.recover()
            popen.assert_not_called()
        self.assertEqual(self.turbo.find(self.sid)["status"], "unknown")
        self.emit(row, "started", "")
        with patch.object(self.turbo, "command", return_value={"run_id": "fake-run", "state": "completed", "summary": "Recovered fake result"}):
            self.turbo.recover()
            self.turbo.reconcile(self.turbo.find(self.sid))
            self.turbo.recover()
            self.turbo.reconcile(self.turbo.find(self.sid))
        self.assertEqual(len(self.store.inbox(self.device)["messages"]), 1)

    def test_malformed_events_and_mismatched_status_refused(self):
        row = self.admit()
        for event in ({}, {"run_id": "--all", "event": "progress", "summary": "x"},
                      {"run_id": "fake-run", "event": "progress", "summary": {}}):
            with self.assertRaises(ValueError):
                self.turbo.event(row["task_id"], event)
        self.emit(row)
        with patch.object(self.turbo, "command", return_value={"run_id": "other", "state": "completed"}):
            with self.assertRaises(ValueError):
                self.tool("turbo_status", {})

    def test_status_receipt_requires_state_and_preserves_quality_results(self):
        row = self.admit()
        self.store.voice.floor(self.sid, "user")
        self.emit(row)
        # Native status receipts use `state`; a legacy `status` field must not settle a run.
        with patch.object(self.turbo, "command", return_value={
                "run_id": "fake-run", "status": "completed", "summary": "legacy shape"}):
            with self.assertRaises(ValueError):
                self.turbo.refresh(self.turbo.find(self.sid))
        self.assertEqual(self.turbo.find(self.sid)["status"], "running")
        receipt = {"run_id": "fake-run", "state": "completed",
                   "quality": {"verified": True}, "results": {"findings": ["fake"]}}
        with patch.object(self.turbo, "command", return_value=receipt) as cmd:
            value = self.tool("turbo_status", {})
        self.assertEqual(value["status"], "completed")
        self.assertEqual(value["quality"], {"verified": True})
        self.assertEqual(value["results"], {"findings": ["fake"]})
        self.assertEqual(cmd.call_args.args, ("status", "fake-run", "--json"))
        self.store.voice.floor(self.sid, "quiet")
        messages = self.store.inbox(self.device)["messages"]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[-1]["params"]["result"]["quality"], {"verified": True})
        # A durable terminal receipt is authoritative and does not query or speak again.
        with patch.object(self.turbo, "command") as cmd:
            self.tool("turbo_status", {})
        cmd.assert_not_called()

    def test_stop_terminal_confirmation_settles_without_speech(self):
        row = self.admit()
        self.emit(row)
        def command(*argv):
            if argv[0] == "stop":
                return {"run_id": "fake-run", "stop_requested": True, "terminal_confirmed": True}
            return {"run_id": "fake-run", "state": "cancelled", "summary": "Fake cancelled"}
        with patch.object(self.turbo, "command", side_effect=command) as cmd:
            value = self.tool("turbo_stop", {})
        # `terminal_confirmed` is not the outcome; read the runtime `state`.
        self.assertEqual([c.args for c in cmd.call_args_list],
                         [("stop", "fake-run", "--json"), ("status", "fake-run", "--json")])
        self.assertEqual(value["status"], "cancelled")
        self.assertTrue(value["stop_requested"])
        inbox = self.store.inbox(self.device)["messages"]
        self.assertTrue(inbox and all(m["say"] == "" for m in inbox))  # stopped speech stays silent

    def test_queued_progress_is_discarded_when_session_is_superseded(self):
        row = self.admit()
        self.store.voice.floor(self.sid, "user")
        self.emit(row)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM voice_speech").fetchone()[0], 1)
        self.session()
        self.turbo.journal.flush()
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM voice_speech").fetchone()[0], 0)
        messages = self.store.inbox(self.device)["messages"]
        self.assertTrue(messages)
        self.assertTrue(all(m["say"] == "" and m["params"]["silent"] for m in messages))

    def test_barge_in_cancels_queued_run_without_pending_state(self):
        row = self.admit()
        with patch.object(self.turbo, "command") as cmd:
            self.store.add_events(self.device, [{"session_id": self.sid, "type": "barge_in"}])
        cmd.assert_not_called()
        self.assertEqual(self.turbo.find(self.sid)["status"], "cancelled")
        with self.store.connect() as db:
            turn = db.execute("SELECT status FROM voice_turns WHERE id=?", (row["task_id"],)).fetchone()[0]
        self.assertEqual(turn, "cancelled")

    def test_deadline_is_turbo_terminal_only(self):
        from z0live_desktop.voice_runtime import TERMINAL
        self.assertNotIn("deadline", TERMINAL)
        row = self.admit()
        self.emit(row, "deadline", "Fake deadline summary")
        self.assertEqual(self.turbo.find(self.sid)["status"], "deadline")
        with self.store.connect() as db:
            turn = db.execute("SELECT status FROM voice_turns WHERE id=?", (row["task_id"],)).fetchone()[0]
            event = db.execute("SELECT body FROM turbo_events WHERE task_id=?", (row["task_id"],)).fetchone()[0]
        self.assertEqual(turn, "deadline")
        self.assertEqual(json.loads(event)["event"], "deadline")
        self.assertFalse(self.turbo.result(self.turbo.find(self.sid))["ok"])
        self.assertEqual([m["say"] for m in self.store.inbox(self.device)["messages"]], ["Fake deadline summary"])

    def test_queued_stop_is_cancelled_not_cancelling(self):
        row = self.admit()
        with patch.object(self.turbo, "command") as cmd:
            result = self.tool("turbo_stop", {})
        cmd.assert_not_called()
        self.assertEqual(result["status"], "cancelled")
        self.assertEqual(self.turbo.find(self.sid)["status"], "cancelled")
        with self.store.connect() as db:
            turn = db.execute("SELECT status FROM voice_turns WHERE id=?", (row["task_id"],)).fetchone()[0]
        self.assertEqual(turn, "cancelled")
        # A later barge/reconcile must not re-open a settled cancellation.
        self.store.voice.reconcile_cancels()
        with self.store.connect() as db:
            turn = db.execute("SELECT status FROM voice_turns WHERE id=?", (row["task_id"],)).fetchone()[0]
        self.assertEqual(turn, "cancelled")

    def test_runtime_progress_events_speak_once_without_startup_noise(self):
        row = self.admit()
        for kind, summary in (("started", "Turbo started"), ("node_waiting", "Waiting"),
                              ("plan_validated", "Plan validated"),
                              ("node_started", "node-1 started"),
                              ("node_completed", "node-1 completed"),
                              ("node_completed", "node-1 completed"),
                              ("verifying", "Verifying now")):
            self.emit(row, kind, summary)
        self.assertEqual(
            [m["say"] for m in self.store.inbox(self.device)["messages"]],
            ["Plan validated", "node-1 started", "node-1 completed", "Verifying now"],
        )

    def test_turbo_prefix_routes_without_voice_plan(self):
        result = self.tool("ask_z0", {"question": "turbo: audit fake repo"})
        self.assertEqual(self.turbo.find(self.sid)["task_id"], result["task_id"])
        self.assertEqual(json.loads(self.turbo.find(self.sid)["args"])["task"], "audit fake repo")

    def test_real_fake_cli_status_receipt_idempotency_and_no_replay(self):
        cli = Path(self.tmp.name) / "fake receipt z0int"
        cli.write_text('''#!/usr/bin/env python3
import json, pathlib, sys
root = pathlib.Path(__file__).parent
state = root / 'receipt-state'
with (root / 'calls').open('a') as log:
    log.write(json.dumps(sys.argv[1:]) + "\\n")
if sys.argv[2] == 'run':
    (root / 'receipt-argv.json').write_text(json.dumps(sys.argv[1:]))
    state.write_text('completed')
    print(json.dumps(dict(run_id='fake-receipt', event='started', summary='')), flush=True)
else:
    print(json.dumps(dict(run_id='fake-receipt', state=state.read_text(),
                           quality={'verified': True}, results={'finding': 'fake receipt'})), flush=True)
''')
        cli.chmod(0o700)
        self.tick.stop()
        task = 'receipt task; $(touch RECEIPT_PWNED)'
        with patch.dict(os.environ, {"Z0LIVE_Z0INT": str(cli)}):
            first = self.tool(args={"task": task}, call="receipt-call")
            second = self.tool(args={"task": task}, call="receipt-call")
            self.assertEqual(first["task_id"], second["task_id"])
            until = time.monotonic() + 4
            while self.turbo.find(self.sid)["status"] != "unknown" and time.monotonic() < until:
                time.sleep(.01)
            value = self.tool("turbo_status", {})
            inbox = self.store.inbox(self.device)
            self.tool("turbo_status", {})
            self.assertEqual(self.store.inbox(self.device, inbox["after"])["messages"], [])
        self.assertEqual(value["status"], "completed")
        self.assertEqual(value["quality"], {"verified": True})
        self.assertEqual(value["results"], {"finding": "fake receipt"})
        self.assertEqual(len(inbox["messages"]), 1)
        self.assertEqual(inbox["messages"][0]["params"]["result"]["results"], {"finding": "fake receipt"})
        argv = json.loads((Path(self.tmp.name) / "receipt-argv.json").read_text())
        self.assertEqual(argv, ["turbo", "run", "--task", task, "--deadline", "10m", "--jsonl"])
        calls = (Path(self.tmp.name) / "calls").read_text().splitlines()
        self.assertEqual(len(calls), 2)
        self.assertFalse((Path(self.tmp.name) / "RECEIPT_PWNED").exists())
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT count(*) FROM turbo_runs").fetchone()[0], 1)

    def test_real_fake_cli_subprocess_run_and_marker_cancellation(self):
        cli = Path(self.tmp.name) / "fake z0int"
        cli.write_text('''#!/usr/bin/env python3
import json, pathlib, sys, time
root = pathlib.Path(__file__).parent
marker = root / 'cancel-marker'
status = root / 'fake-status'
if sys.argv[2] == 'run':
    (root / 'argv.json').write_text(json.dumps(sys.argv[1:]))
    status.write_text('running')
    print(json.dumps(dict(run_id='fake-run',event='started',summary='')), flush=True)
    until = time.monotonic() + 5
    while not marker.exists() and time.monotonic() < until:
        time.sleep(.01)
    state = 'cancelled' if marker.exists() else 'failed'
    status.write_text(state)
    print(json.dumps(dict(run_id='fake-run',event=state,summary='Fake CLI terminal')), flush=True)
elif sys.argv[2] == 'stop':
    marker.write_text(sys.argv[3])
    print(json.dumps(dict(run_id='fake-run', stop_requested=True, terminal_confirmed=False)))
else:
    print(json.dumps(dict(run_id='fake-run', state=status.read_text(), summary='Fake CLI status')))
''')
        cli.chmod(0o700)
        self.tick.stop()
        task = 'fake task; $(touch NEVER)'
        with patch.dict(os.environ, {"Z0LIVE_Z0INT": str(cli)}):
            self.tool(args={"task": task})
            until = time.monotonic() + 4
            while not self.turbo.find(self.sid)["run_id"] and time.monotonic() < until:
                time.sleep(.01)
            self.assertEqual(self.turbo.find(self.sid)["run_id"], "fake-run")
            self.tool("turbo_stop", {})
            until = time.monotonic() + 4
            while self.turbo.running and time.monotonic() < until:
                time.sleep(.01)
            status = self.tool("turbo_status", {})
            self.assertEqual(status["status"], "cancelled")
            other_sid = self.session()
            with self.assertRaises(LookupError):
                self.tool("turbo_status", {"run_id": "fake-run"}, sid=other_sid)
        self.assertFalse(self.turbo.running)
        self.assertEqual((Path(self.tmp.name) / "cancel-marker").read_text(), "fake-run")
        argv = json.loads((Path(self.tmp.name) / "argv.json").read_text())
        self.assertEqual(argv, ["turbo", "run", "--task", task, "--deadline", "10m", "--jsonl"])
        self.assertEqual(self.turbo.find(self.sid)["status"], "cancelled")
        with self.store.connect() as db:
            turn = db.execute("SELECT status FROM voice_turns WHERE id=?", (status["task_id"],)).fetchone()[0]
        self.assertEqual(turn, "cancelled")
        self.assertFalse((Path(self.tmp.name) / "NEVER").exists())
        self.assertEqual(self.store.inbox(self.device)["messages"], [])


if __name__ == "__main__":
    unittest.main()
