"""Semantic-router fast lane: it races only in turbo/ultra (policy/RULES.md: "Turbo mode stacks ... sr:free"), it never
sees the chat brief, TV or call, and normal mode drops every router binding, even one in the layout. No network."""
import asyncio
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("HERMES_VOICE_ENGINE", "gpt-live")
import mobile_live as live  # noqa: E402,F401  (same import order as test_tiers)
import tiers  # noqa: E402
from test_tiers import Call  # noqa: E402

CODEX = {"provider": "openai-codex", "model": "gpt-6-luna"}
ROUTER = {"provider": "custom", "model": "vllm-sr/turbo", "base_url": tiers.lane_layout.SR_URL}
CFG = {"fast": [CODEX], "medium": [{"provider": "p", "model": "sonnet"}]}


class ModeTests(unittest.TestCase):
    def state(self, body):
        d = tempfile.mkdtemp()
        p = Path(d) / "state.json"
        p.write_text(body)
        return p

    def test_reads_the_conductor_mode(self):
        self.assertEqual(tiers.conductor_mode(self.state(json.dumps({"mode": "turbo"}))), "turbo")
        self.assertEqual(tiers.conductor_mode(self.state(json.dumps({"mode": "ultra"}))), "ultra")

    def test_unreadable_or_odd_state_is_normal(self):
        self.assertEqual(tiers.conductor_mode(Path("/nonexistent/state.json")), "normal")
        self.assertEqual(tiers.conductor_mode(self.state("{not json")), "normal")
        self.assertEqual(tiers.conductor_mode(self.state(json.dumps({"mode": "warp"}))), "normal")
        self.assertEqual(tiers.conductor_mode(self.state("[]")), "normal")


class ForRequestTests(unittest.TestCase):
    def test_normal_mode_is_unchanged_without_router_lanes(self):
        self.assertEqual(tiers.for_request(CFG, "normal"), CFG)

    def test_turbo_puts_the_router_first_in_the_fast_tier(self):
        out = tiers.for_request(CFG, "turbo")
        self.assertEqual(out["fast"][0]["model"], "vllm-sr/turbo")
        self.assertEqual(out["fast"][1:], [CODEX])
        self.assertEqual(out["medium"], CFG["medium"])
        self.assertEqual(CFG["fast"], [CODEX], "for_request must not mutate the layout")

    def test_turbo_adds_one_router_lane_only(self):
        out = tiers.for_request({"fast": [ROUTER, CODEX]}, "ultra")
        self.assertEqual(sum(1 for b in out["fast"] if b.get("base_url")), 1)

    def test_normal_mode_drops_a_router_lane_written_into_the_layout(self):
        out = tiers.for_request({"fast": [ROUTER, CODEX], "slow": [ROUTER]}, "normal")
        self.assertEqual(out, {"fast": [CODEX]})

    def test_off_switch(self):
        with mock.patch.dict(os.environ, {"Z0LIVE_SR_FAST": "off"}):
            self.assertEqual(tiers.for_request(CFG, "turbo"), CFG)


class RouterSeesOnlyTheRequest(unittest.IsolatedAsyncioTestCase):
    async def test_router_gets_bare_system_and_others_get_context(self):
        seen = {}

        async def ask(http, binding, system, user, timeout):
            seen[binding["model"]] = (system, user)
            await asyncio.sleep(0.01 if binding.get("base_url") else 0.05)
            return {"text": "SKIP" if binding.get("base_url") else "Ottawa.", "first_s": 0.1}

        with mock.patch.object(tiers, "ask", ask):
            text, model, _, errors = await tiers.race(None, "fast", [ROUTER, CODEX], "Context from his Hermes chat: PRIVATE",
                                                      "capital of Canada?")
        self.assertEqual((text, model), ("Ottawa.", "gpt-6-luna"))
        self.assertEqual(errors, {"vllm-sr/turbo": "SKIP"})
        self.assertNotIn("PRIVATE", seen["vllm-sr/turbo"][0])
        self.assertIn("PRIVATE", seen["gpt-6-luna"][0])


class CallPathTests(unittest.IsolatedAsyncioTestCase):
    async def run_call(self, mode):
        call, asked = Call(), []

        async def ask(http, binding, system, user, timeout):
            asked.append(binding["model"])
            return {"text": "Ottawa." if binding.get("base_url") else "SKIP", "first_s": 0.1}

        with mock.patch.object(tiers, "ask", ask), mock.patch.object(tiers, "load", lambda: CFG), \
                mock.patch.object(tiers, "conductor_mode", lambda: mode):
            await call.tier_lanes("d1", "capital of Canada?")
        return call, asked

    async def test_turbo_call_traces_the_router_result_without_giving_it_the_floor(self):
        call, asked = await self.run_call("turbo")
        self.assertIn("vllm-sr/turbo", asked)
        self.assertEqual(call.spoke, [])
        self.assertIn("lanes.tier_claim_held", call.events)
        self.assertIn("lanes.mode", call.events)

    async def test_normal_call_never_asks_the_router(self):
        call, asked = await self.run_call("normal")
        self.assertNotIn("vllm-sr/turbo", asked)
        self.assertEqual(call.spoke, [])
        self.assertNotIn("lanes.mode", call.events)


if __name__ == "__main__":
    unittest.main()
