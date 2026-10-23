"""In-app phone control: routing, tier escalation, confirmation and spoken results.

Uses the real Store/inbox/event path and the real Delegator; only the model tiers and the
phone itself are fakes (the phone answers through Store.add_events like the APK does).
"""

import io
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
from z0live_desktop import phone_control as pc  # noqa: E402
from z0live_desktop.core import Store  # noqa: E402
from z0live_desktop.delegation import Delegator  # noqa: E402

TOOLS = [
    {"type": "function", "name": n, "description": n, "parameters": {"type": "object", "properties": {}}}
    for n in ("open_app", "phone_navigation", "phone_ui_tree", "phone_do", "phone_act")
]
CHOOSE = {
    "ok": True, "status": "choose", "package": "com.openai.chatgpt", "snapshot": "h1",
    "candidates": [
        {"id": "sb1", "l": "Open sidebar", "r": "button", "score": 0.8},
        {"id": "mo2", "l": "More options", "r": "button", "score": 0.8},
        {"id": "nc3", "l": "New chat", "r": "button", "score": 0},
    ],
    "decision": {"tier": "phone_lexical", "confidence": 0.8, "margin": 0.0, "ms": 0.4},
}


def done(label, landed=True, appeared=("Chats", "Search")):
    return {"ok": True, "status": "done", "landed": landed, "via": "node",
            "target": {"id": "x", "label": label, "role": "button"},
            "appeared": list(appeared) if landed else [], "ms": {"total": 120.0}}


class FakeCall:
    def __init__(self, sid):
        self.sid, self.call_id = sid, "fake"
        self.closed = threading.Event()
        self.appended = []

    def append(self, did, text, channel=None):
        self.appended.append((did, text, channel))
        return True


class NoRoute:
    def __init__(self):
        self.calls = []

    def route(self, did, text, tools):
        self.calls.append(text)
        return None


class FixedTier:
    def __init__(self, id_, confidence, tier="groot"):
        self.id, self.confidence, self.tier, self.calls = id_, confidence, tier, []

    def choose(self, goal, cands):
        self.calls.append((goal, [c["id"] for c in cands]))
        return {"tier": self.tier, "id": self.id, "confidence": self.confidence, "ms": 20.0}


class FakePhone(threading.Thread):
    """Answers queued inbox actions through the real event endpoint, like the APK."""

    def __init__(self, store, device, sid, answer):
        super().__init__(daemon=True)
        self.store, self.device, self.sid, self.answer = store, device, sid, answer
        self.after = max([m["id"] for m in store.inbox(device)["messages"]] or [0])
        self.seen, self.stop = [], threading.Event()

    def run(self):
        handled = set()
        while not self.stop.is_set():
            for item in self.store.inbox(self.device, self.after)["messages"]:
                if item["id"] in handled or not item.get("action"):
                    continue
                handled.add(item["id"])
                self.seen.append((item["action"], item["params"]))
                result = self.answer(item["action"], item["params"])
                self.store.add_events(self.device, [{
                    "session_id": self.sid, "type": "tool_result",
                    "data": {"id": item["id"], "name": item["action"], "success": result.get("ok", False),
                             "result": result, "event_id": "r%d" % item["id"]},
                }])
            time.sleep(0.01)


class PhoneControlTests(unittest.TestCase):
    def setUp(self):
        for p in (patch("z0live_desktop.voice_runtime.load_plan", return_value=None),
                  patch.dict(os.environ, {"Z0LIVE_LIVE_TRANSPORT": "bidi"})):
            p.start()
            self.addCleanup(p.stop)
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"))
        self.addCleanup(self.tmp.cleanup)
        self.store = Store(self.tmp.name)
        self.device = self.store.authenticate(self.store.pair("unit"))
        with patch("z0live_desktop.core.mint", side_effect=AssertionError("bidi must not mint")):
            self.sid = self.store.new_session(
                self.device, {"client": "lawnchair", "tools": TOOLS, "transports": ["bidi", "realtime"]}
            )["session_id"]

    def run_delegation(self, answer, text, tier1=None, tier2=None, did="item_1", delegator=None):
        router = NoRoute()
        delegator = delegator or Delegator(
            self.store, router=router, phone_timeout=3,
            escalator=pc.Escalator(tier1 or FixedTier(None, 0.0), tier2 or FixedTier(None, 0.0, "jev")),
        )
        self.store._live = type("L", (), {"delegator": delegator, "close": lambda s, sid: None})()
        phone = FakePhone(self.store, self.device, self.sid, answer)
        phone.start()
        self.addCleanup(phone.stop.set)
        call = FakeCall(self.sid)
        self.store.add_events(self.device, [{"session_id": self.sid, "type": "user_transcript", "text": text}])
        delegator.delegate(call, did, text)
        end = time.time() + 5
        while time.time() < end and not any(c is None for _, _, c in call.appended):
            time.sleep(0.02)
        phone.stop.set()
        phone.join(2)
        # finish() speaks first, then records: let it write before the store dir is removed.
        end = time.time() + 3
        while time.time() < end:
            trace = delegator.trace(self.sid)
            if trace and "final" in trace[0].get("marks", {}):
                break
            time.sleep(0.02)
        finals = [t for _, t, c in call.appended if c is None]
        self.assertTrue(finals, "no spoken final")
        return delegator, phone, router, finals[-1]

    def test_in_app_step_goes_straight_to_phone_do_without_a_model(self):
        _, phone, router, final = self.run_delegation(
            lambda a, p: done("Open sidebar"), "Can you click on the menu button")
        self.assertEqual(phone.seen, [("phone_do", {"goal": "Can you click on the menu button"})])
        self.assertEqual(router.calls, [], "liaison model must not run for in-app steps")
        self.assertIn("Tapped Open sidebar", final)
        self.assertIn("Chats", final)

    def test_unsure_phone_escalates_to_groot_then_acts_by_id(self):
        def answer(action, params):
            return CHOOSE if action == "phone_do" else done("More options")

        tier1 = FixedTier("mo2", 0.97)
        delegator, phone, _, final = self.run_delegation(answer, "tap the menu button", tier1=tier1)
        self.assertEqual([a for a, _ in phone.seen], ["phone_do", "phone_act"])
        self.assertEqual(phone.seen[1][1], {"id": "mo2"})
        self.assertEqual(tier1.calls[0][1][:2], ["sb1", "mo2"])
        self.assertIn("Tapped More options", final)
        end = time.time() + 3
        while time.time() < end and "final" not in delegator.trace(self.sid)[0]["marks"]:
            time.sleep(0.02)  # finish() speaks first, then records
        marks = delegator.trace(self.sid)[0]["marks"]
        self.assertEqual(marks["decided"]["tier"], "groot")
        self.assertEqual(marks["tier0"]["tier"], "phone_lexical")

    def test_low_confidence_everywhere_asks_the_owner_instead_of_guessing(self):
        tier2 = FixedTier("sb1", 0.4, "jev")
        _, phone, _, final = self.run_delegation(
            lambda a, p: CHOOSE, "click on the menu button", tier1=FixedTier("sb1", 0.5), tier2=tier2)
        self.assertEqual([a for a, _ in phone.seen], ["phone_do"], "no tap on a guess")
        self.assertEqual(len(tier2.calls), 1, "jev consulted after groot was unsure")
        self.assertIn("Open sidebar", final)
        self.assertIn("More options", final)
        self.assertTrue(final.endswith("?"))

    def test_consequential_tap_waits_for_spoken_yes(self):
        def answer(action, params):
            if action == "phone_do":
                return {"ok": False, "status": "confirm_required", "reason": "'Send prompt' can send",
                        "target": {"id": "sp9", "label": "Send prompt", "role": "button"}}
            return done("Send prompt", appeared=())

        delegator, phone, _, final = self.run_delegation(answer, "press send")
        self.assertIn("should I?", final)
        self.assertEqual([a for a, _ in phone.seen], ["phone_do"])
        _, phone2, _, final2 = self.run_delegation(answer, "yes", did="item_2", delegator=delegator)
        self.assertEqual(phone2.seen[-1], ("phone_act", {"id": "sp9", "confirm": True}))
        self.assertIn("Tapped Send prompt", final2)

    def test_no_cancels_a_pending_confirmation(self):
        def answer(action, params):
            return {"ok": False, "status": "confirm_required",
                    "target": {"id": "d1", "label": "Delete", "role": "button"}}

        delegator, _, _, _ = self.run_delegation(answer, "tap delete")
        _, phone2, _, final = self.run_delegation(answer, "no, don't", did="item_2", delegator=delegator)
        self.assertEqual(phone2.seen, [])
        self.assertIn("left it alone", final)
        self.assertIsNone(delegator.confirms.peek(self.sid))

    def test_tap_that_did_not_land_is_said_plainly(self):
        _, _, _, final = self.run_delegation(lambda a, p: done("Open sidebar", landed=False), "tap the menu")
        self.assertIn("nothing changed", final)

    def test_permission_required_tells_the_owner_the_exact_missing_step(self):
        answer = lambda a, p: {"ok": False, "status": "permission_required", "missing": "service_disabled",  # noqa: E731
                               "instruction": "Turn on z0live in Settings > Accessibility > Installed apps."}
        _, _, _, final = self.run_delegation(answer, "click the menu button")
        self.assertIn("Settings > Accessibility", final)

    def test_app_launches_and_computer_work_are_not_in_app_steps(self):
        self.assertTrue(pc.wants_in_app("Can you click on the menu button", TOOLS))
        self.assertTrue(pc.wants_in_app("open the sidebar", TOOLS))
        self.assertFalse(pc.wants_in_app("open ChatGPT", TOOLS))
        self.assertFalse(pc.wants_in_app("why is CI failing", TOOLS))
        self.assertFalse(pc.wants_in_app("tap the menu", TOOLS[:1]), "old APK without phone_do")

    def test_ui_tree_scope_requires_a_screen_observation_or_phone_operation(self):
        for request in [
            "Read my phone screen", "Please inspect the UI tree", "Can you describe the screen?",
            "What's on my screen?", "Tell me what is visible on the phone screen",
            "Which app is open?", "What app is in the foreground?", "tap the menu button",
        ]:
            with self.subTest(request=request):
                self.assertTrue(pc.screen_intent(request))
        for request in [
            "Are you connected through the sample app?", "Why is the sample app slow?",
            "The phone has a screen", "Do not read the screen", "What apps support this model?",
            "Tell Hermes to inspect the screen", "Did you send my message?",
        ]:
            with self.subTest(request=request):
                self.assertFalse(pc.screen_intent(request))

    def test_tier1_reads_grammar_letter_and_renormalises_letter_probabilities(self):
        reply = {"choices": [{"message": {"content": "B"}, "logprobs": {"content": [{"top_logprobs": [
            {"token": "The", "logprob": -0.1}, {"token": "B", "logprob": -1.0}, {"token": "A", "logprob": -3.0}]}]}}]}

        class Resp(io.BytesIO):
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        sent = []

        def opener(request, timeout):
            sent.append(json.loads(request.data))
            return Resp(json.dumps(reply).encode())

        d = pc.Tier1(url="http://unit", model="m", opener=opener).choose(
            "tap the menu", [{"id": "a", "label": "Open sidebar", "role": "button"},
                             {"id": "b", "label": "More options", "role": "button"}])
        self.assertEqual(d["id"], "b")
        self.assertAlmostEqual(d["confidence"], 0.881, places=2)
        self.assertEqual(sent[0]["grammar"], 'root ::= "A" | "B"')
        self.assertIn('"tap the menu"', sent[0]["messages"][0]["content"])

    def test_candidates_skip_static_text_and_dedupe(self):
        cands = pc.candidates_of({"candidates": CHOOSE["candidates"], "nodes": [
            {"id": "sb1", "l": "Open sidebar", "r": "button"}, {"id": "t", "l": "Title", "r": "text", "static": True},
            {"id": "z", "l": "Ask anything", "r": "edit", "edit": True}]})
        self.assertEqual([c["id"] for c in cands], ["sb1", "mo2", "nc3", "z"])

    def test_jev_tier_never_sends_sensitive_labels_and_fails_closed_without_jevkit(self):
        missing = pc.Tier2(loader=lambda: (None, None)).choose("tap", [{"id": "a", "label": "x", "role": "b"}])
        self.assertIsNone(missing["id"])
        sent = []

        class Choose:
            MIN_CONFIDENCE = 0.65

            @staticmethod
            def choose(request, timeout):
                sent.append(request)
                return {"selected_id": "a", "confidence": 0.9}

        class Privacy:
            @staticmethod
            def is_sensitive(text):
                return "4111" in text

        d = pc.Tier2(loader=lambda: (Choose, Privacy)).choose(
            "tap the menu", [{"id": "a", "label": "Open sidebar", "role": "button"},
                             {"id": "c", "label": "Card 4111 1111", "role": "text"}])
        self.assertEqual(d["id"], "a")
        self.assertNotIn("4111", json.dumps(sent))
        self.assertTrue({"reobserve", "abstain"} <= {c["id"] for c in sent[0]["candidates"]})


if __name__ == "__main__":
    unittest.main()
