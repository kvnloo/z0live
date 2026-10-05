from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from typing import Any


@dataclass(slots=True)
class ResourceSample:
    gpu_index: int | None = None
    gpu_util_pct: float | None = None
    gpu_memory_used_mb: float | None = None
    gpu_memory_free_mb: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "gpu_index": self.gpu_index,
            "gpu_util_pct": self.gpu_util_pct,
            "gpu_memory_used_mb": self.gpu_memory_used_mb,
            "gpu_memory_free_mb": self.gpu_memory_free_mb,
        }


class ResourceTracker:
    def __init__(self, gpu_index: int | None = None) -> None:
        self.gpu_index = gpu_index
        self.samples = 0
        self.max_gpu_util_pct: float | None = None
        self.max_gpu_memory_used_mb: float | None = None
        self.min_gpu_memory_free_mb: float | None = None

    def capture(self) -> ResourceSample:
        sample = sample_nvidia(self.gpu_index)
        self.samples += 1
        if sample.gpu_util_pct is not None:
            self.max_gpu_util_pct = max(self.max_gpu_util_pct or 0.0, sample.gpu_util_pct)
        if sample.gpu_memory_used_mb is not None:
            self.max_gpu_memory_used_mb = max(
                self.max_gpu_memory_used_mb or 0.0, sample.gpu_memory_used_mb
            )
        if sample.gpu_memory_free_mb is not None:
            self.min_gpu_memory_free_mb = (
                sample.gpu_memory_free_mb
                if self.min_gpu_memory_free_mb is None
                else min(self.min_gpu_memory_free_mb, sample.gpu_memory_free_mb)
            )
        return sample

    def summary(self) -> dict[str, Any]:
        return {
            "samples": self.samples,
            "gpu_index": self.gpu_index,
            "max_gpu_util_pct": self.max_gpu_util_pct,
            "max_gpu_memory_used_mb": self.max_gpu_memory_used_mb,
            "min_gpu_memory_free_mb": self.min_gpu_memory_free_mb,
        }


def sample_nvidia(gpu_index: int | None = None) -> ResourceSample:
    if shutil.which("nvidia-smi") is None:
        return ResourceSample(gpu_index=gpu_index)
    cmd = [
        "nvidia-smi",
        "--query-gpu=index,utilization.gpu,memory.used,memory.free",
        "--format=csv,noheader,nounits",
    ]
    try:
        out = subprocess.check_output(cmd, text=True, timeout=2)
    except (subprocess.SubprocessError, OSError):
        return ResourceSample(gpu_index=gpu_index)
    for raw in out.splitlines():
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 4:
            continue
        try:
            idx = int(parts[0])
            if gpu_index is not None and idx != gpu_index:
                continue
            return ResourceSample(
                gpu_index=idx,
                gpu_util_pct=float(parts[1]),
                gpu_memory_used_mb=float(parts[2]),
                gpu_memory_free_mb=float(parts[3]),
            )
        except ValueError:
            continue
    return ResourceSample(gpu_index=gpu_index)
