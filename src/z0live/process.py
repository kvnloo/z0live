from __future__ import annotations

import os
import shlex
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass

from .plan import VoicePlan


@dataclass(slots=True)
class ProcessStop:
    returncode: int | None
    forced: bool


class LocalActorProcess:
    def __init__(self, plan: VoicePlan, command: str | None = None) -> None:
        self.plan = plan
        self.command_override = command
        self.process: subprocess.Popen | None = None
        self.ssl_dir: str | None = None

    def _command(self) -> list[str]:
        if self.plan.adapter != "personaplex":
            raise ValueError(f"no local process launcher for adapter={self.plan.adapter!r}")
        self.ssl_dir = tempfile.mkdtemp(prefix="z0live-ssl-")
        template = self.command_override or os.environ.get("Z0LIVE_PERSONAPLEX_CMD")
        values = {
            "ssl_dir": self.ssl_dir,
            "host": self.plan.endpoint_host,
            "port": self.plan.endpoint_port,
            "python": sys.executable,
        }
        if template:
            return [piece.format(**values) for piece in shlex.split(template)]
        return [
            sys.executable,
            "-m",
            "moshi.server",
            "--host",
            self.plan.endpoint_host,
            "--port",
            str(self.plan.endpoint_port),
            "--ssl",
            self.ssl_dir,
            "--quantize-4bit",
        ]

    @property
    def pid(self) -> int | None:
        return None if self.process is None else self.process.pid

    def start(self) -> int:
        if self.process is not None and self.process.poll() is None:
            return self.process.pid
        env = os.environ.copy()
        if self.plan.device_index is not None:
            env["CUDA_VISIBLE_DEVICES"] = str(self.plan.device_index)
        cwd = os.environ.get("Z0LIVE_PERSONAPLEX_ROOT") or None
        self.process = subprocess.Popen(
            self._command(),
            cwd=cwd,
            env=env,
            start_new_session=True,
        )
        return self.process.pid

    def wait_ready(self, timeout_seconds: float = 300.0) -> None:
        deadline = time.monotonic() + timeout_seconds
        while time.monotonic() < deadline:
            if self.process is None:
                raise RuntimeError("actor process not started")
            code = self.process.poll()
            if code is not None:
                raise RuntimeError(f"actor process exited during warmup: {code}")
            try:
                with socket.create_connection(
                    (self.plan.endpoint_host, self.plan.endpoint_port), timeout=0.2
                ):
                    return
            except OSError:
                time.sleep(0.1)
        raise TimeoutError(
            f"actor did not open {self.plan.endpoint_host}:{self.plan.endpoint_port} "
            f"within {timeout_seconds:.0f}s"
        )

    def stop(self, grace_seconds: float = 5.0) -> ProcessStop:
        proc = self.process
        forced = False
        if proc is None:
            self._cleanup()
            return ProcessStop(None, forced)
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=max(0.0, grace_seconds))
            except subprocess.TimeoutExpired:
                forced = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2)
        code = proc.returncode
        self._cleanup()
        return ProcessStop(code, forced)

    def _cleanup(self) -> None:
        if self.ssl_dir:
            shutil.rmtree(self.ssl_dir, ignore_errors=True)
            self.ssl_dir = None
