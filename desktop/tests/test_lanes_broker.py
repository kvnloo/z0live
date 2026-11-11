"""z0live.lanes.v1 on the broker: config + hot reload, blackboard, fan-out splice, text/voice config."""

import copy
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop import lanes, lanes_intent, lanes_v1  # noqa: E402
from z0live_desktop.blackboard import Blackboard, Stale  # noqa: E402
from z0live_desktop.core import Store  # noqa: E402
from z0live_desktop.lane_fanout import Fanout  # noqa: E402

CARD_SHA = "685fc02f4755f42bc5fbc06c347d955d5a157ce91ace20b8f56153d4a95e5214"  # aodl-views af65518 lanes.py (v1.1)
PLAN = {"meta_role": "plan", "roles": {"smol": {"provider": "anthropic", "model": "claude-haiku-4-5"},
                                       "plan": {"provider": "anthropic", "model": "claude-opus-5-5"},
                                       "slow": {"provider": "anthropic", "model": "claude-opus-5-5"},
                                       "task": {"provider": "openai-codex", "model": "gpt-6.1-sol"}}}


class FakeModels:
    """Scripted lane replies by model name: (delay_s, text) or an exception."""

    plan = PLAN

    def __init__(self, replies):
        self.replies, self.calls = replies, []

    def complete(self, binding, prompt, turn="lane", timeout=20, schema=None):
        b = lanes.resolve(binding, PLAN)
        self.calls.append((b["model"], prompt))
        delay, reply = self.replies.get(b["model"], (0, RuntimeError("down")))
        if isinstance(reply, dict):  # prompt-prefix -> reply (one model, several lane jobs)
            reply = next((v for k, v in reply.items() if prompt.startswith(k)), RuntimeError("no script"))
        time.sleep(delay)
        if isinstance(reply, Exception):
            raise reply
        return {"text": reply, "model": b["model"], "ms": int(delay * 1000)}


class Base(unittest.TestCase):
    def setUp(self):
        selected = patch("z0live_desktop.voice_runtime.load_plan", return_value=None)
        selected.start()
        self.addCleanup(selected.stop)
        minted = patch("z0live_desktop.core.mint", return_value={"value": "unit", "expires_at": 1})
        minted.start()  # never mint a real provider secret from a test
        self.addCleanup(minted.stop)
        # Lanes keep verifying on pool threads after on_final returns; a late sqlite write must not fail cleanup.
        self.tmp = tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR"), ignore_cleanup_errors=True)
        self.addCleanup(self.tmp.cleanup)
        self.cfg_path = Path(self.tmp.name) / "z0live-lanes.json"
        env = patch.dict(os.environ, {"Z0LIVE_LANES": str(self.cfg_path)})
        env.start()
        self.addCleanup(env.stop)
        self.store = Store(Path(self.tmp.name) / "broker")
        self.device = self.store.authenticate(self.store.pair("unit"))
        self.sid = self.store.new_session(self.device, {"client": "web", "context": {}, "tools": []})["session_id"]


class ConfigTests(Base):
    def test_card_validator_is_verbatim(self):
        self.assertEqual(hashlib.sha256(Path(lanes_v1.__file__).read_bytes()).hexdigest(), CARD_SHA)

    def test_presets_valid_default_is_d_and_b_is_the_spoken_example(self):
        for name, cfg in lanes.PRESETS.items():
            self.assertEqual(lanes.validate(cfg), [], name)
        self.assertIs(lanes.DEFAULT, lanes.L)  # L2.1: D plus self-labelled kinds, chat-only speak, board-only gather
        self.assertEqual((lanes.L["splice"]["speak_kinds"], lanes.L["lanes"]["smol"]["gather"]["speak"]), (["chat"], []))
        d = lanes.D
        self.assertEqual(d["lanes"]["smol"]["trigger"]["on"], "partials")
        self.assertEqual((d["lanes"]["driver"]["start"], d["lanes"]["driver"]["parallel"]), ("endpoint", True))
        self.assertEqual((d["splice"]["gate"], d["splice"]["gate_kinds"]), ("pre_speak", ["math", "hard", "gather"]))
        self.assertEqual((d["lanes"]["smol"]["router"], d["lanes"]["council"]["enabled"]), ("qwen3-1.7b", False))
        self.assertEqual(lanes_intent.parse(lanes.EXAMPLE, lanes.B)["lanes"], lanes.B["lanes"])
        self.assertEqual(lanes_intent.counts(lanes.B), {"fast": 2, "small": 2, "large": 2, "council": 1})
        self.assertEqual(lanes_intent.counts(lanes.A), {"fast": 0, "small": 1, "large": 1, "council": 0})
        self.assertEqual(lanes.preset("preset b")["revision"], "preset-B-2f-2s-2l-1c")
        self.assertEqual(lanes_intent.parse("use preset A")["revision"], "preset-A-0f-1s-1l-0c")
        self.assertIsNone(lanes.preset("2 fast"))
        spoken = lanes_intent.parse("start live with 2 fast, 1 large, no council")
        self.assertEqual(lanes_intent.counts(spoken), {"fast": 2, "small": 1, "large": 1, "council": 0})
        self.assertEqual(spoken["splice"]["gate"], "pre_speak")  # counts apply to preset D
        one = lanes_intent.parse("one fast lane")
        self.assertEqual(one["lanes"]["smol"]["router"], "qwen3-0.6b-q8")  # router follows the fast tier

    def test_privacy_classes_and_binding_filter(self):
        cls = lanes.privacy_class
        self.assertEqual([cls("what's the capital of france"), cls("did my build pass"),
                          cls("how far is the pharmacy from my house"), cls("read me my wifi password")],
                         ["public", "personal", "location", "credentials"])
        self.assertEqual(cls("capital of france", "location"), "location")  # caller floor wins
        free = {"provider": "free", "model": "vllm-sr/free", "route": "vllm-sr", "billing": "free"}
        local = {"provider": "local", "model": "qwen3.5-4b", "route": "local", "billing": "free"}
        role = {"provider": "voice_plan", "model": "smol", "route": "voice_plan"}
        gpt_role = {"provider": "voice_plan", "model": "task", "route": "voice_plan"}
        self.assertEqual([lanes.allowed(b, "public", PLAN) for b in (free, local, role)], [True] * 3)
        self.assertEqual([lanes.allowed(b, "personal", PLAN) for b in (free, local, role, gpt_role)],
                         [False, True, True, False])
        for c in ("location", "credentials"):
            self.assertEqual([lanes.allowed(b, c, PLAN) for b in (free, local, role)], [False, True, False])

    def test_edits_rebind_one_tier_and_counts_reshape(self):
        cfg = lanes_intent.parse("make the large lanes Opus and Sonnet")
        self.assertEqual([b["model"] for b in lanes.large(cfg)], ["claude-opus-5-5", "claude-sonnet-5"])
        self.assertEqual(lanes.tier(cfg, "fast"), lanes.tier(lanes.DEFAULT, "fast"))
        cfg = lanes_intent.parse("gimme one fast lane, three small, 1 large and no council", cfg)
        self.assertEqual(lanes_intent.counts(cfg), {"fast": 1, "small": 3, "large": 1, "council": 0})
        self.assertEqual(lanes.validate(cfg), [])
        with self.assertRaises(ValueError):
            lanes_intent.parse("gimme 6 fast lanes")  # pool has 3
        with self.assertRaises(ValueError):
            lanes_intent.parse("what's the weather")

    def test_roles_resolve_from_voice_plan_and_gpt_roles_are_refused(self):
        self.assertEqual(lanes.resolve({"provider": "voice_plan", "model": "smol", "route": "voice_plan"}, PLAN)["model"],
                         "claude-haiku-4-5")
        with self.assertRaisesRegex(ValueError, "fx/pi"):
            lanes.resolve({"provider": "voice_plan", "model": "task", "route": "voice_plan"}, PLAN)
        bad = json.loads(json.dumps(lanes.DEFAULT))
        bad["lanes"]["driver"]["verifiers"] = [{"provider": "openai", "model": "gpt-6.1-sol", "route": "hermes"}]
        self.assertTrue(lanes.validate(bad))

    def test_hot_reload_keeps_last_good_on_bad_edit(self):
        live = lanes.Lanes()
        self.assertEqual(live.get()["revision"], lanes.DEFAULT["revision"])
        self.assertEqual(live.status()["source"], "default")
        lanes_intent.apply("2 fast, 1 small, 1 large, no council")
        self.assertEqual(lanes_intent.counts(live.get()), {"fast": 2, "small": 1, "large": 1, "council": 0})
        good = live.get()["revision"]
        self.cfg_path.write_text('{"schema": "z0live.lanes.v1", "lanes": {}}')
        self.assertEqual(live.get()["revision"], good)
        self.assertTrue(live.status()["issues"])


class BlackboardTests(Base):
    def test_versions_roles_and_epochs(self):
        board = Blackboard(self.store)
        e = board.write(self.sid, "small:qwen", "small", "Paris.", 0.6, 0)
        with self.assertRaises(PermissionError):
            board.verify(e, "small:qwen", "small")
        with self.assertRaises(PermissionError):
            board.write(self.sid, "small:qwen", "small", "x", 1, 0, status="verified")
        new = board.correct(e, "large:opus", "large", "Paris, France.", 0.9)
        view = board.read(self.sid)
        self.assertEqual([(x["id"], x["status"], x["supersedes"]) for x in view["entries"]], [(new, "corrected", e)])
        self.assertEqual(board.entry(e)["superseded_by"], new)
        self.assertFalse(board.verify(e, "large:opus", "large"))  # superseded, not current
        self.assertEqual(board.bump(self.sid, "barge_in"), 1)
        self.assertEqual(board.read(self.sid)["entries"], [])
        with self.assertRaises(Stale):
            board.write(self.sid, "small:qwen", "small", "late", 0.5, 0)


class FanoutTests(Base):
    def fanout(self, replies, spoken, preset="B"):
        models = FakeModels(replies)
        out = Fanout(self.store, models=models, publish=lambda sid, say, src: spoken.append((say, src)))
        out.calls[self.sid] = copy.deepcopy(lanes.PRESETS[preset])
        return out, models

    def test_race_writes_answer_and_intent_then_large_corrects_once(self):
        spoken = []
        out, models = self.fanout({
            "qwen3-0.6b-q8": (0, '{"route":"answer","intent":"capital of australia"}'),
            "qwen3-1.7b": (0.3, '{"route":"answer","intent":"x"}'),
            "claude-haiku-4-5": (0.05, '{"route":"answer","answer":"Sydney.","confidence":0.6}'),
            "qwen3.5-4b": (0.5, '{"route":"meta","answer":""}'),
            "claude-opus-5-5": (0.05, '{"verdict":"correct","answer":"Canberra is the capital.","confidence":0.95}'),
            "claude-sonnet-5": (0.2, '{"verdict":"correct","answer":"Canberra.","confidence":0.9}'),
        }, spoken)
        receipt = out.on_final(self.sid, "what's the capital of australia")
        self.assertEqual(receipt["answer"], "Sydney.")
        self.assertEqual(out.board.entry(receipt["entry"])["turn"], "what's the capital of australia")
        first = out.read(self.sid)  # the voice hears the draft
        self.assertEqual([(e["role"], e["kind"], e["status"]) for e in first["entries"]],
                         [("fast", "intent", "draft"), ("small", "answer", "draft")])
        receipt["verify"].result(timeout=5)
        view = out.read(self.sid)["entries"]
        self.assertEqual(view[-1]["status"], "corrected")
        self.assertEqual(view[-1]["text"], "Canberra is the capital.")
        self.assertEqual(spoken, [("Actually, Canberra is the capital.", f"lane-correction:{view[-1]['id']}")])
        # second turn: the one-correction budget is spent, nothing more is spoken
        receipt = out.on_final(self.sid, "and of new zealand")
        receipt["verify"].result(timeout=5)
        out.read(self.sid)
        self.assertEqual(len(spoken), 1)

    def test_agreeing_verifier_marks_verified_and_meta_is_no_answer(self):
        out, _ = self.fanout({"claude-haiku-4-5": (0, '{"route":"answer","answer":"Hi Kevin."}'),
                              "claude-opus-5-5": (0, '{"verdict":"agree","answer":""}'),
                              "claude-sonnet-5": (0, '{"verdict":"agree","answer":""}')}, [])
        receipt = out.on_final(self.sid, "hey")
        receipt["verify"].result(timeout=5)
        self.assertEqual(out.read(self.sid)["entries"][-1]["status"], "verified")
        out, _ = self.fanout({"claude-haiku-4-5": (0, '{"route":"meta","answer":""}')}, [])
        self.assertIsNone(out.on_final(self.sid, "did my build pass")["answer"])

    def test_barge_in_epoch_drops_a_late_lane(self):
        out, _ = self.fanout({"claude-haiku-4-5": (0.4, '{"route":"answer","answer":"Late."}')}, [])
        result = {}
        t = threading.Thread(target=lambda: result.update(out.on_final(self.sid, "tell me a joke")))
        t.start()
        time.sleep(0.1)
        out.cancel(self.sid, "barge_in")
        t.join(5)
        self.assertEqual(result.get("dropped"), "stale epoch")
        self.assertEqual(out.read(self.sid)["entries"], [])

    def test_deadline_bounds_the_race(self):
        out, _ = self.fanout({"claude-haiku-4-5": (2.0, '{"route":"answer","answer":"Slow."}')}, [])
        start = time.monotonic()
        self.assertIsNone(out.on_final(self.sid, "hello")["answer"])
        self.assertLess(time.monotonic() - start, 1.9)  # smol.deadline_ms = 1200


class BrokerWiringTests(Base):
    def test_transcript_fans_out_and_voice_tools_read_and_configure(self):
        env = patch.dict(os.environ, {"Z0LIVE_LANES_FANOUT": "on"})
        env.start()
        self.addCleanup(env.stop)
        models = FakeModels({"claude-haiku-4-5": (0, '{"route":"answer","answer":"Hello."}'),
                             "qwen3-1.7b": (0, '{"route":"answer","intent":"greeting","kind":"chat","complete":true}'),
                             "claude-opus-5-5": (0, '{"route":"answer","answer":"Hello there."}')})
        self.store.lanes.models = models
        self.assertEqual(self.store.lanes.cfg(self.sid)["revision"], lanes.DEFAULT["revision"])  # default preset
        self.store.add_events(self.device, [{"session_id": self.sid, "type": "user_transcript", "text": "hello there"}])
        for _ in range(50):
            entries = self.store.tool(self.device, "read_context", {"session_id": self.sid, "arguments": {}})["entries"]
            if any(e["kind"] == "answer" for e in entries):
                break
            time.sleep(0.05)
        self.assertEqual([e["text"] for e in entries if e["kind"] == "answer"], ["Hello."])
        result = self.store.tool(self.device, "configure_lanes",
                                 {"session_id": self.sid, "arguments": {"request": "make the large lanes opus and sonnet"}})
        self.assertTrue(result["ok"], result)
        self.assertIn("claude-opus-5-5, claude-sonnet-5", result["say"])
        self.assertEqual(self.store.lanes.cfg(self.sid)["revision"], result["revision"])  # this call only
        self.assertFalse(self.cfg_path.exists())
        refused = self.store.tool(self.device, "configure_lanes", {"session_id": self.sid, "arguments": {"request": "9 fast"}})
        self.assertFalse(refused["ok"])
        self.store.add_events(self.device, [{"session_id": self.sid, "type": "barge_in"}])
        self.assertEqual(self.store.tool(self.device, "read_context", {"session_id": self.sid, "arguments": {}})["entries"], [])


class PresetDTests(Base):
    """E1's winner D: router kind, pre-speak gate, parallel Opus, heartbeat speculation, privacy."""

    def fanout(self, replies, spoken, preset="D"):
        models = FakeModels(replies)
        out = Fanout(self.store, models=models, publish=lambda sid, say, src: spoken.append((say, src)))
        out.calls[self.sid] = copy.deepcopy(lanes.PRESETS[preset])
        return out, models

    def test_gate_holds_a_risky_answer_until_parallel_opus_checks_it(self):
        spoken = []
        out, models = self.fanout({
            "qwen3-1.7b": (0, '{"route":"answer","intent":"sum","kind":"math","complete":true}'),
            "claude-haiku-4-5": (0, '{"route":"answer","answer":"It is 12.","confidence":0.6}'),
            "claude-opus-5-5": (0.3, '{"route":"answer","answer":"It is 13.","confidence":0.9}'),
        }, spoken)
        receipt = out.on_final(self.sid, "what is 6 plus 7", speak=True)
        self.assertEqual((receipt["kind"], receipt["gated"]), ("math", True))
        held = out.read(self.sid)
        self.assertEqual([e["kind"] for e in held["entries"]], ["intent"])
        self.assertEqual((held["held"], spoken), (1, []))  # nothing unverified is heard or said
        receipt["verify"].result(timeout=5)
        view = out.read(self.sid)["entries"]
        self.assertEqual((view[-1]["status"], view[-1]["text"]), ("corrected", "It is 13."))
        self.assertEqual([say for say, _ in spoken], ["It is 13."])  # first words, so no "Actually"
        self.assertTrue(models.calls[0][1].startswith("Classify") or any(p.startswith("You are the verifier")
                                                                        for _, p in models.calls))
        # agreeing claim: verified, released and spoken once
        out, _ = self.fanout({
            "qwen3-1.7b": (0, '{"route":"answer","intent":"sum","kind":"math","complete":true}'),
            "claude-haiku-4-5": (0, '{"route":"answer","answer":"Thirteen, 13."}'),
            "claude-opus-5-5": (0.1, '{"route":"answer","answer":"6 plus 7 is 13."}'),
        }, spoken)
        receipt = out.on_final(self.sid, "and what is 6 plus 7 again", speak=True)
        self.assertEqual(receipt["verify"].result(timeout=5)[0]["verdict"], "agree")
        self.assertEqual(out.read(self.sid)["entries"][-1]["status"], "verified")
        self.assertEqual(spoken[-1][0], "Thirteen, 13.")

    def test_simple_kind_is_spoken_at_once(self):
        spoken = []
        out, _ = self.fanout({
            "qwen3-1.7b": (0, '{"route":"answer","intent":"capital","kind":"simple","complete":true}'),
            "claude-haiku-4-5": (0, '{"route":"answer","answer":"Canberra."}'),
            "claude-opus-5-5": (0.2, '{"route":"answer","answer":"Canberra."}'),
        }, spoken)
        receipt = out.on_final(self.sid, "what is the capital of australia", speak=True)
        self.assertFalse(receipt["gated"])
        self.assertEqual(spoken[0][0], "Canberra.")
        receipt["verify"].result(timeout=5)
        self.assertEqual(out.read(self.sid)["entries"][-1]["status"], "verified")

    def test_heartbeat_spec_is_reused_only_when_the_partial_is_a_clean_prefix(self):
        intent = '{"route":"answer","intent":"capital","kind":"simple","complete":true}'
        out, models = self.fanout({
            "qwen3-0.6b-q8": (0, intent), "qwen3-1.7b": (0, intent),
            "claude-haiku-4-5": (0, '{"route":"answer","answer":"Paris."}'),
            "claude-opus-5-5": (0, '{"route":"answer","answer":"Paris."}'),
        }, [])
        first = out.on_partial(self.sid, "what is the capital of france")
        self.assertEqual(first["spec"], "Paris.")
        self.assertIsNone(out.on_partial(self.sid, "what is the capital of france please"))  # one spec per turn
        self.assertNotIn("spec", [e["kind"] for e in out.read(self.sid)["entries"]])  # the voice never reads it
        haiku = sum(m == "claude-haiku-4-5" for m, _ in models.calls)
        receipt = out.on_final(self.sid, "what is the capital of france please")
        self.assertEqual(receipt["small"], [{"reused_spec": first["entry"]}])
        self.assertEqual(sum(m == "claude-haiku-4-5" for m, _ in models.calls), haiku)  # no second smol call
        receipt["verify"].result(timeout=5)
        out.on_partial(self.sid, "what is the capital of spain")
        receipt = out.on_final(self.sid, "what is the capital of spain no wait of portugal")
        self.assertIn("spec_discarded", receipt)
        self.assertEqual(sum(m == "claude-haiku-4-5" for m, _ in models.calls), haiku + 2)

    def test_privacy_class_filters_every_lane_before_dispatch(self):
        cfg = copy.deepcopy(lanes.B)
        cfg["lanes"]["smol"]["models"].append({"provider": "free", "model": "vllm-sr/free", "route": "vllm-sr",
                                               "billing": "free", "tier": "small"})
        out, models = self.fanout({"qwen3.5-4b": (0, '{"route":"answer","answer":"It is on the router label."}')}, [])
        out.calls[self.sid] = cfg
        receipt = out.on_final(self.sid, "what's my wifi password")
        self.assertEqual(receipt["privacy"], "credentials")
        self.assertEqual(set(receipt["privacy_blocked"]), {"small:smol", "small:vllm-sr/free", "large:plan",
                                                           "large:claude-sonnet-5"})
        self.assertTrue({m for m, _ in models.calls} <= {"qwen3-0.6b-q8", "qwen3-1.7b", "qwen3.5-4b"})
        self.assertNotIn("verify", receipt)  # no lane may check it, so nothing leaves the machine
        receipt = out.on_final(self.sid, "did my build pass")
        self.assertEqual((receipt["privacy"], receipt["privacy_blocked"]), ("personal", ["small:vllm-sr/free"]))

    def test_spoken_launch_and_mid_call_change_through_transcripts(self):
        spoken = []
        out, models = self.fanout({}, spoken)
        receipt = out.on_final(self.sid, "start live with 2 fast, 1 large, no council", speak=True)
        self.assertTrue(receipt["configured"]["ok"])
        self.assertEqual(receipt["configured"]["counts"], {"fast": 2, "small": 1, "large": 1, "council": 0})
        self.assertTrue(spoken[0][0].startswith("Lanes set: 2 fast, 1 small, 1 large"))
        receipt = out.on_final(self.sid, "make the large lanes opus and sonnet")
        self.assertEqual([b["model"] for b in lanes.large(out.cfg(self.sid))], ["claude-opus-5-5", "claude-sonnet-5"])
        self.assertEqual(out.on_final(self.sid, "switch to preset B")["configured"]["revision"], "preset-B-2f-2s-2l-1c")
        self.assertEqual(models.calls, [])  # configuring calls no model
        receipt = out.on_final(self.sid, "set a timer for two large pizzas")
        self.assertNotIn("configured", receipt)

    def test_relay_route_launches_and_takes_turns(self):
        sid = self.store.new_session(self.device, {"client": "web", "context": {}, "tools": [],
                                                   "lanes": "preset A"})["session_id"]
        self.assertEqual(self.store.lanes.cfg(sid)["revision"], "preset-A-0f-1s-1l-0c")
        self.store.lanes.models = FakeModels({"claude-haiku-4-5": (0, '{"route":"answer","answer":"Hi."}')})
        result = self.store.lane_turn(self.device, {"session_id": sid, "text": "hey there"})
        self.assertEqual((result["receipt"]["answer"], result["checking"]), ("Hi.", False))
        self.assertEqual(self.store.lane_turn(self.device, {"session_id": sid, "text": "hey", "final": False})["receipt"],
                         None)  # preset A has no partial trigger
        launched = self.store.lane_launch(self.device, {"session_id": sid, "choice": "2 fast, 1 large, no council"})
        self.assertEqual(launched["counts"], {"fast": 2, "small": 1, "large": 1, "council": 0})
        with self.assertRaises(LookupError):
            self.store.lane_turn("someone-else", {"session_id": sid, "text": "hey"})


class L2Tests(Base):
    """L2 (11-11 real-call replay): self-labelled kind + confidence gate, gather lane, dead lanes."""

    def fanout(self, replies, spoken, **smol):
        out = Fanout(self.store, models=FakeModels(replies), publish=lambda sid, say, src: spoken.append((say, src)))
        cfg = copy.deepcopy(lanes.D)
        cfg["lanes"]["smol"].update(self_label=True, **smol)
        cfg["splice"]["gate_below"] = 0.7
        out.calls[self.sid] = cfg
        return out

    def test_confident_answer_speaks_without_a_router_call_and_unsure_one_is_held(self):
        spoken = []
        out = self.fanout({"claude-haiku-4-5": (0, '{"route":"answer","answer":"Hey Kevin.","kind":"chat",'
                                                   '"confidence":0.9}'),
                           "claude-opus-5-5": (0.3, '{"verdict":"correct","answer":"Hey.","confidence":0.9}')}, spoken)
        r = out.on_final(self.sid, "hey can you hear me", speak=True)
        self.assertEqual((r["answer"], r["kind"], r["gated"]), ("Hey Kevin.", "chat", False))
        self.assertNotIn("fast", r)  # no router call
        self.assertEqual(spoken[0][0], "Hey Kevin.")
        out = self.fanout({"claude-haiku-4-5": (0, '{"route":"answer","answer":"Maybe 4.","kind":"math",'
                                                   '"confidence":0.4}'),
                           "claude-opus-5-5": (5, '{"verdict":"correct"}')}, spoken := [])
        r = out.on_final(self.sid, "what is 17 times 23", speak=True)
        self.assertTrue(r["gated"])
        self.assertEqual(spoken, [])

    def test_meta_turn_runs_gather_skips_an_unavailable_engine_and_writes_the_next(self):
        spoken = []
        g = {"enabled": True, "profile": "", "models": [
            {"provider": "anthropic", "model": "claude-haiku-4-5", "route": "hermes"},
            {"provider": "local", "model": "qwen3.5-4b", "route": "local", "hermes_provider": "custom:local-qwen4b"}],
            "speak": ["qwen3.5-4b"]}
        out = self.fanout({"claude-haiku-4-5": (0, '{"route":"meta","answer":"","kind":"gather"}')}, spoken, gather=g)
        outputs = iter([("Anthropic credentials are rate-limited for claude-haiku-4-5; other Claude models remain "
                         "available.\n", 0), ("You have two lanes running right now.\nsession_id: x\n", 0)])
        seen = []

        def run(cmd, **kw):
            seen.append(cmd)
            text, code = next(outputs)
            self.assertEqual(kw["env"]["FAST_LANE_OFF"], "1")
            return subprocess.CompletedProcess(cmd, code, text, "")

        with patch("z0live_desktop.lane_fanout.subprocess.run", side_effect=run):
            r = out.on_final(self.sid, "are multiple lanes working", speak=True, privacy="personal")
            for _ in range(50):
                if spoken:
                    break
                time.sleep(0.05)
        self.assertTrue(r["gathering"])
        self.assertEqual([c[c.index("--provider") + 1] for c in seen], ["anthropic", "custom:local-qwen4b"])
        self.assertIn("web,session_search,z0_file_search", seen[0])  # read-only toolsets only
        view = out.read(self.sid)["entries"]
        self.assertEqual([(e["lane"], e["text"]) for e in view if e["lane"].startswith("gather:")],
                         [("gather:qwen3.5-4b", "You have two lanes running right now.")])
        self.assertEqual(spoken, [("You have two lanes running right now.", spoken[0][1])])

    def test_gather_engine_outside_speak_list_writes_the_board_but_stays_silent(self):
        # L2.1: on the 48-turn real-call replay the local gather engine was wrong 18 times, right 3.
        spoken = []
        g = {"enabled": True, "profile": "", "models": [
            {"provider": "local", "model": "qwen3.5-4b", "route": "local", "hermes_provider": "custom:local-qwen4b"}]}
        out = self.fanout({"claude-haiku-4-5": (0, '{"route":"meta","answer":"","kind":"gather"}')}, spoken, gather=g)
        done = subprocess.CompletedProcess([], 0, "You have two lanes running right now.\n", "")
        with patch("z0live_desktop.lane_fanout.subprocess.run", return_value=done):
            r = out.on_final(self.sid, "are multiple lanes working", speak=True, privacy="personal")
            for _ in range(50):
                if any(e["lane"].startswith("gather:") for e in out.read(self.sid)["entries"]):
                    break
                time.sleep(0.05)
        self.assertFalse(r["gathering"])  # the relay does not wait for it
        self.assertIn("gather:qwen3.5-4b", [e["lane"] for e in out.read(self.sid)["entries"]])
        self.assertEqual(spoken, [])

    def test_speak_kinds_holds_a_confident_simple_fact(self):
        # L2.1: "who did the Knicks beat in the last finals" got a confident, stale answer from the small lane.
        spoken = []
        out = self.fanout({"claude-haiku-4-5": (0, '{"route":"answer","answer":"The Lakers, in 1970.",'
                                                   '"kind":"simple","confidence":0.95}')}, spoken)
        out.calls[self.sid]["splice"]["speak_kinds"] = ["chat"]
        r = out.on_final(self.sid, "who did the Knicks beat in the last finals", speak=True)
        self.assertTrue(r["gated"])
        self.assertEqual(spoken, [])

    def test_gather_respects_privacy_credentials_turns_get_local_engines_only(self):
        g = {"enabled": True, "models": [{"provider": "anthropic", "model": "claude-haiku-4-5", "route": "hermes"}]}
        out = self.fanout({"qwen3.5-4b": (0, '{"route":"meta","answer":""}')}, [], gather=g)
        out.calls[self.sid]["lanes"]["smol"]["models"].append(
            {"provider": "local", "model": "qwen3.5-4b", "route": "local", "billing": "free", "tier": "small"})
        with patch("z0live_desktop.lane_fanout.subprocess.run") as run:
            r = out.on_final(self.sid, "read me my wifi password")
        self.assertFalse(r.get("gathering"))
        run.assert_not_called()

    def test_a_dead_local_lane_is_skipped_for_the_rest_of_the_call(self):
        out = self.fanout({"claude-haiku-4-5": (0, '{"route":"answer","answer":"Hi.","confidence":0.9}'),
                           "qwen3-0.6b-q8": (0, OSError("connection refused"))}, [])
        out.calls[self.sid]["lanes"]["smol"]["self_label"] = False
        out.calls[self.sid]["lanes"]["smol"]["router"] = None
        out.calls[self.sid]["lanes"]["smol"]["models"][0] = dict(out.calls[self.sid]["lanes"]["smol"]["models"][0])
        first = out.on_final(self.sid, "hi")
        self.assertIn({"model": "qwen3-0.6b-q8", "error": "OSError"}, first["fast"])
        second = out.on_final(self.sid, "hello again")
        self.assertIn("fast:qwen3-0.6b-q8", second["dead_skipped"])
        self.assertNotIn("qwen3-0.6b-q8", [t.get("model") for t in second.get("fast", [])])
        self.assertEqual(second["answer"], "Hi.")  # the call goes on without the dead lane


if __name__ == "__main__":
    unittest.main()
