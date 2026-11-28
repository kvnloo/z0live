"""Display bridge: browser surfaces (e.g. the God's Eye View globe) that a voice call or Hermes agent drives.

A display tab registers its compact action list, keeps a heartbeat, receives commands over an
authenticated SSE inbox and posts one result per command. The broker correlates results, so a
caller's `globe` tool call returns the display's own verdict, or an honest timeout. Registry state is
in memory only: a broker restart forgets displays until their next heartbeat re-registers them.

See docs/DISPLAYS.md for the wire protocol.
"""

from __future__ import annotations

import os
import re
import threading
import time
import uuid

from .core import dumps

NAME = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
ACTION = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,63}$")
STALE_SECONDS = float(os.environ.get("Z0LIVE_DISPLAY_STALE_SECONDS", "15"))
RESULT_SECONDS = float(os.environ.get("Z0LIVE_DISPLAY_RESULT_SECONDS", "12"))
MAX_TOOLS = 64
MAX_QUEUE = 16
# The live model sees the GEV display as one tool (plus a read-only view), whatever its action count.
GLOBE = "gev"
TOOL_NAMES = ("globe", "globe_context")
GUIDANCE = """
A globe display is connected for this call (the God's Eye View map on the user's screen). When he asks
to see, go to or show a place, region, layer or live data (\"take me to Tokyo\", \"show flights over
Texas\"), call globe with one listed action and its args; call globe_context first when you need the
current view. Say what the result says: it moved only when the result is ok; a refusal, error or
timeout means it did not (or may not have) happened. Never claim the screen changed without an ok result.
"""


class DisplayError(ValueError):
    pass


DEFAULT_TOKEN_FILE = "~/.config/z0live/hermes-mobile-relay.token"


def client_token(token_file=None):
    """Owner credential for agent callers: Z0LIVE_TOKEN, else a private paired-device token file
    (Z0LIVE_TOKEN_FILE, default the Hermes relay device the installer pairs). Never printed."""
    token = os.environ.get("Z0LIVE_TOKEN", "").strip()
    if token:
        return token
    path = os.path.expanduser(token_file or os.environ.get("Z0LIVE_TOKEN_FILE") or DEFAULT_TOKEN_FILE)
    try:
        return open(path).read().strip() or None
    except OSError:
        return None


def client_call(path, body=None, token=None, base=None, timeout=None):
    """Call the running broker as an owner-authorized agent (bin/z0live globe, the Hermes plugin).
    Returns the broker's JSON or {ok:false,error} without raising and without echoing the token."""
    import json
    import urllib.error
    import urllib.request

    base = (base or os.environ.get("Z0LIVE_BROKER_URL")
            or "http://127.0.0.1:" + os.environ.get("Z0LIVE_PORT", "8791")).rstrip("/")
    token = token or client_token()
    if not token:
        return {"ok": False, "error": "no z0live device token (set Z0LIVE_TOKEN or Z0LIVE_TOKEN_FILE)"}
    req = urllib.request.Request(
        base + path,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout or RESULT_SECONDS + 8) as r:
            return json.loads(r.read(2_000_000))
    except urllib.error.HTTPError as exc:
        try:
            detail = json.loads(exc.read(4000)).get("error", "")
        except ValueError:
            detail = ""
        exc.close()
        return {"ok": False, "error": f"broker HTTP {exc.code}", "detail": detail}
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        return {"ok": False, "error": f"broker unavailable ({type(exc).__name__})"}


def globe(action=None, args=None, **kw):
    """Agent surface: run one globe action (or read the view when action is None)."""
    if action is None:
        return client_call("/v1/displays/" + GLOBE, **kw)
    return client_call("/v1/displays/" + GLOBE + "/command", {"action": action, "args": args or {}}, **kw)


class Displays:
    def __init__(self, clock=time.monotonic, stale=None, timeout=None):
        self.clock = clock
        self.stale = STALE_SECONDS if stale is None else stale
        self.timeout = RESULT_SECONDS if timeout is None else timeout
        self.lock = threading.Condition()
        self.entries: dict[str, dict] = {}

    # ----- display side -------------------------------------------------------------------------
    def register(self, device, body):
        if not isinstance(body, dict):
            raise DisplayError("expected object")
        name = body.get("display")
        if not isinstance(name, str) or not NAME.fullmatch(name):
            raise DisplayError("display must match [a-z][a-z0-9_-]{0,31}")
        tools = body.get("tools")
        if not isinstance(tools, list) or not 1 <= len(tools) <= MAX_TOOLS:
            raise DisplayError(f"tools must list 1..{MAX_TOOLS} actions")
        clean, seen = [], set()
        for t in tools:
            if (
                not isinstance(t, dict)
                or not isinstance(t.get("name"), str)
                or not ACTION.fullmatch(t["name"])
                or t["name"] in seen
                or not isinstance(t.get("description", ""), str)
            ):
                raise DisplayError("each tool needs a unique name and a string description")
            seen.add(t["name"])
            clean.append({"name": t["name"], "description": t.get("description", "")[:200]})
        context = self._context(body.get("context", {}))
        with self.lock:
            old = self.entries.get(name)
            if old and old["device"] != device and self._alive(old):
                raise DisplayError("display is held by another paired device")
            entry = {
                "device": device,
                "tools": clean,
                "context": context,
                "seen": self.clock(),
                "registered": time.time(),
                # A re-registering tab (reload) keeps queued work only when it is the same device.
                "queue": old["queue"] if old and old["device"] == device else [],
                "pending": old["pending"] if old and old["device"] == device else {},
                "generation": uuid.uuid4().hex,
            }
            self.entries[name] = entry
            self.lock.notify_all()
        return {"ok": True, "display": name, "stale_after": self.stale, "result_timeout": self.timeout,
                "generation": entry["generation"]}

    def heartbeat(self, device, name, body=None):
        with self.lock:
            entry = self._owned(device, name)
            if isinstance(body, dict) and "context" in body:
                entry["context"] = self._context(body["context"])
            entry["seen"] = self.clock()
            return {"ok": True, "pending": len(entry["queue"])}

    def update_context(self, device, name, body):
        if not isinstance(body, dict) or "context" not in body:
            raise DisplayError("context required")
        return self.heartbeat(device, name, body)

    def next_commands(self, device, name, wait=0.0):
        """Commands not yet delivered, oldest first; expired ones are dropped, never executed late."""
        deadline = self.clock() + wait
        with self.lock:
            while True:
                entry = self._owned(device, name)
                now = self.clock()
                live = []
                for cmd in entry["queue"]:
                    if cmd["deadline"] > now:
                        live.append(cmd)
                entry["queue"] = []
                if live or now >= deadline:
                    return [{"id": c["id"], "action": c["action"], "args": c["args"]} for c in live]
                self.lock.wait(min(0.5, deadline - now))

    def result(self, device, name, body):
        if not isinstance(body, dict) or not isinstance(body.get("id"), str) or not isinstance(body.get("ok"), bool):
            raise DisplayError("result needs id and boolean ok")
        output = body.get("output", "")
        if len(dumps(output)) > 8000:
            raise DisplayError("output must be at most 8000 characters")
        with self.lock:
            entry = self._owned(device, name)
            entry["seen"] = self.clock()
            pending = entry["pending"].get(body["id"])
            if pending is None:
                return {"ok": True, "accepted": False, "reason": "unknown or expired command"}
            pending["result"] = {"ok": body["ok"], "output": output}
            self.lock.notify_all()
            return {"ok": True, "accepted": True}

    # ----- caller side --------------------------------------------------------------------------
    def alive(self, name=GLOBE):
        with self.lock:
            entry = self.entries.get(name)
            return entry if entry and self._alive(entry) else None

    def status(self, name=GLOBE):
        with self.lock:
            entry = self.entries.get(name)
            if not entry or not self._alive(entry):
                return {"display": name, "connected": False}
            return {"display": name, "connected": True, "actions": [t["name"] for t in entry["tools"]],
                    "tools": entry["tools"], "context": entry["context"],
                    "seen_seconds_ago": round(self.clock() - entry["seen"], 1)}

    def command(self, name, action, args, timeout=None):
        if not isinstance(action, str) or not ACTION.fullmatch(action):
            raise DisplayError("action must be a listed action name")
        if args is None:
            args = {}
        if not isinstance(args, dict) or len(dumps(args)) > 8000:
            raise DisplayError("args must be an object of at most 8000 characters")
        wait = self.timeout if timeout is None else min(float(timeout), self.timeout)
        with self.lock:
            entry = self.entries.get(name)
            if not entry or not self._alive(entry):
                return {"ok": False, "status": "no_display",
                        "output": f"The {name} display is not connected, so nothing moved."}
            if action not in {t["name"] for t in entry["tools"]}:
                return {"ok": False, "status": "unknown_action",
                        "output": f"{action} is not an action this display offers.",
                        "actions": [t["name"] for t in entry["tools"]]}
            if len(entry["queue"]) >= MAX_QUEUE:
                return {"ok": False, "status": "busy", "output": "The display has too many queued commands."}
            cid = uuid.uuid4().hex
            start = self.clock()
            cmd = {"id": cid, "action": action, "args": args, "deadline": start + wait}
            pending = {"result": None}
            entry["queue"].append(cmd)
            entry["pending"][cid] = pending
            self.lock.notify_all()
            try:
                while pending["result"] is None:
                    left = start + wait - self.clock()
                    if left <= 0:
                        break
                    self.lock.wait(min(0.25, left))
            finally:
                entry["pending"].pop(cid, None)
                entry["queue"] = [c for c in entry["queue"] if c["id"] != cid]
        if pending["result"] is None:
            return {"ok": False, "status": "timeout", "id": cid,
                    "output": f"The display did not answer within {wait:g} s; the {action} may or may not have happened."}
        return {"ok": pending["result"]["ok"], "status": "done" if pending["result"]["ok"] else "failed",
                "id": cid, "output": pending["result"]["output"]}

    # ----- live-model surface -------------------------------------------------------------------
    def mint_tools(self):
        """Builtins offered at mint: `globe` + `globe_context` while a GEV display is alive, else none."""
        entry = self.alive(GLOBE)
        if not entry:
            return []
        names = [t["name"] for t in entry["tools"]]
        menu = "; ".join(f"{t['name']}: {t['description']}" if t["description"] else t["name"] for t in entry["tools"])
        return [
            {
                "type": "function",
                "name": "globe",
                "description": ("Move or change the God's Eye View globe on the user's screen. Pick one action and pass "
                                "its args object. Actions: " + menu)[:4000],
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {"type": "string", "enum": names},
                        "args": {"type": "object", "description": "Arguments for the action, e.g. {\"query\":\"Tokyo\"}."},
                    },
                    "required": ["action"],
                    "additionalProperties": False,
                },
            },
            {
                "type": "function",
                "name": "globe_context",
                "description": "Read the globe's current view (camera, active layers, selection). Read only.",
                "parameters": {"type": "object", "properties": {}, "additionalProperties": False},
            },
        ]

    def tool(self, name, args):
        if name == "globe_context":
            if args:
                raise DisplayError("globe_context takes no arguments")
            return {"ok": True, **self.status(GLOBE)}
        if set(args) - {"action", "args"} or "action" not in args:
            raise DisplayError("globe takes action and optional args")
        return self.command(GLOBE, args["action"], args.get("args") or {})

    # ----- internals ----------------------------------------------------------------------------
    def _alive(self, entry):
        return self.clock() - entry["seen"] <= self.stale

    def _owned(self, device, name):
        entry = self.entries.get(name)
        if not entry or entry["device"] != device:
            raise LookupError("display not registered by this device")
        return entry

    @staticmethod
    def _context(value):
        if not isinstance(value, dict) or len(dumps(value)) > 16000:
            raise DisplayError("context must be an object of at most 16000 characters")
        return value
