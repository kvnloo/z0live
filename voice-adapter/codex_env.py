"""Minimal process environment for the adapter's `codex app-server` child (same idea as claude_env.py)."""
import os
from pathlib import Path


def codex_env() -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", os.defpath),
        "HOME": os.environ.get("HOME") or str(Path.home()),
    }
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        env["CODEX_HOME"] = codex_home
    return env
