from __future__ import annotations

import functools
import mimetypes
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


def default_web_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "web" / "dist"


class _Handler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".wasm": "application/wasm",
        ".js": "text/javascript",
    }

    def log_message(self, format: str, *args) -> None:
        return None


class StaticWebServer:
    def __init__(
        self,
        directory: str | Path,
        *,
        host: str = "127.0.0.1",
        port: int = 8780,
    ) -> None:
        self.directory = Path(directory).resolve()
        self.host = host
        self.port = int(port)
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    @property
    def address(self) -> tuple[str, int] | None:
        if self._server is None:
            return None
        host, port = self._server.server_address[:2]
        return str(host), int(port)

    def start(self) -> tuple[str, int]:
        if not self.directory.is_dir():
            raise FileNotFoundError(
                f"z0live web build not found at {self.directory}; run npm --prefix web install && npm --prefix web run build"
            )
        handler = functools.partial(_Handler, directory=str(self.directory))
        self._server = ThreadingHTTPServer((self.host, self.port), handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever,
            name="z0live-web",
            daemon=True,
        )
        self._thread.start()
        assert self.address is not None
        return self.address

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2)
            self._thread = None
