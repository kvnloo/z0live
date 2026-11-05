"""GPT role bindings run through fx, never inside Hermes (owner rule 2026-11-04)."""

import hashlib
import json
import os
from pathlib import Path
import sys
import time
import threading
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_voice_stack as base
from z0live_desktop.fx_runner import harness, hermes_binding, is_gpt
from z0live_desktop.voice_runtime import load_plan

SOL = {"provider": "openai-codex", "model": "gpt-6.1-sol"}
FAKE_FX = """#!/bin/sh
while [ $# -gt 0 ]; do [ "$1" = --model ] && m=$2; echo "$1" >> "$FAKE_FX_LOG"; shift; done
[ -n "$FAKE_FX_SLEEP" ] && sleep "$FAKE_FX_SLEEP"
printf '{"final_output":"fx evidence","model":"%s","exit_code":0,"usage":{"input_tokens":3,"output_tokens":2}}' "$m"
"""


def signed(value):
    value = {k: v for k, v in value.items() if k != "digest"}
    value["digest"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value


def fx_plan(**task):
    value = base.plan()
    value["roles"]["default"] = {**SOL, "harness": "fx", "effort": "medium"}
    value["roles"]["task"] = {**SOL, "harness": "fx", "effort": "medium", **task}
    return signed(value)


class FxTests(base.StackTests):
    def setUp(self):
        super().setUp()
        self.sid = self.session(fx_plan())
        root = Path(self.tmp.name)
        self.fx = root / "fx"
        self.fx.write_text(FAKE_FX)
        self.fx.chmod(0o700)
        self.fxenv = patch.dict(
            os.environ,
            {"Z0LIVE_FX": str(self.fx), "FAKE_FX_LOG": str(root / "fx.log"),
             "Z0LIVE_FX_WORKDIR": str(root)},
        )
        self.fxenv.start()

    def tearDown(self):
        self.fxenv.stop()
        super().tearDown()

    def load(self, value):
        path = Path(self.tmp.name) / "plan.json"
        path.write_text(json.dumps(signed(value)))
        with patch.dict(os.environ, {"Z0LIVE_VOICE_PLAN": str(path)}):
            return load_plan()

    def test_no_gpt_binding_resolves_to_hermes(self):
        # The pre-fix plan shape (GPT with no harness) is rejected at load.
        old = base.plan()
        old["roles"]["task"] = dict(SOL)
        for bad in (dict(SOL), {**SOL, "harness": "hermes"}):
            old["roles"]["task"] = bad
            with self.assertRaises(RuntimeError):
                self.load(old)
            with self.assertRaises(RuntimeError):
                hermes_binding(bad)
        plan = self.load(fx_plan())
        for binding in plan["roles"].values():
            self.assertFalse(is_gpt(binding) and harness(binding) == "hermes")
        # Invariants of the schema: fx needs effort; meta must be a Hermes session.
        with self.assertRaises(RuntimeError):
            self.load({**fx_plan(), "roles": {**fx_plan()["roles"], "task": {**SOL, "harness": "fx"}}})
        with self.assertRaises(RuntimeError):
            self.load({**fx_plan(), "meta_role": "default"})

    def test_stored_gpt_hermes_plan_never_reaches_hermes(self):
        legacy = base.plan()
        legacy["roles"]["task"] = legacy["roles"]["plan"] = dict(SOL)
        self.sid = self.session(legacy)
        reply = self.ask()
        self.wait(reply["id"])
        calls = len(self.fake.calls)
        with self.assertRaises(RuntimeError):
            self.journal.fanout(reply["id"], ["repo A"], self.fake)
        self.assertEqual(self.fake.calls[calls:], [])
        # The meta job for a GPT meta binding is refused and failed, not submitted.
        worker = base.Worker(self.store, threading.Event())
        worker.api = lambda *a, **k: self.fail("GPT meta reached Hermes")
        with self.store.connect() as db:
            job = db.execute("SELECT id FROM jobs WHERE status='queued' ORDER BY id LIMIT 1").fetchone()
            db.execute("UPDATE jobs SET kind='voice_meta',session_id=?,request=NULL WHERE id=?",
                       (self.sid, job[0]))
        worker.tick()
        with self.store.connect() as db:
            row = db.execute("SELECT status,error FROM jobs WHERE id=?", (job[0],)).fetchone()
        self.assertEqual(row[0], "failed")
        self.assertIn("must not run inside Hermes", row[1])

    def test_fx_worker_runs_explicit_model_effort_and_lands_in_voice_runs(self):
        reply = self.ask()
        self.wait(reply["id"])
        result = self.journal.fanout(reply["id"], ["repo A"], self.fake)
        self.assertEqual(result[0]["status"], "completed")
        self.assertEqual(result[0]["output"], "fx evidence")
        self.assertTrue(result[0]["run_id"].startswith("fx:"))
        self.assertFalse([c for c in self.fake.calls if c[0] in {"/v1/runs", "/api/sessions"}])
        argv = (Path(self.tmp.name) / "fx.log").read_text().split("\n")
        self.assertEqual(argv[:8], ["ask", "--json", "--no-save", "--model", "gpt-6.1-sol",
                                    "--effort", "medium", "--"])
        with self.store.connect() as db:
            row = db.execute("SELECT * FROM voice_runs WHERE turn_id=?", (reply["id"],)).fetchone()
        self.assertEqual((row["status"], row["model"], row["output"]),
                         ("completed", "gpt-6.1-sol", "fx evidence"))
        self.assertEqual(json.loads(row["request"])["harness"], "fx")
        started = [e for e in self.journal.read(reply["id"]) if e["event"] == "started"]
        self.assertEqual(started[0]["details"]["argv"][:4], ["nice", "-n", "10", "timeout"])

    def test_fx_cancel_kills_child_and_missing_fx_fails_visibly(self):
        reply = self.ask()
        self.wait(reply["id"])
        result = []
        with patch.dict(os.environ, {"FAKE_FX_SLEEP": "30"}):
            thread = threading.Thread(target=lambda: result.extend(
                self.journal.fanout(reply["id"], ["slow"], self.fake)))
            thread.start()
            time.sleep(0.5)
            self.store.add_events(self.device, [{"session_id": self.sid, "type": "barge_in",
                                                 "data": {"event_id": "unit-fx-cut"}}])
            thread.join(8)
        self.assertFalse(thread.is_alive())
        self.assertEqual(result[0]["status"], "cancelled")
        self.voice.reconcile_cancels()
        other = self.ask("second", call="unit-call-2")
        self.wait(other["id"])
        with patch.dict(os.environ, {"Z0LIVE_FX": str(Path(self.tmp.name) / "no-fx")}):
            missing = self.journal.fanout(other["id"], ["needs fx"], self.fake)
        self.assertEqual(missing[0]["status"], "failed")
        self.assertIn("fx is not installed", missing[0]["output"])


# Run only the fx tests here; the base stack tests run in their own file.
for _name in dir(base.StackTests):
    if _name.startswith("test_"):
        setattr(FxTests, _name, None)
