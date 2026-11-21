import asyncio
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

import mobile_live as live
import primary_host


class PrimaryHostTests(unittest.IsolatedAsyncioTestCase):
    async def test_streams_first_sentence_before_completion_and_preserves_call_scope(self):
        requests = []
        first = asyncio.Event()
        spoken = []

        async def respond(request):
            requests.append((await request.json(), request.headers.get('X-Hermes-Session-Key')))
            response = web.StreamResponse(headers={'Content-Type': 'text/event-stream'})
            await response.prepare(request)
            delta = {'type': 'response.output_text.delta', 'delta': 'First sentence. Second'}
            await response.write(f'data: {json.dumps(delta)}\n\n'.encode())
            await asyncio.wait_for(first.wait(), 2)
            final = {'type': 'response.completed', 'response': {'status': 'completed', 'output': [
                {'type': 'message', 'role': 'assistant', 'content': [
                    {'type': 'output_text', 'text': 'First sentence. Second sentence.'}]}]}}
            await response.write(f'data: {json.dumps(final)}\n\n'.encode())
            return response

        def speak(text):
            spoken.append(text)
            first.set()

        app = web.Application()
        app.router.add_post('/p/poteto/v1/responses', respond)
        host = primary_host.PrimaryHost('call-one')
        host.brief = 'Pinned call context'
        async with TestServer(app) as server, ClientSession() as http:
            with patch.object(primary_host, 'PROFILE', 'poteto'), patch.object(primary_host, 'api_key', return_value='fixture'), \
                 patch.dict(os.environ, {'Z0LIVE_PRIMARY_HOST_PORT': str(server.port)}):
                for ask, agent in [('first', host), ('follow-up', host), ('new call', primary_host.PrimaryHost('two'))]:
                    first.clear()
                    self.assertEqual(await agent.ask(http, ask, 3, speak), 'First sentence. Second sentence.')
        self.assertEqual(spoken, ['First sentence.'] * 3)
        key = 'z0live-primary-call-one'
        self.assertEqual([(body['input'], body['conversation'], header) for body, header in requests],
                         [('first', key, key), ('follow-up', key, key),
                          ('new call', 'z0live-primary-two', 'z0live-primary-two')])
        self.assertIn('Pinned call context', requests[0][0]['instructions'])
        self.assertNotIn('Pinned call context', requests[2][0]['instructions'])

    async def test_lost_legacy_reply_never_falls_back_to_oneshot(self):
        call = live.Call(live.Manager(None))
        self.addAsyncCleanup(call.close, 'test')
        call.chat = 'long-chat'
        with patch.object(live.chat_continuity, 'ask_chat', AsyncMock(side_effect=RuntimeError)), \
             patch.object(live, 'run_hermes') as replay, self.assertRaises(RuntimeError):
            await call.ask_hermes('d1', 'Perform the action once')
        replay.assert_not_called()
