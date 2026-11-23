"""Minimal process environment for the adapter's Claude CLI children."""
import os
from pathlib import Path


def child_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME") or str(Path.home()),
        "FAST_LANE_CHILD": "1",
    }
    config_dir = os.environ.get("Z0LIVE_CLAUDE_CONFIG_DIR")
    if config_dir:
        env["CLAUDE_CONFIG_DIR"] = os.path.expanduser(config_dir)
    return env
