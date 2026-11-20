#!/usr/bin/env python3
"""Native APIServerAdapter only: no GatewayRunner, messaging or host schedulers."""

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path
import re
import secrets
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("Z0LIVE_DATA", str(ROOT.parents[1] / "data/z0live")))
DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
parser = argparse.ArgumentParser()
parser.add_argument("--native", action="store_true", help=argparse.SUPPRESS)
parser.add_argument("--profile", help="Run an isolated guest host for this Hermes profile")
parser.add_argument("--port", type=int)
args = parser.parse_args()
PORT = args.port if args.port is not None else (18644 if args.profile else 8644)
if not 1 <= PORT <= 65535 or (args.profile and PORT in (8644, 8645)):
    parser.error("guest host needs a separate port")
KEY = DATA / ".hermes-api-key"
if args.profile:
    if not re.fullmatch(r"[a-zA-Z0-9_-]+", args.profile) or args.profile == "default":
        parser.error("expected a named Hermes profile")
    home = Path.home() / ".hermes/profiles" / args.profile
    if not home.is_dir():
        parser.error("Hermes profile does not exist")
    os.environ["HERMES_HOME"] = str(home)
    # Don't inherit a different CLI profile selector from the launcher.
    os.environ.pop("HERMES_PROFILE", None)
    KEY = Path.home() / ".config/z0live/guest-hosts" / (args.profile + ".key")
    KEY.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
if not args.native:
    try:
        fd = os.open(KEY, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    KEY.chmod(0o600)
    os.environ.update(
        API_SERVER_ENABLED="true",
        API_SERVER_HOST="127.0.0.1",
        API_SERVER_PORT=str(PORT),
        API_SERVER_KEY=KEY.read_text().strip(),
        TERMINAL_CWD=str(ROOT),
        HERMES_DASHBOARD="0",
    )
    os.chdir(ROOT)
    runtime = json.loads(
        subprocess.check_output(
            [os.environ.get("Z0LIVE_HERMES_BIN", str(Path.home() / ".local/bin/hermes")), "--print-runtime-command"],
            text=True,
        )
    )
    launch = ["--profile", args.profile] if args.profile else []
    os.execv(runtime[0], [runtime[0], "-I", str(Path(__file__).resolve()), "--native",
                         "--port", str(PORT), *launch])

sys.path.insert(
    0, os.environ.get("Z0LIVE_HERMES_SOURCE", "/workspace/hermes-home/hermes-agent")
)
import hermes_bootstrap  # noqa: E402,F401 - initializes the installed Hermes runtime
from gateway.config import PlatformConfig  # noqa: E402
from gateway.platforms.api_server import APIServerAdapter  # noqa: E402


async def main():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
    sys.path.insert(0, str(ROOT / "desktop"))
    from native_policy import PinnedVoiceRuns

    class VoiceAPIServer(PinnedVoiceRuns, APIServerAdapter):
        pass

    adapter = VoiceAPIServer(
        PlatformConfig(
            enabled=True,
            extra={
                "host": "127.0.0.1",
                "port": PORT,
                "key": KEY.read_text().strip(),
                "model_name": "z0live",
                "cors_origins": [],
                "direct_model_requests": True,
            },
        )
    )
    stop = asyncio.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        asyncio.get_running_loop().add_signal_handler(sig, stop.set)
    if not await adapter.connect():
        await adapter.disconnect()
        raise RuntimeError("native Hermes adapter failed to start")
    logging.info(
        "z0live API-only host pid=%s cwd=%s profile=%s port=%s; no messaging, cron or kanban",
        os.getpid(), ROOT, args.profile or "inherited", PORT,
    )
    sys.path.insert(0, str(ROOT / "desktop"))
    from liaison_host import serve_liaison

    liaison = await serve_liaison(KEY.read_text().strip()) if not args.profile and PORT == 8644 else None
    try:
        await stop.wait()
    finally:
        if liaison:
            await liaison.cleanup()
        tasks = list(getattr(adapter, "_active_run_tasks", {}).values())
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=120)
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
        await adapter.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
