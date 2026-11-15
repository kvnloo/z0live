#!/usr/bin/env python3
"""Native APIServerAdapter only: no GatewayRunner, messaging or host schedulers."""

import asyncio
import json
import logging
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
DATA = Path(os.environ.get("Z0LIVE_DATA", str(ROOT.parents[1] / "data/z0live")))
DATA.mkdir(parents=True, exist_ok=True, mode=0o700)
KEY = DATA / ".hermes-api-key"
if "--native" not in sys.argv:
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
        API_SERVER_PORT="8644",
        API_SERVER_KEY=KEY.read_text().strip(),
        TERMINAL_CWD=str(ROOT),
        HERMES_DASHBOARD="0",
    )
    os.chdir(ROOT)
    runtime = json.loads(
        subprocess.check_output(
            [str(Path.home() / ".local/bin/hermes"), "--print-runtime-command"],
            text=True,
        )
    )
    os.execv(runtime[0], [runtime[0], "-I", str(Path(__file__).resolve()), "--native"])

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
                "port": 8644,
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
        "z0live API-only host pid=%s cwd=%s; no messaging, cron or kanban",
        os.getpid(),
        ROOT,
    )
    sys.path.insert(0, str(ROOT / "desktop"))
    from liaison_host import serve_liaison

    liaison = await serve_liaison(KEY.read_text().strip())
    try:
        await stop.wait()
    finally:
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
