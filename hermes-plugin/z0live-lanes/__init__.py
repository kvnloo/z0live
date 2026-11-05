"""/z0live-lanes chat command and `z0live_lanes` tool: the text path of z0live lane configuration.

Same parser as the voice intent (broker tool `configure_lanes`): desktop/z0live_desktop/lanes_intent.py.
The broker hot-reloads the written z0live.lanes.v1 file ($Z0LIVE_LANES or ~/.z0int/config/z0live-lanes.json).
"""
import json
import sys
from pathlib import Path

DESKTOP = Path(__file__).resolve().parents[2] / "desktop"
SCHEMA = {"name": "z0live_lanes",
          "description": "Show or change z0live voice helper lanes. request: the owner's words, e.g. "
                         "'gimme 2 fast lanes, 2 small, 2 large, one council' or 'make the large lanes Opus and "
                         "Sonnet'; empty = show the active lanes.",
          "parameters": {"type": "object", "properties": {"request": {"type": "string"}},
                         "additionalProperties": False}}


def run(request=""):
    if str(DESKTOP) not in sys.path:
        sys.path.insert(0, str(DESKTOP))
    from z0live_desktop import lanes, lanes_intent
    request = (request or "").strip()
    if not request or request == "status":
        live = lanes.Lanes()
        return {"ok": True, **live.status(), "say": lanes_intent.describe(live.get())}
    try:
        return lanes_intent.apply(request[:500])
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


def command(raw_args="", invocation=None):
    result = run(raw_args)
    return result.get("say") or result.get("error") or json.dumps(result)


def tool(args, **_kw):
    return json.dumps(run((args or {}).get("request", "")))


def register(ctx):
    ctx.register_command("z0live-lanes", handler=command,
                         description="Show or set z0live voice lanes (fast/small/large/council)",
                         args_hint="[gimme 2 fast lanes, 2 small, 2 large, one council]")
    ctx.register_tool(name="z0live_lanes", toolset="z0live", schema=SCHEMA, handler=tool, emoji="🎚️")
