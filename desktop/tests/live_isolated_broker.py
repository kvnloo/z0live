#!/usr/bin/env python3
"""Start an isolated loopback broker for opt-in live audio qualification.

Usage: Z0LIVE_DATA=/private/dir Z0LIVE_PORT=18791 python3 desktop/tests/live_isolated_broker.py
It never touches the deployed data directory: it copies only the native Hermes API key,
pre-marks Hermes seeding as done, turns off notes to Hermes, and routes delegations and
voice-stack runs to qualification-only Hermes sessions.
"""

import os
from pathlib import Path
import shutil
import sys

ROOT = Path(__file__).resolve().parents[2]
DEPLOYED = Path(os.environ.get("Z0LIVE_DEPLOYED_DATA", str(ROOT.parents[1] / "data/z0live")))
data = Path(os.environ["Z0LIVE_DATA"]).resolve()
if data == DEPLOYED.resolve():
    sys.exit("refusing to run a qualification broker on the deployed data directory")
os.environ.setdefault("Z0LIVE_PORT", "18791")
# The qualification page is served from the broker's own loopback origin.
os.environ.setdefault("Z0LIVE_CORS_ORIGINS", "http://127.0.0.1:" + os.environ["Z0LIVE_PORT"])
os.environ["Z0LIVE_HERMES_NOTES"] = "off"
os.environ.setdefault("Z0LIVE_DELEGATE_SESSION", "z0live-voice-qualification")
data.mkdir(parents=True, exist_ok=True, mode=0o700)
key = data / ".hermes-api-key"
if not key.exists():
    shutil.copyfile(DEPLOYED / ".hermes-api-key", key)
    key.chmod(0o600)
sys.path.insert(0, str(ROOT / "desktop"))
from z0live_desktop.core import Store, dumps  # noqa: E402

store = Store(data)
with store.connect() as db:
    for name in ("initialized", "work_tools_v1"):
        db.execute("INSERT OR IGNORE INTO meta VALUES(?, 'true')", (name,))
    db.execute(
        "INSERT OR REPLACE INTO meta VALUES('hermes_session', ?)",
        (dumps("z0live-stack-qualification"),),
    )
from z0live_desktop.server import serve  # noqa: E402

serve()
