"""Stack behavior with fake inference/native APIs. No provider or owner history access."""

import hashlib
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop.core import Store, dumps
from z0live_desktop.voice_runtime import load_plan
from z0live_desktop.worker import Worker


def plan(model="unit-haiku"):
    value = {
        "schema": "z0int.voice_plan.v1",
        "model": "gpt-realtime",
        "voice": "marin",
        "adapter": "desktop-webrtc",
        "harness": "hermes",
        "meta_role": "plan",
        "fallbacks": [],
        "roles": {
            r: {"provider": "unit", "model": model if r == "smol" else "unit-" + r}
            for r in ["smol", "default", "plan", "slow", "task", "judge"]
        },
    }
    value["digest"] = hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return value


class FakeNative:
    base = "http://unit"

    def __init__(self):
        self.calls = []
        self.runs = {}
        self.stopped = set()
        self.lock = threading.Lock()
        self.finish = True
        self.small_fail = False

    def small(self, turn, binding, prompt, on_delta=None):
        self.calls.append(("small", binding, prompt))
        if self.small_fail:
            raise RuntimeError("unit unavailable")
        return {
            "type": "done",
            "text": json.dumps(
                {"action": "delegate", "brief": "unit brief", "say": "Checking."}
            )
            if prompt.startswith("Turn the user's")
            else "Unit short answer.",
            "ms": 7,
            "usage": {"input_tokens": 10, "output_tokens": 5},
        }

    def cancel_liaison(self, turn):
        self.calls.append(("cancel_small", turn))

    def stream(self, *args, **kwargs):
        yield {"type": "tool.started", "tool_name": "terminal", "seq": 0}
        yield {
            "type": "message.delta",
            "delta": "private reasoning must not be spoken",
            "seq": 1,
        }

    def api(self, path, body=None, idem=None):
        with self.lock:
            self.calls.append((path, body, idem))
            if path.startswith("/api/sessions/"):
                raise RuntimeError("upstream HTTP 404")
            if path == "/api/sessions":
                return {"session": body}
            if path == "/v1/runs":
                ident = "unit-" + idem
                self.runs[ident] = body
                return {"run_id": ident}
            ident = path.split("/")[3]
            if path.endswith("/stop"):
                self.stopped.add(ident)
                return {"status": "stopping"}
            return {
                "run_id": ident,
                "status": "cancelled"
                if ident in self.stopped
                else "completed"
                if self.finish
                else "running",
                "output": "unit evidence with citation",
                "usage": {"input_tokens": 20, "output_tokens": 8},
            }


class StackTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.env = patch.dict(
            os.environ,
            {
                "Z0LIVE_VOICE_PLAN": str(Path(self.tmp.name) / "missing"),
                # These tests cover the L1/L2 liaison stack, now opt-in for gpt-realtime work.
                "Z0LIVE_REALTIME_WORK": "liaison",
            },
        )
        self.env.start()
        self.store = Store(self.tmp.name)
        self.device = self.store.authenticate(self.store.pair("unit"))
        self.other = self.store.authenticate(self.store.pair("other"))
        self.sid = self.session(plan())
        self.voice = self.store.voice
        self.fake = FakeNative()
        self.voice.native = self.fake
        self.journal = self.voice.journal
        self.source = patch(
            "z0live_desktop.work_sources.z0",
            return_value={"items": [], "gaps": ["unit missing source"]},
        )
        self.source.start()

    def session(self, selected):
        with (
            patch("z0live_desktop.voice_runtime.load_plan", return_value=selected),
            patch(
                "z0live_desktop.core.mint",
                return_value={"value": "unit", "expires_at": 1},
            ),
        ):
            return self.store.new_session(self.device, {"client": "web"})["session_id"]

    def tearDown(self):
        self.voice.pool.shutdown(wait=True)
        for watcher in self.voice.watchers:
            watcher.join(2)
        self.source.stop()
        self.env.stop()
        self.tmp.cleanup()

    def ask(self, question="unit request", call="unit-call"):
        return self.store.tool(
            self.device,
            "ask_z0",
            {
                "session_id": self.sid,
                "call_id": call,
                "arguments": {"question": question},
            },
        )

    def wait(self, turn, state="meta_queued"):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            with self.store.connect() as db:
                row = db.execute(
                    "SELECT * FROM voice_turns WHERE id=?", (turn,)
                ).fetchone()
            if row["status"] == state:
                return dict(row)
            time.sleep(0.01)
        self.fail("state not reached: " + str(dict(row)))

    def test_hot_cache_has_no_inference_and_pinned_plan(self):
        reply = self.ask("what model are you using?")
        self.assertEqual(reply["cache"], "pinned_voice_plan")
        self.assertIn("unit-haiku", reply["say"])
        self.assertEqual(self.fake.calls, [])
        another = self.session(plan("changed-haiku"))
        self.assertEqual(
            self.journal.plan(self.sid)["roles"]["smol"]["model"], "unit-haiku"
        )
        self.assertEqual(
            self.journal.plan(another)["roles"]["smol"]["model"], "changed-haiku"
        )
        events = self.journal.read(reply["id"])
        self.assertEqual(events[0]["details"]["inference_calls"], 0)
        self.assertEqual(
            (Path(self.tmp.name) / "traces" / (reply["id"] + ".jsonl")).stat().st_mode
            & 0o777,
            0o600,
        )

    def test_idempotent_admission_and_changed_arguments_rejected(self):
        reply = self.ask()
        self.assertEqual(self.ask()["id"], reply["id"])
        with self.assertRaises(ValueError):
            self.ask("different request")
        self.wait(reply["id"])
        with self.store.connect() as db:
            self.assertEqual(
                db.execute(
                    "SELECT count(*) FROM jobs WHERE kind='voice_meta'"
                ).fetchone()[0],
                1,
            )

    def test_ownership_and_schema_before_execution(self):
        with self.assertRaises(LookupError):
            self.store.tool(
                self.other,
                "ask_z0",
                {"session_id": self.sid, "arguments": {"question": "unit"}},
            )
        with self.assertRaises(ValueError):
            self.store.tool(
                self.device,
                "ask_z0",
                {
                    "session_id": self.sid,
                    "arguments": {"question": "unit", "model": "not allowed"},
                },
            )
        sid = self.session(None)
        with self.assertRaises(RuntimeError):
            self.store.tool(
                self.device,
                "ask_z0",
                {"session_id": sid, "arguments": {"question": "unit"}},
            )

    def test_l1_down_is_faithful_handoff_not_model_fallback(self):
        self.fake.small_fail = True
        reply = self.ask("literal owner request")
        row = self.wait(reply["id"])
        self.assertEqual(row["brief"], "literal owner request")
        self.assertIn(
            "brief_unavailable", [e["event"] for e in self.journal.read(reply["id"])]
        )

    def test_native_meta_binding_and_condensed_result(self):
        reply = self.ask()
        self.wait(reply["id"])
        worker = Worker(self.store, threading.Event())
        worker.api = self.fake.api
        worker.tick()
        self.wait(reply["id"], "completed")
        run = next(c for c in self.fake.calls if c[0] == "/v1/runs")
        self.assertEqual(run[1]["model"], "unit-plan")
        self.assertIn("Initial z0intelligence evidence", run[1]["input"])
        self.assertIn("fanout --turn " + reply["id"], run[1]["input"])
        spoken = self.store.inbox(self.device)["messages"]
        self.assertTrue(any(m["say"] == "Unit short answer." for m in spoken))
        self.assertFalse(any("private reasoning" in m["say"] for m in spoken))
        self.assertTrue(any(e["usage"] for e in self.journal.read(reply["id"])))
        condensation = [c[2] for c in self.fake.calls if c[0] == "small"][-1]
        self.assertIn('"owner_request":"unit request"', condensation)
        self.assertIn('"native_status":"completed"', condensation)

    def test_waiting_has_one_ack_then_only_the_actual_result(self):
        reply = self.ask("Check the current build")
        self.wait(reply["id"])
        worker = Worker(self.store, threading.Event())
        worker.api = self.fake.api
        worker.tick()
        self.wait(reply["id"], "completed")
        spoken = [m["say"] for m in self.store.inbox(self.device)["messages"]]
        self.assertEqual(spoken, ["Unit short answer."])
        self.assertLessEqual(len(reply["say"].split()), 4)
        self.assertEqual(reply.get("speech"), "silent")

    def test_empty_waiting_updates_never_enter_the_speech_queue(self):
        reply = self.ask("Check the build")
        self.wait(reply["id"])
        self.voice.floor(self.sid, "user")
        for event, say in (
            ("memory_started", "Checking memory and current context."),
            ("meta_started", "The work agent is checking it."),
            ("workers_started", "2 agents are checking the context."),
            ("heartbeat", "Still working."),
            (
                "partial-1",
                "I haven't gotten an update yet but something will be coming soon.",
            ),
            ("partial-2", "No update yet but something might come in soon."),
            ("partial-3", "   "),
        ):
            self.journal.speak(reply["id"], say, event)
        self.voice.floor(self.sid, "quiet")
        self.assertEqual(self.store.inbox(self.device)["messages"], [])

    def test_real_blocker_after_a_status_prefix_is_not_dropped(self):
        reply = self.ask("Check the build")
        self.wait(reply["id"])
        finding = "No update yet: the build is blocked by HTTP 503."
        self.journal.speak(reply["id"], finding, "partial-blocker")
        self.assertEqual(
            [m["say"] for m in self.store.inbox(self.device)["messages"]], [finding]
        )

    def test_same_finding_deduplicates_across_event_ids_and_restart(self):
        reply = self.ask("Check the build")
        self.wait(reply["id"])
        self.journal.speak(reply["id"], "The build passed.", "partial-first")
        from z0live_desktop.voice_runtime import Journal

        restarted = Journal(self.store)
        restarted.speak(reply["id"], "  The   build passed!  ", "partial-retry")
        restarted.speak(reply["id"], "The lint check failed.", "partial-next")
        self.assertEqual(
            [m["say"] for m in self.store.inbox(self.device)["messages"]],
            ["The build passed.", "The lint check failed."],
        )

    def test_terminal_answer_replaces_held_progress_and_retries_stay_silent(self):
        reply = self.ask("Check the build")
        self.wait(reply["id"])
        self.assertEqual(self.ask("Check the build").get("speech"), "silent")
        self.voice.floor(self.sid, "user")
        self.journal.speak(reply["id"], "The compile step passed.", "partial-compile")
        self.voice.finish(reply["id"], "The build and lint passed.")
        self.voice.floor(self.sid, "quiet")
        self.assertEqual(
            [m["say"] for m in self.store.inbox(self.device)["messages"]],
            ["The build and lint passed."],
        )

    def test_legacy_status_rows_remain_cursor_visible_but_silent(self):
        reply = self.ask("Check the build")
        self.wait(reply["id"])
        self.store.publish(
            "The work agent is checking it.",
            sid=self.sid,
            source="voice:" + reply["id"] + ":meta_started",
            params={"turn_id": reply["id"], "event": "meta_started"},
        )
        page = self.store.inbox(self.device)
        self.assertEqual(len(page["messages"]), 1)
        item = page["messages"][0]
        self.assertEqual(item["say"], "")
        self.assertTrue(item["params"]["silent"])
        self.assertEqual(item["source"], "voice:" + reply["id"] + ":meta_started")
        self.assertEqual(page["after"], item["id"])

    def test_liaison_cannot_block_handoff_for_its_own_execution_authority(self):
        with patch.object(
            self.fake,
            "small",
            return_value={
                "text": dumps(
                    {
                        "action": "clarify",
                        "brief": "Run the requested read-only pwd",
                        "say": "Need clarification on execution authority.",
                    }
                ),
                "ms": 1,
            },
        ):
            reply = self.ask("Run read-only pwd in the broker checkout")
            self.wait(reply["id"])
        self.assertEqual(self.store.inbox(self.device)["messages"], [])

    def test_concrete_missing_human_input_still_asks_one_question(self):
        with patch.object(
            self.fake,
            "small",
            return_value={
                "text": dumps(
                    {
                        "action": "clarify",
                        "brief": "Need a repository",
                        "missing_input": "repository name",
                        "say": "Which repository should I check?",
                    }
                ),
                "ms": 1,
            },
        ):
            reply = self.ask("Check that repository")
            self.wait(reply["id"], "completed")
        self.assertEqual(
            [m["say"] for m in self.store.inbox(self.device)["messages"]],
            ["Which repository should I check?"],
        )

    def test_floor_holds_and_quiet_flushes_once(self):
        reply = self.ask("what model are you using?")
        self.voice.floor(self.sid, "user")
        self.journal.speak(reply["id"], "unit queued", "unit-progress")
        self.assertEqual(self.store.inbox(self.device)["messages"], [])
        self.voice.floor(self.sid, "assistant")
        self.assertEqual(self.store.inbox(self.device)["messages"], [])
        self.voice.floor(self.sid, "quiet")
        self.voice.floor(self.sid, "quiet")
        self.assertEqual(
            [m["say"] for m in self.store.inbox(self.device)["messages"]],
            ["unit queued"],
        )

    def test_two_workers_actual_native_runs_model_lock_and_usage(self):
        reply = self.ask()
        self.wait(reply["id"])
        results = self.journal.fanout(reply["id"], ["repo A", "repo B"], self.fake)
        self.assertEqual([r["status"] for r in results], ["completed", "completed"])
        starts = [
            e
            for e in self.journal.read(reply["id"])
            if e["layer"] == "L3" and e["event"] == "started"
        ]
        self.assertEqual(len(starts), 2)
        self.assertTrue(all(e["model"] == "unit-task" for e in starts))
        locks = [c for c in self.fake.calls if c[0] == "/api/sessions"]
        self.assertTrue(all(c[1]["require_model_lock"] for c in locks))
        # Lost caller replay reuses native idempotency, not two new runs.
        self.journal.fanout(reply["id"], ["repo A", "repo B"], self.fake)
        self.assertEqual(len(self.fake.runs), 2)

    def test_interrupt_stops_two_children_and_suppresses_stale_speech(self):
        self.fake.finish = False
        reply = self.ask()
        self.wait(reply["id"])
        result = []
        thread = threading.Thread(
            target=lambda: result.extend(
                self.journal.fanout(reply["id"], ["A", "B"], self.fake)
            )
        )
        thread.start()
        deadline = time.monotonic() + 3
        while len(self.fake.runs) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
        self.voice.floor(self.sid, "user")
        self.store.add_events(
            self.device,
            [
                {
                    "session_id": self.sid,
                    "type": "barge_in",
                    "data": {"event_id": "unit-interrupt"},
                }
            ],
        )
        thread.join(4)
        self.assertFalse(thread.is_alive())
        self.voice.reconcile_cancels()
        self.wait(reply["id"], "cancelled")
        self.assertEqual(len(self.fake.stopped), 2)
        self.assertTrue(all(r["status"] == "cancelled" for r in result))
        self.voice.floor(self.sid, "quiet")
        self.journal.speak(reply["id"], "stale", "stale")
        self.assertFalse(
            any(m["say"] == "stale" for m in self.store.inbox(self.device)["messages"])
        )

    def test_worker_timeout_is_not_success(self):
        self.fake.finish = False
        reply = self.ask()
        self.wait(reply["id"])
        with patch.dict(os.environ, {"Z0LIVE_WORKER_SECONDS": "0.01"}):
            result = self.journal.fanout(reply["id"], ["timed worker"], self.fake)
        self.assertEqual(result[0]["status"], "cancelled")
        self.assertEqual(len(self.fake.stopped), 1)

    def test_atomic_events_no_cancel_on_rejected_batch_and_dedupe(self):
        reply = self.ask()
        self.wait(reply["id"])
        with self.assertRaises(ValueError):
            self.store.add_events(
                self.device,
                [
                    {"session_id": self.sid, "type": "cancel"},
                    {"session_id": self.sid, "type": "bad"},
                ],
            )
        self.wait(reply["id"])
        event = {
            "session_id": self.sid,
            "type": "user_transcript",
            "text": "never mind",
            "data": {"event_id": "one"},
        }
        self.store.add_events(self.device, [event])
        self.assertEqual(self.store.add_events(self.device, [event])["accepted"], 0)
        self.wait(reply["id"], "cancelled")

    def test_summary_context_seed_never_spoken(self):
        worker = Worker(self.store, threading.Event())
        worker.api = self.fake.api
        with self.store.connect() as db:
            for kind in ["summary", "context", "seed", "feedback", "feedback_note"]:
                self.store.enqueue(db, kind, "unit note-to-Hermes", self.sid)
        for _ in range(5):
            worker.tick()
        self.assertEqual(self.store.inbox(self.device)["messages"], [])

    def test_historical_summary_stays_silent_even_in_same_active_session(self):
        with self.store.connect() as db:
            job = self.store.enqueue(db, "summary", "old summary", self.sid)
            db.execute(
                "UPDATE jobs SET status='completed',run_id='unit-old-summary' WHERE id=?",
                (job,),
            )
        self.store.publish(
            "old native summary reply", sid=self.sid, source="run:unit-old-summary"
        )
        message = self.store.inbox(self.device)["messages"][0]
        self.assertEqual(message["say"], "")
        self.assertEqual(message["params"]["suppressed"], "note_to_hermes")

    def test_status_only_session_does_not_enqueue_summary(self):
        self.store.add_events(
            self.device,
            [
                {
                    "session_id": self.sid,
                    "type": "app_state",
                    "data": {"connected": True},
                }
            ],
        )
        self.store.end(self.device, self.sid)
        with self.store.connect() as db:
            self.assertEqual(
                db.execute("SELECT count(*) FROM jobs WHERE kind='summary'").fetchone()[
                    0
                ],
                0,
            )

    def test_old_ended_or_superseded_session_never_speaks_backlog(self):
        reply = self.ask("what model are you using?")
        self.journal.speak(reply["id"], "requested live update", "live")
        self.assertEqual(
            self.store.inbox(self.device)["messages"][0]["say"], "requested live update"
        )
        self.store.end(self.device, self.sid)
        self.journal.speak(reply["id"], "ended update", "ended")
        self.assertTrue(
            all(m["say"] == "" for m in self.store.inbox(self.device)["messages"])
        )
        self.session(plan())
        self.journal.speak(reply["id"], "old backlog", "old")
        self.assertTrue(
            all(m["say"] == "" for m in self.store.inbox(self.device)["messages"])
        )
        with self.assertRaises(ValueError):
            self.ask("new work from old session", call="old")
        # Notes/broadcasts with no user-started originating voice session are silent too.
        self.store.publish("unsolicited note", source="unit-note")
        inbox = self.store.inbox(self.device)
        self.assertTrue(all(m["say"] == "" for m in inbox["messages"]))
        self.assertEqual(inbox["after"], inbox["messages"][-1]["id"])

    def test_native_policy_disables_fallback_for_explicit_bindings(self):
        from native_policy import PinnedVoiceRuns

        class Base:
            def _create_agent(self, *args, **kwargs):
                return kwargs

        class Host(PinnedVoiceRuns, Base):
            pass

        request = Host()._create_agent(
            requested_provider="unit", requested_model="literal-model"
        )
        self.assertTrue(request["confirmed_runtime_lock"])
        self.assertEqual(request["requested_model"], "literal-model")
        self.assertNotIn("confirmed_runtime_lock", Host()._create_agent())

    def test_lost_child_acceptance_stays_uncertain_not_false_cancelled(self):
        reply = self.ask()
        self.wait(reply["id"])
        original = self.fake.api

        def lost(path, body=None, idem=None):
            if path == "/v1/runs":
                original(path, body, idem)  # Native accepted, transport lost the reply.
                raise RuntimeError("upstream unavailable")
            return original(path, body, idem)

        self.fake.api = lost
        result = self.journal.fanout(reply["id"], ["one read-only worker"], self.fake)
        self.assertEqual(result[0]["status"], "unknown")
        self.voice.cancel(self.sid)
        self.wait(reply["id"], "cancelling")
        self.assertEqual(len(self.fake.runs), 1)
        self.assertFalse(
            any(e["event"] == "cancelled" for e in self.journal.read(reply["id"]))
        )

    def test_recovery_delivers_saved_result_without_native_reexecution(self):
        reply = self.ask()
        row = self.wait(reply["id"])
        with self.store.connect() as db:
            db.execute(
                "UPDATE jobs SET status='completed',result='saved verified answer' WHERE id=?",
                (row["job_id"],),
            )
        before = len(self.fake.calls)
        self.voice.recover()
        self.voice.recover()
        self.wait(reply["id"], "completed")
        self.assertEqual(len(self.fake.calls), before)
        messages = [
            m
            for m in self.store.inbox(self.device)["messages"]
            if m["params"].get("event") == "result"
        ]
        self.assertEqual(len(messages), 1)
        self.assertIn("saved verified answer", messages[0]["say"])

    def test_model_usage_trace_is_session_scoped(self):
        reply = self.ask("what model are you using?")
        self.store.add_events(
            self.device,
            [
                {
                    "session_id": self.sid,
                    "type": "model_usage",
                    "data": {
                        "turn_id": reply["id"],
                        "usage": {"input_tokens": 12, "output_tokens": 2},
                    },
                }
            ],
        )
        self.assertTrue(
            any(
                e["layer"] == "L0" and e["usage"]["input_tokens"] == 12
                for e in self.journal.read(reply["id"])
            )
        )
        other_sid = self.session(plan())
        with self.assertRaises(ValueError):
            self.store.add_events(
                self.device,
                [
                    {
                        "session_id": other_sid,
                        "type": "model_usage",
                        "data": {"turn_id": reply["id"], "usage": {"input_tokens": 12}},
                    }
                ],
            )

    def test_invalid_plan_is_not_repaired(self):
        path = Path(self.tmp.name) / "plan.json"
        path.write_text(dumps(plan()))
        with patch.dict(os.environ, {"Z0LIVE_VOICE_PLAN": str(path)}):
            self.assertEqual(load_plan()["digest"], plan()["digest"])
            value = plan()
            value["roles"]["smol"]["model"] = " whitespace "
            path.write_text(dumps(value))
            with self.assertRaises(RuntimeError):
                load_plan()
