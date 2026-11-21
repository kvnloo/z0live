import importlib.util
import unittest
from unittest.mock import patch
from aiohttp import ClientSession, web
from aiohttp.test_utils import TestServer

from pathlib import Path

spec = importlib.util.spec_from_file_location('proxy', Path(__file__).with_name('dashboard_proxy.py'))
proxy = importlib.util.module_from_spec(spec)
spec.loader.exec_module(proxy)

class RoutingTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_exact_voice_routes_are_intercepted_and_auth_is_preserved(self):
        async def dashboard(request):
            return web.json_response({'target': 'dashboard'})
        async def voice(request):
            return web.json_response({'target': 'voice', 'auth': request.headers.get('Authorization'),
                'query': dict(request.query), 'body': await request.json() if request.method == 'POST' else None})
        apps = []
        for handler in (dashboard, voice):
            app = web.Application();app.router.add_route('*', '/{tail:.*}', handler)
            server = TestServer(app);await server.start_server();apps.append(server)
        http = ClientSession()
        app = web.Application();app['client'] = http;app.router.add_route('*', '/{tail:.*}', proxy.http_proxy)
        server = TestServer(app);await server.start_server()
        try:
            with patch.object(proxy, 'UPSTREAM_HTTP', str(apps[0].make_url('/')).rstrip('/')), patch.dict(
                    proxy.os.environ, {'HERMES_MOBILE_VOICE_UPSTREAM': str(apps[1].make_url('/')).rstrip('/')}):
                async with http.post(server.make_url('/api/audio/voice-live/session?profile=clean'),
                        json={'sdp': 'fixture'}, headers={'Authorization': 'Bearer fixture'}) as response:
                    result = await response.json()
                self.assertEqual(result, {'target': 'voice', 'auth': 'Bearer fixture',
                    'query': {'profile': 'clean'}, 'body': {'sdp': 'fixture'}})
                for path in ('/api/health', '/api/audio/voice-live/session-extra', '/api/auth/me'):
                    async with http.get(server.make_url(path)) as response:
                        self.assertEqual((await response.json())['target'], 'dashboard')
        finally:
            await server.close();await http.close()
            for s in apps:await s.close()

if __name__ == '__main__':unittest.main()
