"""lane_layout: the lanes attached to the conversation (parse, validation, atomic write, tool, voice command)."""
import asyncio
import importlib.util
import json
import multiprocessing
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import lane_layout as L
import mobile_live as live
import tiers

ROOT = Path(__file__).resolve().parent.parent


class Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = Path(self.dir.name) / "layout.json"
        # seeds read the real tiers.json/lane-pool.json: point them at nothing so tests see the defaults
        self.patches = [patch.object(L, "TIERS_FILE", Path(self.dir.name) / "tiers.json"),
                        patch.object(L, "POOL_FILE", Path(self.dir.name) / "lane-pool.json"),
                        patch.object(L, "FILE", self.path),
                        patch.object(L, "CONDUCTOR_STATE", Path(self.dir.name) / "state.json")]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.dir.cleanup()


class ParseTests(unittest.TestCase):
    def test_commands(self):
        cases = {
            "add two opus lanes": {"action": "add", "model": "opus", "count": 2, "role": ""},
            "add two opus lanes to the call": {"action": "add", "model": "opus", "count": 2, "role": "voice"},
            "add 2 haiku answer lanes": {"action": "add", "model": "haiku", "count": 2, "role": "voice"},
            "drop the sol lanes from the call": {"action": "remove", "model": "sol", "count": None, "role": "voice"},
            "Add 3 sol lanes.": {"action": "add", "model": "sol", "count": 3, "role": ""},
            "add to soul lanes": {"action": "add", "model": "sol", "count": 2, "role": ""},  # STT for "add two sol"
            "Okay, add another haiku lane.": {"action": "add", "model": "haiku", "count": 1, "role": ""},
            "give me five sol lanes for the research pool": {"action": "add", "model": "sol", "count": 5,
                                                              "role": "research"},
            "add two sonnet lanes to the fast tier": {"action": "add", "model": "sonnet", "count": 2, "role": "fast"},
            "drop the sol lanes": {"action": "remove", "model": "sol", "count": None, "role": ""},
            "remove one haiku lane please": {"action": "remove", "model": "haiku", "count": 1, "role": ""},
            "set the haiku lanes to 2": {"action": "set", "model": "haiku", "count": 2, "role": ""},
            "how many lanes": {"action": "list"},
            # the shape delegations arrive in: fillers, then the question (synthetic)
            "Okay, sounds good. Um, and how many lanes are attached right now": {"action": "list"},
        }
        for text, want in cases.items():
            with self.subTest(text=text):
                self.assertEqual(L.command(text), want)

    def test_not_commands(self):
        for text in ["So is it working ? Like the lanes now? Yes", "why did you add two opus lanes",
                     "he said add two opus lanes", "don't add two opus lanes", "how many lanes are running",
                     "what's the weather", "add two opus lanes and then email Sarah", "",
                     "the fast lane on the highway was blocked", "Research the boat sandboxes. Use a sol lane.",
                     "Check the builds. Add two sol lanes.", "stop the sol lanes", "kill two opus lanes",
                     "use 2 sol lanes", "start three haiku lanes"]:
            with self.subTest(text=text):
                self.assertIsNone(L.command(text))


class ValidateTests(Tmp):
    def test_unknown_model_lists_the_valid_ones(self):
        with self.assertRaises(L.LayoutError) as e:
            L.act("add", "gemini", 1)
        self.assertIn("haiku, sonnet, opus, sol", str(e.exception))

    def test_every_family_runs_its_newest_version(self):
        # Kevin, 11-27: "just use the latest version for every role". Versioned names mean the family.
        for word, fam in (("terra", "terra"), ("gpt-6.1-terra", "terra"), ("6.1 sol", "sol"), ("astra", "astra"),
                          ("luna", "luna")):
            with self.subTest(word=word):
                self.assertEqual(L._name(word), fam)
        self.assertEqual(L.command("add two terra lanes"), {"action": "add", "model": "terra", "count": 2, "role": ""})
        self.assertEqual(L.command("add an astra lane to the call")["model"], "astra")

    def test_latest_codex_reads_the_catalog(self):
        cat = Path(self.dir.name) / "models_cache.json"
        cat.write_text(json.dumps({"models": [
            {"slug": "gpt-6.2-sol", "visibility": "list"}, {"slug": "gpt-6.1-terra", "visibility": "list"},
            {"slug": "gpt-5.6-terra", "visibility": "list"}, {"slug": "gpt-6.3-terra", "visibility": "hide"},
            {"slug": "gpt-6.10-sol", "visibility": "list"}, {"slug": "gpt-6-luna", "visibility": "list"},
            {"slug": "gpt-reserve", "visibility": "hide"}]}))
        self.assertEqual(L.latest_codex("terra", cat), "gpt-6.1-terra")  # hidden 6.3 is not offered
        self.assertEqual(L.latest_codex("sol", cat), "gpt-6.10-sol")     # numeric, not string, order
        self.assertEqual(L.latest_codex("luna", cat), "gpt-6-luna")
        self.assertEqual(L.latest_codex("astra", cat), L.CODEX_FALLBACK["astra"])  # missing -> fallback
        self.assertEqual(L.latest_codex("sol", Path(self.dir.name) / "missing.json"), L.CODEX_FALLBACK["sol"])

    def test_caps(self):
        with self.assertRaises(L.LayoutError):
            L.act("add", "haiku", 5, "voice")  # fast already has terra + luna; cap 6 per tier
        with self.assertRaises(L.LayoutError):
            L.act("add", "sol", 14)  # worker pool: 5 + 14 > 18

    def test_last_voice_lane_cannot_go(self):
        L.act("remove", "sonnet", role="voice")
        L.act("remove", "sol", role="voice")
        L.act("remove", "opus", role="voice")
        L.act("remove", "luna", role="voice")
        with self.assertRaises(L.LayoutError):
            L.act("remove", "terra", role="voice")
        self.assertEqual(L.current()[0]["voice"]["fast"], {"terra": 1})  # the refused write left the file alone

    def test_fable_is_tv_only(self):
        out = L.act("add", "fable", 1, "voice")
        self.assertEqual(out["role"], "research")
        with self.assertRaises(L.LayoutError):
            L.validate({"voice": {"fast": {"fable": 1}}, "research": {}})


class StoreTests(Tmp):
    def test_absent_file_changes_nothing(self):
        self.assertIsNone(L.load())
        with patch.object(tiers, "FILE", Path(self.dir.name) / "tiers.json"):
            self.assertEqual(tiers.load(), tiers.TIERS)

    def test_seed_reproduces_todays_tiers_json(self):
        live_tiers = {"fast": [{"provider": L.CLAUDE, "model": "claude-haiku-5-5"}] * 3,
                      "medium": [{"provider": L.CLAUDE, "model": "claude-sonnet-5-5"},
                                 {"provider": L.CODEX, "model": "gpt-6.2-sol"}],
                      "slow": [{"provider": L.CLAUDE, "model": "claude-opus-5-5"},
                               {"provider": L.CODEX, "model": "gpt-6.2-sol"}]}
        L.TIERS_FILE.write_text(json.dumps(live_tiers))
        self.assertEqual(L.tiers(L.seed()), live_tiers)
        self.assertEqual(L.seed()["research"], L.RESEARCH_DEFAULT)

    def test_bare_add_means_worker_lanes(self):
        out = L.act("add", "sol", 2)
        self.assertEqual(out["layout"]["research"]["sol"], 7)
        self.assertEqual(out["layout"]["voice"], L.VOICE_DEFAULT)
        self.assertEqual(L.act("add", "sol", 3)["layout"]["research"]["sol"], 10)  # the brief's "add 3 sol lanes"
        self.assertIn("Added 2 GPT 6.2 Sol to the worker lanes", out["say"])

    def test_add_writes_and_tiers_load_sees_it_at_once(self):
        out = L.act("add", "sol", 2, "voice")
        self.assertTrue(out["ok"])
        self.assertEqual(out["layout"]["voice"]["medium"], {"sonnet": 1, "sol": 3})
        self.assertIn("Added 2 GPT 6.2 Sol to the medium answer lanes", out["say"])
        self.assertEqual(json.loads(self.path.read_text())["voice"]["medium"]["sol"], 3)
        self.assertEqual([b["model"] for b in tiers.load()["medium"]], ["claude-sonnet-5-5"] + ["gpt-6.2-sol"] * 3)

    def test_remove_all_of_a_model_from_every_tier(self):
        out = L.act("remove", "sol", role="voice")
        self.assertEqual(out["layout"]["voice"], {"fast": {"terra": 1, "luna": 1}, "medium": {"sonnet": 1}, "slow": {"opus": 1}})
        self.assertEqual(out["layout"]["research"]["sol"], 5)  # the TV pool is its own role

    def test_research_role_and_set(self):
        L.act("set", "sol", 4, "research")
        self.assertEqual(L.current()[0]["research"]["sol"], 4)

    def test_invalid_file_is_ignored_by_readers(self):
        self.path.write_text("{not json")
        self.assertIsNone(L.load())
        self.path.write_text(json.dumps({"voice": {"fast": {"gemini": 1}}, "research": {}}))
        self.assertIsNone(L.load())

    def test_write_is_atomic_and_leaves_no_tmp(self):
        L.act("add", "opus", 1)
        replaced = []
        real = os.replace
        with patch.object(L.os, "replace", side_effect=lambda a, b: (replaced.append((a, b)), real(a, b))):
            L.act("add", "opus", 1)
        self.assertEqual(Path(replaced[0][1]), self.path)
        self.assertEqual(sorted(p.name for p in Path(self.dir.name).iterdir()), ["layout.json", "layout.json.lock"])

    def test_failed_write_keeps_the_old_file(self):
        L.act("add", "opus", 1)
        before = self.path.read_text()
        with patch.object(L.os, "replace", side_effect=OSError("disk full")), self.assertRaises(OSError):
            L.act("add", "opus", 1)
        self.assertEqual(self.path.read_text(), before)
        self.assertEqual(len(list(Path(self.dir.name).glob("*.tmp"))), 0)

    def test_concurrent_adds_from_processes_all_land(self):
        ctx = multiprocessing.get_context("fork")
        procs = [ctx.Process(target=_add_one, args=(str(self.path), str(L.TIERS_FILE), str(L.POOL_FILE)))
                 for _ in range(8)]
        for p in procs:
            p.start()
        for p in procs:
            p.join(20)
        self.assertEqual(L.current()[0]["research"]["sonnet"], 2 + 8)

    def test_list_includes_running_tv_lanes(self):
        L.CONDUCTOR_STATE.write_text(json.dumps({"asks": [{"lanes": [{"state": "running"}, {"state": "done"}]}]}))
        out = L.act("list")
        self.assertFalse(out["saved"])
        self.assertIn("Answer lanes on the call: 6", out["say"])
        self.assertIn("Worker lanes: 17", out["say"])
        self.assertIn("1 workers running now", out["say"])

    def test_run_refuses_out_loud(self):
        out = L.run("add six haiku lanes to the call")
        self.assertFalse(out["ok"])
        self.assertTrue(out["say"].startswith("I couldn't change the lanes"))


def _add_one(path, tiers_file, pool_file):
    L.TIERS_FILE, L.POOL_FILE = Path(tiers_file), Path(pool_file)
    L.act("add", "sonnet", 1, "research", path=Path(path))


class RouterLaneTests(unittest.IsolatedAsyncioTestCase):
    async def test_router_lane_never_gets_the_call_context(self):
        systems = {}

        async def fake_ask(http, binding, system, user, timeout):
            systems[binding["model"]] = system
            return {"text": "SKIP", "first_s": 0.1}

        with patch.object(tiers, "ask", fake_ask):
            await tiers.race(None, "fast", [L.MODELS["sr"]["binding"], L.MODELS["haiku"]["binding"]],
                             "Context from his Hermes chat: private plans", "capital of canada?")
        self.assertNotIn("private plans", systems["vllm-sr/turbo"])
        self.assertIn("private plans", systems["claude-haiku-5-5"])

    async def test_sr_lane_calls_the_router_directly(self):
        seen = {}

        class Resp:
            status = 200

            async def json(self, content_type=None):
                return {"choices": [{"message": {"content": " Ottawa  is the capital. "}}]}

            async def __aenter__(self):
                return self

            async def __aexit__(self, *a):
                return False

        class Http:
            def post(self, url, json, timeout):
                seen["url"], seen["body"] = url, json
                return Resp()

        out = await tiers.ask(Http(), L.MODELS["sr"]["binding"], "sys", "capital of canada?", 4)
        self.assertEqual(out["text"], "Ottawa is the capital.")
        self.assertEqual(seen["url"], L.SR_URL + "/chat/completions")
        self.assertEqual(seen["body"]["model"], "vllm-sr/turbo")
        with self.assertRaises(RuntimeError):
            await tiers.ask(Http(), {"provider": "custom", "model": "x", "base_url": "http://evil/v1"}, "", "", 4)


class VoiceCommandTests(Tmp, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        Tmp.setUp(self)

    async def asyncSetUp(self):
        self.call = live.Call(live.Manager(None), profile="")
        self.call.protocol.seen.append("d1")
        self.call.protocol.seen.append("d2")
        self.sent = []
        self.call.send = lambda event, to_provider=False: self.sent.append(event)

    async def asyncTearDown(self):
        await self.call.close("test")

    def spoken(self):
        return " ".join(c.get("text", "") for e in self.sent for c in e.get("content", []) if isinstance(c, dict))

    async def test_add_on_the_call_never_reaches_hermes(self):
        with patch.object(live, "run_hermes", side_effect=AssertionError("Hermes must not run")):
            began = time.monotonic()
            await self.call.host_delegate("d1", "Add two opus lanes.")
            took = time.monotonic() - began
        self.assertLess(took, 0.5)
        self.assertEqual(L.current()[0]["research"]["opus"], 4)
        self.assertIn("Added 2 Opus 5.5 to the worker lanes", self.spoken())
        await self.call.host_delegate("d2", "add two opus lanes to the call")
        self.assertEqual(L.current()[0]["voice"]["slow"], {"opus": 3, "sol": 1})

    async def test_same_words_delegated_twice_change_the_layout_once(self):
        self.call.protocol.turn_text["user"] = "add two opus lanes"
        with patch.object(live, "run_hermes", side_effect=AssertionError("Hermes must not run")):
            await self.call.host_delegate("d1", "Add two opus lanes.")
            await self.call.host_delegate("d2", "add two opus lanes")
        self.assertEqual(L.current()[0]["research"]["opus"], 4)

    async def test_delegation_before_and_after_the_user_final_applies_once(self):
        with patch.object(live, "run_hermes", side_effect=AssertionError("Hermes must not run")):
            self.call.protocol.turn_text["user"] = "what's the weather"  # previous turn: the final is not in yet
            await self.call.host_delegate("d1", "add two opus lanes")
            self.call.protocol.turn_text["user"] = "uh add two opus lanes"  # the final lands, the voice delegates again
            await self.call.host_delegate("d2", "Add two opus lanes.")
        self.assertEqual(L.current()[0]["research"]["opus"], 4)

    async def test_user_words_count_when_the_request_is_paraphrased(self):
        self.call.protocol.turn_text["user"] = "uh drop the sol lanes from the call"
        with patch.object(live, "run_hermes", side_effect=AssertionError("Hermes must not run")):
            await self.call.host_delegate("d1", "Remove the GPT Sol lanes from the call layout")
        self.assertNotIn("sol", L.current()[0]["voice"]["medium"])

    async def test_how_many_lanes_reads_without_writing(self):
        with patch.object(live, "run_hermes", side_effect=AssertionError("Hermes must not run")):
            await self.call.host_delegate("d1", "how many lanes are attached right now")
        self.assertFalse(self.path.exists())
        self.assertIn("Answer lanes on the call: 6", self.spoken())

    async def test_broker_board_receipt_is_ignored_while_tiers_run(self):
        class Relay:
            turn = {"receipt": {"configured": {"ok": False, "say": "I couldn't set that lane layout"}}}

        self.call.relay = Relay()
        with patch.object(live, "TIERS_ON", True), patch.object(live, "run_hermes", return_value="Done.") as hermes, \
                patch.object(self.call, "tier_lanes", side_effect=lambda *a: asyncio.sleep(0)):
            try:
                await self.call.host_delegate("d1", "gimme 2 fast lanes, 2 small, 2 large, one council")
            finally:
                self.call.relay = None
        self.assertTrue(hermes.called)  # not a lane_layout command and not the dead board: Hermes (with the tool)
        self.assertNotIn("couldn't set that lane layout", self.spoken())

    async def test_other_requests_still_go_to_hermes(self):
        self.assertFalse(self.call.lane_command("d1", "what's the weather"))

    async def test_previous_lane_words_never_swallow_a_new_request(self):
        self.call.protocol.turn_text["user"] = "add two opus lanes"  # last turn; the new turn.done is not in yet
        self.assertFalse(self.call.lane_command("d2", "what's the weather"))
        self.assertFalse(self.path.exists())

    async def test_lane_command_is_not_a_delegation_start(self):
        events, real = [], self.call.trace

        class Spy:
            def __call__(self, ev, **kw):
                events.append(ev)

            def __getattr__(self, name):
                return getattr(real, name)

        self.call.trace = Spy()
        await self.call.host_delegate("d1", "add two opus lanes")
        self.call.trace = real
        self.assertIn("lanes.layout", events)
        self.assertNotIn("delegation.start", events)  # the conductor would turn it into worker lanes


class PluginToolTests(Tmp):
    def setUp(self):
        super().setUp()
        spec = importlib.util.spec_from_file_location("z0live_plugin_under_test", ROOT / "__init__.py")
        self.plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.plugin)
        # the plugin loads lane_layout by path: point that copy at the temp dir too
        mod = self.plugin._layout_module()
        for name in ("TIERS_FILE", "POOL_FILE", "CONDUCTOR_STATE"):
            setattr(mod, name, getattr(L, name))
        cfg = {**self.plugin.DEFAULTS, "layout_file": str(self.path), "lanes_file": str(Path(self.dir.name) / "x"),
               "traces_dir": str(Path(self.dir.name) / "traces")}
        self.tool = self.plugin.make_lanes(cfg.get, lambda: False)

    def tearDown(self):
        sys.modules.pop("_z0live_plugin_lane_layout", None)
        super().tearDown()

    def call(self, **args):
        return json.loads(self.tool(args))

    def test_structured_add_is_one_fast_call(self):
        out = self.call(action="add", model="sol", count=2, role="voice")
        self.assertTrue(out["ok"])
        self.assertLess(out["ms"], 1000)
        self.assertEqual(out["layout"]["voice"]["medium"]["sol"], 3)
        self.assertEqual(json.loads(self.path.read_text())["by"], "hermes")

    def test_words_work_too(self):
        self.assertEqual(self.call(request="add 2 sol lanes")["layout"]["research"]["sol"], 7)
        self.assertIn("Answer lanes on the call", self.call()["say"])

    def test_errors_list_valid_models(self):
        out = self.call(action="add", model="gemini", count=1)
        self.assertFalse(out["ok"])
        self.assertIn("valid models", out["error"])

    def test_configure_tiers_goes_into_an_existing_layout(self):
        self.call(action="add", model="opus", count=1, role="voice")
        cfg = {**self.plugin.DEFAULTS, "layout_file": str(self.path), "tiers_file": str(L.TIERS_FILE)}
        configure = self.plugin.make_configure(cfg.get)
        out = json.loads(configure({"tiers": {"fast": [{"provider": L.CLAUDE, "model": "claude-haiku-5-5"}]}}))
        self.assertTrue(out["ok"])
        self.assertEqual(L.current()[0]["voice"], {"fast": {"haiku": 1}})
        self.assertFalse(L.TIERS_FILE.exists())  # one source of truth: tiers.json is not written behind the layout
        bad = json.loads(configure({"tiers": {"fast": [{"provider": "x", "model": "gpt-6.1-terra"}]}}))
        self.assertIn("valid models", bad["error"])
        self.assertTrue(json.loads(configure({"reset_tiers": True}))["ok"])
        self.assertEqual(L.current()[0]["voice"], L.VOICE_DEFAULT)

    def test_board_requests_still_reach_the_broker_board(self):
        self.assertEqual(self.call(request="board gimme 2 fast lanes")["error"], "z0live stack not configured")

    def test_conductor_workers_never_see_the_lane_tools(self):
        tools = {}

        class Ctx:
            def get_config(self, key, default=None):
                return default

            def register_tool(self, name, **kw):
                tools[name] = kw

            def register_command(self, *a, **kw):
                pass

        self.plugin.register(Ctx())
        with patch.dict(os.environ, {"Z0LIVE_LANE_WORKER": "1"}):
            self.assertFalse(tools["z0live_lanes"]["check_fn"]())
            self.assertFalse(tools["z0live_configure"]["check_fn"]())

    def test_registered_with_the_new_parameters(self):
        tools = {}

        class Ctx:
            def get_config(self, key, default=None):
                return default

            def register_tool(self, name, **kw):
                tools[name] = kw

            def register_command(self, *a, **kw):
                pass

        self.plugin.register(Ctx())
        self.assertIn("z0live_lanes", tools)
        self.assertEqual(set(tools["z0live_lanes"]["schema"]["parameters"]["properties"]),
                         {"action", "model", "count", "role", "request"})


if __name__ == "__main__":
    unittest.main()
