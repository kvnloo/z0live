"""The plugin reaches only the matching live call and sends stock text to the voice leg."""
import asyncio
import importlib.util
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from aiohttp.test_utils import TestClient, TestServer

import live_progress
from mobile_live import Protocol, make_app

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("progress_plugin", ROOT / "__init__.py")
assert SPEC is not None and SPEC.loader is not None
plugin = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(plugin)
TOKEN = "synthetic-test-token"
LINE = "I'm still checking the details."


class ProgressTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.enterContext(patch.dict(os.environ, {plugin.PROGRESS_TOKEN_ENV: TOKEN}))  # the adapter's copy
        # The plugin reads its copy through Hermes's profile-scoped get_secret, which only exists inside Hermes.
        self.enterContext(patch.object(plugin, "_secret", {plugin.PROGRESS_TOKEN_ENV: TOKEN}.get))
        app = make_app()
        app.cleanup_ctx.clear()
        self.call = SimpleNamespace(chat="test-chat", closed=False, provider_ready=True,
                                    provider_dc=SimpleNamespace(readyState="open"), leg=None,
                                    protocol=Protocol(), send=Mock(), trace=SimpleNamespace(turn_id=1))
        self.calls = {"call": self.call}
        app["manager"] = SimpleNamespace(calls=self.calls)
        self.client = await self.enterAsyncContext(TestClient(TestServer(app, host="127.0.0.1")))
        self.enterContext(patch.object(plugin, "PROGRESS_PORT", self.client.port))
        self.say = plugin.make_say([LINE])

    async def test_plugin_to_gpt_speakable_event(self):
        result = await asyncio.to_thread(self.say, {"text": LINE}, session_id="test-chat")
        self.assertEqual(json.loads(result), {"ok": True})
        self.call.send.assert_called_once_with(
            {"type": "session.context.append", "channel": "speakable",
             "content": [{"type": "input_text", "text": '"Agent Final Message":\n\n' + LINE}]},
            to_provider=True)

    async def test_grok_progress_requests_response_without_completing_a_delegation(self):
        self.call.leg = SimpleNamespace(ready=True, ws=SimpleNamespace(closed=False), note=AsyncMock(), raw=AsyncMock())
        result = await asyncio.to_thread(self.say, {"text": LINE}, session_id="test-chat")
        self.assertEqual(json.loads(result), {"ok": True})
        self.call.leg.note.assert_awaited_once_with("Brief progress update from Hermes. Say this aloud: " + LINE, 1)
        self.call.leg.raw.assert_awaited_once_with({"type": "response.create"})
        self.call.send.assert_not_called()

    async def test_rejects_unauthorized_unready_foreign_ambiguous_and_private_input(self):
        async def post(text=LINE, sid="test-chat", headers=None):
            async with self.client.post(live_progress.PATH, json={"session_id": sid, "text": text},
                                        headers=headers) as response:
                return response.status

        self.assertEqual(await post(), 401)
        auth = {"Authorization": f"Bearer {TOKEN}"}
        self.assertEqual(await post(headers=auth, text="private fixture"), 400)
        self.assertEqual(await post(headers=auth, sid="foreign-chat"), 409)
        self.call.provider_ready = False
        self.assertEqual(await post(headers=auth), 409)
        self.call.provider_ready = True
        self.calls["other"] = self.call
        self.assertEqual(await post(headers=auth), 409)
        self.call.send.assert_not_called()
