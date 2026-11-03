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


def _try_shared_runtime_endpoint(sid: str, home: str):
    """Return a GatewayEndpoint for a GUI/dashboard session via the shared_runtime_url handshake,
    or None when the owner does not advertise one. Raises ValueError with a user-facing message
    when the owner IS found but the handshake fails or the URL is invalid.

    This is the z0live-side adapter for the Hermes shared_runtime_url mechanism
    (hermes_cli/shared_session_attach.py). It requires two Hermes-side changes to work:
    1. tui_gateway/session_lifecycle.py: _lease_metadata() must include
       shared_runtime_url=http://127.0.0.1:{port} when surface='gui'.
    2. hermes_cli/web_server_dashboard.py (or a new router): implement
       GET /api/session-attach?session_id=...&lease_id=...&profile_home=...
       returning {session_id:..., lease_id:..., profile_home:...,
                  websocket_url: ws://127.0.0.1:{port}/api/ws}.
    Once those land, this adapter discovers the dashboard's WS URL, connects to it,
    and prompt.submit works exactly like a CLI session.
    """
    try:
        from hermes_cli.shared_session_attach import discover_attach_url
        ws_url = discover_attach_url(sid, registry_home=home)
        if ws_url is None:
            return None  # session not in active_sessions.json at all
        # discover_attach_url returns an authenticated ws:// URL; wrap it in a GatewayEndpoint.
        from hermes_cli.gateway_client import GatewayEndpoint
        return GatewayEndpoint(ws_url=ws_url)
    except ValueError:
        # Owner exists but doesn't advertise shared_runtime_url yet (Hermes-side not deployed).
        return None
    except Exception:
        return None


async def deliver(request):
    home = os.environ["HERMES_HOME"]
    found = discover_gateway_endpoint(home)
    shared_endpoint = None
    if not attachable(found):
        # Fallback: GUI sessions owned by hermes dashboard use the shared_runtime_url handshake.
        # This path activates once Hermes implements: _lease_metadata(shared_runtime_url=...) and
        # GET /api/session-attach in the dashboard router.
        shared_endpoint = _try_shared_runtime_endpoint(sid, home)
        if shared_endpoint is None:
            return {ok: False, error: gateway_unavailable, state: found.state,
                    reason: found.reason_code}
    endpoint = shared_endpoint or found.endpoint
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
        reply = {}
        if request.get("wait") and receipt.get("admission_id") and receipt.get("status") in {"queued", "started", "terminal"}:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            from z0live_desktop.gateway_reply import wait_reply
            reply = await wait_reply(client, snap["session_id"], receipt["admission_id"], request["wait"])
        return {**out, "ok": receipt.get("status") in {"queued", "started", "terminal"},
                "mode": "next_turn", "status": receipt.get("status"),
                "admission_id": receipt.get("admission_id"), **reply}


def main():
    request = json.loads(sys.stdin.read())
    try:
        result = asyncio.run(asyncio.wait_for(deliver(request), 30 + min(300, max(0, request.get("wait", 0)))))
    except GatewayClientError as exc:
        result = {"ok": False, "error": "gateway_refused", "detail": str(exc)[:300]}
    except (OSError, TimeoutError, ValueError, KeyError) as exc:
        result = {"ok": False, "error": type(exc).__name__, "detail": str(exc)[:300]}
    print(json.dumps(result))


if __name__ == "__main__":
    main()
