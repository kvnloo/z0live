from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile

from .plan import VoicePlan


class ProcessActor:
    """One session-scoped actor process.

    z0live owns lifecycle only. Candidate selection came from z0intelligence.
    """

    def __init__(
        self,
        plan: VoicePlan,
        *,
        backend_port: int,
        command: str | None = None,
    ) -> None:
        self.plan = plan
        self.backend_port = int(backend_port)
        self.command_override = command
        self.process: subprocess.Popen | None = None
        self._ssl_dir: str | None = None

    def _command(self) -> list[str]:
        if self.plan.adapter != "personaplex":
            raise ValueError(f"unsupported actor adapter: {self.plan.adapter}")
        self._ssl_dir = tempfile.mkdtemp(prefix="z0live-ssl-")
        template = self.command_override or os.environ.get("Z0LIVE_PERSONAPLEX_CMD")
        values = {
            "ssl_dir": self._ssl_dir,
            "host": self.plan.endpoint_host,
            "port": self.backend_port,
            "python": sys.executable,
        }
        if template:
            return [part.format(**values) for part in shlex.split(template)]
        return [
            sys.executable,
            "-m",
            "moshi.server",
            "--host",
            self.plan.endpoint_host,
            "--port",
            str(self.backend_port),
            "--ssl",
            self._ssl_dir,
            "--quantize-4bit",
        ]

    def start(self) -> subprocess.Popen:
        if self.process is not None and self.process.poll() is None:
            return self.process
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
        return self.process

    def poll(self) -> int | None:
        return None if self.process is None else self.process.poll()

    @property
    def pid(self) -> int | None:
        return None if self.process is None else self.process.pid

    def stop(self, *, grace_seconds: float = 5.0) -> int | None:
        proc = self.process
        if proc is None:
            self._cleanup()
            return None
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                proc.wait(timeout=max(0.0, grace_seconds))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.wait(timeout=2)
        code = proc.returncode
        self._cleanup()
        return code

    def _cleanup(self) -> None:
        if self._ssl_dir:
            shutil.rmtree(self._ssl_dir, ignore_errors=True)
            self._ssl_dir = None
