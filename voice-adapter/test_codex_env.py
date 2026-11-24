import os
import unittest
from unittest.mock import MagicMock, patch

import mobile_live


class CodexEnvTests(unittest.TestCase):
    def _spawned_env(self, env):
        proc = MagicMock()
        with patch.dict(os.environ, env, clear=True), \
             patch.object(mobile_live.subprocess, "Popen", return_value=proc) as popen, \
             patch.object(mobile_live.selectors, "DefaultSelector") as selector, \
             patch.object(mobile_live.shutil, "which", return_value="/bin/codex"):
            selector.return_value.select.return_value = []  # no reply: refresh times out at once
            with self.assertRaises(RuntimeError):
                mobile_live.refresh_codex_login()
        self.assertEqual(popen.call_args.args[0], ["/bin/codex", "app-server", "--listen", "stdio://"])
        return popen.call_args.kwargs["env"]

    def test_app_server_gets_only_the_allowlist(self):
        env = {"PATH": "/bin", "HOME": "/home/u", "CODEX_HOME": "/home/u/.codex-alt",
               "OPENAI_API_KEY": "sk-should-not-pass", "CODEX_API_KEY": "should-not-pass",
               "UNRELATED_SERVICE_TOKEN": "ghp_should_not_pass", "XAI_API_KEY": "xai-should-not-pass"}
        child = self._spawned_env(env)
        self.assertEqual(child, {"PATH": "/bin", "HOME": "/home/u", "CODEX_HOME": "/home/u/.codex-alt"})
        self.assertNotIn("UNRELATED_SERVICE_TOKEN", child)

    def test_codex_home_only_when_set(self):
        child = self._spawned_env({"PATH": "/bin", "HOME": "/home/u", "SOME_SECRET": "x"})
        self.assertEqual(child, {"PATH": "/bin", "HOME": "/home/u"})
