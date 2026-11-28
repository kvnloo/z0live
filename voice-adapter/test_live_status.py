"""Status questions answer from the live records (N216, 11-28): the TV conductor, the kanban board, the z0live job
ledger, the cron list and the call rubric, each read at question time and spoken with its read time."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import live_status as ls

_TZ = os.environ.get("TZ")


def setUpModule():
    os.environ["TZ"] = "America/Chicago"  # clock times in the answers are Kevin's wall clock (CI runs in UTC)
    time.tzset()


def tearDownModule():
    if _TZ is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _TZ
    time.tzset()


READ_AT = 1795844962.0  # 2026-11-27 23:49:22 CST, the +446 s mark of the 23:42 call


def conductor(tmp: Path, lanes) -> Path:
    path = tmp / "state.json"
    path.write_text(json.dumps({"asks": [{"id": "234846-036", "text": "latency", "lanes": lanes}]}))
    return path


class Fixture(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="live-status-"))
        self.cards = []

    def sources(self, lanes=(), cards=None, cron=None, jobs=(), rubric=None):
        cron_path = self.tmp / "jobs.json"
        cron_path.write_text(json.dumps({"jobs": cron or []}))
        rubric_path = self.tmp / "history.jsonl"
        rubric_path.write_text("".join(json.dumps(r) + "\n" for r in (rubric or [])))
        return ls.Sources(conductor=conductor(self.tmp, list(lanes)), cron=cron_path, rubric=rubric_path,
                          kanban=lambda: list(cards if cards is not None else self.cards), jobs=lambda: list(jobs))


class TopicTests(unittest.TestCase):
    def test_status_questions_name_their_topics(self):
        cases = {
            "how many lanes are running right now?": {"lanes"},
            "what's running": {"lanes"},
            "are the hill climbs still running?": {"climbs"},
            "what's blocked": {"blockers"},
            "what's the status of t_47402876": {"cards"},
            "is the job done yet": {"jobs"},
            "how are the response times": {"latency"},
        }
        for text, want in cases.items():
            self.assertTrue(want <= set(ls.topics(text)), (text, ls.topics(text)))

    def test_actions_and_other_questions_are_not_status(self):
        for text in ("start the hill climb", "add two opus lanes", "what's the capital of france",
                     "kick off the drive check", "tell me a joke"):
            self.assertEqual(ls.topics(text), [], text)


class LaneAnswerTests(Fixture):
    def test_lane_count_comes_from_the_conductor_and_board_with_the_read_time(self):
        lanes = [{"id": "234846-036-1", "model": "gpt-6.2-sol", "state": "running", "t0": READ_AT - 80, "title": "a"},
                 {"id": "234846-036-2", "model": "gpt-6.2-sol", "state": "done", "t0": READ_AT - 200, "title": "b"},
                 {"id": "234918-037-1", "model": "claude-haiku-4-5", "state": "running", "t0": READ_AT - 30,
                  "title": "c"}]
        cards = [{"id": "t_47402876", "title": "PC handoff", "status": "running", "assignee": "tek"},
                 {"id": "t_9a54606c", "title": "sweep", "status": "done", "assignee": "tek"}]
        snap = ls.read(["lanes"], self.sources(lanes, cards), now=READ_AT)
        text = ls.render(snap)
        self.assertIn("23:49:22", text)
        self.assertIn("2 TV lanes running", text)
        self.assertIn("1 kanban card running (t_47402876)", text)
        self.assertEqual({"234846-036-1", "234918-037-1", "t_47402876"} & snap["ids"],
                         {"234846-036-1", "234918-037-1", "t_47402876"})


TV_CRON = {"id": "328934019fc6", "name": "z0live TV dashboard hill climb", "enabled": True,
           "schedule_display": "every 120m", "last_run_at": "2026-11-27T22:09:55.1-06:00",
           "next_run_at": "2026-11-28T00:09:55.1-06:00", "last_status": "ok", "state": "scheduled"}
VOICE_CRON = {"id": "bb9f40de45bc", "name": "z0live-intel-climb", "enabled": True, "schedule_display": "every 60m",
              "last_run_at": "2026-11-27T23:23:28.5-06:00", "next_run_at": "2026-11-28T00:23:28.5-06:00",
              "last_status": "ok", "state": "scheduled"}
OTHER_CRON = {"id": "48edbd9fc28a", "name": "fleet-tick", "enabled": True, "schedule_display": "every 3m",
              "last_run_at": "2026-11-27T23:48:56-06:00", "next_run_at": "2026-11-27T23:51:56-06:00",
              "last_status": "ok", "state": "scheduled"}


class ClimbAnswerTests(Fixture):
    def test_each_climb_is_its_cron_record_verbatim(self):
        snap = ls.read(["climbs"], self.sources(cron=[TV_CRON, VOICE_CRON, OTHER_CRON]), now=READ_AT)
        text = ls.render(snap)
        self.assertIn("2 hill climbs scheduled", text)
        self.assertIn("z0live TV dashboard hill climb (328934019fc6): every 120m, last ran 22:09, ok, next 00:09", text)
        self.assertIn("z0live-intel-climb (bb9f40de45bc): every 60m, last ran 23:23, ok, next 00:23", text)
        self.assertNotIn("fleet-tick", text)
        self.assertLessEqual({"328934019fc6", "bb9f40de45bc"}, snap["ids"])


class AllProfilesClimbTests(Fixture):
    def test_climbs_of_every_profile_are_read_and_paused_ones_said_as_paused(self):
        tek, poteto = self.tmp / "tek.json", self.tmp / "poteto.json"
        tek.write_text(json.dumps({"jobs": [TV_CRON]}))
        poteto.write_text(json.dumps({"jobs": [dict(VOICE_CRON, id="c0ffee000001", name="z0live-climb-A-conversation",
                                                    enabled=False, state="paused")]}))
        src = self.sources()
        src.cron = [tek, poteto]
        text = ls.render(ls.read(["climbs"], src, now=READ_AT))
        self.assertIn("1 hill climb scheduled:", text)
        self.assertIn("z0live TV dashboard hill climb (328934019fc6): every 120m", text)
        self.assertIn("1 paused: z0live-climb-A-conversation (c0ffee000001).", text)


class BoardJobLatencyTests(Fixture):
    CARDS = [{"id": "t_47402876", "title": "PC handoff", "status": "running", "assignee": "tek"},
             {"id": "t_af0dc821", "title": "drive handoff", "status": "blocked", "assignee": "tek"},
             {"id": "t_11111111", "title": "guest install", "status": "blocked", "assignee": "ayo"},
             {"id": "t_9a54606c", "title": "sweep", "status": "done", "assignee": "tek"}]

    def test_blockers_are_the_blocked_cards_by_id(self):
        text = ls.render(ls.read(["blockers"], self.sources(cards=self.CARDS), now=READ_AT))
        self.assertIn("2 kanban cards blocked: t_af0dc821 (drive handoff), t_11111111 (guest install).", text)

    def test_a_named_card_is_its_board_status(self):
        snap = ls.read(["cards"], self.sources(cards=self.CARDS), now=READ_AT, request="did anyone pick up t_47402876?")
        self.assertIn("t_47402876 (PC handoff) is running, assigned to tek.", ls.render(snap))
        snap = ls.read(["cards"], self.sources(cards=self.CARDS), now=READ_AT, request="what about t_deadbeef?")
        self.assertIn("t_deadbeef is not on the board.", ls.render(snap))

    def test_open_jobs_come_from_the_job_ledger(self):
        jobs = [{"id": "t_0cc8dfeb", "engine": "kanban", "title": "drive check", "state": "running", "age_s": 340}]
        snap = ls.read(["jobs"], self.sources(jobs=jobs), now=READ_AT)
        self.assertIn("1 z0live job open: t_0cc8dfeb (drive check) running, 340 s old.", ls.render(snap))
        self.assertIn("t_0cc8dfeb", snap["ids"])

    def test_latency_is_the_newest_rubric_row_verbatim(self):
        row = {"at": "2026-11-27T23:23:00-06:00", "window_since": "2026-11-21", "calls": 14,
               "aggregate": {"ttfw_p50_s": 0.43, "ttfw_p90_s": 6.88, "deleg_first_answer_p50_s": 20.21,
                             "deleg_first_answer_p90_s": 124.94}}
        old = dict(row, at="2026-11-27T16:00:00-06:00", aggregate={"ttfw_p50_s": 3.4})
        text = ls.render(ls.read(["latency"], self.sources(rubric=[old, row]), now=READ_AT))
        self.assertIn("first words p50 0.43 s, p90 6.88 s; first answer to a delegated request p50 20.21 s, "
                      "p90 124.94 s; 14 calls since 2026-11-21, measured 23:23.", text)

    def test_an_unreadable_record_is_said_not_guessed(self):
        def broken():
            raise RuntimeError("board locked")
        src = self.sources()
        src.kanban = broken
        text = ls.render(ls.read(["blockers"], src, now=READ_AT))
        self.assertIn("I couldn't read the kanban board just now.", text)


class KanbanReaderTests(Fixture):
    def test_the_board_is_read_read_only_without_archived_cards(self):
        import sqlite3
        db = self.tmp / "kanban.db"
        con = sqlite3.connect(db)
        con.execute("create table tasks (id text primary key, title text, assignee text, status text, "
                    "created_at integer, started_at integer)")
        con.executemany("insert into tasks values (?,?,?,?,?,?)",
                        [("t_aaaaaaaa", "live", "tek", "running", 1, 2), ("t_bbbbbbbb", "old", "tek", "archived", 1, 2),
                         ("t_cccccccc", "wait", "pc", "blocked", 1, None)])
        con.commit()
        con.close()
        before = db.stat().st_mtime_ns
        cards = ls.kanban_db(db)()
        self.assertEqual(sorted(c["id"] for c in cards), ["t_aaaaaaaa", "t_cccccccc"])
        self.assertEqual({c["id"]: c["status"] for c in cards}["t_aaaaaaaa"], "running")
        self.assertEqual(db.stat().st_mtime_ns, before)

    def test_the_board_path_follows_the_hermes_resolution(self):
        root = self.tmp / "hermes"
        self.assertEqual(ls.board_db_path({}, root), root / "kanban.db")
        self.assertEqual(ls.board_db_path({"HERMES_KANBAN_BOARD": "z0live"}, root),
                         root / "kanban" / "boards" / "z0live" / "kanban.db")
        self.assertEqual(ls.board_db_path({"HERMES_KANBAN_DB": "/x/k.db", "HERMES_KANBAN_BOARD": "z0live"}, root),
                         Path("/x/k.db"))


class GateTests(Fixture):
    def snap(self):
        lanes = [{"id": "234846-036-1", "model": "gpt-6.2-sol", "state": "running", "t0": READ_AT - 80}]
        cards = [{"id": "t_47402876", "title": "PC handoff", "status": "running", "assignee": "tek"},
                 {"id": "t_9a54606c", "title": "sweep", "status": "done", "assignee": "tek"}]
        return ls.read(["lanes", "cards"], self.sources(lanes, cards), now=READ_AT)

    def test_a_state_claim_without_an_id_read_this_turn_is_not_said(self):
        for claim in ("Zero lanes are running.", "Every lane has finished.", "Nobody picked up the PC handoff.",
                      "Both hill climbs were stopped by the symlink.", "Five lanes are still running."):
            out, dropped = ls.gate(claim, self.snap())
            self.assertEqual(out, ls.CANT_CONFIRM, claim)
            self.assertEqual(len(dropped), 1, claim)

    def test_a_cited_id_whose_record_disagrees_is_corrected_from_the_record(self):
        out, dropped = ls.gate("The PC handoff t_47402876 hasn't started; nobody picked it up.", self.snap())
        self.assertEqual(out, "t_47402876 is running as of 23:49:22.")
        out, _ = ls.gate("t_9a54606c is still running.", self.snap())
        self.assertEqual(out, "t_9a54606c is done as of 23:49:22.")

    def test_a_cited_id_that_agrees_and_plain_facts_are_kept(self):
        text = "Lane 234846-036-1 is running. The drive has three volumes."
        self.assertEqual(ls.gate(text, self.snap()), (text, []))
        self.assertEqual(ls.gate("t_9a54606c finished.", self.snap())[0], "t_9a54606c finished.")

    def test_an_id_not_read_this_turn_does_not_count(self):
        out, _ = ls.gate("t_12345678 is running.", self.snap())
        self.assertEqual(out, ls.CANT_CONFIRM)
        out, _ = ls.gate("t_12345678 is running.", self.snap(), extra_ids={"t_12345678"})
        self.assertEqual(out, "t_12345678 is running.")


class RewordedTests(unittest.TestCase):
    CLIMBS = ("As of 23:49:22: 2 hill climbs scheduled: z0live TV dashboard hill climb (328934019fc6): every 120m, "
              "last ran 22:09, ok, next 00:09. z0live-intel-climb (bb9f40de45bc): every 60m, last ran 23:23, ok, "
              "next 00:23.")

    def test_changed_counts_and_clocks_are_caught(self):
        self.assertTrue(ls.reworded(self.CLIMBS, "Both hill climbs run every hour."))
        self.assertTrue(ls.reworded(self.CLIMBS, "The voice climb ran just before the 22:09 TV run, at 22:00."))
        self.assertTrue(ls.reworded("As of 23:49:22: 4 TV lanes running (4 gpt-6.2-sol).",
                                    "Five lanes are still running."))

    def test_the_same_numbers_in_spoken_form_pass(self):
        self.assertEqual(ls.reworded("As of 23:49:22: 2 TV lanes running (2 gpt-6.2-sol). 1 kanban card running "
                                     "(t_47402876).",
                                     "As of 11:49 PM, two TV lanes are running, both gpt 6.2 sol, two of them. "
                                     "One kanban card is running, t_47402876."), [])
        self.assertEqual(ls.reworded(self.CLIMBS, self.CLIMBS), [])


if __name__ == "__main__":
    unittest.main()
