"""Exercise endpoint defaults in fresh interpreters without deployment settings."""
import os
from pathlib import Path
import subprocess
import sys
import unittest


class HostConfigTests(unittest.TestCase):
    def test_unset_base_and_router_skip_and_reject_sr(self):
        desktop = Path(__file__).resolve().parents[1]
        env = {**os.environ, "PYTHONPATH": str(desktop)}
        for key in ("Z0LIVE_BASE_URL", "Z0LIVE_SR_URL", "Z0LIVE_SYSTEM_MODEL_URL"):
            env.pop(key, None)
        subprocess.run([sys.executable, "-c", """
import copy
from z0live_desktop import core, lanes, lanes_intent, phone_system

assert not core.BASE and not lanes.SR_BASE
assert phone_system.CHOOSER_CHAIN == phone_system.WATCH_CHAIN == [phone_system.LOCAL_ENDPOINT]
config = lanes_intent.parse("two fast, two small")
assert all(b["route"] != "vllm-sr" for b in config["lanes"]["smol"]["models"])

try:
    lanes_intent.parse("three small")
except ValueError as exc:
    assert "only 2 small lanes are available" in str(exc), exc
else:
    raise AssertionError("intent parser offered an unconfigured router")

config = copy.deepcopy(config)
config["lanes"]["smol"]["models"].append(lanes_intent.model_binding("vllm", "small"))
assert any("vllm-sr requires Z0LIVE_SR_URL" in msg for msg in lanes.validate(config))
"""], env=env, check=True)
