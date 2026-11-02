"""System-level phone control over groot's adb: volume, do-not-disturb, Bluetooth,
brightness and media, each verified by reading the setting back.

The phone's own app tools (`volume`, `media_control`) can only send one step or one key
and cannot read the result. This lane runs from groot through adb, so it can set exact
levels and report what the phone now says (`landed`), not what was sent.

Fast path, in order:

* tier 0 - a deterministic grammar over the owner's words (well under a millisecond);
* tier 1 - one closed-catalog choice from a fast model (`Z0LIVE_SYSTEM_MODEL_URL`, an
  OpenAI-compatible endpoint such as the vLLM semantic router), only when the words are
  clearly about a system setting but the grammar did not parse them;
* a watcher model reviews each executed action afterwards and records a verdict. It
  never acts, retries or speaks: the LLM watches the loop, it does not control it.

The catalog is closed and reversible. Nothing here can wipe, uninstall, place calls,
send messages, or turn off Wi-Fi (that can cut the adb link the lane runs on).
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path

STREAMS = {"media": 3, "music": 3, "ring": 2, "ringer": 2, "notification": 5,
           "notifications": 5, "alarm": 4, "alarms": 4, "call": 0, "voice": 0}
STREAM_NAMES = {3: "media", 2: "ring", 5: "notification", 4: "alarm", 0: "call"}
ZEN = {"0": "off", "1": "priority only", "2": "total silence", "3": "alarms only"}
WORDS = {"zero": 0, "ten": 10, "twenty": 20, "thirty": 30, "forty": 40, "fifty": 50,
         "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
         "a hundred": 100, "one hundred": 100, "half": 50, "max": 100, "maximum": 100,
         "full": 100, "min": 0, "minimum": 0}
STEP = 15  # percent for "louder"/"turn it up"; "a lot" doubles it

CATALOG = {
    "volume_set": "Set one stream's volume to a percent. args: stream (media|ring|notification|alarm|call), percent 0-100",
    "volume_step": "Raise or lower one stream's volume. args: stream, direction (up|down), big (bool)",
    "mute": "Mute media (remembers the level). args: none",
    "unmute": "Restore media to the level before mute. args: none",
    "dnd": "Do not disturb. args: mode (on|off|alarms)",
    "bluetooth": "Bluetooth radio. args: enabled (bool)",
    "brightness_set": "Screen brightness percent; turns adaptive off. args: percent 0-100",
    "brightness_step": "Screen brighter or dimmer by a fifth. args: direction (up|down)",
    "brightness_auto": "Adaptive brightness. args: enabled (bool)",
    "media": "Media key to the active player. args: action (play|pause|next|previous)",
    "status": "Read volume, do-not-disturb, Bluetooth and brightness without changing anything. args: none",
}

HINT = re.compile(
    r"\b(volume|louder|quieter|softer|mute|unmute|silence|do not disturb|don't disturb|dnd|"
    r"bluetooth|brightness|brighter|dimmer|darker|blinding|screen (?:light|bright)|too (?:loud|quiet|bright|dark)|"
    r"notifications?|interruptions?|leave me alone|crank it|"
    r"music|song|track|playback|pause|resume|skip|ringer|ring(?:tone| volume)|alarm volume)\b",
    re.I,
)
QUESTION = re.compile(r"^\W*(?:why|how come|did|didn't|does|has|have|should|who)\b", re.I)


def system_hint(text: str) -> bool:
    """The words are about a phone system setting (not necessarily parseable)."""
    from . import relay, relay_status
    if not text or relay.wants_relay(text) or relay_status.wants_status(text):
        return False
    return bool(HINT.search(text)) and not QUESTION.match(text)


def _percent(text: str):
    m = re.search(r"(\d{1,3})\s*(?:%|percent|per cent)?", text)
    if m:
        return max(0, min(100, int(m.group(1))))
    for word, value in sorted(WORDS.items(), key=lambda kv: -len(kv[0])):
        if re.search(r"\b" + word + r"\b", text, re.I):
            return value
    return None


def _stream(text: str) -> str:
    m = re.search(r"\b(media|music|ringer|ring|notifications?|alarms?|call|voice)\b", text, re.I)
    return {"music": "media", "ringer": "ring", "notifications": "notification", "alarms": "alarm",
            "voice": "call"}.get(m.group(1).lower(), m.group(1).lower()) if m else "media"


def parse(text: str) -> dict | None:
    """Tier 0: the owner's words -> one catalog action, or None. Never guesses."""
    t = " ".join((text or "").lower().replace("’", "'").split())
    t = re.sub(r"^(?:(?:ok(?:ay)?|hey|yeah|so)[, ]+)?(?:please |can you |could you |would you )*", "", t)
    t = t.rstrip(" .!?")
    if not t or QUESTION.match(t):
        return None
    if re.search(r"\b(what(?:'s| is)|how (?:loud|bright)|check)\b.*\b(volume|brightness|bluetooth|do not disturb|dnd)\b", t):
        return {"action": "status", "args": {}}
    if re.fullmatch(r"(?:un-?mute|unmute (?:the )?(?:music|media|phone|it|sound|audio))", t):
        return {"action": "unmute", "args": {}}
    if re.fullmatch(r"(?:mute|silence)(?: (?:the )?(?:music|media|phone|it|sound|audio|everything))?", t):
        return {"action": "mute", "args": {}}
    if re.search(r"\b(?:no|silence|stop|block|mute|hold) (?:all |my |the |any )?notifications\b|\bnotifications? off\b", t):
        return {"action": "dnd", "args": {"mode": "on"}}
    dnd = re.search(r"\b(do not disturb|don't disturb|dnd|focus mode)\b", t)
    if dnd:
        if re.search(r"\b(off|disable|stop|end|turn off|exit)\b", t):
            return {"action": "dnd", "args": {"mode": "off"}}
        if re.search(r"\balarms?\b", t):
            return {"action": "dnd", "args": {"mode": "alarms"}}
        if re.search(r"\b(on|enable|start|turn on|put .* on|go into|activate)\b", t) or t == dnd.group(1):
            return {"action": "dnd", "args": {"mode": "on"}}
        return None
    if "bluetooth" in t:
        if re.search(r"\b(off|disable|kill)\b", t):
            return {"action": "bluetooth", "args": {"enabled": False}}
        if re.search(r"\b(on|enable|start)\b", t):
            return {"action": "bluetooth", "args": {"enabled": True}}
        return None
    if re.search(r"\b(?:auto|adaptive|automatic) brightness\b", t):
        off = bool(re.search(r"\b(off|disable)\b", t))
        return {"action": "brightness_auto", "args": {"enabled": not off}}
    if re.search(r"\bbrightness\b|\bscreen (?:light|bright)", t):
        p = _percent(t)
        number = bool(re.search(r"\d|\b(max|maximum|min|minimum|full|half)\b", t))
        if p is not None and (number or re.search(r"\b(to|at)\b", t)):
            return {"action": "brightness_set", "args": {"percent": p}}
        if re.search(r"\b(up|raise|increase|higher|more)\b", t):
            return {"action": "brightness_step", "args": {"direction": "up"}}
        if re.search(r"\b(down|lower|decrease|less|reduce)\b", t):
            return {"action": "brightness_step", "args": {"direction": "down"}}
        return None
    shade = re.fullmatch(r"(?:make (?:the )?screen |screen )?(brighter|dimmer|darker)(?: please)?", t)
    if shade:
        return {"action": "brightness_step", "args": {"direction": "up" if shade.group(1) == "brighter" else "down"}}
    media = re.fullmatch(
        r"(pause|play|resume|stop|skip|next|previous|go back)(?: (?:the |this |a |that )?"
        r"(?:music|song|track|playback|it|audio|podcast|video))?(?: (?:song|track))?", t)
    if media:
        verb = media.group(1)
        action = {"resume": "play", "stop": "pause", "skip": "next", "go back": "previous"}.get(verb, verb)
        return {"action": "media", "args": {"action": action}}
    if re.fullmatch(r"(?:next|skip (?:this |the )?)(?:song|track)|skip(?: it| this)?", t):
        return {"action": "media", "args": {"action": "next"}}
    if re.fullmatch(r"(?:previous|last|go back a) (?:song|track)", t):
        return {"action": "media", "args": {"action": "previous"}}
    if "volume" in t or re.search(r"\b(louder|quieter|softer)\b", t) or re.search(r"\bturn (?:it|the music|music|this) (?:up|down)\b", t):
        stream = _stream(t)
        big = bool(re.search(r"\b(a lot|way|much|lot)\b", t))
        if re.search(r"\b(max|maximum|full|all the way up)\b", t):
            return {"action": "volume_set", "args": {"stream": stream, "percent": 100}}
        if re.search(r"\b(louder|up|raise|increase|higher)\b", t) and not re.search(r"\bto\b", t):
            return {"action": "volume_step", "args": {"stream": stream, "direction": "up", "big": big}}
        if re.search(r"\b(quieter|softer|down|lower|decrease|reduce)\b", t) and not re.search(r"\bto\b", t):
            return {"action": "volume_step", "args": {"stream": stream, "direction": "down", "big": big}}
        p = _percent(re.sub(r"\b(media|music|ringer|ring|notification|alarm|call)\b", "", t))
        if p is not None:
            return {"action": "volume_set", "args": {"stream": stream, "percent": p}}
    return None


def validate(choice) -> dict | None:
    """Accept a model's JSON only if it names a catalog action with well-typed args."""
    if not isinstance(choice, dict) or not isinstance(choice.get("args", {}), dict):
        return None
    action, args = choice.get("action"), dict(choice.get("args") or {})
    if action not in CATALOG:
        return None
    if "stream" in args and args["stream"] not in STREAMS:
        return None
    if "percent" in args:
        if not isinstance(args["percent"], (int, float)) or not 0 <= args["percent"] <= 100:
            return None
        args["percent"] = int(args["percent"])
    if action in {"volume_set"} and "percent" not in args:
        return None
    if action == "volume_step" and args.get("direction") not in {"up", "down"}:
        return None
    if action == "dnd" and args.get("mode") not in {"on", "off", "alarms"}:
        return None
    if action in {"bluetooth", "brightness_auto"} and not isinstance(args.get("enabled"), bool):
        return None
    if action == "brightness_set" and "percent" not in args:
        return None
    if action == "media" and args.get("action") not in {"play", "pause", "next", "previous"}:
        return None
    return {"action": action, "args": args}


# -- adb ------------------------------------------------------------------------------

class Adb:
    """Run shell commands on the owner's phone, resolved by its hardware serial number.

    `Z0LIVE_PHONE_SERIALNO` pins the phone (ro.serialno). Among transports that reach it,
    the tailnet one is preferred. Without the pin, exactly one 100.x transport is required,
    the same rule the gesture fallback uses; several candidates are never guessed between.
    """

    def __init__(self, serialno=None, runner=subprocess.run, timeout=4.0):
        self.serialno = serialno if serialno is not None else os.environ.get("Z0LIVE_PHONE_SERIALNO", "")
        self.runner, self.timeout = runner, timeout
        self.cache: tuple[float, str | None] = (0.0, None)
        self.lock = threading.Lock()

    def _run(self, argv, timeout=None):
        return self.runner(argv, capture_output=True, text=True, timeout=timeout or self.timeout, check=False)

    def transport(self) -> str | None:
        with self.lock:
            stamp, serial = self.cache
            if serial and time.monotonic() - stamp < 60:
                return serial
            try:
                out = self._run(["adb", "devices"], timeout=3).stdout
            except (OSError, subprocess.TimeoutExpired):
                return None
            ready = [ln.split()[0] for ln in out.splitlines()[1:] if len(ln.split()) >= 2 and ln.split()[1] == "device"]
            if self.serialno:
                matches = []
                for t in ready:
                    try:
                        got = self._run(["adb", "-s", t, "shell", "getprop", "ro.serialno"], timeout=3).stdout.strip()
                    except (OSError, subprocess.TimeoutExpired):
                        continue
                    if got == self.serialno:
                        matches.append(t)
                matches.sort(key=lambda t: (not t.startswith("100."), t))
                serial = matches[0] if matches else None
            else:
                tailnet = [t for t in ready if t.startswith("100.") and ":" in t]
                serial = tailnet[0] if len(tailnet) == 1 else None
            self.cache = (time.monotonic(), serial)
            return serial

    def sh(self, command: str) -> str:
        serial = self.transport()
        if not serial:
            raise PhoneUnavailable("adb_unavailable")
        try:
            result = self._run(["adb", "-s", serial, "shell", command])
        except subprocess.TimeoutExpired as exc:
            raise PhoneUnavailable("adb_timeout") from exc
        except OSError as exc:
            raise PhoneUnavailable("adb_unavailable") from exc
        if result.returncode:
            with self.lock:
                self.cache = (0.0, None)
            raise PhoneUnavailable("adb_failed: " + (result.stderr or result.stdout).strip()[:120])
        return result.stdout


class PhoneUnavailable(RuntimeError):
    pass


def _volume(adb, stream: int) -> tuple[int, int]:
    out = adb.sh(f"cmd media_session volume --stream {stream} --get")
    m = re.search(r"volume is (\d+) in range \[(\d+)\.\.(\d+)\]", out)
    if not m:
        raise PhoneUnavailable("volume_unreadable")
    return int(m.group(1)), int(m.group(3))


def _pct(level, top):
    return round(100 * level / top) if top else 0


class System:
    """Execute catalog actions and read the result back from the phone."""

    def __init__(self, adb=None, sleep=time.sleep):
        self.adb = adb or Adb()
        self.sleep = sleep
        self.muted_from: int | None = None

    def run(self, action: str, args: dict) -> dict:
        started = time.perf_counter()
        try:
            out = getattr(self, "do_" + action)(**args)
        except PhoneUnavailable as exc:
            out = {"ok": False, "landed": False, "status": str(exc),
                   "say": "I can't reach your phone's system controls right now."}
        except TypeError:
            out = {"ok": False, "landed": False, "status": "bad_args", "say": "I couldn't work out that setting."}
        out["ms"] = round((time.perf_counter() - started) * 1000, 1)
        out["action"], out["args"] = action, args
        return out

    def _set_volume(self, stream, level):
        self.adb.sh(f"cmd media_session volume --stream {stream} --set {level}")
        return _volume(self.adb, stream)

    def do_volume_set(self, stream="media", percent=50):
        code = STREAMS[stream]
        _, top = _volume(self.adb, code)
        want = round(percent * top / 100)
        now, top = self._set_volume(code, want)
        name = STREAM_NAMES[code]
        landed = now == want
        return {"ok": landed, "landed": landed, "level": now, "max": top,
                "say": f"{name.capitalize()} volume is at {_pct(now, top)} percent." if landed
                else f"I asked for {percent} percent but {name} volume reads {_pct(now, top)} percent."}

    def do_volume_step(self, stream="media", direction="up", big=False):
        code = STREAMS[stream]
        before, top = _volume(self.adb, code)
        delta = max(1, round((STEP * (2 if big else 1)) * top / 100))
        want = max(0, min(top, before + (delta if direction == "up" else -delta)))
        now, top = self._set_volume(code, want)
        name = STREAM_NAMES[code]
        if before == want:
            return {"ok": True, "landed": True, "level": now, "max": top,
                    "say": f"{name.capitalize()} volume is already at {'max' if direction == 'up' else 'zero'}."}
        landed = now == want
        return {"ok": landed, "landed": landed, "level": now, "max": top, "before": before,
                "say": f"{name.capitalize()} volume {'up' if direction == 'up' else 'down'} to {_pct(now, top)} percent."
                if landed else f"{name.capitalize()} volume didn't change; it reads {_pct(now, top)} percent."}

    def do_mute(self):
        before, top = _volume(self.adb, 3)
        if before:
            self.muted_from = before
        now, _ = self._set_volume(3, 0)
        return {"ok": now == 0, "landed": now == 0, "level": now, "before": before,
                "say": "Media is muted." if now == 0 else "Mute didn't take; media volume is still up."}

    def do_unmute(self):
        before, top = _volume(self.adb, 3)
        want = self.muted_from or max(1, round(top * 0.4))
        now, top = self._set_volume(3, want)
        self.muted_from = None
        return {"ok": now == want, "landed": now == want, "level": now, "before": before,
                "say": f"Media volume is back at {_pct(now, top)} percent."}

    def do_dnd(self, mode="on"):
        arg = {"on": "priority", "off": "off", "alarms": "alarms"}[mode]
        self.adb.sh(f"cmd notification set_dnd {arg}")
        zen = self.adb.sh("settings get global zen_mode").strip()
        want = {"on": "1", "off": "0", "alarms": "3"}[mode]
        landed = zen == want
        return {"ok": landed, "landed": landed, "zen_mode": zen,
                "say": ("Do not disturb is off." if mode == "off" else f"Do not disturb is on, {ZEN[want]}.")
                if landed else f"Do not disturb reads {ZEN.get(zen, zen)}."}

    def do_bluetooth(self, enabled=True):
        self.adb.sh("cmd bluetooth_manager " + ("enable" if enabled else "disable"))
        want = "1" if enabled else "0"
        state = ""
        for _ in range(12):  # the radio takes a moment; read until it settles (<= ~3 s)
            state = self.adb.sh("settings get global bluetooth_on").strip()
            if state == want:
                break
            self.sleep(0.25)
        landed = state == want
        word = "on" if enabled else "off"
        return {"ok": landed, "landed": landed, "bluetooth_on": state,
                "say": f"Bluetooth is {word}." if landed else f"I asked for Bluetooth {word}, but it hasn't changed yet."}

    def _brightness(self):
        value = int(self.adb.sh("settings get system screen_brightness").strip() or 0)
        mode = self.adb.sh("settings get system screen_brightness_mode").strip()
        return value, mode

    def do_brightness_set(self, percent=50):
        want = max(1, round(255 * percent / 100))
        self.adb.sh("settings put system screen_brightness_mode 0")
        self.adb.sh(f"settings put system screen_brightness {want}")
        value, mode = self._brightness()
        landed = value == want and mode == "0"
        return {"ok": landed, "landed": landed, "brightness": value, "adaptive": mode == "1",
                "say": f"Brightness is at {round(100 * value / 255)} percent, adaptive off." if landed
                else f"Brightness reads {round(100 * value / 255)} percent."}

    def do_brightness_step(self, direction="up"):
        value, _ = self._brightness()
        percent = round(100 * value / 255) + (20 if direction == "up" else -20)
        return self.do_brightness_set(max(1, min(100, percent)))

    def do_brightness_auto(self, enabled=True):
        self.adb.sh(f"settings put system screen_brightness_mode {1 if enabled else 0}")
        _, mode = self._brightness()
        landed = mode == ("1" if enabled else "0")
        return {"ok": landed, "landed": landed, "adaptive": mode == "1",
                "say": f"Adaptive brightness is {'on' if enabled else 'off'}." if landed else "Adaptive brightness didn't change."}

    def _playback(self):
        """The first active media session: {package, state}, or None when nothing is active."""
        out = self.adb.sh("dumpsys media_session")
        detail = out.split("=== DETAILED SESSION INFO ===", 1)[-1].split("\nUser Records:", 1)[0]
        # Each session is a 2-space-indented header followed by 4-space-indented fields.
        for block in re.split(r"\n(?=  \S)", detail):
            if re.search(r"^\s+active=true\s*$", block, re.M):
                m = re.search(r"state=PlaybackState \{state=([A-Z_]+|\d+)", block)
                pkg = re.search(r"^\s+package=([\w.]+)", block, re.M)
                return {"state": m.group(1) if m else None, "package": pkg.group(1) if pkg else None}
        return None

    def do_media(self, action="pause"):
        before = self._playback()
        self.adb.sh(f"cmd media_session dispatch {action}")
        after = before
        for _ in range(6):
            self.sleep(0.15)
            after = self._playback()
            if action in {"next", "previous"} or (after and after.get("state") != (before or {}).get("state")):
                break
        state = (after or {}).get("state")
        playing = state in {"PLAYING", "3"}
        paused = state in {"PAUSED", "STOPPED", "2", "1"}
        if action == "play":
            landed = playing
        elif action == "pause":
            landed = paused or after is None
        else:
            landed = None  # a track change has no reliable state to read
        if after is None and action == "play":
            say = "Nothing is set up to play right now."
        elif landed is None:
            say = f"Sent {action}."
        elif landed:
            say = {"play": "Playing.", "pause": "Paused."}[action]
        else:
            say = f"I sent {action}, but the player still says {str(state).lower()}."
        return {"ok": landed is not False, "landed": landed, "player": after, "say": say}

    def do_status(self):
        level, top = _volume(self.adb, 3)
        zen = self.adb.sh("settings get global zen_mode").strip()
        bt = self.adb.sh("settings get global bluetooth_on").strip()
        value, mode = self._brightness()
        say = (f"Media volume {_pct(level, top)} percent, do not disturb {ZEN.get(zen, zen)}, "
               f"Bluetooth {'on' if bt == '1' else 'off'}, brightness {round(100 * value / 255)} percent"
               + (" adaptive." if mode == "1" else "."))
        return {"ok": True, "landed": True, "media": [level, top], "zen_mode": zen, "bluetooth_on": bt,
                "brightness": value, "adaptive": mode == "1", "say": say}


# -- fast model tier and watcher -------------------------------------------------------

MODEL_URL = os.environ.get("Z0LIVE_SYSTEM_MODEL_URL", "http://10.43.216.55:8899/v1/chat/completions")
MODEL_TIMEOUT = float(os.environ.get("Z0LIVE_SYSTEM_MODEL_TIMEOUT", "2.5"))
# Tried in order until one returns a usable answer. Explicit models through the vLLM
# semantic router (Cerebras/Groq) answer a catalog choice in ~0.15-0.25 s; its `auto`
# recipe fans out and can return 403 for some prompts, so name the model. groot's
# llama-server router is the local fallback (~0.1-0.3 s warm, ~15 s when it has unloaded).


def _router(model, **extra):
    return {"url": MODEL_URL, "model": model, "extra": {"max_tokens": 400, "reasoning_effort": "low", **extra}}


GROOT = {"url": os.environ.get("Z0LIVE_TIER1_URL", "http://100.113.138.100:11530/v1/chat/completions"),
         "model": os.environ.get("Z0LIVE_TIER1_MODEL", "qwen3-4b-q4km"),
         "extra": {"chat_template_kwargs": {"enable_thinking": False}, "max_tokens": 120}}
CHOOSER_CHAIN = [_router(os.environ.get("Z0LIVE_SYSTEM_MODEL", "cerebras-qwen-3.8-27b")),
                 _router("groq-gpt-oss-20b"), GROOT]
WATCH_CHAIN = [_router(os.environ.get("Z0LIVE_SYSTEM_WATCH_MODEL", "cerebras-gpt-oss-120b"), reasoning_effort="medium",
                       max_tokens=1500),
               _router("cerebras-qwen-3.8-27b"), GROOT]


def _chat_one(prompt, endpoint, timeout, opener=urllib.request.urlopen):
    body = {"model": endpoint["model"], "temperature": 0, "max_tokens": 120,
            "messages": [{"role": "user", "content": prompt}], **endpoint.get("extra", {})}
    # Cloudflare in front of Cerebras/Groq refuses the default Python-urllib agent (403).
    req = urllib.request.Request(endpoint["url"], json.dumps(body).encode(),
                                 {"Content-Type": "application/json", "User-Agent": "z0live-phone-system/1"})
    started = time.perf_counter()
    with opener(req, timeout=timeout) as response:
        reply = json.load(response)
    text = (reply.get("choices") or [{}])[0].get("message", {}).get("content") or ""
    return text, round((time.perf_counter() - started) * 1000, 1), reply.get("model") or endpoint["model"]


def chain_chat(chain, timeout=MODEL_TIMEOUT, opener=urllib.request.urlopen):
    """A chat function over endpoints in order; the first JSON-bearing reply wins."""
    def chat(prompt):
        errors = []
        for endpoint in chain:
            try:
                text, ms, model = _chat_one(prompt, endpoint, timeout, opener)
            except Exception as exc:  # noqa: BLE001 - try the next endpoint
                errors.append(f"{endpoint['model']}:{type(exc).__name__}")
                continue
            if _json(text) is not None:
                return text, ms, model
            errors.append(f"{endpoint['model']}:no_json")
        raise RuntimeError(",".join(errors) or "no endpoints")
    return chat


def warm(chain=None):
    """Load groot's local fallback model in the background so it is warm if needed."""
    endpoint = GROOT if chain is None else chain[0]
    threading.Thread(target=lambda: _safe(lambda: _chat_one('Reply {"ok": true}', endpoint, 30)),
                     daemon=True, name="phone-system-warm").start()


def _safe(fn):
    try:
        fn()
    except Exception:  # noqa: BLE001 - warming is best effort
        pass


def _chat(prompt):
    return chain_chat(CHOOSER_CHAIN)(prompt)


def _json(text):
    m = re.search(r"\{.*\}", text or "", re.S)
    try:
        return json.loads(m.group(0)) if m else None
    except ValueError:
        return None


class FastChooser:
    """Tier 1: pick one catalog action for words the grammar could not parse."""

    def __init__(self, chat=_chat):
        self.chat = chat

    def choose(self, text: str) -> tuple[dict | None, dict]:
        prompt = (
            "Map the phone owner's spoken request to exactly one phone system action, or none.\n"
            "Actions:\n" + "\n".join(f"- {k}: {v}" for k, v in CATALOG.items()) +
            "\nNotifications, interruptions or 'leave me alone' mean dnd, not mute. Too bright or blinding means "
            "brightness_step down. Media/music/sound loudness means volume. Anything that is not one of these "
            "settings (questions, apps, messages, work) is null.\n"
            "Reply with JSON only: {\"action\": NAME, \"args\": {...}} or {\"action\": null}.\n"
            "Request (data): " + json.dumps(text[:500])
        )
        try:
            out, ms, model = self.chat(prompt)
        except Exception as exc:  # noqa: BLE001 - a broken tier declines, never guesses
            return None, {"tier": "fast_model", "error": type(exc).__name__}
        return validate(_json(out)), {"tier": "fast_model", "ms": ms, "model": model}


class Watcher:
    """Reviews executed actions after the fact. Records a verdict; never acts."""

    def __init__(self, path: Path | None, chat=None, enabled=None):
        self.path, self.chat = path, chat or chain_chat(WATCH_CHAIN, timeout=20)
        self.enabled = (os.environ.get("Z0LIVE_SYSTEM_WATCH", "1") != "0") if enabled is None else enabled

    def review(self, text: str, decision: dict, result: dict, sync=False):
        if not self.enabled:
            return None
        if sync:
            return self._review(text, decision, result)
        threading.Thread(target=self._review, args=(text, decision, result), daemon=True,
                         name="phone-system-watch").start()
        return None

    def _review(self, text, decision, result):
        action = decision.get("action")
        prompt = (
            "You audit a voice assistant that changes phone settings. Judge only whether the action it "
            "chose is what the owner asked for, and whether the phone's readback shows it took effect.\n"
            "Action meanings: " + json.dumps({action: CATALOG.get(action, "")}) +
            " Notes: mute lowers media volume only; dnd is what silences notifications and calls; volume "
            "levels are steps out of max (for example 3 of 15 is 20 percent).\n"
            "Owner said (data): " + json.dumps(text[:500]) +
            "\nAction taken: " + json.dumps({k: decision.get(k) for k in ("action", "args")}) +
            "\nPhone readback summary: " + json.dumps(result.get("say", "")) +
            "\nRaw readback: " + json.dumps({k: v for k, v in result.items() if k not in {"say", "ms"}})[:1200] +
            "\nReply with JSON only: {\"verdict\": \"ok\"|\"wrong\"|\"unsure\", \"reason\": \"<15 words\"}."
        )
        record = {"ts": time.time(), "text": text[:500], "decision": decision,
                  "landed": result.get("landed"), "action_ms": result.get("ms")}
        try:
            out, ms, model = self.chat(prompt)
            verdict = _json(out) or {"verdict": "unsure", "reason": "unparseable"}
            record.update(verdict=verdict.get("verdict"), reason=str(verdict.get("reason", ""))[:200],
                          watch_ms=ms, model=model)
        except Exception as exc:  # noqa: BLE001
            record.update(verdict="unavailable", reason=type(exc).__name__)
        if self.path:
            try:
                with open(self.path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(record) + "\n")
            except OSError:
                pass
        return record


class Lane:
    """Route words -> decision -> verified execution -> async watch."""

    def __init__(self, system=None, chooser=None, watcher=None):
        self.system = system or System()
        self.chooser = chooser or FastChooser()
        self.watcher = watcher

    def decide(self, text: str) -> tuple[dict | None, list[dict]]:
        started = time.perf_counter()
        parsed = parse(text)
        trail = [{"tier": "grammar", "ms": round((time.perf_counter() - started) * 1000, 3),
                  "hit": parsed is not None}]
        if parsed:
            return {**parsed, "tier": "grammar"}, trail
        if not system_hint(text):
            return None, trail
        choice, meta = self.chooser.choose(text)
        trail.append({**meta, "hit": choice is not None})
        return ({**choice, "tier": "fast_model"} if choice else None), trail

    def handle(self, text: str) -> tuple[dict | None, dict | None, list[dict]]:
        decision, trail = self.decide(text)
        if not decision:
            return None, None, trail
        result = self.system.run(decision["action"], decision["args"])
        if self.watcher:
            self.watcher.review(text, decision, result)
        return decision, result, trail
