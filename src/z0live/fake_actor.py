from __future__ import annotations

import argparse
import socketserver


class Echo(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        while True:
            data = self.request.recv(65536)
            if not data:
                return
            self.request.sendall(data)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, required=True)
    args = p.parse_args(argv)
    with Server((args.host, args.port), Echo) as server:
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
