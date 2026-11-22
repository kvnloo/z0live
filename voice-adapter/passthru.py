"""Passthru: z0 joins Kevin's other call (Meet, Zoom web, Teams, any WebRTC page, canvas, whiteboard).

Android lets only one ordinary app hear the mic, and call audio (USAGE_VOICE_COMMUNICATION) cannot
be captured, so the phone cannot merge a Meet call into the z0 call. Instead a browser on the Studio
joins the same meeting as "z0" (passthru/meeting_leg.mjs). This module bridges that browser to the
live z0 call:

  meeting participants ──browser tap──▶ RoomAudio ──mixed into──▶ z0's ears (provider input)
  z0's voice (provider output) ──AudioBridge tee──▶ browser mic ──▶ meeting

Floor policy (``Z0LIVE_PASSTHRU_FLOOR``, default ``addressed``): in a meeting z0 speaks into the room
only for a turn that names it ("z0, ...", "hey zero", "Hermes, ..."). ``open`` lets every reply
through, ``listen`` never speaks into the room. Kevin's own phone leg keeps hearing every reply
unless ``Z0LIVE_PASSTHRU_PHONE`` is ``mute``.

Voice control is parsed from Kevin's own transcript (``command()``), so it works with any engine and
needs no model tool: "z0 merge the calls", "join my meeting", "share my screen", "share the canvas",
"stop sharing", "leave the meeting".
"""
from __future__ import annotations

import asyncio
from collections import deque
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import secrets
import shutil
import subprocess
import time
from typing import Callable

import numpy as np

HERE = Path(__file__).resolve().parent
LEG_SCRIPT = HERE / "passthru" / "meeting_leg.mjs"
FRAME = 960            # 20 ms at 48 kHz
MAX_ROOM_FRAMES = 12   # 240 ms of room audio buffered at most; older frames are dropped, not delayed
ROOM_GAIN = float(os.environ.get("Z0LIVE_PASSTHRU_ROOM_GAIN", "1.0"))
REPLY_TAIL = 1.2       # seconds a released reply keeps flowing after turn.done (WebRTC audio drains late)

MEETING_URL = re.compile(
    r"https?://(?:[\w-]+\.)*(?:meet\.google\.com|zoom\.us|teams\.microsoft\.com|teams\.live\.com|meet\.jit\.si|"
    r"whereby\.com|discord\.com|webex\.com|excalidraw\.com|tldraw\.com|miro\.com|figma\.com)/[^\s\"'<>)]*",
    re.I)
ANY_URL = re.compile(r"https?://[^\s\"'<>)]+", re.I)

_NAME = r"(?:z0|z zero|zero|zed zero|zee zero|z-zero|hermes)"
_VOCATIVE = re.compile(rf"(?:^|[.!?,;]\s*|\b(?:hey|hi|ok|okay|yo)\s*,?\s*){_NAME}\b", re.I)
_COMMANDS = (
    ("join", re.compile(r"\b(?:merge (?:the |my |our |both )?calls?|merge (?:us|me) in|passthr(?:u|ough)(?: mode)?(?: on)?|"
                        r"(?:join|hop into|come (?:in|into)|get on) (?:my|the|this) (?:meeting|call|meet|zoom|video call)|"
                        r"bring (?:you|yourself) (?:in|into))\b", re.I)),
    ("leave", re.compile(r"\b(?:leave (?:the |my )?(?:meeting|call)|(?:end|stop|turn off) passthr(?:u|ough)|unmerge|"
                         r"drop (?:out of|off) (?:the )?(?:meeting|call))\b", re.I)),
    ("unshare", re.compile(r"\b(?:stop (?:sharing|presenting|the share|screen ?share)|unshare)\b", re.I)),
    ("share", re.compile(r"\b(?:share|present|show (?:them|everyone))\s+(?:my |the |your )?"
                         r"(?P<what>phone|screen|canvas|atlas|whiteboard|board|tv|this|that)\b", re.I)),
    ("floor", re.compile(r"\b(?:(?P<open>talk freely|speak freely|open floor)|(?P<listen>just listen|listen only|stay quiet|"
                         r"be quiet)|(?P<addressed>only (?:speak|talk) when (?:i |someone )?(?:say|call|address)))\b", re.I)),
)


def addressed(text: str) -> bool:
    return bool(_VOCATIVE.search(text or ""))


def command(text: str, *, active: bool) -> tuple[str, str] | None:
    """Parse a passthru command from Kevin's transcript. Outside passthru only "join" counts, and
    only when Kevin names z0 or says "passthru"/"merge", so ordinary talk about meetings stays talk."""
    text = (text or "").strip()
    if not text:
        return None
    for name, rx in _COMMANDS:
        m = rx.search(text)
        if not m:
            continue
        if name == "join":
            if not active and not (addressed(text) or re.search(r"merge|passthr", text, re.I)):
                continue
            url = MEETING_URL.search(text) or ANY_URL.search(text)
            return "join", url.group(0) if url else ""
        if not active:
            return None
        if name == "share":
            return "share", m.group("what").lower()
        if name == "floor":
            return "floor", next(k for k in ("open", "listen", "addressed") if m.group(k))
        return name, ""
    return None


# ---------------------------------------------------------------------------- link discovery
def phone_links(adb: Callable[..., str], now: float | None = None) -> list[tuple[str, str]]:
    """Meeting links the phone can see right now, best first: (url, where-found).

    Calendar events in progress (or starting within 15 min), then notifications, then the foreground
    activity. ``adb(*args)`` returns the shell output of ``adb shell args``."""
    now = now or time.time()
    found: list[tuple[str, str]] = []

    def add(text: str, where: str):
        for m in MEETING_URL.finditer(text or ""):
            url = m.group(0).rstrip(".,;")
            if all(url != u for u, _ in found):
                found.append((url, where))

    lo, hi = int((now - 3 * 3600) * 1000), int((now + 15 * 60) * 1000)
    rows = adb("content", "query", "--uri", "content://com.android.calendar/instances/when/%d/%d" % (lo, hi),
               "--projection", "title:begin:end:eventLocation:description")
    live = []
    for row in (rows or "").splitlines():
        b, e = re.search(r"\bbegin=(\d+)", row), re.search(r"\bend=(\d+)", row)
        if b and e and int(b.group(1)) - 15 * 60_000 <= now * 1000 <= int(e.group(1)):
            live.append((abs(int(b.group(1)) - now * 1000), row))
    for _, row in sorted(live):
        add(row, "calendar")
    add(adb("dumpsys", "notification", "--noredact"), "notification")
    add(adb("dumpsys", "activity", "activities"), "screen")
    return found


# ---------------------------------------------------------------------------- audio
def to_mono_s16(frame) -> np.ndarray:
    """An aiortc/PyAV audio frame as int16 mono at its own rate (aiortc decodes Opus to 48 kHz s16)."""
    a = frame.to_ndarray()
    if a.dtype != np.int16:
        a = (np.clip(a.astype(np.float32), -1, 1) * 32767).astype(np.int16)
    channels = len(frame.layout.channels)
    if a.shape[0] == 1 and channels > 1:          # packed interleaved: (1, samples*channels)
        a = a.reshape(-1, channels).mean(axis=1)
    elif a.shape[0] > 1:                          # planar: (channels, samples)
        a = a.mean(axis=0)
    return np.asarray(a, dtype=np.float32).reshape(-1).astype(np.int16)


class RoomAudio:
    """Meeting audio from the browser, in 20 ms int16 mono chunks; bounded so z0 never hears the room late."""

    def __init__(self):
        self.chunks: deque[np.ndarray] = deque(maxlen=MAX_ROOM_FRAMES)
        self.dropped = self.received = 0
        self.loud_at = float("-inf")

    def push(self, pcm: bytes):
        a = np.frombuffer(pcm, dtype=np.int16)
        for i in range(0, len(a) - FRAME + 1, FRAME):
            if len(self.chunks) == self.chunks.maxlen:
                self.dropped += 1
            chunk = a[i:i + FRAME]
            if np.abs(chunk).max(initial=0) > 900:
                self.loud_at = time.monotonic()
            self.chunks.append(chunk)
            self.received += 1

    def take(self, n: int) -> np.ndarray | None:
        out, have = [], 0
        while self.chunks and have < n:
            out.append(self.chunks.popleft()); have += FRAME
        if not out:
            return None
        a = np.concatenate(out)
        if len(a) > n:  # put the remainder back at the front
            self.chunks.appendleft(a[n:]); a = a[:n]
        return np.pad(a, (0, n - len(a)))


def mix_into(frame, room: np.ndarray | None, gain: float = ROOM_GAIN):
    """A copy of an s16 mic frame with room audio added (same layout, rate and timestamps)."""
    if room is None:
        return frame
    import av
    a = frame.to_ndarray().astype(np.int32)
    channels = len(frame.layout.channels)
    r = (room.astype(np.float32) * gain).astype(np.int32)
    if frame.format.is_planar:                      # (channels, samples)
        a += r[None, : a.shape[1]]
    else:                                           # packed: (1, samples*channels)
        a += np.repeat(r, channels)[None, : a.shape[1]]
    out = av.AudioFrame.from_ndarray(np.clip(a, -32768, 32767).astype(np.int16), format=frame.format.name,
                                     layout=frame.layout.name)
    out.sample_rate, out.pts = frame.sample_rate, frame.pts
    if frame.time_base is not None:
        out.time_base = frame.time_base
    return out


# ---------------------------------------------------------------------------- floor
@dataclass
class Floor:
    policy: str = os.environ.get("Z0LIVE_PASSTHRU_FLOOR", "addressed")
    released_at: float = float("-inf")   # Kevin or a guest named z0 at this time
    reply_open: bool = False             # the current assistant reply may reach the room
    tail_until: float = float("-inf")

    def user_said(self, text: str, now: float):
        if addressed(text):
            self.released_at = now

    def reply_started(self, now: float):
        self.reply_open = self.policy == "open" or (self.policy == "addressed" and now - self.released_at < 20)

    def reply_done(self, now: float):
        if self.reply_open:
            self.tail_until = now + REPLY_TAIL
        self.reply_open = False

    def may_speak(self, now: float) -> bool:
        if self.policy == "listen":
            return False
        return self.policy == "open" or self.reply_open or now < self.tail_until


# ---------------------------------------------------------------------------- the leg
class Passthru:
    """One call's meeting leg: the browser process, its loopback socket and the audio both ways."""

    def __init__(self, call, *, node: str | None = None, script: Path = LEG_SCRIPT):
        self.call = call
        self.node = node or os.environ.get("Z0LIVE_NODE") or shutil.which("node") or "node"
        self.script = script
        self.token = secrets.token_urlsafe(24)
        self.room = RoomAudio()
        self.floor = Floor()
        self.proc: asyncio.subprocess.Process | None = None
        self.ws = None
        self.url = ""
        self.sharing = ""
        self.started_at = None
        self.voice_out = bytearray()
        self.counts = {"voice_frames": 0, "voice_sent": 0, "mixed": 0}
        self.phone_output = os.environ.get("Z0LIVE_PASSTHRU_PHONE", "keep")  # keep | mute
        self.runner = None
        self.base = ""        # http://127.0.0.1:PORT of this leg's loopback app
        self.mirror = None    # PhoneMirror while the phone screen is shared

    @property
    def active(self) -> bool:
        return self.proc is not None and self.proc.returncode is None

    def trace(self, ev, **data):
        self.call.trace("passthru." + ev, **data)

    # -- lifecycle
    async def start(self, url: str, ws_url: str = "", *, share: str = "", headless: bool = True):
        if self.active:
            await self.stop("rejoin")
        self.url, self.started_at = url, time.monotonic()
        if not ws_url:
            ws_url = await self.serve()
        cfg = {"url": url, "ws": ws_url + "?call=" + self.call.id, "token": self.token,
               "name": os.environ.get("Z0LIVE_PASSTHRU_NAME", "z0 (Kevin's assistant)"),
               "headless": headless, "share": share}
        self.proc = await asyncio.create_subprocess_exec(
            self.node, str(self.script), json.dumps(cfg), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL)
        self.trace("start", url=url, share=share, floor=self.floor.policy, pid=self.proc.pid)
        self.call.spawn(self._read())

    async def _read(self):
        proc = self.proc
        while proc and proc.stdout:
            line = await proc.stdout.readline()
            if not line:
                break
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            name = str(ev.pop("ev", "event"))[:40]
            if name == "stats":
                ev["room_frames"], ev["room_dropped"] = self.room.received, self.room.dropped
                ev.update(self.counts)
            self.trace("leg." + name, **{k: v for k, v in ev.items() if k != "t"})
        if proc:
            await proc.wait()
            self.trace("exit", code=proc.returncode)

    async def send(self, cmd: str, **data):
        if self.active and self.proc.stdin:
            self.proc.stdin.write((json.dumps({"cmd": cmd, **data}) + "\n").encode())
            await self.proc.stdin.drain()

    async def share(self, target_url: str, label: str):
        self.sharing = label
        await self.send("share", url=target_url)
        self.trace("share", target=label)

    async def unshare(self):
        self.sharing = ""
        await self.send("unshare")
        if self.mirror:
            await self.mirror.stop(); self.mirror = None

    async def serve(self) -> str:
        """This leg's loopback app: the browser socket and the phone mirror page. Returns the ws URL."""
        from aiohttp import web

        async def ws(request):
            sock = web.WebSocketResponse(max_msg_size=1 << 20, heartbeat=20)
            await sock.prepare(request)
            await self.socket(sock)
            return sock

        async def phone_page(request):
            if not secrets.compare_digest(request.query.get("k", ""), self.token):
                raise web.HTTPForbidden()
            return web.Response(content_type="text/html", text=(
                '<!doctype html><title>phone</title><body style="margin:0;background:#000;display:flex;'
                'align-items:center;justify-content:center;height:100vh"><img style="max-height:100vh;max-width:100vw" '
                f'src="/phone.mjpg?k={self.token}">'))

        async def phone_stream(request):
            if not secrets.compare_digest(request.query.get("k", ""), self.token) or not self.mirror:
                raise web.HTTPForbidden()
            resp = web.StreamResponse(headers={"Content-Type": "multipart/x-mixed-replace; boundary=f",
                                               "Cache-Control": "no-store"})
            await resp.prepare(request)
            async for jpg in self.mirror.frames():
                await resp.write(b"--f\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n" % len(jpg) + jpg + b"\r\n")
            return resp

        app = web.Application()
        app.router.add_get("/ws", ws)
        app.router.add_get("/phone", phone_page)
        app.router.add_get("/phone.mjpg", phone_stream)
        self.runner = web.AppRunner(app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"
        return f"ws://127.0.0.1:{port}/ws"

    async def stop(self, reason: str):
        if not self.proc:
            return
        proc, self.proc = self.proc, None
        try:
            if proc.returncode is None and proc.stdin:
                proc.stdin.write(b'{"cmd":"leave"}\n')
                await proc.stdin.drain()
            await asyncio.wait_for(proc.wait(), 8)
        except (asyncio.TimeoutError, BrokenPipeError, ConnectionResetError):
            proc.kill()
        if self.ws is not None:
            await self.ws.close()
            self.ws = None
        if self.runner is not None:
            await self.runner.cleanup()
            self.runner, self.base = None, ""
        self.sharing = ""
        self.trace("stop", reason=reason, secs=round(time.monotonic() - (self.started_at or time.monotonic()), 1),
                   room_frames=self.room.received, room_dropped=self.room.dropped, **self.counts)

    # -- audio
    def ears(self, mic_frame):
        """Called on every mic frame bound for the provider: adds the meeting to what z0 hears."""
        if not self.active:
            return mic_frame
        room = self.room.take(mic_frame.samples)
        if room is not None:
            self.counts["mixed"] += 1
        return mix_into(mic_frame, room)

    def voice(self, out_frame):
        """Called on every z0 output frame: forwards it to the meeting when the floor allows."""
        if not self.active or self.ws is None or self.ws.closed:
            return
        self.counts["voice_frames"] += 1
        if not self.floor.may_speak(time.monotonic()):
            return
        self.voice_out += to_mono_s16(out_frame).tobytes()
        if len(self.voice_out) >= FRAME * 2 * 2:  # ship 40 ms at a time
            data, self.voice_out = bytes(self.voice_out), bytearray()
            self.counts["voice_sent"] += 1
            self.call.spawn(self.ws.send_bytes(data))

    def phone_muted(self) -> bool:
        return self.active and self.phone_output == "mute"

    # -- the browser's socket (served by the adapter's aiohttp app)
    async def socket(self, ws):
        first = await ws.receive()
        try:
            hello = json.loads(first.data) if isinstance(first.data, str) else {}
        except ValueError:
            hello = {}
        if not secrets.compare_digest(str(hello.get("token", "")), self.token):
            await ws.close(code=4401)
            return
        if hello.get("frame") != "top" and self.ws is not None and not self.ws.closed:
            await ws.close(code=4409)   # a sub-frame never displaces the meeting page's socket
            return
        self.ws = ws
        self.trace("socket", href=str(hello.get("href", ""))[:200], frame=hello.get("frame"))
        async for msg in ws:
            if msg.type.name == "BINARY":
                self.room.push(msg.data)
        if self.ws is ws:
            self.ws = None


# ---------------------------------------------------------------------------- phone screen
def adb_prefix() -> list[str]:
    """How the Studio reaches Kevin's phone. The S25 sits on groot's adb (the Studio has no route to it)."""
    return (os.environ.get("Z0LIVE_PASSTHRU_ADB") or "ssh -o ConnectTimeout=4 -o BatchMode=yes groot adb").split()


def phone_serial(run=subprocess.run) -> str:
    serial = os.environ.get("Z0LIVE_PASSTHRU_PHONE_SERIAL", "")
    if serial:
        return serial
    out = run([*adb_prefix(), "devices"], capture_output=True, text=True, timeout=8, check=False).stdout
    devices = [line.split()[0] for line in out.splitlines()[1:] if line.strip().endswith("device")]
    return next((d for d in devices if ":" in d), devices[0] if devices else "")


def adb_shell(serial: str):
    def run(*args: str) -> str:
        try:
            return subprocess.run([*adb_prefix(), "-s", serial, "shell", *args], capture_output=True, text=True,
                                  timeout=8, check=False).stdout
        except (OSError, subprocess.TimeoutExpired):
            return ""
    return run


class PhoneMirror:
    """The phone screen as JPEG frames: adb screenrecord (H.264) decoded by ffmpeg, restarted at its time limit."""

    def __init__(self, serial: str, fps: int = 12):
        self.serial, self.fps = serial, fps
        self.latest: bytes = b""
        self.seq = 0
        self.task = None
        self.changed = asyncio.Event()

    def start(self):
        self.task = asyncio.ensure_future(self._run())

    async def _run(self):
        ffmpeg = os.environ.get("Z0LIVE_FFMPEG") or shutil.which("ffmpeg") or str(
            Path.home() / ".hermes/tools/ffmpeg-9.0.1-darwin-arm64/ffmpeg")
        while True:
            rec = [*adb_prefix(), "-s", self.serial, "exec-out", "screenrecord", "--output-format=h264",
                   "--size", "720x1560", "--bit-rate", "4000000", "-"]
            cmd = " ".join(map(shlex_quote, rec)) + " | " + " ".join(map(shlex_quote, [
                ffmpeg, "-loglevel", "error", "-f", "h264", "-i", "pipe:0", "-vf", f"fps={self.fps}",
                "-f", "image2pipe", "-c:v", "mjpeg", "-q:v", "6", "pipe:1"]))
            proc = await asyncio.create_subprocess_shell(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            buf = b""
            try:
                while True:
                    chunk = await proc.stdout.read(65536)
                    if not chunk:
                        break
                    buf += chunk
                    while True:
                        a = buf.find(b"\xff\xd8"); b = buf.find(b"\xff\xd9", a + 2)
                        if a < 0 or b < 0:
                            break
                        self.latest, buf = buf[a:b + 2], buf[b + 2:]
                        self.seq += 1
                        self.changed.set(); self.changed = asyncio.Event()
            finally:
                if proc.returncode is None:
                    proc.kill()
                await proc.wait()
            await asyncio.sleep(0.5)  # screenrecord stops at its 3-minute limit; start the next segment

    async def frames(self):
        seen = -1
        while self.task and not self.task.done():
            if self.seq != seen and self.latest:
                seen = self.seq
                yield self.latest
            try:
                await asyncio.wait_for(self.changed.wait(), 10)
            except asyncio.TimeoutError:
                continue

    async def stop(self):
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except BaseException:  # noqa: BLE001 - cancellation of the capture pipeline
                pass
            self.task = None


def shlex_quote(s: str) -> str:
    import shlex
    return shlex.quote(s)


# ---------------------------------------------------------------------------- control
SENSITIVE = {"share", "floor:open"}   # only Kevin's own phone leg or chat may ask for these


def canvas_url() -> str:
    url = os.environ.get("Z0LIVE_PASSTHRU_CANVAS_URL")
    if url:
        return url
    token = Path.home() / ".config/z0live/tv-token"
    tok = token.read_text().strip() if token.exists() else ""
    return "http://127.0.0.1:8793/" + (f"?token={tok}" if tok else "")


def say(call, text: str):
    """Have z0 tell Kevin something short in its own voice (commentary for GPT-Live, note+response for Grok)."""
    if call.closed:
        return
    line = "Passthru update. Tell Kevin in one short sentence: " + text
    if getattr(call, "leg", None):
        call.spawn(call.leg.note(line, call.trace.turn_id))
        call.spawn(call.leg.raw({"type": "response.create"}))
    else:
        call.speak_for(None, line)


async def handle(call, action: str, arg: str = "", *, from_kevin: bool) -> str:
    """Run one passthru command. Returns what z0 should say (also traced)."""
    p = call.passthru
    key = action if action != "floor" else "floor:" + arg
    if key in SENSITIVE and not from_kevin:
        call.trace("passthru.refused", action=action, arg=arg, why="room_voice")
        return "Only Kevin can ask me to share or speak freely, from his phone or chat."
    if action == "join":
        url, where = arg, "spoken"
        if not url:
            links = await asyncio.to_thread(lambda: phone_links(adb_shell(phone_serial())))
            if not links:
                links = [(m.group(0), "chat") for m in MEETING_URL.finditer(getattr(call, "context_text", "") or "")][-1:]
            if not links:
                call.trace("passthru.no_link")
                return ("I couldn't find a meeting link on your phone or in our chat. Share the link to Hermes "
                        "and say merge again.")
            url, where = links[0]
        await p.start(url)
        call.trace("passthru.join", where=where, host=re.sub(r"^https?://([^/]+).*", r"\1", url))
        host = re.sub(r"^https?://(?:www\.)?([^/]+).*", r"\1", url)
        return f"Joining your {host} call now, found in your {where}. You may need to admit me."
    if not p.active:
        return "I'm not in a meeting right now."
    if action == "leave":
        await p.stop("voice")
        return "I left the meeting. It's just us again."
    if action == "unshare":
        await p.unshare()
        return "Stopped sharing."
    if action == "floor":
        p.floor.policy = arg
        return {"open": "I'll speak freely in the meeting.", "listen": "I'll just listen.",
                "addressed": "I'll only speak when someone says my name."}[arg]
    if action == "share":
        what = arg
        if what in ("phone", "screen"):
            serial = await asyncio.to_thread(phone_serial)
            if not serial:
                return "I can't reach your phone over adb right now."
            if p.mirror:
                await p.mirror.stop()
            p.mirror = PhoneMirror(serial); p.mirror.start()
            await p.share(f"{p.base}/phone?k={p.token}", "phone")
            return "Sharing your phone screen with the meeting. Notifications will show too."
        if what in ("canvas", "atlas", "tv", "whiteboard", "board", "this", "that"):
            target = arg if arg.startswith("http") else canvas_url()
            await p.share(target, "canvas")
            return "Sharing the canvas with the meeting."
        if what.startswith("http"):
            await p.share(what, "url")
            return "Sharing that page with the meeting."
    return "I didn't catch that passthru command."


async def run_command(call, action: str, arg: str, *, from_kevin: bool):
    try:
        line = await handle(call, action, arg, from_kevin=from_kevin)
    except Exception as exc:  # noqa: BLE001 - a failed join must never break the call
        call.trace("passthru.failed", action=action, detail=str(exc)[:300])
        line = "Passthru hit an error: " + str(exc)[:120]
    call.trace("passthru.said", action=action, line=line)
    say(call, line)


PATH = "/_z0live/passthru"


async def control(request):
    """Loopback control for Hermes (chat/tools): POST {session_id, action, arg}. Same token as z0live_say."""
    import hmac
    import ipaddress
    from aiohttp import web
    if not request.remote or not ipaddress.ip_address(request.remote).is_loopback:
        raise web.HTTPForbidden()
    token = os.environ.get("Z0LIVE_PROGRESS_TOKEN")
    if not token:
        raise web.HTTPNotFound()
    if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), f"Bearer {token}".encode()):
        raise web.HTTPUnauthorized()
    try:
        data = await request.json()
        sid, action, arg = data["session_id"], data["action"], str(data.get("arg") or "")
        if action not in ("join", "leave", "share", "unshare", "floor", "status"):
            raise ValueError()
    except (ValueError, TypeError, KeyError):
        raise web.HTTPBadRequest() from None
    calls = [c for c in request.app["manager"].calls.values()
             if (c.chat == sid or c.id == sid) and not c.closed and getattr(c, "passthru", None)]
    if len(calls) != 1:
        raise web.HTTPConflict()
    call, = calls
    p = call.passthru
    if action == "status":
        return web.json_response({"active": p.active, "url_host": re.sub(r"^https?://([^/]+).*", r"\1", p.url) if p.url else "",
                                  "sharing": p.sharing, "floor": p.floor.policy, "room_frames": p.room.received,
                                  **p.counts})
    line = await handle(call, action, arg, from_kevin=True)
    say(call, line)
    return web.json_response({"ok": True, "say": line})


def enabled() -> bool:
    return os.environ.get("Z0LIVE_PASSTHRU", "on").lower() not in ("0", "off", "false", "no")
