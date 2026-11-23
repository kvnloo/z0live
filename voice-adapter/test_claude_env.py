import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import fable_lanes
import mobile_live


class ClaudeEnvTests(unittest.IsolatedAsyncioTestCase):
    async def test_both_claude_processes_receive_exactly_the_allowlist(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            env = {
                "PATH": "/bin",
                "HOME": directory,
                "Z0LIVE_CLAUDE_CONFIG_DIR": "~/configured-claude",
                "CLAUDE_CONFIG_DIR": "/ignored",
                "FAST_LANE_CHILD": "ignored",
                "GROQ_API_KEY": "service-key",
                "UNRELATED_SECRET": "do not forward",
            }
            expected = {"PATH": "/bin", "HOME": directory, "FAST_LANE_CHILD": "1",
                        "CLAUDE_CONFIG_DIR": str(home / "configured-claude")}
            with patch.dict(os.environ, env, clear=True), \
                 patch.object(fable_lanes.subprocess, "run", return_value=subprocess.CompletedProcess(
                     [], 0, stdout='{"result":"ready"}')) as run, \
                 patch.object(mobile_live.asyncio, "create_subprocess_exec", new_callable=AsyncMock) as spawn:
                fable_lanes.run("history", "test question", home, 1, [], fable=False)
                haiku = mobile_live.WarmHaiku("test system")
                with patch.object(haiku, "ask", new_callable=AsyncMock):
                    await haiku.start()
                self.assertEqual(run.call_args.kwargs["env"], expected)
                self.assertEqual(spawn.call_args.kwargs["env"], expected)

                del os.environ["Z0LIVE_CLAUDE_CONFIG_DIR"]
                fable_lanes.run("history", "test question", home, 1, [], fable=False)
                del expected["CLAUDE_CONFIG_DIR"]
                self.assertEqual(run.call_args.kwargs["env"], expected)
