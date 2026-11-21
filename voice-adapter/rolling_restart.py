"""Launch the inactive Studio slot, switch admissions, and retire the old slot."""
from __future__ import annotations

import fcntl
import http.client
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

from voice_slots import Route, Slots


class LaunchAgents:
    def __init__(self):
        domain = f"gui/{os.getuid()}"
        self.targets = {9168: f"{domain}/com.zer0.z0live.voice", 9169: f"{domain}/com.zer0.z0live.voice-next"}

    def pid(self, port):
        result = subprocess.check_output(["/bin/launchctl", "print", self.targets[port]], text=True)
        match = re.search(r"^\s*pid = (\d+)$", result, re.MULTILINE)
        return int(match[1]) if match else None

    def start(self, port):
        subprocess.run(["/bin/launchctl", "kickstart", self.targets[port]], check=True)

    def stop(self, port):
        result = subprocess.run(["/bin/launchctl", "kill", "SIGTERM", self.targets[port]])
        if result.returncode and self.pid(port) is not None:
            result.check_returncode()


def wait_for(condition, timeout, error):
    deadline = time.monotonic() + timeout
    while not condition():
        if time.monotonic() >= deadline:
            raise RuntimeError(error)
        time.sleep(0.2)


def get(port, path):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=2)
    try:
        conn.request("GET", path)
        response = conn.getresponse()
        return response.status, response.getheader("X-Z0Live-Voice-Port"), response.read()
    finally:
        conn.close()


def ready(jobs, port):
    pid = jobs.pid(port)
    if pid is None:
        return False
    try:
        status, _, body = get(port, "/_z0live/ready")
        return status == 200 and json.loads(body) == {"ready": True, "pid": pid}
    except (OSError, ValueError, http.client.HTTPException):
        return False


def proxy_routes_to(proxy_port, active):
    status, port, _ = get(proxy_port, "/api/audio/voice-live/status")
    if status != 401 or port != str(active):
        raise RuntimeError(f"Managed proxy on {proxy_port} did not route to slot {active}")


def pin_release(slots, port, release):
    target = slots.release(port)
    temporary = target.with_suffix(".new")
    temporary.unlink(missing_ok=True)
    temporary.symlink_to(release, target_is_directory=True)
    temporary.replace(target)


def retire(slots, jobs, port):
    with slots.admission_lock(port) as lease:
        def idle():
            try:
                fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
                return True
            except BlockingIOError:
                return False
        wait_for(idle, 90, f"Slot {port} still has signaling requests; rerun to finish")
        if jobs.pid(port) is not None:
            jobs.stop(port)
    # Do not send SIGKILL if this wait expires. The old release keeps draining.
    wait_for(lambda: jobs.pid(port) is None, 30, f"Slot {port} is still draining; rerun to finish")


def roll(slots, jobs, release, proxy_port=9165):
    with (slots.directory / "deploy.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        route = slots.read()
        active, other = route.active, slots.other(route.active)
        proxy_routes_to(proxy_port, active)
        if route.phase == "draining":
            retire(slots, jobs, other)
            slots.write(Route(active))
            print(f"Slot {other} retired; slot {active} is active", flush=True)
            return

        if not ready(jobs, active):
            raise RuntimeError(f"Active slot {active} is not ready")
        if release is None:
            raise RuntimeError("Pass a prepared release directory to start a rollout")
        release = release.resolve(strict=True)
        adapter = release / "voice-adapter"
        if not (adapter / "mobile_live.py").is_file() or not os.access(adapter / ".venv/bin/python", os.X_OK):
            raise RuntimeError("Release needs voice-adapter/mobile_live.py and a prepared voice-adapter/.venv")
        if not (release / "desktop").is_dir():
            raise RuntimeError("Release needs desktop/")
        pid = jobs.pid(other)
        if route.phase == "steady" and pid is not None:
            raise RuntimeError(f"Inactive slot {other} is unexpectedly running; inspect it before rolling")
        if route.phase == "staging" and slots.release(other).resolve() != release:
            raise RuntimeError("Rerun with the same staged release directory")
        if route.phase == "steady":
            pin_release(slots, other, release)
            slots.write(Route(active, "staging"))
        if pid is None:
            jobs.start(other)
        wait_for(lambda: ready(jobs, other), 45, f"Slot {other} is not ready; route unchanged")
        slots.write(Route(other, "draining"))
        print(f"New calls: slot {other}. Draining slot {active}.", flush=True)
        proxy_routes_to(proxy_port, other)
        retire(slots, jobs, active)
        slots.write(Route(other))
        print(f"Slot {active} retired; slot {other} is active", flush=True)


def run_slot(slots, port):
    route = slots.read()
    if port != route.active and route.phase != "staging":
        return
    release = slots.release(port).resolve(strict=True)
    adapter = release / "voice-adapter"
    os.chdir(adapter)
    os.environ.update(HERMES_MOBILE_VOICE_PORT=str(port), Z0LIVE_DESKTOP=str(release / "desktop"))
    python = str(adapter / ".venv/bin/python")
    os.execv(python, [python, "mobile_live.py"])


def main():
    command, directory, *args = sys.argv[1:]
    slots = Slots(directory)
    if command == "init":
        slots.directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not (slots.directory / "route.json").exists():
            pin_release(slots, slots.ports[0], Path(args[0]).resolve(strict=True))
            slots.write(Route(slots.ports[0]))
    elif command == "run":
        port = int(args[0])
        if port not in slots.ports:
            raise ValueError("Invalid slot")
        run_slot(slots, port)
    elif command == "roll" and len(args) <= 1:
        roll(slots, LaunchAgents(), Path(args[0]) if args else None)
    else:
        raise ValueError("Invalid rolling restart command")


if __name__ == "__main__":
    try:
        main()
    except (BlockingIOError, OSError, RuntimeError, ValueError, subprocess.CalledProcessError) as exc:
        sys.exit(str(exc))
