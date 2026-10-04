from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SUPPORTED_SCHEMA = "z0int.voice_plan.v1"


@dataclass(frozen=True)
class VoicePlan:
    raw: dict[str, Any]

    @property
    def plan_id(self) -> str:
        return str(self.raw["plan_id"])

    @property
    def actor_id(self) -> str:
        return str(self.raw["actor_id"])

    @property
    def adapter(self) -> str:
        return str(self.raw["adapter"])

    @property
    def endpoint_host(self) -> str:
        return str((self.raw.get("endpoint") or {}).get("host") or "127.0.0.1")

    @property
    def endpoint_port(self) -> int:
        return int((self.raw.get("endpoint") or {}).get("port") or 8998)

    @property
    def idle_unload_seconds(self) -> int:
        return max(0, int((self.raw.get("resource") or {}).get("idle_unload_seconds") or 0))

    @property
    def device_index(self) -> int | None:
        device = self.raw.get("device") or {}
        value = device.get("index")
        return None if value is None else int(value)


def validate_plan(raw: dict[str, Any]) -> VoicePlan:
    if raw.get("schema") != SUPPORTED_SCHEMA:
        raise ValueError(f"unsupported VoicePlan schema: {raw.get('schema')!r}")
    if not raw.get("plan_id"):
        raise ValueError("VoicePlan missing plan_id")
    if not raw.get("actor_id") or not raw.get("adapter"):
        raise ValueError("VoicePlan missing actor_id/adapter")
    admission = raw.get("admission") or {}
    if admission.get("admitted") is not True:
        status = admission.get("status") or "unknown"
        reclaim = admission.get("reclaim_needed_mb")
        suffix = f"; reclaim_needed_mb={reclaim}" if reclaim is not None else ""
        raise ValueError(f"VoicePlan not admitted: {status}{suffix}")
    resource = raw.get("resource") or {}
    if resource.get("residency") != "session":
        raise ValueError("z0live brainstorm currently requires session residency")
    return VoicePlan(raw=raw)


def load_plan(path: str | Path) -> VoicePlan:
    raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("VoicePlan must be a JSON object")
    return validate_plan(raw)
