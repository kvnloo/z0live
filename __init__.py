"""z0live as a Hermes plugin: see and steer the z0live voice stack from any Hermes chat.

This repo is the plugin. `hermes plugins install https://github.com/kvnloo/z0live` copies the whole tree, so the
voice adapter, lane server and `bin/z0live` ship with it; the services themselves are installed by
`deploy/macos/install.sh` (launchd) and keep their own logins. Registered here:

  z0live_status     voice engine, guest voice, lane models, lane server health, latest call's trace summary (read only)
  z0live_configure  engine for the next call (gpt-live | grok), second voice (grok | off), lane models per tier
  z0live_lanes      the helper-lane board in plain words ("gimme 2 fast lanes, 2 small, 2 large, one council")
  /z0live-lanes     the same, as a chat command

Nothing leaves the machine: the tools read z0live's local files, make one loopback GET to the lane server and may
run this repo's own `bin/z0live`. No credentials are read.
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent
TOOLSET = "z0live"
ENGINES = {"gpt-live": "voice_plan", "grok": "grok_voice"}
GUESTS = ("grok", "off")
TIERS = ("fast", "medium", "slow")
SUMMARY_EVENTS = ("delegation.start", "lanes.spoken", "delegation.done", "delegation.failed", "engine.unavailable",
                  "guest.said", "guest.failed")
DEFAULTS = {
    "z0live_repo": "",  # empty = this plugin's own tree
    "lanes_file": "~/.z0int/config/z0live-lanes.json",
    "tiers_file": "~/.config/z0live/tiers.json",
    "voices_file": "~/.config/z0live/voices.json",
    "traces_dir": "~/.local/share/z0live/voice-traces",
    "lane_url": "http://127.0.0.1:8646",
}


def _path(value: str) -> Path:
    return Path(os.path.expanduser(str(value)))


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def _engine(lanes_file: Path) -> str:
    try:
        actor = _read_json(lanes_file)["lanes"]["voice"]["actor"]
    except (TypeError, KeyError):
        return "gpt-live (no lanes config)"
    return next((name for name, a in ENGINES.items() if a == actor), actor)


def _lane_server(url: str) -> str:
    try:
        with urllib.request.urlopen(url.rstrip("/") + "/v1/ask", timeout=2):
            return "up"
    except urllib.error.HTTPError as exc:  # it answers GET with 404/405: listening
        return "up" if exc.code in (404, 405, 501) else f"error {exc.code}"
    except Exception as exc:  # noqa: BLE001
        return f"down ({type(exc).__name__})"


def latest_call(traces: Path) -> Dict[str, Any]:
    """The newest call trace, summarized: when, how long, engine, and what the lanes, guest and Hermes did."""
    files = sorted(traces.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime) if traces.is_dir() else []
    if not files:
        return {"calls": 0}
    newest, counts, first, last, engine, close = files[-1], {}, None, None, None, None
    for line in newest.read_text(errors="ignore").splitlines():
        try:
            e = json.loads(line)
        except ValueError:
            continue
        first = e.get("wall") if first is None else first
        last = e.get("wall", last)
        ev = e.get("ev", "")
        if ev == "call.created":
            engine = e.get("engine")
        if ev == "call.closed":
            close = e.get("reason")
        if ev in SUMMARY_EVENTS:
            counts[ev] = counts.get(ev, 0) + 1
    return {"calls": len(files), "trace": str(newest), "engine": engine,
            "started": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(first)) if first else None,
            "seconds": round(last - first, 1) if first and last else None,
            "open": close is None and last is not None and time.time() - last < 60, "closed_reason": close,
            "events": counts}


def make_status(cfg):
    def handle(args: Dict[str, Any], **_kw: Any) -> str:
        try:
            tiers = _read_json(_path(cfg("tiers_file"))) or "defaults in voice-adapter/tiers.py"
            guest = (_read_json(_path(cfg("voices_file"))) or {}).get("guest") or "off"
            return json.dumps({"engine": _engine(_path(cfg("lanes_file"))), "guest": guest, "tiers": tiers,
                               "lane_server": _lane_server(cfg("lane_url")),
                               "latest_call": latest_call(_path(cfg("traces_dir")))}, ensure_ascii=False)
        except Exception as exc:  # a tool handler never raises
            logger.warning("z0live: status failed (%s)", type(exc).__name__)
            return json.dumps({"error": f"z0live_status failed: {type(exc).__name__}"})
    return handle


def valid_tiers(tiers: Any) -> bool:
    return isinstance(tiers, dict) and bool(tiers) and all(
        t in TIERS and isinstance(v, list) and v and all(isinstance(b, dict) and b.get("provider") and b.get("model")
                                                          for b in v) for t, v in tiers.items())


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=1))
    tmp.replace(path)  # atomic: the adapter reads these files while calls run


def make_configure(cfg):
    def handle(args: Dict[str, Any], **_kw: Any) -> str:
        args, done = args or {}, {}
        try:
            engine, guest = args.get("engine"), args.get("guest")
            if engine and engine not in ENGINES:
                return json.dumps({"error": f"engine must be one of {sorted(ENGINES)}"})
            if guest and guest not in GUESTS:
                return json.dumps({"error": f"guest must be one of {list(GUESTS)}"})
            if "tiers" in args and args["tiers"] is not None and not valid_tiers(args["tiers"]):
                return json.dumps({"error": "tiers must map fast/medium/slow to [{provider, model}, ...]"})
            if engine:
                cli = _path(cfg("z0live_repo") or ROOT) / "bin" / "z0live"
                out = subprocess.run([sys.executable, str(cli), "engine", engine], capture_output=True, text=True,
                                     timeout=30)
                if out.returncode != 0:
                    return json.dumps({"error": "z0live engine failed", "detail": out.stderr.strip()[-300:]})
                done["engine"] = engine
            if guest:
                _write_json(_path(cfg("voices_file")), {"guest": "" if guest == "off" else guest})
                done["guest"] = guest
            if "tiers" in args:
                tiers_file = _path(cfg("tiers_file"))
                if args["tiers"] is None:
                    tiers_file.unlink(missing_ok=True)
                    done["tiers"] = "defaults"
                else:
                    _write_json(tiers_file, args["tiers"])
                    done["tiers"] = args["tiers"]
            if not done:
                return json.dumps({"error": "nothing to change: pass engine, guest and/or tiers"})
            return json.dumps({"ok": True, "applied": done,
                               "note": "engine and guest apply from the next call; tiers from the next request"})
        except Exception as exc:  # a tool handler never raises
            logger.warning("z0live: configure failed (%s)", type(exc).__name__)
            return json.dumps({"error": f"z0live_configure failed: {type(exc).__name__}"})
    return handle


def lanes(request: str = "") -> Dict[str, Any]:
    """The helper-lane board, in words. Same parser as the voice intent (desktop/z0live_desktop/lanes_intent.py)."""
    desktop = str(ROOT / "desktop")
    if desktop not in sys.path:
        sys.path.insert(0, desktop)
    from z0live_desktop import lanes as board, lanes_intent
    request = (request or "").strip()
    if not request or request == "status":
        live = board.Lanes()
        return {"ok": True, **live.status(), "say": lanes_intent.describe(live.get())}
    try:
        return lanes_intent.apply(request[:500])
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}


def lanes_tool(args: Dict[str, Any], **_kw: Any) -> str:
    try:
        return json.dumps(lanes((args or {}).get("request", "")))
    except Exception as exc:  # a tool handler never raises
        return json.dumps({"ok": False, "error": f"z0live_lanes failed: {type(exc).__name__}"})


def lanes_command(raw_args: str = "", invocation: Any = None) -> str:
    result = json.loads(lanes_tool({"request": raw_args}))
    return result.get("say") or result.get("error") or json.dumps(result)


STATUS_SCHEMA = {
    "name": "z0live_status",
    "description": ("Show the z0live voice stack on this machine: the voice engine the next call uses, whether a "
                    "second voice (Grok) joins, the model lanes per tier, whether the lane server is up, and a "
                    "summary of the latest call (when, how long, how often lanes, the guest and Hermes answered). "
                    "Read only."),
    "parameters": {"type": "object", "properties": {}},
}
CONFIGURE_SCHEMA = {
    "name": "z0live_configure",
    "description": ("Change z0live: the voice engine for the next call (gpt-live or grok), the second voice that "
                    "answers when called by name (grok, or off), and/or the lane models per tier. tiers maps "
                    "fast/medium/slow to lists of {provider, model} that race within the tier; tiers=null restores "
                    "the defaults."),
    "parameters": {"type": "object", "properties": {
        "engine": {"type": "string", "enum": sorted(ENGINES)},
        "guest": {"type": "string", "enum": list(GUESTS)},
        "tiers": {"type": ["object", "null"], "description": "{fast|medium|slow: [{provider, model}, ...]}"},
    }},
}
LANES_SCHEMA = {
    "name": "z0live_lanes",
    "description": ("Show or change z0live's helper-lane board in plain words. request: the owner's words, e.g. "
                    "'gimme 2 fast lanes, 2 small, 2 large, one council' or 'make the large lanes Opus and Sonnet'; "
                    "empty = show the active lanes."),
    "parameters": {"type": "object", "properties": {"request": {"type": "string"}}, "additionalProperties": False},
}


def register(ctx) -> None:  # noqa: D401 - entry point required by Hermes
    from . import meetings

    def cfg(key: str) -> str:
        defaults = {**DEFAULTS, **meetings.DEFAULTS}
        return ctx.get_config(key, default=defaults[key])

    ctx.register_tool(name="z0live_status", toolset=TOOLSET, schema=STATUS_SCHEMA, handler=make_status(cfg),
                      description="z0live voice stack status (read only)", emoji="🎙️")
    ctx.register_tool(name="z0live_configure", toolset=TOOLSET, schema=CONFIGURE_SCHEMA, handler=make_configure(cfg),
                      description="Pick the z0live voice engine, second voice and lane models", emoji="🎛️")
    ctx.register_tool(name="z0live_lanes", toolset=TOOLSET, schema=LANES_SCHEMA, handler=lanes_tool,
                      description="Show or set z0live helper lanes in plain words", emoji="🎚️")
    ctx.register_command("z0live-lanes", handler=lanes_command,
                         description="Show or set z0live voice lanes (fast/small/large/council)",
                         args_hint="[gimme 2 fast lanes, 2 small, 2 large, one council]")
    meetings.register(ctx, cfg)
