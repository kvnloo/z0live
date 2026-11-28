"""F2: status.answer answers exactly the asked status question or yields; background owns asks it has delegated; only
verified cards speak facts."""
import asyncio
import unittest

import live_status as ls
import mobile_live as live


def make_call():
    call = live.Call(live.Manager(None), profile="tek")
    call.live_records = True
    return call


class ExactTopics(unittest.TestCase):
    def test_oss_repo_rundown_is_not_a_lane_count(self):
        for q in ("Give me a rundown of the OSS repos, what lanes are on them?",
                  "What's the status of the open source repo lanes?",
                  "Can you summarize what the lanes did?"):
            self.assertEqual(ls.exact_topics(q), [], q)

    def test_real_status_questions_still_answer(self):
        self.assertEqual(ls.exact_topics("How many lanes are running?"), ["lanes"])
        self.assertIn("cards", ls.exact_topics("Is t_64e4d398 picked up?"))
        self.assertEqual(ls.exact_topics("What's the latency?"), ["latency"])


class Authority(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.call = make_call()
        self.reads = []

        async def read(topics, request=""):
            self.reads.append(topics)
            return {"read_at": 1795844962.0, "topics": sorted(topics), "ids": set(), "state": {}, "errors": {}}
        self.call.status_read = read

    async def asyncTearDown(self):
        await self.call.close("test")

    async def test_subject_question_yields(self):
        self.assertFalse(await self.call.status_answer("d1", "Give me the OSS repo rundown, how many lanes?"))
        self.assertEqual(self.reads, [])

    async def test_background_active_or_completed_owns_ask(self):
        q = "How many lanes are running?"
        for state in ("active", "completed"):
            self.call.background_asks = {live.lane_cards.request_key(q): state}
            self.assertFalse(await self.call.status_answer("d1", q), state)
        self.assertEqual(self.reads, [])

    async def test_earlier_lane_delegation_of_same_ask_owns_it(self):
        q = "How many lanes are running?"
        self.call.ask_dids[live.lane_cards.request_key(q)] = "d0"
        self.assertFalse(await self.call.status_answer("d1", q))

    async def test_unowned_status_question_answers(self):
        self.call.ask_dids.clear()
        self.assertTrue(await self.call.status_answer("d1", "How many lanes are running?"))
        self.assertEqual(len(self.reads), 1)


class VerifiedCards(unittest.IsolatedAsyncioTestCase):
    LANE = {"id": "l1", "model": "claude-haiku-4-5", "state": "done", "card": {"summary": "Fixed it."}}

    async def test_unverified_card_silent_when_required(self):
        call = make_call()
        call.require_verified_cards = True
        try:
            self.assertEqual(await call.card_text([self.LANE]), "")
            lane = dict(self.LANE, card={"summary": "Notes only.", "verified": True})
            self.assertIn("Notes only", await call.card_text([lane]))
        finally:
            await call.close("test")

    def test_card_verified_flag(self):
        self.assertFalse(live.Call.card_verified(self.LANE))
        self.assertTrue(live.Call.card_verified({"card": {"verified": True}}))


if __name__ == "__main__":
    unittest.main()
