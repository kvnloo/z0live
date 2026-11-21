"""The Studio proxy and deployer share one atomic route and per-slot admission locks."""
from __future__ import annotations

import asyncio
import fcntl
import json
import os
import tempfile
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from pathlib import Path


@dataclass(frozen=True)
class Route:
    active: int
    phase: str = "steady"


class Slots:
    def __init__(self, directory: str | Path, ports: tuple[int, int] = (9168, 9169)):
        self.directory = Path(directory)
        self.ports = ports

    def read(self) -> Route:
        route = Route(**json.loads((self.directory / "route.json").read_text()))
        if type(route.active) is not int or route.active not in self.ports or route.phase not in (
            "steady", "staging", "draining",
        ):
            raise ValueError("Invalid voice route")
        return route

    def write(self, route: Route) -> None:
        with tempfile.NamedTemporaryFile(mode="w", dir=self.directory, delete=False) as f:
            temporary = Path(f.name)
            try:
                f.write(json.dumps(asdict(route)) + "\n")
                f.close()
                temporary.replace(self.directory / "route.json")
            finally:
                temporary.unlink(missing_ok=True)

    def other(self, active: int) -> int:
        first, second = self.ports
        return second if active == first else first

    def release(self, port: int) -> Path:
        return self.directory / f"release-{port}"

    def admission_lock(self, port: int):
        return (self.directory / f"admission-{port}.lock").open("a")

    @asynccontextmanager
    async def admit(self):
        while True:
            port = self.read().active
            with self.admission_lock(port) as lock:
                try:
                    fcntl.flock(lock, fcntl.LOCK_SH | fcntl.LOCK_NB)
                except BlockingIOError:
                    pass
                else:
                    # A cutover between read and lock must not admit a late request.
                    if self.read().active == port:
                        yield f"http://127.0.0.1:{port}"
                        return
            await asyncio.sleep(0.01)
