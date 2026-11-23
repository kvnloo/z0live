#!/usr/bin/env python3
"""Tailnet-only HTTP/WebSocket proxy for the loopback Hermes dashboard."""

from __future__ import annotations

import asyncio
import os
from aiohttp import ClientConnectionError, ClientSession, ClientTimeout, WSMsgType, WSServerHandshakeError, web

from voice_slots import Slots

VOICE_PATHS = {
    "/api/audio/voice-live/status",
    "/api/audio/voice-live/session",
    "/api/audio/voice-live/diagnostics",
}
VOICE_SLOTS = web.AppKey("voice_slots", Slots)

# HERMES_DASHBOARD_UPSTREAM: where the real dashboard listens. The desktop host binds it to loopback; the Studio
# binds it to its tailnet address, so this proxy sits on another port there.
UPSTREAM_HOST = os.environ.get("HERMES_DASHBOARD_UPSTREAM", "127.0.0.1:9119")
UPSTREAM_HTTP = f"http://{UPSTREAM_HOST}"
UPSTREAM_WS = f"ws://{UPSTREAM_HOST}"
# HERMES_PROXY_FORWARDED_PROTO: the scheme the app really uses. The dashboard trusts loopback proxies and
# marks session cookies Secure when this says https; an http:// client (the Studio front door) then never
# returns them and every /api/auth/ws-ticket fails with no_cookie. The desktop host sits behind tailscale serve TLS.
FORWARDED_PROTO = os.environ.get("HERMES_PROXY_FORWARDED_PROTO", "https")
HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
}


def upstream_headers(request: web.Request) -> dict[str, str]:
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in HOP_HEADERS
        and key.lower() not in {"host", "origin", "content-length"}
    }
    headers["Host"] = UPSTREAM_HOST
    headers["Origin"] = UPSTREAM_HTTP
    headers["X-Forwarded-Host"] = request.host
    headers["X-Forwarded-Proto"] = FORWARDED_PROTO
    return headers


async def websocket_proxy(request: web.Request) -> web.StreamResponse:
    protocols = tuple(
        value.strip()
        for value in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
        if value.strip()
    )
    headers = {
        key: value for key, value in upstream_headers(request).items()
        if not key.lower().startswith("sec-websocket-")
    }
    upstream_url = f"{UPSTREAM_WS}{request.rel_url}"
    session: ClientSession = request.app["client"]
    try:
        upstream = await session.ws_connect(
            upstream_url, headers=headers, protocols=protocols,
            autoping=True, heartbeat=30,
        )
    except WSServerHandshakeError as exc:
        return web.Response(status=exc.status, text="WebSocket upstream rejected the connection")
    async with upstream:
        downstream = web.WebSocketResponse(
            protocols=(upstream.protocol,) if upstream.protocol else (),
            autoping=True, heartbeat=30,
        )
        await downstream.prepare(request)
        async def downstream_to_upstream() -> None:
            async for message in downstream:
                if message.type == WSMsgType.TEXT:
                    await upstream.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await upstream.send_bytes(message.data)
                elif message.type == WSMsgType.CLOSE:
                    await upstream.close()

        async def upstream_to_downstream() -> None:
            async for message in upstream:
                if message.type == WSMsgType.TEXT:
                    await downstream.send_str(message.data)
                elif message.type == WSMsgType.BINARY:
                    await downstream.send_bytes(message.data)
                elif message.type == WSMsgType.CLOSE:
                    await downstream.close()

        tasks = [
            asyncio.create_task(downstream_to_upstream()),
            asyncio.create_task(upstream_to_downstream()),
        ]
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
        await asyncio.gather(*done, *pending, return_exceptions=True)
    return downstream


async def http_proxy(request: web.Request) -> web.StreamResponse:
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await websocket_proxy(request)

    body = await request.read()
    session: ClientSession = request.app["client"]
    if request.path in VOICE_PATHS:
        slots = request.app.get(VOICE_SLOTS)
        if slots is not None:
            try:
                async with slots.admit() as upstream:
                    response = await forward_http(request, body, session, upstream)
                    response.headers["X-Z0Live-Voice-Port"] = upstream.rsplit(":", 1)[1]
                    return response
            except (OSError, ValueError, TypeError, ClientConnectionError, TimeoutError):
                raise web.HTTPServiceUnavailable(text="Voice adapter unavailable") from None
        upstream = os.environ.get("HERMES_MOBILE_VOICE_UPSTREAM", "").rstrip("/")
        if upstream:
            return await forward_http(request, body, session, upstream)
    return await forward_http(request, body, session, UPSTREAM_HTTP)


async def forward_http(request, body, session, upstream):
    async with session.request(
        request.method,
        f"{upstream}{request.rel_url}",
        headers=upstream_headers(request),
        data=body or None,
        allow_redirects=False,
    ) as response:
        payload = await response.read()
        headers = {
            key: value
            for key, value in response.headers.items()
            if key.lower() not in HOP_HEADERS and key.lower() != "content-length"
        }
        return web.Response(status=response.status, body=payload, headers=headers)


async def client_context(app: web.Application):
    timeout = ClientTimeout(total=None, connect=10, sock_read=None)
    app["client"] = ClientSession(timeout=timeout)
    yield
    await app["client"].close()


def make_app(slots=None):
    app = web.Application(client_max_size=64 * 1024 * 1024)
    if slots is not None:
        app[VOICE_SLOTS] = slots
    app.cleanup_ctx.append(client_context)
    app.router.add_route("*", "/{tail:.*}", http_proxy)
    return app


def main() -> None:
    directory = os.environ.get("Z0LIVE_VOICE_SLOTS")
    app = make_app(Slots(directory) if directory else None)
    # HERMES_PROXY_HOSTS: comma list of bind addresses; add the k3s cni0 gateway so the
    # Tailscale operator's ingress pod on the desktop host can reach this proxy. Default stays loopback-only.
    hosts = [h for h in os.environ.get("HERMES_PROXY_HOSTS", "127.0.0.1").split(",") if h]
    web.run_app(app, host=hosts, port=int(os.environ.get("HERMES_PROXY_PORT", "9165")), access_log=None)


if __name__ == "__main__":
    main()
