"""N216 replay: the 13 false claims of the 11-27 23:42 call (R2 audit, ~/.hermes/profiles/tek/workspace/z0live-loop/
R2-REPORT.md, register rows 234227 FALSE) go back through the path that produced each one, with the live records as
they stood at that minute. Each must come out corrected from the records or refused ("I can't confirm that").

Paths: T = a tool-free tier lane (tier_lanes), B = a background Hermes answer (host_delegate -> ledger_answer),
V = the voice rewording a status answer (check_verbatim), C = a conductor lane card (card_text).
"""
import asyncio
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import live_status as ls
import mobile_live as live

_TZ = os.environ.get("TZ")


def setUpModule():
    os.environ["TZ"] = "America/Chicago"
    time.tzset()


def tearDownModule():
    if _TZ is None:
        os.environ.pop("TZ", None)
    else:
        os.environ["TZ"] = _TZ
    time.tzset()


CALL_START = 1795844547.0  # 2026-11-27 23:42:27 CST (trace 234227)
TV_CRON = {"id": "328934019fc6", "name": "z0live TV dashboard hill climb", "enabled": True,
           "schedule_display": "every 120m", "last_run_at": "2026-11-27T22:09:55.1-06:00",
           "next_run_at": "2026-11-28T00:09:55.1-06:00", "last_status": "ok", "state": "scheduled"}
VOICE_CRON = {"id": "bb9f40de45bc", "name": "z0live-intel-climb", "enabled": True, "schedule_display": "every 60m",
              "last_run_at": "2026-11-27T23:23:28.5-06:00", "next_run_at": "2026-11-28T00:23:28.5-06:00",
              "last_status": "ok", "state": "scheduled"}
CARDS = [{"id": "t_9a54606c", "title": "Past-week sweep", "status": "running", "assignee": "tek"},
         {"id": "t_47402876", "title": "PC handoff: reuse chat routing", "status": "running", "assignee": "tek"},
         {"id": "t_af0dc821", "title": "PC drive handoff", "status": "running", "assignee": "tek"}]
RUBRIC = {"at": "2026-11-27T23:23:00-06:00", "window_since": "2026-11-20", "calls": 13,
          "aggregate": {"ttfw_p50_s": 0.43, "ttfw_p90_s": 6.88, "deleg_first_answer_p50_s": 20.21,
                        "deleg_first_answer_p90_s": 124.94}}


def sol(n, state="running"):
    return {"id": f"234846-036-{n}", "model": "gpt-6.2-sol", "state": state, "t0": CALL_START + 300, "title": "latency"}


def haiku(n, state="running"):
    return {"id": f"234918-037-{n}", "model": "claude-haiku-4-5", "state": state, "t0": CALL_START + 400,
            "title": "latency"}


AT_412 = [sol(1), sol(2)]                                    # N: +412 s, two Sol outstanding
AT_446 = [sol(1), sol(2, "done"), haiku(1), haiku(2), haiku(3)]  # +446 s: four outstanding

# (row, path, at seconds into the call, conductor lanes then, the false words, the request that drew them)
FALSE_CLAIMS = [
    ("234227:195/279", "T", 23, AT_412, "Neither scheduled climb is running anymore, because of the scratch symlink.",
     "are the hill climbs still running?"),
    ("234227:195/279b", "T", 23, AT_412, "The latest climb check was about eight hours ago.",
     "when did the hill climbs last run?"),
    ("234227:907", "V", 60, AT_412, "Both of them run every hour.", "how often do the hill climbs run?"),
    ("234227:1183/1307", "T", 160, AT_412, "The symlink is still the blocker and neither climb has started.",
     "did the hill climbs start?"),
    ("234227:1557", "B", 230, AT_412, "Voice round eight ran at precisely 23:22.", "say more about that"),
    ("234227:1557/1859", "V", 240, AT_412, "The voice round ran just before that 22:09 TV dispatch.",
     "when did each hill climb last run?"),
    ("234227:2003/2064", "C", 250, AT_412, "Both climbs are blocked by one symlink.", ""),
    ("234227:2538/2563", "T", 412, AT_412, "Zero lanes are running.", "how many lanes are running right now?"),
    ("234227:2829", "V", 446, AT_446, "Five are still running now.", "how many lanes are running right now?"),
    ("234227:2635/2829", "B", 426, AT_412, "Every lane so far has finished.", "say more about that"),
    ("234227:2635/2829b", "B", 426, AT_412, "The first result takes 30 to 67 seconds.",
     "say more about that"),
    ("234227:5099/5210", "B", 900, AT_412, "The PC bot doesn't run the server that bot-to-bot messaging needs.",
     "say more about that"),
    ("234227:6750", "B", 992, AT_412, "The PC handoff implementation hasn't started and no one has picked it up.",
     "say more about that"),
]


class Replay(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="n216-"))
        self.env = mock.patch.dict(os.environ, {"Z0LIVE_LEDGER": str(self.tmp / "ledger")})
        self.env.start()

    async def asyncTearDown(self):
        self.env.stop()

    def call_at(self, at, lanes):
        state = self.tmp / f"state-{at}.json"
        state.write_text(json.dumps({"asks": [{"id": "234846-036", "text": "latency", "lanes": lanes}]}))
        cron = self.tmp / "jobs.json"
        cron.write_text(json.dumps({"jobs": [TV_CRON, VOICE_CRON]}))
        rubric = self.tmp / "history.jsonl"
        rubric.write_text(json.dumps(RUBRIC) + "\n")
        call = live.Call(live.Manager(None), profile="tek")
        call.status_sources = ls.Sources(conductor=state, cron=cron, rubric=rubric, kanban=lambda: list(CARDS),
                                         jobs=lambda: [])
        call.status_clock = lambda: CALL_START + at
        call.protocol.seen.extend(["d1", "d2"])
        sent = []
        call.send = lambda event, to_provider=False, turn_id=None: sent.append(event)
        return call, sent

    @staticmethod
    def spoken(sent):
        return " ".join(c.get("text", "") for e in sent for c in e.get("content", []) if isinstance(c, dict)
                        and e.get("type") in ("delegation.context.append", "session.context.append")
                        and c.get("text", "").startswith('"Agent Final Message"'))

    async def run_path(self, path, at, lanes, claim, request):
        call, sent = self.call_at(at, lanes)
        try:
            if path == "T":
                async def race(http, name, bindings, context, req):
                    return claim, "gpt-6.1-terra", 0.4, []
                with mock.patch.object(live.tiers, "load", return_value={"fast": [{"provider": "x", "model": "y"}]}), \
                        mock.patch.object(live.tiers, "race", side_effect=race):
                    call.latest_did = "d1"
                    call.ledger_open("d1", "tell me more")
                    await call.tier_lanes("d1", "tell me more")
                tier = self.spoken(sent)
                status = ""
                if request:  # the same question now: answered from the records, never by a tier
                    call2, sent2 = self.call_at(at, lanes)
                    with mock.patch.object(live, "run_hermes", side_effect=AssertionError("records, not Hermes")):
                        await call2.host_delegate("d2", request)
                    status = self.spoken(sent2)
                    await call2.close("test")
                return tier, status
            if path == "B":
                with mock.patch.object(live, "run_hermes", return_value=claim):
                    await call.host_delegate("d1", request)
                return self.spoken(sent), ""
            if path == "V":
                with mock.patch.object(live, "run_hermes", side_effect=AssertionError("records, not Hermes")):
                    await call.host_delegate("d1", request)
                status = self.spoken(sent)
                sent.clear()
                call.check_verbatim(claim)
                return self.spoken(sent), status
            if path == "C":
                lane = {"id": "234609-035-2", "model": "claude-haiku-4-5", "state": "done",
                        "card": {"summary": claim}, "title": "Hill climb launch blocker"}
                return await call.card_text([lane]), ""
        finally:
            await call.close("test")

    async def test_all_13_false_claims_come_out_corrected_or_refused(self):
        outcomes = []
        for row, path, at, lanes, claim, request in FALSE_CLAIMS:
            said, status = await self.run_path(path, at, lanes, claim, request)
            with self.subTest(row=row, path=path):
                self.assertNotIn(claim, said, "the false words were spoken as they were")
                corrected = path == "V" and "correction:" in said
                refused = ls.CANT_CONFIRM in said or (path in ("T", "C") and not said.strip())
                self.assertTrue(corrected or refused, (row, said))
                if status:  # the status question itself, answered from the records of that minute
                    self.assertIn("As of", status)
            outcomes.append((row, path, "corrected" if corrected else "refused", said[:160], status[:300]))
        out = Path(os.environ.get("N216_REPLAY_OUT", self.tmp / "replay.json"))
        out.write_text(json.dumps(outcomes, indent=1))
        self.assertEqual(len(outcomes), 13)

    async def test_status_answers_at_each_minute_match_the_records(self):
        _, status = await self.run_path("T", 412, AT_412, "Zero lanes are running.",
                                        "how many lanes are running right now?")
        self.assertIn("As of 23:49:19:", status)
        self.assertIn("2 TV lanes running (2 gpt-6.2-sol)", status)
        self.assertIn("3 kanban cards running (t_9a54606c, t_47402876, t_af0dc821)", status)
        _, status = await self.run_path("T", 23, AT_412, "x running.", "are the hill climbs still running?")
        self.assertIn("z0live TV dashboard hill climb (328934019fc6): every 120m, last ran 22:09, ok, next 00:09", status)
        self.assertIn("z0live-intel-climb (bb9f40de45bc): every 60m, last ran 23:23, ok, next 00:23", status)

    async def test_true_claims_that_cite_a_record_still_pass(self):
        said, _ = await self.run_path("B", 160, AT_412, "The past-week sweep t_9a54606c is running.",
                                      "say more about that")
        self.assertIn("The past-week sweep t_9a54606c is running.", said)
        self.assertNotIn(ls.CANT_CONFIRM, said)
        said, _ = await self.run_path("B", 160, AT_412, "It is 72 degrees outside.", "what's the weather")
        self.assertIn("It is 72 degrees outside.", said)

    async def test_a_status_question_the_voice_never_delegates_is_answered_from_the_records(self):
        # Live probe 11-28 03:16 (release 1a35311): the voice said "Let me check.", sent no delegation, and 70 s later
        # said "Three lanes are active right now." with 0 TV lanes and 1 card running. Kevin's own words are enough.
        call, sent = self.call_at(412, AT_412)
        try:
            user = {"type": "turn.done", "turn": {"id": "u1", "role": "user", "start_ms": 0, "end_ms": 2000,
                                                  "transcript": " How many lanes are running right now"}}
            call.message(json.dumps(user), True)
            for _ in range(100):
                await asyncio.sleep(0.01)
                if self.spoken(sent):
                    break
            answer = self.spoken(sent)
            self.assertIn("As of 23:49:19: 2 TV lanes running (2 gpt-6.2-sol).", answer)
            with mock.patch.object(live, "run_hermes", side_effect=AssertionError("answered already")):
                await call.host_delegate("d1", "How many lanes are running right now?")  # a late delegation: once
            self.assertEqual(self.spoken(sent).count("As of"), 1)
            sent.clear()
            voice = {"type": "turn.done", "turn": {"id": "a1", "role": "assistant", "start_ms": 2000, "end_ms": 5000,
                                                   "transcript": " Three lanes are active right now."}}
            call.message(json.dumps(voice), True)
            self.assertIn("correction: As of 23:49:19: 2 TV lanes running", self.spoken(sent))
        finally:
            await call.close("test")

    async def test_completion_worded_as_doing_a_round_still_needs_proof(self):
        said, _ = await self.run_path("B", 412, AT_412, "The orchard monitor did its second round at 11:20 PM.",
                                      "say more about that")
        self.assertIn(ls.CANT_CONFIRM, said)
        self.assertNotIn("11:20", said)

    async def test_earlier_cards_obey_corrections_and_the_proof_gate(self):
        call, sent = self.call_at(412, AT_412)
        try:
            call.card_floor_free = lambda now: True
            call.status_gap_open = lambda now, **kwargs: True
            call.spoken_for = {
                "d0": ["Actually, the north entrance stays open."],
                "d1": ["I'll check the museum hours."],
                "d2": ["Correction: the museum cafe closes at noon."],
            }
            call.earlier_cards = [("d1", {"id": "sample-1", "card": {
                "verified": True, "summary": "The museum cafe closes at six."}})]
            await call.say_earlier_cards(time.monotonic(), "d2")
            self.assertFalse(self.spoken(sent))
            self.assertEqual(call.counts["lane_cards_skipped"], 1)

            call.earlier_cards = [("d1", {"id": "sample-2", "card": {
                "verified": True, "summary": "The archive migration is running now."}})]
            await call.say_earlier_cards(time.monotonic(), "d2")
            self.assertFalse(self.spoken(sent))
            self.assertEqual(call.counts["status_claims_dropped"], 1)

            call.earlier_cards = [("d1", {"id": "sample-3", "card": {
                "verified": True, "summary": "The north entrance stays open."}})]
            await call.say_earlier_cards(time.monotonic(), "d2")
            self.assertIn("The north entrance stays open.", self.spoken(sent))
        finally:
            await call.close("test")

    async def test_a_named_card_contradicted_by_the_board_is_corrected(self):
        said, _ = await self.run_path("B", 992, AT_412, "t_47402876 hasn't started; no one picked it up.",
                                      "say more about that")
        self.assertIn("t_47402876 is running as of 23:58:59.", said)


if __name__ == "__main__":
    unittest.main()
