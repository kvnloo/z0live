from __future__ import annotations

import asyncio
import time
from typing import Any

from .plan import VoicePlan
from .process import LocalActorProcess
from .registry import create_actor
from .resources import ResourceTracker


async def probe_actor(
    plan: VoicePlan,
    *,
    actor_command: str | None = None,
    ready_timeout_seconds: float = 300.0,
    settle_seconds: float = 1.0,
) -> dict[str, Any]:
    """Headless actor warm/handshake/resource probe.

    For local PersonaPlex this loads the selected model process, waits for its
    TCP listener, completes the actual actor WebSocket handshake, captures
    resource samples, then tears everything down. No microphone is required.
    """
    process: LocalActorProcess | None = None
    actor = create_actor(plan)
    resources = ResourceTracker(plan.device_index)
    before = resources.capture().to_dict()
    started = time.monotonic()
    process_ready_s: float | None = None
    actor_ready_s: float | None = None
    stop_result = None
    error: str | None = None

    try:
        if plan.provider == "local" and plan.adapter in ("personaplex", "moshi"):
            process = LocalActorProcess(plan, command=actor_command)
            process.start()
            await asyncio.to_thread(process.wait_ready, ready_timeout_seconds)
            process_ready_s = time.monotonic() - started

        actor_start = time.monotonic()
        await actor.start()
        actor_ready_s = time.monotonic() - actor_start
        warm = resources.capture().to_dict()

        if settle_seconds > 0:
            await asyncio.sleep(settle_seconds)
        steady = resources.capture().to_dict()
    except Exception as exc:
        error = str(exc)
        warm = resources.capture().to_dict()
        steady = warm
    finally:
        try:
            await actor.close()
        except Exception as exc:
            if error is None:
                error = f"actor_close: {exc}"
        if process is not None:
            stop_result = await asyncio.to_thread(process.stop)
        await asyncio.sleep(0.25)
        released = resources.capture().to_dict()

    return {
        "schema": "z0live.warm_probe.v1",
        "ok": error is None,
        "error": error,
        "plan_id": plan.plan_id,
        "actor_id": plan.actor_id,
        "provider": plan.provider,
        "adapter": plan.adapter,
        "model": plan.model,
        "process_ready_ms": (
            None if process_ready_s is None else round(process_ready_s * 1000.0, 3)
        ),
        "actor_handshake_ms": (
            None if actor_ready_s is None else round(actor_ready_s * 1000.0, 3)
        ),
        "total_warm_ms": round((time.monotonic() - started) * 1000.0, 3),
        "resources": {
            "before": before,
            "warm": warm,
            "steady": steady,
            "released": released,
            "summary": resources.summary(),
        },
        "process_stop": (
            None
            if stop_result is None
            else {
                "returncode": stop_result.returncode,
                "forced": stop_result.forced,
            }
        ),
    }
