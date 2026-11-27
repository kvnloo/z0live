"""Authenticated loopback ingress for stock, non-private voice progress lines."""
import hmac
import ipaddress
import json
import os
from pathlib import Path

from aiohttp import web

PATH = "/_z0live/progress"
LINES = frozenset(json.loads(Path(__file__).with_name("progress_lines.json").read_text()))


async def progress(request):
    if not request.remote or not ipaddress.ip_address(request.remote).is_loopback:
        raise web.HTTPForbidden()
    token = os.environ.get("Z0LIVE_PROGRESS_TOKEN")
    if not token:
        raise web.HTTPNotFound()
    if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()):
        raise web.HTTPUnauthorized()
    if request.content_type != "application/json":
        raise web.HTTPUnsupportedMediaType()
    try:
        data = await request.json()
        sid, text = data["session_id"], data["text"]
        if not isinstance(sid, str) or not sid or not isinstance(text, str) or text not in LINES:
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise web.HTTPBadRequest() from None

    calls = [c for c in request.app["manager"].calls.values() if c.chat == sid and not c.closed]
    if len(calls) != 1:
        raise web.HTTPConflict()
    call, = calls
    if call.leg:
        if not call.leg.ready or call.leg.ws is None or call.leg.ws.closed:
            raise web.HTTPConflict()
    elif not call.provider_ready or call.provider_dc.readyState != "open":
        raise web.HTTPConflict()
    # One status budget for the call (mobile_live.STATUS_GAP): a line inside the gap of another status line or the
    # voice's ack, or over Kevin or a reply, is dropped. Still 204: an error only makes the agent try again.
    if not call.take_status("ingress"):
        return web.Response(status=204)
    if call.leg:
        await call.leg.note("Brief progress update from Hermes. Say this aloud: " + text, call.trace.turn_id)
        await call.leg.raw({"type": "response.create"})
    else:
        for event in call.protocol.from_mobile({"type": "session.commentary.append", "content": text}):
            call.send(event, to_provider=True)
    return web.Response(status=204)
