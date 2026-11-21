#!/usr/bin/env python3
"""Create a disposable pinned target through the existing gateway; never start a gateway."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import uuid

if "--native" not in sys.argv:
    runtime = json.loads(subprocess.check_output([str(Path.home() / ".local/bin/hermes"), "--print-runtime-command"], text=True))
    os.execv(runtime[0], [runtime[0], "-I", str(Path(__file__).resolve()), "--native", sys.argv[1]])
sys.path.insert(0, os.environ.get("Z0LIVE_HERMES_SOURCE", "/workspace/hermes-home/hermes-agent"))
import hermes_bootstrap  # noqa: E402,F401
from hermes_cli.gateway_client import connect_gateway  # noqa: E402
from hermes_cli.gateway_runtime import attachable, discover_gateway_endpoint  # noqa: E402


async def main():
    home = os.environ["HERMES_HOME"]
    found = discover_gateway_endpoint(home)
    if not attachable(found):
        raise RuntimeError("existing gateway unavailable; no fallback")
    async with connect_gateway(endpoint=found.endpoint) as client:
        desc = await client.rpc("runtime.describe")
        required = {"model", "provider", "title", "cwd"}
        contract = desc.get("session_create", {})
        if not required.issubset(contract.get("parameters", [])) or "oneshot" not in contract.get("sources", []):
            raise RuntimeError("gateway does not support exact qualification creation")
        snap = await client.rpc("session.create", request_id=uuid.uuid4().hex, source="oneshot", model="gpt-6-luna", provider="openai-codex", title="[test] Dash native voice qualification", cwd=str(Path(__file__).resolve().parents[2]))
        out = Path(sys.argv[-1])
        out.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as stream:
            json.dump({"profile": "clean", "session": snap["stored_session_id"]}, stream)
        print(json.dumps({"qualification_target_created": True, "exact_binding_written_privately": True}))


asyncio.run(asyncio.wait_for(main(), 20))
