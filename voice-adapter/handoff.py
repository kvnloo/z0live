"""Switch a Hermes Live Voice call to z0live on the phone when Kevin leaves the call screen.

Mode (HERMES_VOICE_HANDOFF): "auto" (default) switches on every phone-side close, because the
Hermes app has no back button: hanging up is how Kevin leaves the call screen (11-16 call: closed
with Hermes still in front). "off" never switches (opt-out). "always" is kept as an alias of auto.
The handoff record is a small JSON file z0live reads when it opens the next call.
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

HERMES_APP = "ai.nousresearch.hermes.mobile"
Z0LIVE_START = ["am", "start", "-a", "z0live.action.START",
                "-n", "app.zer0.z0live/app.lawnchair.zer0.voice.z0live.Z0LiveCommandActivity"]
PHONE_CLOSES = ("client_closed", "transport_closed")
PATH = Path(os.environ.get("Z0LIVE_HANDOFF_FILE",
                           "/mnt/zer0models/workspace/zer0/data/z0live/handoff.json"))


def mode() -> str:
    value = os.environ.get("HERMES_VOICE_HANDOFF", "auto").strip().lower()
    return value if value in ("auto", "always", "off") else "auto"


def decide(reason: str, front: str | None, how: str) -> bool:
    """True when this close should continue on z0live. Unknown foreground in auto mode = stay ended."""
    if how == "off" or reason not in PHONE_CLOSES:
        return False
    return True


def adb(serial: str, *args: str, timeout: float = 4.0) -> str:
    out = subprocess.run(["adb", "-s", serial, "shell", *args], capture_output=True, text=True,
                         timeout=timeout, check=False)
    return out.stdout


def foreground(serial: str) -> str | None:
    """Package of the resumed activity on the phone, None when unreadable."""
    try:
        text = adb(serial, "dumpsys", "activity", "activities")
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in text.splitlines():
        if "mResumedActivity" in line or "topResumedActivity" in line:
            for word in line.split():
                if "/" in word and "." in word:
                    return word.split("/")[0].rstrip("}")
    return None


def record(chat: str, profile: str, exchanges: list[tuple[str, str]], call_id: str,
           path: Path = PATH) -> dict:
    data = {"at": time.time(), "from": "hermes-mobile-live", "call": call_id, "chat": chat,
            "profile": profile, "exchanges": [[r, t[-600:]] for r, t in exchanges[-8:]]}
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data))
    tmp.replace(path)
    return data


def start_z0live(serial: str) -> bool:
    try:
        return "Error" not in adb(serial, *Z0LIVE_START, timeout=6.0)
    except (OSError, subprocess.TimeoutExpired):
        return False
