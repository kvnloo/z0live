#!/usr/bin/env python3
"""Deliver one message into a live Hermes session through the gateway that owns it.

Runs under the installed Hermes runtime (same re-exec as hermes_host.py) and speaks the
gateway's own client protocol, exactly as the CLI/TUI viewers and Dash's ACP projection do:

  discover_gateway_endpoint (read-only, never starts a gateway)
  -> session.resume (attach as a viewer, no history)
  -> running turn: session.steer (lands in the turn's next tool result)
     idle, or steer refused: prompt.submit (durable FIFO, runs as the next turn)

`/v1/runs` is not used: the API server refuses sessions another surface owns (409
permission_denied), and on a multiplexed listener a profile without its own API key is
unreachable, while the unprefixed route silently lands in the *default* profile.

stdin:  {"session_id": "...", "text": "...", "submission_id": "...", "mode": "auto|next_turn"}
stdout: one JSON object. Never prints credentials or tickets.
"""

import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys

if "--native" not in sys.argv:
    runtime = json.loads(
        subprocess.check_output(
            [str(Path.home() / ".local/bin/hermes"), "--print-runtime-command"], text=True
        )
    )
    os.execv(runtime[0], [runtime[0], "-I", str(Path(__file__).resolve()), "--native"])

sys.path.insert(0, os.environ.get("Z0LIVE_HERMES_SOURCE", "/workspace/hermes-home/hermes-agent"))
import hermes_bootstrap  # noqa: E402,F401 - initializes the installed Hermes runtime
from hermes_cli.gateway_client import GatewayClientError, connect_gateway  # noqa: E402
from hermes_cli.gateway_runtime import attachable, discover_gateway_endpoint  # noqa: E402


async def deliver(request):
    home = os.environ["HERMES_HOME"]
    found = discover_gateway_endpoint(home)
    if not attachable(found):
        return {"ok": False, "error": "gateway_unavailable", "state": found.state,
                "reason": found.reason_code}
    endpoint = found.endpoint
    assert endpoint is not None  # attachable() guarantees a ready endpoint
    sid, text = request["session_id"], request["text"]
    async with connect_gateway(endpoint=endpoint) as client:
        snap = await client.rpc("session.resume", session_id=sid, omit_messages=True,
                                defer_history=True)
        info = snap.get("info") or {}
        out = {"session_id": snap.get("stored_session_id") or sid, "title": info.get("title") or "",
               "gateway": endpoint.api_origin.rsplit(":", 1)[-1]}
        if snap.get("running") and request.get("mode", "auto") == "auto":
            try:
                steer = await client.rpc("session.steer", session_id=snap["session_id"], text=text,
                                         execution_generation=snap["execution_generation"])
            except GatewayClientError as exc:
                steer = {"status": "rejected", "error": str(exc)[:200]}
            if steer.get("status") == "queued":
                return {**out, "ok": True, "mode": "steer", "status": "queued",
                        "execution_generation": steer.get("execution_generation")}
            out["steer_refused"] = steer.get("status") or "rejected"
        receipt = await client.rpc("prompt.submit", session_id=snap["session_id"], text=text,
                                   submission_id=request["submission_id"])
        return {**out, "ok": receipt.get("status") in {"queued", "started", "terminal"},
                "mode": "next_turn", "status": receipt.get("status"),
                "admission_id": receipt.get("admission_id")}


def main():
    request = json.loads(sys.stdin.read())
    try:
        result = asyncio.run(asyncio.wait_for(deliver(request), 30))
    except GatewayClientError as exc:
        result = {"ok": False, "error": "gateway_refused", "detail": str(exc)[:300]}
    except (OSError, TimeoutError, ValueError, KeyError) as exc:
        result = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:300]}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
