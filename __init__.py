"""Hermes tools for a separately installed z0live voice stack.

Status and configuration use local files and a lane-server health probe. The
opt-in progress tool sends text to the local adapter, which passes it to the
call's voice provider. See the README for the stack's privacy disclosure.
"""
from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
import subprocess
import sys
import time
from http.client import HTTPConnection, HTTPException
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlsplit

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
    "broker_url": "http://127.0.0.1:8791",
    "device_token_file": "~/.config/z0live/hermes-mobile-relay.token",
}


def _path(value: str | Path) -> Path:
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
        base = urlsplit(url)
        if base.scheme != "http" or base.hostname not in ("127.0.0.1", "::1"):
            return "unknown (lane_url must use loopback HTTP)"
        conn = HTTPConnection(base.hostname, base.port, timeout=2)
        try:
            conn.request("GET", base.path.rstrip("/") + "/health")
            with conn.getresponse() as response:
                status = response.status
        finally:
            conn.close()
    except (OSError, HTTPException, ValueError) as exc:
        return f"down ({type(exc).__name__})"
    if status in (404, 405, 501):
        return "unknown (/health not supported)"
    return "up" if status == 200 else f"error {status}"


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
        t in TIERS and isinstance(v, list) and v and all(
            isinstance(b, dict) and b.keys() == {"provider", "model"}
            and all(isinstance(s, str) and s.strip() for s in b.values()) for b in v
        ) for t, v in tiers.items())


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(value, indent=1))
    tmp.replace(path)  # atomic: the adapter reads these files while calls run


def make_configure(cfg):
    def handle(args: Dict[str, Any], **_kw: Any) -> str:
        args, done = args or {}, {}
        try:
            engine, guest, reset = args.get("engine"), args.get("guest"), args.get("reset_tiers", False)
            if "engine" in args and (not isinstance(engine, str) or engine not in ENGINES):
                return json.dumps({"error": f"engine must be one of {sorted(ENGINES)}"})
            if "guest" in args and guest not in GUESTS:
                return json.dumps({"error": f"guest must be one of {list(GUESTS)}"})
            if not isinstance(reset, bool) or (reset and "tiers" in args):
                return json.dumps({"error": "pass reset_tiers=true to reset; do not also pass tiers"})
            if "tiers" in args and not valid_tiers(args["tiers"]):
                return json.dumps({"error": "tiers must map fast/medium/slow to [{provider, model}, ...]"})
            if engine:
                cli = _path(cfg("z0live_repo") or ROOT) / "bin" / "z0live"
                env = {**os.environ, "Z0LIVE_LANES": str(_path(cfg("lanes_file")).absolute())}
                out = subprocess.run([sys.executable, str(cli), "engine", engine], capture_output=True, text=True,
                                     timeout=30, env=env)
                if out.returncode != 0:
                    return json.dumps({"error": "z0live engine failed", "detail": out.stderr.strip()[-300:]})
                done["engine"] = engine
            if guest:
                _write_json(_path(cfg("voices_file")), {"guest": "" if guest == "off" else guest})
                done["guest"] = guest
            if reset:
                _path(cfg("tiers_file")).unlink(missing_ok=True)
                done["tiers"] = "defaults"
            elif "tiers" in args:
                _write_json(_path(cfg("tiers_file")), args["tiers"])
                done["tiers"] = args["tiers"]
            if not done:
                return json.dumps({"error": "nothing to change: pass engine, guest, tiers or reset_tiers"})
            return json.dumps({"ok": True, "applied": done,
                               "note": "engine and guest apply from the next call; tiers from the next request"})
        except Exception as exc:  # a tool handler never raises
            logger.warning("z0live: configure failed (%s)", type(exc).__name__)
            return json.dumps({"error": f"z0live_configure failed: {type(exc).__name__}"})
    return handle


def _desktop_module(name):
    package = "_z0live_plugin_desktop"
    if package not in sys.modules:
        spec = importlib.util.spec_from_file_location(package, ROOT / "desktop/z0live_desktop/__init__.py")
        if spec is None or spec.loader is None:
            raise ImportError("z0live desktop package is missing")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        sys.modules[package] = module
    return importlib.import_module(f"{package}.{name}")


def make_lanes(cfg, installed):
    def handle(args: Dict[str, Any], **_kw: Any) -> str:
        try:
            if not installed():
                return json.dumps({"ok": False, "error": "z0live stack not configured"})
            request = (args.get("request") or "").strip()
            path = _path(cfg("lanes_file"))
            intent = _desktop_module("lanes_intent")
            if not request or request == "status":
                live = intent.lanes.Lanes(path)
                return json.dumps({"ok": True, **live.status(), "say": intent.describe(live.get())})
            return json.dumps(intent.apply(request[:500], path=path))
        except ValueError as exc:
            return json.dumps({"ok": False, "error": str(exc)})
        except Exception as exc:  # a tool handler never raises
            return json.dumps({"ok": False, "error": f"z0live_lanes failed: {type(exc).__name__}"})
    return handle


PROGRESS_TOKEN_ENV = "Z0LIVE_PROGRESS_TOKEN"


def _secret(name):
    """Profile-scoped secret through Hermes (catalog rule 11); a bare os.environ read can see another profile's key."""
    from agent.secret_scope import get_secret
    return get_secret(name)
PROGRESS_PORT = 9168


def make_say(lines):
    def handle(args: Dict[str, Any], *, session_id: str = "", **_kw: Any) -> str:
        text = (args or {}).get("text")
        if not isinstance(text, str) or text not in lines:
            return json.dumps({"error": "Choose one of the stock progress lines"})
        token = _secret(PROGRESS_TOKEN_ENV)
        if not token or not session_id:
            return json.dumps({"error": "Voice progress is not configured for this Hermes session"})
        conn = HTTPConnection("127.0.0.1", PROGRESS_PORT, timeout=2)
        try:
            conn.request("POST", "/_z0live/progress", json.dumps({"session_id": session_id, "text": text}),
                         {"Content-Type": "application/json", "Authorization": f"Bearer {token}"})
            with conn.getresponse() as response:
                if response.status == 204:
                    return json.dumps({"ok": True})
                if response.status == 409:
                    return json.dumps({"error": "No unique, ready live call for this Hermes session"})
                return json.dumps({"error": f"Voice adapter rejected progress (HTTP {response.status})"})
        except (OSError, HTTPException, ValueError):
            return json.dumps({"error": "Voice progress adapter unavailable"})
        finally:
            conn.close()
    return handle


GLOBE_TOKEN_ENV = "Z0LIVE_TOKEN"


def _globe_token(cfg):
    """Paired-device token: the profile secret, else the private token file. Never returned to the model."""
    token = (_secret(GLOBE_TOKEN_ENV) or "").strip()
    if token:
        return token
    try:
        return _path(cfg("device_token_file")).read_text().strip()
    except OSError:
        return ""


def make_globe(cfg):
    def handle(args: Dict[str, Any], **_kw: Any) -> str:
        try:
            args = args or {}
            action, action_args = args.get("action"), args.get("args") or {}
            if action is not None and (not isinstance(action, str) or not action.strip()):
                return json.dumps({"ok": False, "error": "action must be a display action name"})
            if not isinstance(action_args, dict):
                return json.dumps({"ok": False, "error": "args must be an object"})
            token = _globe_token(cfg)
            if not token:
                return json.dumps({"ok": False, "error": "no z0live device token for this profile"})
            displays = _desktop_module("displays")
            return json.dumps(displays.globe(action, action_args, token=token, base=cfg("broker_url")),
                              ensure_ascii=False)
        except Exception as exc:  # a tool handler never raises
            return json.dumps({"ok": False, "error": f"z0live_globe failed: {type(exc).__name__}"})
    return handle


GLOBE_SCHEMA = {
    "name": "z0live_globe",
    "description": ("Drive the God's Eye View globe the owner has open on his screen (the same display his z0live "
                    "voice call controls). Omit action to read the current view and the list of actions; otherwise "
                    "pass one listed action and its args, e.g. {action:'fly-to', args:{query:'Tokyo'}}. "
                    "Only an ok result means the globe changed; timeout means unknown."),
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "action": {"type": "string"}, "args": {"type": "object"}}},
}


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
                    "answers when called by name (grok or off), and/or the adapter's fast/medium/slow model tiers. "
                    "Models within each tier race. Set reset_tiers=true instead of tiers to restore defaults."),
    "parameters": {"type": "object", "additionalProperties": False, "properties": {
        "engine": {"type": "string", "enum": sorted(ENGINES)},
        "guest": {"type": "string", "enum": list(GUESTS)},
        "reset_tiers": {"type": "boolean"},
        "tiers": {"type": "object", "additionalProperties": False, "properties": {
            tier: {"type": "array", "minItems": 1, "items": {
                "type": "object", "additionalProperties": False, "required": ["provider", "model"],
                "properties": {key: {"type": "string", "minLength": 1} for key in ("provider", "model")},
            }} for tier in TIERS
        }},
    }},
}
LANES_SCHEMA = {
    "name": "z0live_lanes",
    "description": ("Show or change the desktop broker's helper-lane board, separate from the adapter's model tiers. "
                    "Pass the owner's words, e.g. 'gimme 2 fast lanes, 2 small, 2 large, one council'. "
                    "Leave request empty for board status."),
    "parameters": {"type": "object", "properties": {"request": {"type": "string"}}, "additionalProperties": False},
}


def register(ctx) -> None:
    """Register tools and the helper-board command with Hermes."""
    def cfg(key: str) -> str:
        return ctx.get_config(key, default=DEFAULTS[key])

    def installed() -> bool:
        return _path(cfg("lanes_file")).is_file() or _path(cfg("traces_dir")).is_dir()

    def can_configure() -> bool:
        return installed() and (_path(cfg("z0live_repo") or ROOT) / "bin/z0live").is_file()

    lanes_tool = make_lanes(cfg, installed)

    def lanes_command(raw_args: str = "", invocation: Any = None) -> str:
        result = json.loads(lanes_tool({"request": raw_args}))
        return result.get("say") or result.get("error") or json.dumps(result)

    ctx.register_tool(name="z0live_status", toolset=TOOLSET, schema=STATUS_SCHEMA, handler=make_status(cfg),
                      check_fn=installed, description="z0live voice stack status (read only)", emoji="🎙️")
    ctx.register_tool(name="z0live_configure", toolset=TOOLSET, schema=CONFIGURE_SCHEMA, handler=make_configure(cfg),
                      check_fn=can_configure, description="Pick the z0live engine, second voice and model tiers",
                      emoji="🎛️")
    ctx.register_tool(name="z0live_lanes", toolset=TOOLSET, schema=LANES_SCHEMA, handler=lanes_tool,
                      check_fn=installed, description="Show or set the desktop helper-lane board", emoji="🎚️")
    ctx.register_tool(name="z0live_globe", toolset=TOOLSET, schema=GLOBE_SCHEMA, handler=make_globe(cfg),
                      check_fn=lambda: installed() and bool(_globe_token(cfg)),
                      description="Move the owner's God's Eye View globe display", emoji="🌐")
    lines = _read_json(ROOT / "voice-adapter/progress_lines.json") or []
    ctx.register_tool(name="z0live_say", toolset=TOOLSET, handler=make_say(lines),
                      check_fn=lambda: bool(lines and _secret(PROGRESS_TOKEN_ENV)),
                      schema={"name": "z0live_say",
                              "description": "Send a stock progress line to the live call for this Hermes session. "
                                             "Use during work, not for the final answer.",
                              "parameters": {"type": "object", "additionalProperties": False, "required": ["text"],
                                             "properties": {"text": {"type": "string", "enum": lines}}}},
                      description="Speak a progress line in the linked live call", emoji="🎙️")
    ctx.register_command("z0live-lanes", handler=lanes_command,
                         description="Show or set the desktop helper-lane board (fast/small/large/council)",
                         args_hint="[gimme 2 fast lanes, 2 small, 2 large, one council]")
