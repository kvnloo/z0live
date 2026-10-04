import socket
import socketserver
import threading
import time

from z0live.proxy import ActivityProxy


class Echo(socketserver.BaseRequestHandler):
    def handle(self):
        data = self.request.recv(65536)
        if data:
            self.request.sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


def test_proxy_forwards_and_marks_meaningful_activity():
    backend_port = free_port()
    public_port = free_port()
    events = []
    backend = Server(("127.0.0.1", backend_port), Echo)
    thread = threading.Thread(target=backend.serve_forever, daemon=True)
    thread.start()
    proxy = ActivityProxy(
        listen_host="127.0.0.1",
        listen_port=public_port,
        backend_host="127.0.0.1",
        backend_port=backend_port,
        on_activity=lambda: events.append(time.monotonic()),
        threshold_bytes=64,
    )
    proxy.start()
    try:
        payload = b"x" * 128
        with socket.create_connection(("127.0.0.1", public_port), timeout=2) as s:
            s.sendall(payload)
            assert s.recv(512) == payload
        deadline = time.time() + 1
        while not events and time.time() < deadline:
            time.sleep(0.01)
        assert events
    finally:
        proxy.stop()
        backend.shutdown()
        backend.server_close()
