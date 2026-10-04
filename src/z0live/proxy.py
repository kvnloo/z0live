from __future__ import annotations

import select
import socket
import socketserver
import threading
from collections.abc import Callable


class ActivityMeter:
    """Turn transport bytes into coarse meaningful-activity heartbeats.

    The threshold filters tiny TCP/TLS/WebSocket keepalives while audio traffic
    crosses it quickly. It does not inspect or decrypt user audio.
    """

    def __init__(self, callback: Callable[[], None], threshold_bytes: int = 2048) -> None:
        self.callback = callback
        self.threshold_bytes = max(1, int(threshold_bytes))
        self._bytes = 0
        self._lock = threading.Lock()

    def add(self, n: int) -> None:
        if n <= 0:
            return
        fire = False
        with self._lock:
            self._bytes += n
            if self._bytes >= self.threshold_bytes:
                self._bytes = 0
                fire = True
        if fire:
            self.callback()


class _ThreadingTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class ActivityProxy:
    """Raw TCP passthrough that observes byte volume without terminating TLS."""

    def __init__(
        self,
        *,
        listen_host: str,
        listen_port: int,
        backend_host: str,
        backend_port: int,
        on_activity: Callable[[], None],
        threshold_bytes: int = 2048,
    ) -> None:
        self.listen_host = listen_host
        self.listen_port = int(listen_port)
        self.backend_host = backend_host
        self.backend_port = int(backend_port)
        self.meter = ActivityMeter(on_activity, threshold_bytes=threshold_bytes)
        self._server: _ThreadingTCPServer | None = None
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        outer = self

        class Handler(socketserver.BaseRequestHandler):
            def handle(self) -> None:
                try:
                    upstream = socket.create_connection(
                        (outer.backend_host, outer.backend_port), timeout=5.0
                    )
                except OSError:
                    return
                with upstream:
                    client = self.request
                    client.setblocking(False)
                    upstream.setblocking(False)
                    sockets = [client, upstream]
                    while True:
                        try:
                            readable, _, exceptional = select.select(sockets, [], sockets, 1.0)
                        except (OSError, ValueError):
                            return
                        if exceptional:
                            return
                        for src in readable:
                            dst = upstream if src is client else client
                            try:
                                data = src.recv(65536)
                            except (BlockingIOError, OSError):
                                return
                            if not data:
                                return
                            outer.meter.add(len(data))
                            try:
                                dst.sendall(data)
                            except OSError:
                                return

        self._server = _ThreadingTCPServer((self.listen_host, self.listen_port), Handler)
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="z0live-proxy", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
