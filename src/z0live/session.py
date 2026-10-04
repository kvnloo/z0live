from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path
from typing import Any

from .plan import VoicePlan
from .process_actor import ProcessActor
from .proxy import ActivityProxy


def _home() -> Path:
    return Path(os.environ.get("Z0LIVE_HOME", "~/.z0live")).expanduser()


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class BrainstormSession:
    def __init__(
        self,
        plan: VoicePlan,
        *,
        actor_command: str | None = None,
        ready_timeout_seconds: float = 300.0,
        poll_seconds: float = 0.25,
        proxy_activity_threshold_bytes: int = 2048,
    ) -> None:
        self.plan = plan
        self.ready_timeout_seconds = float(ready_timeout_seconds)
        self.poll_seconds = float(poll_seconds)
        self.backend_port = self.plan.endpoint_port + 1
        self.actor = ProcessActor(plan, backend_port=self.backend_port, command=actor_command)
        self.proxy: ActivityProxy | None = None
        self.started_monotonic: float | None = None
        self.last_activity_monotonic: float | None = None
        self._last_activity_file_mtime = 0.0
        self.state_dir = _home()
        self.state_path = self.state_dir / "brainstorm.json"
        self.activity_path = self.state_dir / "brainstorm.activity"
        self.receipt_path = self.state_dir / "receipts.jsonl"
        self.proxy_activity_threshold_bytes = int(proxy_activity_threshold_bytes)

    def _assert_singleton(self) -> None:
        if not self.state_path.exists():
            return
        try:
            state = json.loads(self.state_path.read_text(encoding="utf-8"))
            pid = int(state.get("controller_pid") or 0)
        except Exception:
            pid = 0
        if pid > 0 and _pid_alive(pid):
            raise RuntimeError(f"brainstorm session already active (controller pid {pid})")
        self.state_path.unlink(missing_ok=True)
        self.activity_path.unlink(missing_ok=True)

    def _write_state(self, phase: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "z0live.session_state.v1",
            "phase": phase,
            "plan_id": self.plan.plan_id,
            "actor_id": self.plan.actor_id,
            "controller_pid": os.getpid(),
            "actor_pid": self.actor.pid,
            "endpoint": {"host": self.plan.endpoint_host, "port": self.plan.endpoint_port},
            "backend_port": self.backend_port,
            "activity_file": str(self.activity_path),
        }
        self.state_path.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")

    def _receipt(self, event: str, **extra: Any) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        row = {
            "schema": "z0live.session_receipt.v1",
            "event": event,
            "unix_time": time.time(),
            "plan_id": self.plan.plan_id,
            "actor_id": self.plan.actor_id,
            "actor_pid": self.actor.pid,
            **extra,
        }
        with self.receipt_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, sort_keys=True) + "\n")

    def touch(self) -> None:
        now = time.monotonic()
        self.last_activity_monotonic = now
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.activity_path.touch()
        try:
            self._last_activity_file_mtime = self.activity_path.stat().st_mtime
        except OSError:
            pass

    def _sync_external_touch(self) -> None:
        try:
            mtime = self.activity_path.stat().st_mtime
        except OSError:
            return
        if mtime > self._last_activity_file_mtime:
            self._last_activity_file_mtime = mtime
            self.last_activity_monotonic = time.monotonic()

    def _wait_backend(self) -> None:
        deadline = time.monotonic() + self.ready_timeout_seconds
        while time.monotonic() < deadline:
            code = self.actor.poll()
            if code is not None:
                raise RuntimeError(f"actor exited during warmup with code {code}")
            try:
                with socket.create_connection(
                    (self.plan.endpoint_host, self.backend_port), timeout=0.2
                ):
                    return
            except OSError:
                time.sleep(self.poll_seconds)
        raise TimeoutError(
            f"actor did not open {self.plan.endpoint_host}:{self.backend_port} "
            f"within {self.ready_timeout_seconds:.0f}s"
        )

    def start(self) -> None:
        self._assert_singleton()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.started_monotonic = time.monotonic()
        self.last_activity_monotonic = self.started_monotonic
        self.actor.start()
        self._write_state("warming")
        self._receipt("actor_spawned", backend_port=self.backend_port)
        try:
            self._wait_backend()
            self.proxy = ActivityProxy(
                listen_host=self.plan.endpoint_host,
                listen_port=self.plan.endpoint_port,
                backend_host=self.plan.endpoint_host,
                backend_port=self.backend_port,
                on_activity=self.touch,
                threshold_bytes=self.proxy_activity_threshold_bytes,
            )
            self.proxy.start()
            self.touch()
            self._write_state("ready")
            self._receipt(
                "session_ready",
                warm_seconds=time.monotonic() - self.started_monotonic,
                endpoint_port=self.plan.endpoint_port,
            )
        except Exception:
            self.close(reason="startup_failure")
            raise

    def idle(self) -> bool:
        timeout = self.plan.idle_unload_seconds
        if timeout <= 0:
            return False
        self._sync_external_touch()
        assert self.last_activity_monotonic is not None
        return (time.monotonic() - self.last_activity_monotonic) >= timeout

    def run_foreground(self) -> str:
        self.start()
        reason = "explicit_exit"
        try:
            while True:
                code = self.actor.poll()
                if code is not None:
                    reason = f"actor_exit:{code}"
                    break
                if self.idle():
                    reason = "idle_timeout"
                    break
                time.sleep(self.poll_seconds)
        except KeyboardInterrupt:
            reason = "explicit_exit"
        finally:
            self.close(reason=reason)
        return reason

    def close(self, *, reason: str) -> None:
        if self.proxy is not None:
            self.proxy.stop()
            self.proxy = None
        code = self.actor.stop()
        duration = (
            None if self.started_monotonic is None else time.monotonic() - self.started_monotonic
        )
        self._receipt(
            "session_stopped",
            reason=reason,
            actor_returncode=code,
            duration_seconds=duration,
        )
        self.state_path.unlink(missing_ok=True)
        self.activity_path.unlink(missing_ok=True)
