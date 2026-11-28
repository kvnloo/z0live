"""TV conductor lane cards spoken on the call they were asked on: one short line per batch, only when nobody is
talking, never for a request Kevin moved on from, never what was already said. Fake state.json, fake call; no network.
Card words are made up."""
import asyncio
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")
import mobile_live as live  # noqa: E402
import lane_cards  # noqa: E402
import tiers  # noqa: E402

REQUEST = "How many boat sandboxes are live right now?"


def lane(lid, model, summary, state="done"):
    return {"id": lid, "title": "t", "model": model, "state": state, "t1": 1.0,
            "card": {"title": "c", "summary": summary, "facts": [], "unknowns": [], "sources": []} if summary else {}}


def ask(call, text, *lanes, aid="a1"):
    return {"id": aid, "call": call, "text": lane_cards.request_key(text), "state": "running", "lanes": list(lanes)}


class Pure(unittest.TestCase):
    def test_readback_does_not_swallow_new_work(self):
        self.assertFalse(lane_cards.readback_requested("Run the tests and tell me the results."))
        self.assertFalse(lane_cards.readback_requested("Don't tell me the results."))
        self.assertFalse(lane_cards.readback_requested("Just tell me the results of a new search for rice cookers."))

    def test_call_key_is_the_conductors(self):
        self.assertEqual(lane_cards.call_key("hermes-subscription-b791819ddd3f4dea968b9dd3822fc4cb"), "b791819ddd3f")

    def test_one_lane_is_named_by_its_model(self):
        self.assertEqual(lane_cards.line([lane("1", "sol 6.2", "9 boat sandboxes live, all idle. Details on the TV.")]),
                         "Sol lane: 9 boat sandboxes live, all idle.")

    def test_lanes_that_finish_together_are_one_line_capped_at_three(self):
        lanes = [lane(str(i), m, f"Fact {i} is fine.") for i, m in enumerate(["haiku 5.5", "sol 6.2", "opus 5.5", "sol 6.2"])]
        self.assertEqual(lane_cards.line(lanes), "4 lanes back. Haiku: Fact 0 is fine. Sol: Fact 1 is fine. "
                                                 "Opus: Fact 2 is fine. And 1 more on the TV.")

    def test_a_long_summary_is_cut_at_a_word(self):
        out = lane_cards.first_sentence("word " * 80)
        self.assertLessEqual(len(out), lane_cards.SUMMARY_CHARS + 1)
        self.assertTrue(out.endswith("word."))

    def test_said_already_counts_digits(self):
        self.assertTrue(lane_cards.said_already("9 boat sandboxes live, all idle.", ["There are 9 boat sandboxes live and all idle."]))
        self.assertFalse(lane_cards.said_already("9 boat sandboxes live, all idle.", ["Checking your sandboxes now."]))


class Watcher(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "state.json"
        self.path.write_text(json.dumps({"asks": []}))
        fast = dict(POLL_SECONDS=0.01, BATCH_SECONDS=0.05, QUIET_SECONDS=0.15, HOLD_SECONDS=5.0, IDLE_SECONDS=5.0,
                    HANDED_SECONDS=0.15)
        self.patches = [mock.patch.object(tiers, "CONDUCTOR_STATE", self.path)] + \
                       [mock.patch.object(lane_cards, k, v) for k, v in fast.items()]
        for p in self.patches:
            p.start()
        self.call = call = live.Call(live.Manager(None), profile="")
        call.tv_present = True
        self.key = lane_cards.call_key(call.id)
        self.sent, self.events = [], []
        call.send = lambda event, to_provider=False, **_: self.sent.append(event)
        real = call.trace

        class Trace:
            def __getattr__(_, name):
                return getattr(real, name)

            def __call__(_, ev, **kw):
                self.events.append((ev, kw))

            def close(_):
                real.close()
        call.trace = Trace()
        self.ask_for("d1", REQUEST)

    async def asyncTearDown(self):
        await self.call.close("test")
        for p in self.patches:
            p.stop()
        self.dir.cleanup()

    def ask_for(self, did, request):
        self.call.protocol.seen.append(did)
        self.call.latest_did = did
        self.call.watch_lane_cards(did, request)

    def write(self, *asks):
        self.path.write_text(json.dumps({"asks": list(asks)}))

    def spoken(self):
        return ["".join(c["text"] for c in e["content"]) for e in self.sent if e.get("channel") == "speakable"]

    def traced(self, name):
        return [kw for ev, kw in self.events if ev == name]

    async def until(self, cond, seconds=2.0):
        for _ in range(int(seconds / 0.01)):
            if cond():
                return
            await asyncio.sleep(0.01)
        self.fail(f"timed out: events={self.events} sent={self.spoken()}")

    async def test_a_card_from_this_call_is_spoken_once_for_its_request(self):
        self.write(ask(self.key, REQUEST, lane("a1-1", "sol 6.2", "9 boat sandboxes live, all idle.")))
        await self.until(lambda: self.sent)
        await asyncio.sleep(0.1)
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(self.sent[0]["delegation_item_id"], "d1")
        self.assertEqual(self.spoken(), ['"Agent Final Message":\n\nSol lane: 9 boat sandboxes live, all idle.'])
        self.assertEqual(self.traced("lanes.card_spoken")[0]["lanes"], ["a1-1"])

    async def test_lanes_finishing_together_are_one_line(self):
        self.write(ask(self.key, REQUEST, lane("a1-1", "sol 6.2", "9 boat sandboxes live."),
                       lane("a1-2", "haiku 5.5", "All of them idle since noon.")))
        await self.until(lambda: self.sent)
        await asyncio.sleep(0.1)
        self.assertEqual(self.spoken(), ['"Agent Final Message":\n\n2 lanes back. Sol: 9 boat sandboxes live. '
                                         'Haiku: All of them idle since noon.'])

    async def test_held_while_kevin_speaks_or_the_voice_answers(self):
        self.call.protocol.user_open = True
        self.write(ask(self.key, REQUEST, lane("a1-1", "sol 6.2", "9 boat sandboxes live.")))
        await asyncio.sleep(0.3)
        self.assertEqual(self.sent, [])
        self.call.protocol.user_open, self.call.protocol.assistant_open = False, True
        await asyncio.sleep(0.2)
        self.assertEqual(self.sent, [])
        self.call.protocol.assistant_open = False
        self.call.output_loud_at = live.time.monotonic()  # the voice's last words still playing
        await asyncio.sleep(0.03)
        self.assertEqual(self.sent, [])
        await self.until(lambda: self.sent)
        self.assertGreater(self.traced("lanes.card_spoken")[0]["held_s"], 0.3)

    async def test_held_right_after_an_answer_was_handed_before_its_reply_opens(self):
        self.call.speak_for("d1", "The boats are on the TV now.")  # Hermes's answer; no turn.created yet
        self.sent.clear()
        self.write(ask(self.key, REQUEST, lane("a1-1", "sol 6.2", "9 boat sandboxes live.")))
        await asyncio.sleep(0.08)
        self.assertEqual(self.sent, [])
        await self.until(lambda: self.sent)

    async def test_the_protocol_tracks_the_voices_open_reply(self):
        p = self.call.protocol
        p.from_provider({"type": "turn.created", "turn": {"role": "assistant", "id": "a1"}})
        self.assertTrue(p.assistant_open)
        p.from_provider({"type": "turn.done", "turn": {"role": "assistant", "id": "a1", "transcript": "Okay."}})
        self.assertFalse(p.assistant_open)
        p.from_provider({"type": "turn.created", "turn": {"role": "assistant", "id": "a2"}})  # cut: no turn.done
        p.from_provider({"type": "turn.created", "turn": {"role": "user", "id": "u2"}})
        self.assertFalse(p.assistant_open)

    async def test_stale_cards_are_deltas_and_overflow_waits_for_a_new_turn_gap(self):
        self.ask_for("d2", "And what's the weather?")
        self.write(ask(self.key, REQUEST, lane("a1-1", "sol 6.2", "9 boat sandboxes live."),
                       lane("a1-2", "opus", "All workers are idle."), lane("a1-3", "haiku", "Owners match.")))
        with mock.patch.object(live, "STATUS_GAP", 0):
            await self.until(lambda: self.sent)
            await asyncio.sleep(0.2)
            self.assertEqual([x["lanes"] for x in self.traced("lanes.card_earlier_spoken")], [["a1-1", "a1-2"]])
            self.final("Okay, what else?", "u1")
            await self.until(lambda: len(self.traced("lanes.card_earlier_spoken")) == 2)
        self.assertEqual(self.traced("lanes.card_earlier_spoken")[1]["lanes"], ["a1-3"])
        self.assertFalse(self.traced("lanes.card_skipped"))
        self.assertIn("On your earlier request", json.dumps(self.sent))
        self.assertIn("9 boat sandboxes live", json.dumps(self.sent))

    async def test_never_repeats_what_a_tier_lane_said(self):
        self.call.speak_for("d1", "There are 9 boat sandboxes live, all idle.", lane=True)
        self.sent.clear()
        self.write(ask(self.key, REQUEST, lane("a1-1", "sol 6.2", "9 boat sandboxes live, all idle."),
                       lane("a1-2", "haiku 5.5", "The oldest one is three days old.")))
        await self.until(lambda: self.sent)
        await asyncio.sleep(0.1)
        self.assertEqual(self.spoken(), ['"Agent Final Message":\n\nHaiku lane: The oldest one is three days old.'])
        self.assertEqual([k["reason"] for k in self.traced("lanes.card_skipped")], ["already_said"])

    async def test_other_calls_and_old_cards_stay_quiet_and_failures_are_traced(self):
        self.write(ask("someothercal", REQUEST, lane("x-1", "sol 6.2", "Not this call.")),
                   ask(self.key, REQUEST, lane("a1-1", "sol 6.2", None, state="failed"), aid="a2"),
                   ask(self.key, "something nobody asked here", lane("a3-1", "sol 6.2", "Unmapped."), aid="a3"))
        await self.until(lambda: len(self.traced("lanes.card_skipped")) == 2)
        await asyncio.sleep(0.1)
        self.assertEqual(self.sent, [])
        self.assertEqual(sorted(k["reason"] for k in self.traced("lanes.card_skipped")), ["failed", "unmapped"])

    async def test_the_watcher_stops_when_idle_and_a_new_request_restarts_it(self):
        with mock.patch.object(lane_cards, "IDLE_SECONDS", 0.05):
            await self.until(lambda: self.call.card_watch is None)
            self.ask_for("d2", "Next one")
            self.assertIsNotNone(self.call.card_watch)

    def final(self, text, tid):
        self.call.message(json.dumps({"type": "turn.done", "turn": {
            "id": tid, "role": "user", "transcript": text}}), True)

    async def test_nov27_reads_back_two_stale_deltas_per_turn_not_a_new_delegation(self):
        self.ask_for("d2", "And what's the weather?")
        self.call.protocol.user_open = True
        self.write(ask(self.key, REQUEST, lane("l1", "sol", "The workers are idle."),
                       lane("l2", "opus", "The build has no warnings."),
                       lane("l3", "haiku", "The invoice matched the receipt.")),
                   ask("another-call", REQUEST, lane("x", "sol", "Unrelated result.")))
        with mock.patch.object(self.call, "ask_hermes", new_callable=mock.AsyncMock) as driver:
            self.call.message(json.dumps({"type": "delegation.created", "item": {"id": "late1", "target": "client",
                "content": [{"type": "text", "text": "Summarize the completed lane findings"}]}}), True)
            self.final("Oh, bro, I'm not at my TV and the TV is off. Can you just tell me the results", "u1")
            await self.until(lambda: len(self.traced("lanes.readback")) == 1)
            self.assertEqual(self.traced("lanes.readback")[0]["did"], "late1")
            self.call.observe_tv_presence({"present": True, "app_age_s": 0.1})
            self.assertTrue(self.call.speak_mode)
            await asyncio.sleep(0.2)
            self.assertEqual(self.traced("lanes.readback")[0]["lanes"], ["l1", "l2"])
            self.assertIn("On your earlier", " ".join(self.spoken()))
            self.assertNotIn("invoice", " ".join(self.spoken()))
            self.assertNotIn("on the TV", " ".join(self.spoken()))
            self.final("Bro, I'm not at the TV. Just tell me the results", "u2")
            await self.until(lambda: len(self.traced("lanes.readback")) == 2)
            self.assertEqual(self.traced("lanes.readback")[1]["lanes"], ["l3"])
            self.assertIn("invoice matched", " ".join(self.spoken()))
            self.assertNotIn("Unrelated", " ".join(self.spoken()))
            self.assertEqual(self.call.protocol.asked, [])
            driver.assert_not_called()
        self.assertFalse(self.traced("delegation.start"))

    async def test_stale_heartbeat_reads_all_fresh_cards_as_sentences(self):
        self.call.observe_tv_presence({"present": False, "app_age_s": 60, "why": "tv app silent"})
        self.write(ask(self.key, REQUEST, lane("l1", "sol", "Amber was selected; details are on the TV."),
                       lane("l2", "opus", "Bronze was rejected."), lane("l3", "haiku", "Copper is optional."),
                       lane("l4", "sol", "Delta is next.")))
        await self.until(lambda: self.traced("lanes.card_spoken"))
        self.assertEqual(self.traced("lanes.card_spoken")[0]["lanes"], ["l1", "l2", "l3", "l4"])
        spoken = " ".join(self.spoken())
        self.assertIn("Delta is next.", spoken)
        self.assertNotIn("on the TV", spoken)


if __name__ == "__main__":
    unittest.main()
