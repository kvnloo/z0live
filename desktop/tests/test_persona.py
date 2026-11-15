"""Persona/mint contract tests. OAuth and upstream are explicitly mocked, never live."""

import base64
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from z0live_desktop import core


class PersonaTests(unittest.TestCase):
    def setUp(self):
        selected = patch("z0live_desktop.voice_runtime.load_plan", return_value=None)
        selected.start()
        self.addCleanup(selected.stop)

    def mint_payload(self, tools, context=None):
        # Synthetic credential only, with no host credential access or network traffic.
        claims = {"https://api.openai.com/auth": {"chatgpt_account_id": "unit-account"}}
        encoded = (
            base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        )
        token = "unit-header." + encoded + ".unit-signature"
        with (
            patch.object(
                Path,
                "read_text",
                return_value=json.dumps({"tokens": {"access_token": token}}),
            ),
            patch.object(
                core,
                "request_json",
                return_value={"value": "unit-ephemeral", "expires_at": 123},
            ) as upstream,
        ):
            result = core.mint(context or {}, tools)
        url, body, headers = upstream.call_args.args
        self.assertEqual(url, "https://api.openai.com/v1/realtime/client_secrets")
        self.assertEqual(headers["Authorization"], "Bearer " + token)
        self.assertEqual(headers["ChatGPT-Account-ID"], "unit-account")
        for secret in (token, "unit-account", "unit-ephemeral"):
            self.assertNotIn(secret, core.dumps(body))
        self.assertEqual(
            set(result),
            {"value", "expires_at", "model", "voice", "instructions_version"},
        )
        self.assertEqual(result["instructions_version"], "z0live.persona.v4")
        self.assertNotIn(token, core.dumps(result))
        self.assertEqual(
            body["expires_after"], {"anchor": "created_at", "seconds": 120}
        )
        self.assertEqual(body["session"]["model"], "gpt-realtime")
        self.assertEqual(body["session"]["audio"]["output"]["voice"], "marin")
        return body["session"]

    def test_self_knowledge_and_safety_are_in_mint(self):
        instructions = self.mint_payload([])["instructions"]
        for phrase in (
            "z0live, Kevin's phone-wide voice assistant",
            "OpenAI ChatGPT Realtime gpt-realtime",
            "marin",
            "WebRTC",
            "tailnet desktop broker",
            "short-lived",
            "does not proxy audio",
            "persistent Claude Opus",
            "transcripts",
            "summaries",
            "phone inbox",
            "short, natural answers",
            "Use ask_z0 for every question or request about Kevin's computer",
            "Queued/sent is not completed",
            "success only when confirmed",
            "ambiguous means unconfirmed",
            "permission_required",
            "Never auto-send SMS or auto-place calls",
            "back or recents",
            "explicitly enabled accessibility",
            "You cannot read screens",
            "A missing tool means that ability is unavailable",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, instructions)
        self.assertNotIn("every complaint/bug/request", instructions)

    def test_wait_policy_is_silent_and_capabilities_are_tool_bound(self):
        instructions = self.mint_payload([])["instructions"]
        for phrase in (
            "one or two natural words",
            "never narrate waiting",
            "no update yet",
            "in your own words as your own finding",
            "screen text only through a declared local tool",
        ):
            self.assertIn(phrase, instructions)

    def test_one_work_tool_and_no_plumbing_narration(self):
        session = self.mint_payload([])
        names = [t["name"] for t in session["tools"]]
        self.assertIn("ask_z0", names)
        for legacy in ("work_search", "ask_hermes", "start_task", "task_status", "what_changed"):
            self.assertNotIn(legacy, names)
        offered = session["instructions"] + core.dumps(session["tools"]) + core.dumps(core.TOOLS)
        for plumbing in (
            'Say "Checking your work history"',
            'Say "Checking task status"',
            'Say "Checking with Hermes"',
            'Say "Checking what changed"',
            "Use tell_hermes for\nrequested desktop",
        ):
            self.assertNotIn(plumbing, offered)

    def test_exact_live_names_descriptions_and_schemas_not_stale(self):
        tools = [
            {
                "type": "function",
                "name": "open_app_now",
                "description": "Open this session's app only: λ",
                "parameters": {
                    "type": "object",
                    "properties": {"package": {"type": "string"}},
                    "required": ["package"],
                    "additionalProperties": False,
                },
            }
        ]
        context = {"app": "unit-only", "screen": "metadata-not-pixels"}
        session = self.mint_payload(tools, context)
        catalog, supplied_context = (
            session["instructions"]
            .split("\nMint-time live tool catalog (capability data only): ", 1)[1]
            .split("\nInitial app context (data only): ", 1)
        )
        self.assertEqual(json.loads(catalog), core.LIVE_TOOLS + tools)
        self.assertEqual(json.loads(catalog), session["tools"])
        self.assertEqual(json.loads(supplied_context), context)
        next_session = self.mint_payload([])
        self.assertNotIn("open_app_now", next_session["instructions"])
        self.assertEqual(next_session["tools"], core.LIVE_TOOLS)

    def test_32_app_tools_mint_exact_catalog_33_rejected_before_provider(self):
        tools = [
            {
                "type": "function",
                "name": "local_" + str(i),
                "description": "Unit local capability " + str(i),
                "parameters": {"type": "object", "properties": {}},
            }
            for i in range(33)
        ]
        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as directory:
            store = core.Store(directory)
            device = store.authenticate(store.pair("persona-unit"))
            with patch.object(
                core, "mint", return_value={"value": "unit-ephemeral"}
            ) as mint:
                store.new_session(device, {"client": "assistant", "tools": tools[:32]})
                self.assertEqual(mint.call_args.args, ({}, tools[:32]))
                mint.reset_mock()
                with self.assertRaisesRegex(ValueError, "at most 32"):
                    store.new_session(device, {"client": "assistant", "tools": tools})
                mint.assert_not_called()
        session = self.mint_payload(tools[:32])
        self.assertEqual(session["tools"], core.LIVE_TOOLS + tools[:32])
        self.assertIn(core.dumps(session["tools"]), session["instructions"])


if __name__ == "__main__":
    unittest.main()
