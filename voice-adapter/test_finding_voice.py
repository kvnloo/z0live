import asyncio
import json
import tempfile
import types
import unittest
from collections import deque
from pathlib import Path
from unittest import mock

import finding_voice as fv

CHOICES = ["read-back first", "warm session first", "both, read-back today"]
SPEC = {"slug": "call-failures",
        "numbers": [{"key": "read", "label": "finished cards read back to you", "value": 9, "unit": "%"},
                    {"key": "wait", "label": "wait for a verified answer (median)", "value": 25.8}],
        "chart": {"bars": [{"key": "F6", "label": "Cards never read back"}, {"key": "F3", "label": "Action asks not done"},
                           {"key": "F1", "label": "Verified answer slow"}]}}
PAGE = {"ok": True, "slug": "call-failures", "mtime": 1.0, "say": "Calls fail at delivery. Which first? A: ... B: ...",
        "ask": {"question": "Fix first?", "choices": CHOICES}}


class ParseTest(unittest.TestCase):
    def test_letters(self):
        for text, want in [("B", "b"), ("C.", "c"), ("option a", "a"), ("Let's do B.", "b"), ("Um, see.", "c"),
                           ("I'd say C", "c"), ("B. The warm session.", "b"), ("go with option b please", "b"),
                           ("Can we do B?", "b")]:
            self.assertEqual(fv.parse_answer(text, CHOICES)[0], want, text)

    def test_article_a_is_not_a_letter(self):
        letter, choice, kind = fv.parse_answer("a warm session for each project sounds right", CHOICES)
        self.assertEqual((letter, kind), ("b", "words"))

    def test_words_and_free_and_not_yet(self):
        self.assertEqual(fv.parse_answer("warm session first", CHOICES)[:2], ("b", "warm session first"))
        self.assertEqual(fv.parse_answer("neither, fix the dropped asks before anything else", CHOICES)[2], "free")
        self.assertEqual(fv.parse_answer("what does F3 mean?", CHOICES)[2], None)
        self.assertEqual(fv.parse_answer("okay", CHOICES)[2], None)
        self.assertEqual(fv.parse_answer("mm-hm", CHOICES)[2], None)

    def test_letter_out_of_range(self):
        self.assertIsNone(fv.parse_answer("D", CHOICES)[0])


class ItemTest(unittest.TestCase):
    def test_keys_labels_cards(self):
        ms = fv.matchers(SPEC)
        self.assertEqual(fv.last_item("look at F3 there", ms), "F3")
        self.assertEqual(fv.last_item("F six is big, but f-3 hurts most", ms), "F3")
        self.assertEqual(fv.last_item("action asks not done is the worst", ms), "F3")
        self.assertEqual(fv.last_item("only 9% of finished cards read back to you", ms), "read")
        self.assertEqual(fv.last_item("the root cause is the stale check", ms), "cause")
        self.assertEqual(fv.last_item("and the next step is read-back", ms), "next")
        self.assertIsNone(fv.last_item("F30 and F13 are not bars, nor is fifty", ms))

    def test_ring_dedupes_and_paces(self):
        p = fv.Page()
        self.assertTrue(p.ring("s", "F3", 10))
        self.assertFalse(p.ring("s", "F3", 20))
        self.assertFalse(p.ring("s", "F1", 10.5))
        self.assertTrue(p.ring("s", "F1", 12))


class PageTest(unittest.TestCase):
    def test_ask_once_then_decision(self):
        p = fv.Page()
        self.assertTrue(p.want_ask(PAGE))
        p.mark_asked(PAGE, 100)
        self.assertFalse(p.want_ask(PAGE))
        self.assertTrue(p.want_ask({**PAGE, "mtime": 2.0}))  # a rebuilt page is a new ask
        self.assertIsNone(p.answer("what's F3?", 101))
        d = p.answer("B", 102)
        self.assertEqual((d["letter"], d["choice"], d["slug"]), ("B", "warm session first", "call-failures"))
        self.assertIsNone(p.answer("C", 103))  # one decision per ask

    def test_answer_window(self):
        p = fv.Page()
        p.mark_asked(PAGE, 0)
        self.assertIsNone(p.answer("B", fv.ANSWER_S + 1))


class FakeHTTP:
    def __init__(self, routes):
        self.routes = routes

    def get(self, url, timeout=None):
        body = next((v for k, v in self.routes.items() if url.endswith(k)), None)
        test = self

        class R:
            status = 200 if body is not None else 404

            async def json(self):
                return body

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False
        del test
        return R()


def fake_call(routes):
    traces = []
    proto = types.SimpleNamespace(exchanges=deque(maxlen=8), turn_text={"user": "", "assistant": ""})
    proto.from_mobile = lambda e: [{"type": "x", "content": e["content"]}]
    sent = []
    call = types.SimpleNamespace(
        id="call-abc", ledger_call="abc", closed=False, leg=None, provider_ready=True,
        provider_dc=types.SimpleNamespace(readyState="open"), protocol=proto,
        manager=types.SimpleNamespace(http=FakeHTTP(routes)), floor_busy=lambda: False,
        stamp_status=lambda now: None, send=lambda e, to_provider=False: sent.append(e),
        trace=lambda ev, **kw: traces.append((ev, kw)))
    return call, traces, sent


class StepTest(unittest.TestCase):
    def test_full_flow(self):
        with tempfile.TemporaryDirectory() as d:
            d = Path(d)
            (d / "call-failures.json").write_text(json.dumps(SPEC))
            with mock.patch.object(fv, "FINDINGS", d), mock.patch.object(fv, "INBOX", d / "inbox.jsonl"), \
                    mock.patch.object(fv, "DECISIONS", d / "decisions.jsonl"), mock.patch.object(fv, "HL_GAP_S", 0):
                routes = {"/api/now": {"show": {"view": "finding", "args": {"slug": "call-failures"}}},
                          "/api/finding?slug=call-failures": PAGE}
                call, traces, sent = fake_call(routes)
                st, seen, specs = fv.Page(), set(), {}
                run = lambda: asyncio.run(fv.step(call, st, seen, specs))  # noqa: E731
                run()
                self.assertEqual(traces, [], "never first: no exchange yet")
                call.protocol.exchanges.append(("user", "show me the finding"))
                run()
                evs = [e for e, _ in traces]
                self.assertEqual(evs, ["finding.ask_spoken", "finding.hl"])
                self.assertEqual(sent[0]["content"], PAGE["say"])
                run()  # asked once per page per call
                self.assertEqual(len(sent), 1)
                call.protocol.turn_text["assistant"] = "the worst one is F3, action asks not done"
                run()
                self.assertEqual([e for e, _ in traces], evs, "the voice's own ask reading never rings")
                call.protocol.exchanges.append(("user", "why is F1 slow?"))
                call.protocol.turn_text["assistant"] = ""
                run()
                self.assertEqual(traces[-1], ("finding.hl", {"slug": "call-failures", "key": "F1", "by": "kevin",
                                                              "words": "why is F1 slow?"}))
                call.protocol.turn_text["assistant"] = "F1 is slow because each answer starts a fresh session"
                run()
                self.assertEqual(len([t for t in traces if t[0] == "finding.hl"]), 2, "same key: no second ring")
                call.protocol.turn_text["assistant"] = "and the root cause is the stale check"
                run()
                self.assertEqual(traces[-1][1]["key"], "cause")
                call.protocol.turn_text["assistant"] = ""
                call.protocol.exchanges.append(("user", "Let's do B."))
                run()
                dec = [kw for e, kw in traces if e == "finding.decision"][0]
                self.assertEqual((dec["letter"], dec["choice"]), ("B", "warm session first"))
                rows = [json.loads(x) for x in (d / "decisions.jsonl").read_text().splitlines()]
                self.assertEqual((rows[0]["call"], rows[0]["letter"]), ("abc", "B"))
                hl = [json.loads(x)["args"]["hl"] for x in (d / "inbox.jsonl").read_text().splitlines()]
                self.assertEqual(hl, ["ask", "F1", "cause", "ask"])

    def test_other_view_or_down_tv_does_nothing(self):
        call, traces, sent = fake_call({"/api/now": {"show": {"view": "needs"}}})
        call.protocol.exchanges.append(("user", "hi"))
        asyncio.run(fv.step(call, fv.Page(), set(), {}))
        call2, traces2, _ = fake_call({})
        call2.protocol.exchanges.append(("user", "hi"))
        asyncio.run(fv.step(call2, fv.Page(), set(), {}))
        self.assertEqual((traces, sent, traces2), ([], [], []))


if __name__ == "__main__":
    unittest.main()
