"""hermes-meetings: run a stock livekit-server on loopback or the tailnet, mint room tokens, track the processes.

Stdlib only, so the Hermes tool handlers need no extra packages. The media path (bot.py) runs in its own venv.

Binding rule (M0 finding): `--bind` alone covers only the signal port; RTC UDP/TCP then listened on every interface,
LAN included. The generated config therefore pins `bind_addresses`, `rtc.ips`, `rtc.interfaces` and disables the TCP
fallback listener. Modes: `loopback` (127.0.0.1 only) and `tailnet` (this host's Tailscale IPv4 only). Nothing binds
0.0.0.0, the LAN, or a public address; there is no LaunchAgent and no Funnel.
"""
from __future__ import annotations

import atexit
import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import shutil

import socket
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

ROLES = ("owner", "full", "view", "bot")
MEET_CLIENT = "http://127.0.0.1:3000/custom"  # trusted, self-hosted livekit-examples/meet
TAILNET = ipaddress.ip_network("100.64.0.0/10")
_CHILDREN: dict[int, subprocess.Popen] = {}
_LIFECYCLE_LOCK = threading.RLock()
_CONTROLLER = secrets.token_hex(16)


def stop_process(proc):
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@atexit.register
def stop_on_exit():
    for proc in reversed(tuple(_CHILDREN.values())):
        stop_process(proc)


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def mint(key: str, secret: str, identity: str, room: str, role: str, *, name: str = "", ttl_s: int = 6 * 3600,
         now: Optional[float] = None) -> str:
    """A LiveKit access token (HS256 JWT) whose grants follow the role. Identity comes from this signed token,
    never from voice (PRD R2.1)."""
    if role not in ROLES:
        raise ValueError(f"role must be one of {ROLES}")
    video: Dict[str, Any] = {"roomJoin": True, "room": room, "canSubscribe": True}
    if role == "owner":
        video.update(canPublish=True, canPublishData=True)
    elif role == "full":
        video.update(canPublish=True, canPublishData=True,
                     canPublishSources=["microphone", "camera", "screen_share", "screen_share_audio"])
    elif role == "view":
        video.update(canPublish=False, canPublishData=False)
    elif role == "bot":
        video.update(canPublish=True, canPublishData=True, canUpdateOwnMetadata=True,
                     canPublishSources=["microphone", "screen_share"])
    iat = int(now if now is not None else time.time())
    claims = {"iss": key, "sub": identity, "name": name or identity, "nbf": iat - 5, "exp": iat + int(ttl_s),
              "video": video, "attributes": {"hermes.role": role}}
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
    body = _b64(json.dumps(claims, separators=(",", ":")).encode())
    sig = hmac.new(secret.encode(), f"{head}.{body}".encode(), hashlib.sha256).digest()
    return f"{head}.{body}.{_b64(sig)}"


def admin_token(key: str, secret: str, room: str = "") -> str:
    iat = int(time.time())
    video: Dict[str, Any] = {"roomList": True, "roomAdmin": True}
    if room:
        video["room"] = room
    claims = {"iss": key, "sub": "hermes-meetings-admin", "nbf": iat - 5, "exp": iat + 300, "video": video}
    head = _b64(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    body = _b64(json.dumps(claims).encode())
    return f"{head}.{body}.{_b64(hmac.new(secret.encode(), f'{head}.{body}'.encode(), hashlib.sha256).digest())}"


def tailnet_ip() -> str:
    exe = shutil.which("tailscale") or "/Applications/Tailscale.app/Contents/MacOS/Tailscale"
    out = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True, timeout=10)
    ip = (out.stdout.split() or [""])[0]
    if not ip or ipaddress.ip_address(ip) not in TAILNET:
        raise RuntimeError("no Tailscale IPv4 address on this host")
    return ip


def config_yaml(mode: str, port: int = 7880, udp_port: int = 7882, ip: str = "") -> str:
    """livekit-server config pinned to one address. Keys are passed via LIVEKIT_KEYS, never written here."""
    if mode == "loopback":
        return (Path(__file__).with_name("lk-loopback.yaml").read_text()
                .replace("__SIGNAL_PORT__", str(int(port))).replace("__RTC_PORT__", str(int(udp_port))))
    elif mode == "tailnet":
        ip = ip or tailnet_ip()
        iface, loopback = "utun", "false"  # prefix match: macOS names the Tailscale tunnel utunN
    else:
        raise ValueError("mode must be loopback or tailnet")
    if mode == "tailnet" and ipaddress.ip_address(ip) not in TAILNET:
        raise ValueError(f"refusing to bind {ip!r}: loopback or tailnet (100.64/10) only")
    return (f"port: {port}\n"
            f"bind_addresses: [\"{ip}\"]\n"
            "rtc:\n"
            f"  node_ip: {ip}\n"
            "  use_external_ip: false\n"
            f"  enable_loopback_candidate: {loopback}\n"
            "  tcp_port: 0\n"
            f"  udp_port: {udp_port}\n"
            f"  interfaces: {{includes: [\"{iface}\"]}}\n"
            f"  ips: {{includes: [\"{ip}/32\"]}}\n"
            "room:\n"
            "  auto_create: true\n"
            "  empty_timeout: 300\n"
            "logging:\n"
            "  level: info\n")


class Meetings:
    """One meeting server per state dir. State: meeting.json (pids, ports, room, url), keys.json (0600)."""

    def __init__(self, state_dir: str | Path, livekit_bin: str = "", bot_python: str = "", bot_env: Optional[Dict[str, str]] = None,
                 *, meet_url: str = MEET_CLIENT):
        self.dir = Path(os.path.expanduser(str(state_dir)))
        self.livekit_bin = livekit_bin or shutil.which("livekit-server") or "/opt/homebrew/bin/livekit-server"
        self.bot_python = bot_python
        self.bot_env = bot_env or {}
        self.meet_url = meet_url

    # -- state ---------------------------------------------------------------------------------------------------
    @property
    def state_file(self) -> Path:
        return self.dir / "meeting.json"

    def state(self) -> Dict[str, Any]:
        try:
            return json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self, state: Dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(state, indent=1))
        tmp.replace(self.state_file)

    def keys(self) -> Dict[str, str]:
        path = self.dir / "keys.json"
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            pass
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        keys = {"key": "hm_" + secrets.token_hex(6), "secret": secrets.token_urlsafe(32)}
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump(keys, f)
        return keys

    # -- lifecycle -----------------------------------------------------------------------------------------------
    def start(self, mode: str = "loopback", room: str = "hermes", port: int = 7880, udp_port: int = 7882,
              bot: bool = True, owner: str = "kevin", bot_args: Optional[List[str]] = None) -> Dict[str, Any]:
        with _LIFECYCLE_LOCK:
            return self._start(mode, room, port, udp_port, bot, owner, bot_args)

    def _start(self, mode, room, port, udp_port, bot, owner, bot_args):
        state = self.state()
        if mode not in ("loopback", "tailnet"):
            raise ValueError("mode must be loopback or tailnet")
        if state and self.owns(state, "server_pid"):
            return {"ok": True, "already_running": True, **self.public(state)}
        if state.get("controller") == _CONTROLLER:
            self.stop_children(state)
        if not Path(self.livekit_bin).exists():
            raise RuntimeError(f"livekit-server not found at {self.livekit_bin} (brew install livekit)")
        ip = "127.0.0.1" if mode == "loopback" else tailnet_ip()
        cfg = self.dir / "livekit.yaml"
        self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        cfg.write_text(config_yaml(mode, port, udp_port, ip))
        keys = self.keys()
        env = {**os.environ, "LIVEKIT_KEYS": f"{keys['key']}: {keys['secret']}"}
        with (self.dir / "livekit-server.log").open("ab") as log:
            server = subprocess.Popen([self.livekit_bin, "--config", str(cfg)], stdout=log, stderr=log,
                                      stdin=subprocess.DEVNULL, env=env, start_new_session=True)
        url = f"ws://{ip}:{port}"
        deadline = time.time() + 15
        while time.time() < deadline:
            if server.poll() is not None:
                raise RuntimeError(f"livekit-server exited {server.returncode}; see {self.dir / 'livekit-server.log'}")
            try:
                with socket.create_connection((ip, port), timeout=0.5):
                    if f"TCP {ip}:{port} (LISTEN)" in self.listeners(server.pid):
                        break
            except OSError:
                pass
            time.sleep(0.2)
        else:
            stop_process(server)
            raise RuntimeError("livekit-server did not open its signal port in 15 s")
        state = {"mode": mode, "room": room, "url": url, "ip": ip, "port": port, "udp_port": udp_port,
                 "owner": owner, "server_pid": server.pid, "started": time.time(), "bot_pid": None}
        state["controller"] = _CONTROLLER
        _CHILDREN[server.pid] = server
        try:
            if bot:
                state["bot_pid"] = self.start_bot(state, bot_args or [])
            self._save(state)
        except Exception:
            self.stop_children(state)
            raise
        return {"ok": True, **self.public(state)}

    def start_bot(self, state: Dict[str, Any], extra: List[str]) -> Optional[int]:
        if not self.bot_python or not Path(self.bot_python).exists():
            raise ValueError("configured bot Python does not exist")
        keys = self.keys()
        token_file = self.dir / "bot.token"
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(mint(keys["key"], keys["secret"], "z0", state["room"], "bot", name="z0 (Hermes)"))

        args = [self.bot_python, str(Path(__file__).with_name("bot.py")), "--url", state["url"],
                "--token-file", str(token_file), "--owner", state["owner"],
                "--status-file", str(self.dir / "bot-status.json")] + extra
        with (self.dir / "bot.log").open("ab") as log:
            proc = subprocess.Popen(args, stdout=log, stderr=log, stdin=subprocess.DEVNULL, start_new_session=True,
                                    env={**os.environ, **self.bot_env})
        _CHILDREN[proc.pid] = proc
        return proc.pid

    def invite(self, identity: str = "", role: str = "full", ttl_minutes: int = 120) -> Dict[str, Any]:
        state = self.state()
        if not state or not self.owns(state, "server_pid"):
            return {"ok": False, "error": "no meeting running; call meetings_start first"}
        if role not in ("owner", "full", "view"):
            return {"ok": False, "error": "role must be owner, full or view"}
        if not 1 <= ttl_minutes <= 360:
            return {"ok": False, "error": "ttl_minutes must be between 1 and 360"}
        if role == "owner":
            identity = state["owner"]  # the owner identity is fixed: whoever holds it is treated as Kevin
        else:
            base = "".join(c for c in (identity or role) if c.isalnum() or c in "-_")[:32] or role
            identity = base if base.startswith("guest-") else f"guest-{base}"
            if identity == state["owner"]:
                return {"ok": False, "error": "that identity is reserved for the owner"}
        keys = self.keys()
        token = mint(keys["key"], keys["secret"], identity, state["room"], role, ttl_s=int(ttl_minutes) * 60)
        link = None if role == "owner" else f"{self.meet_url}?" + urllib.parse.urlencode({
            "liveKitUrl": state["url"], "token": token})
        note = ("Loopback server: only clients on this Mac can join." if state["mode"] == "loopback" else
                "Tailnet server: the browser must be on the tailnet. A https page needs wss://, so a browser join "
                "needs a tailnet-only HTTPS front (tailscale serve) first; native SDK clients work as is.")
        return {"ok": True, "identity": identity, "role": role, "room": state["room"], "url": state["url"],
                "token": token, "link": link, "expires_minutes": ttl_minutes, "note": note}

    def participants(self) -> List[Dict[str, Any]]:
        state = self.state()
        keys = self.keys()
        http = "http://" + state["url"].split("://", 1)[1]
        req = urllib.request.Request(http + "/twirp/livekit.RoomService/ListParticipants",
                                     data=json.dumps({"room": state["room"]}).encode(),
                                     headers={"Content-Type": "application/json",
                                              "Authorization": "Bearer " + admin_token(keys["key"], keys["secret"], state["room"])})
        try:
            with urllib.request.urlopen(req, timeout=3) as r:
                data = json.loads(r.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:  # room not created yet
                return []
            raise
        out = []
        for p in data.get("participants", []):
            out.append({"identity": p.get("identity"), "name": p.get("name"),
                        "role": (p.get("attributes") or {}).get("hermes.role", ""),
                        "tracks": [{"source": t.get("source", ""), "mime": t.get("mimeType", ""), "sid": t.get("sid")}
                                   for t in p.get("tracks", [])]})
        return out

    def listeners(self, pid: int) -> List[str]:
        lsof = shutil.which("lsof") or "/usr/sbin/lsof"
        out = subprocess.run([lsof, "-nP", "-a", "-p", str(pid), "-i"], capture_output=True, text=True, timeout=10)
        return sorted({" ".join(line.split()[7:]) for line in out.stdout.splitlines()[1:] if line.split()})

    def status(self) -> Dict[str, Any]:
        state = self.state()
        if not state:
            return {"ok": True, "running": False}
        up = self.owns(state, "server_pid")
        res: Dict[str, Any] = {"ok": True, "running": up, **self.public(state),
                               "bot_running": self.owns(state, "bot_pid")}
        if up:
            try:
                res["participants"] = self.participants()
            except Exception as exc:  # noqa: BLE001
                res["participants_error"] = type(exc).__name__
            try:
                res["listeners"] = self.listeners(int(state["server_pid"]))
            except Exception:  # noqa: BLE001
                pass
        try:
            res["bot"] = json.loads((self.dir / "bot-status.json").read_text())
        except (OSError, ValueError):
            pass
        return res

    def owns(self, state, key) -> bool:
        child = _CHILDREN.get(state.get(key)) if state.get("controller") == _CONTROLLER else None
        return child is not None and child.poll() is None

    def stop_children(self, state):
        stopped = []
        for key in ("bot_pid", "server_pid"):
            pid = state.get(key)
            proc = _CHILDREN.pop(pid, None) if state.get("controller") == _CONTROLLER else None
            if proc is None or proc.poll() is not None:
                continue
            stop_process(proc)
            stopped.append(key.replace("_pid", ""))
        return stopped

    def leave(self) -> Dict[str, Any]:
        with _LIFECYCLE_LOCK:
            return self._leave()

    def _leave(self):
        state = self.state()
        if state.get("server_pid") and state.get("controller") != _CONTROLLER:
            return {"ok": False, "error": "Meeting was started by a different controller; no recorded PID was signaled."}
        stopped = self.stop_children(state)
        if state:
            state.update(server_pid=None, bot_pid=None, stopped=time.time())
            self._save(state)
        (self.dir / "bot.token").unlink(missing_ok=True)
        return {"ok": True, "stopped": stopped}

    @staticmethod
    def public(state: Dict[str, Any]) -> Dict[str, Any]:
        return {k: state.get(k) for k in ("mode", "room", "url", "owner", "server_pid", "bot_pid")}
