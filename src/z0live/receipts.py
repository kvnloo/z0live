from __future__ import annotations

import json
import threading
import time
from pathlib import Path
from typing import Any


class ReceiptWriter:
    def __init__(self, path: str | Path) -> None:
        self.path = Path(path).expanduser()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def emit(self, event: str, **fields: Any) -> None:
        row = {
            "schema": "z0live.receipt.v1",
            "event": event,
            "unix_time": time.time(),
            **fields,
        }
        line = json.dumps(row, sort_keys=True, ensure_ascii=False, default=str) + "\n"
        with self._lock:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(line)
