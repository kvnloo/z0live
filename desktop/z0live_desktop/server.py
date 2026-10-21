"""Loopback HTTP and authenticated SSE transport."""

import fcntl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import logging
import os
import signal
import threading
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit
from .core import BASE, Store, VERSION, dumps
from .worker import Worker

LOG = logging.getLogger("z0live.http")


class Server(ThreadingHTTPServer):
    store: Store
    stop: threading.Event
    origins: set[str]
    sse_clients: int
    sse_lock: Any
    daemon_threads = True
    allow_reuse_address = True


class Handler(BaseHTTPRequestHandler):
    server: Any
    protocol_version = "HTTP/1.1"
    server_version = "z0live"

    def setup(self):
        super().setup()
        self.connection.settimeout(35)

    def log_message(self, format, *args):
        # Never log query strings/headers/body (could contain credentials).
        LOG.info("%s %s", self.command, urlsplit(self.path).path)

    def reply(self, status, obj):
        raw = dumps(obj).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Connection", "close")
        origin = self.headers.get("Origin")
        if origin and origin in self.server.origins:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(raw)
        self.close_connection = True

    def do_OPTIONS(self):
        origin = self.headers.get("Origin")
        if origin not in self.server.origins:
            return self.reply(403, {"error": "origin not allowed"})
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", origin)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header(
            "Access-Control-Allow-Headers", "Authorization, Content-Type, Last-Event-ID"
        )
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        self.handle_api("GET")

    def do_POST(self):
        self.handle_api("POST")

    def handle_api(self, method):
        parsed = urlsplit(self.path)
        path = parsed.path
        try:
            origin = self.headers.get("Origin")
            if origin and origin not in self.server.origins:
                return self.reply(403, {"error": "origin not allowed"})
            authorization = self.headers.get("Authorization", "")
            device = self.server.store.authenticate(
                authorization[7:] if authorization.startswith("Bearer ") else ""
            )
            if not device:
                return self.reply(401, {"error": "device bearer token required"})
            body = None
            if method == "POST":
                if self.headers.get("Transfer-Encoding"):
                    raise ValueError("chunked requests unsupported")
                length = int(self.headers.get("Content-Length", "0"))
                if not 1 <= length <= 1_000_000:
                    raise ValueError("body length must be 1..1000000 bytes")
                if self.headers.get_content_type() != "application/json":
                    raise ValueError("Content-Type must be application/json")
                body = json.loads(self.rfile.read(length))
            query = parse_qs(parsed.query)
            store = self.server.store
            if method == "GET" and path == "/v1/devices/self":
                result = {"device_id": device}
            elif method == "POST" and path == "/v1/gesture":
                result = store.gestures.submit(device, body)
            elif method == "POST" and path == "/v1/gesture/listener":
                result = store.gestures.listener(device, body)
            elif method == "POST" and path == "/v1/gesture/activity":
                result = store.gestures.activity(device, body)
            elif method == "POST" and path == "/v1/gesture/pin":
                result = store.gestures.pin(device, body)
            elif method == "POST" and path == "/v1/gesture/ack":
                result = store.gestures.ack(device, body)
            elif method == "GET" and path == "/v1/gesture/commands":
                result = {**store.gestures.commands(device), **store.ring_recordings.poll(device)}
            elif method == "POST" and path == "/v1/gesture/recording-report":
                result = store.ring_recordings.report(device, body)
            elif method == "GET" and path == "/v1/health":
                result = {"ok": True, "version": VERSION}
            elif method == "POST" and path == "/v1/realtime/session":
                result = store.new_session(device, body)
            elif method == "POST" and path == "/v1/live/calls":
                result = store.live.open(device, body)
            elif method == "GET" and path == "/v1/live/trace":
                sid = query.get("session_id", [""])[0]
                with store.connect() as db:
                    store.owned(db, sid, device)
                call = store.live.get(sid)
                result = {
                    "delegations": store.live.delegator.trace(sid),
                    "call": {
                        "call_id": call.call_id,
                        "open": call.opened.is_set() and not call.closed.is_set(),
                        "marks": call.marks,
                        "stats": call.stats,
                    }
                    if call
                    else None,
                }
            elif method == "POST" and path == "/v1/events":
                result = store.add_events(device, body)
            elif method == "POST" and path == "/v1/hermes/send":
                result = store.send(device, body)
            elif method == "GET" and path == "/v1/sessions":
                result = store.sessions(device)
            elif method == "GET" and path == "/v1/trace":
                sid = query.get("session_id", [None])[0]
                turn = query.get("turn_id", [None])[0]
                if not sid and not turn:
                    raise ValueError("session_id or turn_id required")
                if turn:
                    with store.connect() as db:
                        row = (
                            db.execute(
                                "SELECT sid FROM voice_turns WHERE id=?", (turn,)
                            ).fetchone()
                            if store.voice
                            else None
                        )
                    if not row:
                        raise LookupError("turn not found")
                    sid = row[0]
                with store.connect() as db:
                    store.owned(db, sid, device)
                result = {"events": store.voice.journal.read(turn, sid)}
            elif method == "POST" and path == "/v1/cancel":
                with store.connect() as db:
                    store.owned(db, body.get("session_id"), device)
                result = store.voice.cancel(
                    body["session_id"], "client_cancel", body.get("turn_id")
                )
            elif method == "GET" and path == "/v1/context":
                result = store.context(device, query.get("session_id", [""])[0])
            elif method == "GET" and path in {"/v1/inbox", "/v1/stream"}:
                after = int(
                    query.get("after", [self.headers.get("Last-Event-ID", "0")])[0]
                )
                if after < 0:
                    raise ValueError("after must be >=0")
                if path == "/v1/stream":
                    return self.stream(device, after)
                result = store.inbox(device, after)
            elif method == "POST" and path.startswith("/v1/tools/"):
                result = store.tool(device, path.rsplit("/", 1)[-1], body)
            elif (
                method == "POST"
                and path.startswith("/v1/sessions/")
                and path.endswith("/end")
            ):
                result = store.end(device, path.split("/")[3])
            else:
                return self.reply(404, {"error": "unknown endpoint"})
            self.reply(200, result)
        except (ValueError, TypeError, KeyError) as exc:
            self.reply(400, {"error": str(exc)[:200]})
        except LookupError:
            self.reply(404, {"error": "not found"})
        except RuntimeError as exc:
            self.reply(502, {"error": str(exc)[:200]})
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        except Exception:
            LOG.exception("request failed")
            self.reply(500, {"error": "internal error; inspect desktop journal"})

    def stream(self, device, after):
        with self.server.sse_lock:
            if self.server.sse_clients >= 16:
                return self.reply(429, {"error": "too many SSE streams"})
            self.server.sse_clients += 1
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.send_header("Connection", "close")
            origin = self.headers.get("Origin")
            if origin in self.server.origins:
                self.send_header("Access-Control-Allow-Origin", origin)
            self.end_headers()
            self.wfile.write(b": z0live connected\n\n")
            self.wfile.flush()
            last = time.monotonic()
            while not self.server.stop.is_set():
                result = self.server.store.inbox(device, after)
                for item in result["messages"]:
                    self.wfile.write(
                        f"id: {item['id']}\nevent: message\ndata: {dumps(item)}\n\n".encode()
                    )
                    after = item["id"]
                if result["messages"] or time.monotonic() - last > 15:
                    self.wfile.write(b": heartbeat\n\n")
                    self.wfile.flush()
                    last = time.monotonic()
                    auth = self.headers.get("Authorization", "")[7:]
                    if not self.server.store.authenticate(auth):
                        break
                self.server.stop.wait(0.5)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.close_connection = True
            with self.server.sse_lock:
                self.server.sse_clients -= 1


def serve():
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    os.umask(0o077)
    store = Store()
    lock = (store.directory / ".service.lock").open("a")
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    stop = threading.Event()
    server = Server(("127.0.0.1", int(os.environ.get("Z0LIVE_PORT", "8791"))), Handler)
    server.store = store
    server.stop = stop
    server.origins = {BASE} | set(
        filter(None, os.environ.get("Z0LIVE_CORS_ORIGINS", "").split(","))
    )
    server.sse_clients = 0
    server.sse_lock = threading.Lock()
    worker = threading.Thread(
        target=Worker(store, stop).run, name="hermes-bridge", daemon=True
    )
    worker.start()

    def shutdown(*_):
        stop.set()
        threading.Thread(target=server.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    LOG.info("listening loopback port=%s pid=%s", server.server_port, os.getpid())
    try:
        server.serve_forever(poll_interval=0.5)
    finally:
        stop.set()
        if store._live is not None:
            store._live.close_all()
        server.server_close()
        worker.join(timeout=30)
