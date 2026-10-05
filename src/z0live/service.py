from __future__ import annotations

import asyncio
import json
import os
import time
from pathlib import Path

from .contracts import TimelineEvent
from .gateway import GatewayAddress, RuntimeGateway
from .harnesses.jsonl import JsonLineHarnessAdapter
from .metrics import TimelineMetrics
from .plan import VoicePlan
from .process import LocalActorProcess
from .receipts import ReceiptWriter
from .registry import create_actor
from .resources import ResourceTracker
from .runtime import ConversationRuntime, RuntimeHooks
from .webserver import StaticWebServer, default_web_dir


def home() -> Path:
    return Path(
        os.environ.get("Z0LIVE_HOME", "~/.z0live")
    ).expanduser()


class LiveService:
    def __init__(
        self,
        plan: VoicePlan,
        *,
        listen: GatewayAddress = GatewayAddress(),
        harness_address: tuple[str, int] | None = None,
        actor_command: str | None = None,
        ready_timeout_seconds: float = 300,
        resource_sample_seconds: float = 5.0,
        web_address: tuple[str, int] | None = None,
        web_dir: str | Path | None = None,
    ) -> None:
        self.plan = plan
        self.listen = listen
        self.harness_address = harness_address
        self.actor_command = actor_command
        self.ready_timeout_seconds = ready_timeout_seconds
        self.resource_sample_seconds = max(0.5, float(resource_sample_seconds))
        self.web_address = web_address
        self.web_dir = Path(web_dir).expanduser() if web_dir is not None else default_web_dir()
        self.web_server: StaticWebServer | None = None
        self.actor_process: LocalActorProcess | None = None
        self.runtime: ConversationRuntime | None = None
        self.gateway: RuntimeGateway | None = None
        self.started_at = time.monotonic()
        self.last_activity = self.started_at
        self._closing = False
        self.state_dir = home()
        self.state_path = self.state_dir / "session.json"
        self.receipts = ReceiptWriter(
            self.state_dir / "receipts.jsonl"
        )
        self.resources = ResourceTracker(plan.device_index)
        self.timeline_metrics = TimelineMetrics()
        self._next_resource_sample = self.started_at

    def touch(self) -> None:
        self.last_activity = time.monotonic()

    def _write_state(self, phase: str) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": "z0live.service_state.v1",
            "phase": phase,
            "plan_id": self.plan.plan_id,
            "actor_id": self.plan.actor_id,
            "adapter": self.plan.adapter,
            "provider": self.plan.provider,
            "controller_pid": os.getpid(),
            "actor_pid": (
                self.actor_process.pid
                if self.actor_process
                else None
            ),
            "gateway": {
                "host": self.listen.host,
                "port": self.listen.port,
            },
            "idle_unload_seconds": (
                self.plan.idle_unload_seconds
            ),
            "web": (
                None
                if self.web_server is None or self.web_server.address is None
                else {
                    "host": self.web_server.address[0],
                    "port": self.web_server.address[1],
                }
            ),
        }
        self.state_path.write_text(
            json.dumps(payload, indent=2) + "\n",
            encoding="utf-8",
        )

    def _assert_singleton(self) -> None:
        if not self.state_path.exists():
            return
        try:
            raw = json.loads(
                self.state_path.read_text(encoding="utf-8")
            )
            pid = int(raw.get("controller_pid") or 0)
            if pid > 0:
                os.kill(pid, 0)
                raise RuntimeError(
                    f"z0live session already active (pid {pid})"
                )
        except ProcessLookupError:
            self.state_path.unlink(missing_ok=True)
        except (
            ValueError,
            json.JSONDecodeError,
            OSError,
        ):
            self.state_path.unlink(missing_ok=True)

    async def start(self) -> None:
        self._assert_singleton()
        self._write_state("starting")
        self.receipts.emit(
            "session_starting",
            plan_id=self.plan.plan_id,
            actor_id=self.plan.actor_id,
            adapter=self.plan.adapter,
        )

        if (
            self.plan.provider == "local"
            and self.plan.adapter in ("personaplex", "moshi")
        ):
            self.actor_process = LocalActorProcess(
                self.plan,
                command=self.actor_command,
            )
            pid = self.actor_process.start()
            self.receipts.emit(
                "actor_process_started",
                plan_id=self.plan.plan_id,
                pid=pid,
            )
            await asyncio.to_thread(
                self.actor_process.wait_ready,
                self.ready_timeout_seconds,
            )

        harness = None
        if self.harness_address is not None:
            harness = JsonLineHarnessAdapter(
                *self.harness_address
            )
        actor = create_actor(self.plan)

        async def on_event(
            event: TimelineEvent,
        ) -> None:
            self.timeline_metrics.observe(event)
            self.receipts.emit(
                "timeline_event",
                plan_id=self.plan.plan_id,
                timeline_event=event.to_dict(),
            )

        self.runtime = ConversationRuntime(
            actor,
            harness=harness,
            hooks=RuntimeHooks(
                on_event=on_event,
                on_activity=self.touch,
            ),
        )
        await self.runtime.start()
        self.gateway = RuntimeGateway(
            self.runtime,
            self.listen,
        )
        await self.gateway.start()

        if self.web_address is not None:
            host, port = self.web_address
            try:
                self.web_server = StaticWebServer(
                    self.web_dir,
                    host=host,
                    port=port,
                )
                web_host, web_port = self.web_server.start()
                self.receipts.emit(
                    "web_ready",
                    plan_id=self.plan.plan_id,
                    host=web_host,
                    port=web_port,
                    directory=str(self.web_dir),
                )
            except FileNotFoundError as exc:
                self.web_server = None
                self.receipts.emit(
                    "web_unavailable",
                    plan_id=self.plan.plan_id,
                    reason=str(exc),
                )

        self.touch()
        self._write_state("ready")
        self.receipts.emit(
            "session_ready",
            plan_id=self.plan.plan_id,
            warm_seconds=(
                time.monotonic() - self.started_at
            ),
            gateway={
                "host": self.listen.host,
                "port": self.listen.port,
            },
            capabilities=actor.capabilities.to_dict(),
        )

    async def run(self) -> str:
        await self.start()
        reason = "explicit_exit"
        try:
            while True:
                idle = self.plan.idle_unload_seconds
                if (
                    idle > 0
                    and time.monotonic() - self.last_activity
                    >= idle
                ):
                    reason = "idle_timeout"
                    break
                now = time.monotonic()
                if now >= self._next_resource_sample:
                    self.resources.capture()
                    self._next_resource_sample = now + self.resource_sample_seconds
                await asyncio.sleep(0.25)
        except asyncio.CancelledError:
            reason = "cancelled"
            raise
        except KeyboardInterrupt:
            reason = "explicit_exit"
        finally:
            await self.close(reason)
        return reason

    async def close(
        self,
        reason: str = "explicit_exit",
    ) -> None:
        if self._closing:
            return
        self._closing = True
        if self.web_server is not None:
            await asyncio.to_thread(self.web_server.stop)
            self.web_server = None
        if self.gateway is not None:
            await self.gateway.close()
            self.gateway = None
        if self.runtime is not None:
            await self.runtime.close()
            self.runtime = None
        process_result = None
        if self.actor_process is not None:
            process_result = await asyncio.to_thread(
                self.actor_process.stop
            )
            self.actor_process = None
        self.receipts.emit(
            "session_stopped",
            plan_id=self.plan.plan_id,
            reason=reason,
            duration_seconds=(
                time.monotonic() - self.started_at
            ),
            process_returncode=(
                process_result.returncode
                if process_result
                else None
            ),
            process_forced=(
                process_result.forced
                if process_result
                else False
            ),
            resources=self.resources.summary(),
            timeline_metrics=self.timeline_metrics.summary(),
        )
        self.state_path.unlink(missing_ok=True)
