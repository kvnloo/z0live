"""xai_secret.py takes the xAI key only from its service env, never from Hermes's OAuth login."""
import io
import json
import os
from pathlib import Path
import runpy
import sys
import types
import unittest
from contextlib import redirect_stdout
from unittest.mock import MagicMock, patch

SCRIPT = str(Path(__file__).with_name("xai_secret.py"))


class _NoHermesAuth(types.ModuleType):
    def __getattr__(self, name):
        raise AssertionError("xai_secret.py must not touch hermes_cli.auth")


class XaiSecretTests(unittest.TestCase):
    def _run(self, env):
        httpx = types.ModuleType("httpx")
        response = MagicMock(status_code=200)
        response.json.return_value = {"value": "ephemeral", "expires_at": 123}
        httpx.post = MagicMock(return_value=response)
        mods = {"hermes_bootstrap": types.ModuleType("hermes_bootstrap"), "httpx": httpx,
                "hermes_cli": types.ModuleType("hermes_cli"), "hermes_cli.auth": _NoHermesAuth("hermes_cli.auth")}
        out = io.StringIO()
        with patch.dict(sys.modules, mods), patch.dict(os.environ, env, clear=True), redirect_stdout(out), \
             patch.object(sys, "path", list(sys.path)):
            runpy.run_path(SCRIPT, run_name="__main__")
        return httpx.post, out.getvalue()

    def test_service_env_key_is_used(self):
        post, out = self._run({"XAI_API_KEY": "  xai-service-key  "})
        self.assertEqual(post.call_args.kwargs["headers"], {"Authorization": "Bearer xai-service-key"})
        self.assertEqual(json.loads(out), {"value": "ephemeral", "expires_at": 123})

    def test_missing_key_exits_without_oauth_fallback(self):
        with self.assertRaises(SystemExit) as caught:
            self._run({})
        self.assertIn("XAI_API_KEY", str(caught.exception.code))

    def test_source_has_no_oauth_fallback(self):
        source = Path(SCRIPT).read_text()
        self.assertNotIn("hermes_cli.auth", source)
        self.assertNotIn("resolve_xai_oauth_runtime_credentials", source)
