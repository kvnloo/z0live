import asyncio
import tempfile
import types
import unittest
from pathlib import Path

import needs_voice as nv

ROWS = [{"id": "t_a", "dups": ["t_a2"], "say": "add command_allowlist line to config.yaml, needs a config change"},
        {"id": "t_b", "dups": [], "say": "earlyoom unmask needs sudo, needs your sudo"},
        {"id": "t_c", "dups": [], "say": "OK to post the agentsview PR, needs a yes or no"}]
NEW = ROWS + [{"id": "t_d", "dups": [], "say": "grant Full Disk Access, needs a yes or no"}]


def payload(present, rows=ROWS, why="marked away"):
    return {"tv": {"present": present, "why": "" if present else why}, "rows": rows}


class WatchTest(unittest.TestCase):
    def test_tv_present_says_nothing_and_counts_as_seen(self):
        w = nv.Watch()
        self.assertIsNone(w.step(payload(True), now=100))
        self.assertEqual(set(w.memory), {"t_a", "t_a2", "t_b", "t_c"})
        self.assertIsNone(w.step(payload(False), now=200))  # seen on the TV a moment ago: nothing to say
        text, ids = w.step(payload(False, NEW), now=300)
        self.assertEqual(ids, {"t_d"})
        self.assertTrue(text.startswith("While you're away from the TV: 4 cards need you. Top: grant Full Disk Access"))

    def test_away_summary_once_then_only_new_cards(self):
        w = nv.Watch()
        text, ids = w.step(payload(False), now=100)
        self.assertTrue(text.startswith("While you're away from the TV: 3 cards need you."))
        self.assertIn("add command_allowlist line to config.yaml", text)
        self.assertNotIn("t_a", text)  # ids are for the screen, not the voice
        w.said(ids, now=100)
        self.assertIsNone(w.step(payload(False), now=120))
        text, ids = w.step(payload(False, NEW), now=140)
        self.assertEqual(text, "New blocker for you: grant Full Disk Access, needs a yes or no.")
        self.assertEqual(ids, {"t_d"})

    def test_unsaid_line_is_offered_again(self):
        w = nv.Watch()
        self.assertIsNotNone(w.step(payload(False), now=1))
        self.assertIsNotNone(w.step(payload(False), now=2))  # said() never ran: the summary is still owed

    def test_heard_cards_stay_quiet_across_calls_until_remember_window_ends(self):
        w = nv.Watch()
        w.said(w.step(payload(False), now=0)[1], now=0)
        nxt = nv.Watch(dict(w.memory))  # the next call loads the same memory
        self.assertIsNone(nxt.step(payload(False), now=3600))
        self.assertIsNotNone(nxt.step(payload(False), now=nv.REMEMBER_S + 1))

    def test_no_rows_no_line(self):
        self.assertIsNone(nv.Watch().step(payload(False, []), now=1))

    def test_memory_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "m.json"
            self.assertEqual(nv.load_memory(p), {})
            nv.save_memory({"t_a": 100.0, "t_old": 1.0}, p, now=200.0 + 7 * 86400 - 150)
            self.assertEqual(nv.load_memory(p), {"t_a": 100.0})  # older than a week is dropped


class FakeDC:
    readyState = "open"


class FakeProtocol:
    def __init__(self):
        self.exchanges = []

    def from_mobile(self, ev):
        return [ev]


class FakeCall:
    id = "c1"
    leg = None
    provider_ready = True
    provider_dc = FakeDC()

    def __init__(self, budget):
        self.closed, self.sent, self.traces, self.budget = False, [], [], list(budget)
        self.protocol = FakeProtocol()
        self.manager = types.SimpleNamespace(http=None)

    def take_status(self, source):
        return self.budget.pop(0) if self.budget else False

    def send(self, ev, to_provider=False):
        self.sent.append(ev)

    def trace(self, event, **data):
        self.traces.append((event, data))
        self.closed = True


class RunTest(unittest.TestCase):
    def test_run_waits_for_first_exchange_then_speaks_through_the_status_budget(self):
        call = FakeCall([False, True])
        polls = []

        async def fetch(_http):
            polls.append(1)
            if len(polls) == 1:
                call.protocol.exchanges.append(("user", "hi"))
            return payload(False)
        with tempfile.TemporaryDirectory() as d:
            saved = (nv.fetch, nv.POLL_S, nv.MEMORY)
            nv.fetch, nv.POLL_S, nv.MEMORY = fetch, 0, Path(d) / "m.json"
            call.protocol.exchanges.append(("user", "hi"))
            try:
                asyncio.run(nv.run(call))
            finally:
                nv.fetch, nv.POLL_S, mem = saved[0], saved[1], nv.MEMORY
                nv.MEMORY = saved[2]
            self.assertEqual(set(nv.load_memory(mem)), {"t_a", "t_a2", "t_b", "t_c"})
        self.assertEqual(len(polls), 2)  # first poll: floor busy, nothing said; second: said
        self.assertEqual(call.sent[0]["type"], "session.commentary.append")
        self.assertIn("3 cards need you", call.sent[0]["content"])
        self.assertEqual(call.traces[0][0], "needs.spoken")

    def test_run_is_silent_before_the_first_exchange(self):
        call = FakeCall([True])
        polls = []

        async def fetch(_http):
            polls.append(1)
            return payload(False)

        async def go():
            task = asyncio.create_task(nv.run(call))
            await asyncio.sleep(0.05)
            call.closed = True
            await task
        saved = (nv.fetch, nv.POLL_S)
        nv.fetch, nv.POLL_S = fetch, 0.001
        try:
            asyncio.run(go())
        finally:
            nv.fetch, nv.POLL_S = saved
        self.assertEqual((polls, call.sent), ([], []))


if __name__ == "__main__":
    unittest.main()
