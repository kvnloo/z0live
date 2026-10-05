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
    def provider(self) -> str:
        return str(self.raw.get("provider") or "unknown")

    @property
    def model(self) -> str:
        return str(self.raw.get("model") or "")

    @property
    def harness(self) -> str:
        return str(self.raw.get("harness") or "")

    @property
    def endpoint(self) -> dict[str, Any]:
        return dict(self.raw.get("endpoint") or {})

    @property
    def endpoint_host(self) -> str:
        return str(self.endpoint.get("host") or "127.0.0.1")

    @property
    def endpoint_port(self) -> int:
        return int(self.endpoint.get("port") or 8998)

    @property
    def idle_unload_seconds(self) -> int:
        return max(0, int((self.raw.get("resource") or {}).get("idle_unload_seconds") or 0))

    @property
    def device_index(self) -> int | None:
        value = (self.raw.get("device") or {}).get("index")
        return None if value is None else int(value)

    @property
    def actor_options(self) -> dict[str, Any]:
        return dict(self.raw.get("actor_options") or {})


def validate_plan(raw: dict[str, Any]) -> VoicePlan:
    if raw.get("schema") != SUPPORTED_SCHEMA:
        raise ValueError(f"unsupported VoicePlan schema: {raw.get('schema')!r}")
    for field in ("plan_id", "actor_id", "adapter"):
        if not raw.get(field):
            raise ValueError(f"VoicePlan missing {field}")
    admission = raw.get("admission") or {}
    if admission.get("admitted") is not True:
        status = admission.get("status") or "unknown"
        reclaim = admission.get("reclaim_needed_mb")
        suffix = f"; reclaim_needed_mb={reclaim}" if reclaim is not None else ""
        raise ValueError(f"VoicePlan not admitted: {status}{suffix}")
    resource = raw.get("resource") or {}
    residency = resource.get("residency")
    if residency not in (None, "session"):
        raise ValueError(f"unsupported VoicePlan residency: {residency!r}")
    return VoicePlan(raw=dict(raw))


def load_plan(path: str | Path) -> VoicePlan:
    raw = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    if not isinstance(raw, dict):
        raise ValueError("VoicePlan must be a JSON object")
    return validate_plan(raw)
