"""Model lanes for the call: one plain model call per request through Hermes's own logins, no agent turn.

Runs under the Hermes runtime (it imports hermes-agent), loopback only. The adapter POSTs
  /v1/ask {"provider", "model", "system", "user", "max_tokens", "timeout"} -> {"text", "first_s", "total_s"}
Measured 11-19 on the Studio, warm: Haiku 5.5 0.73 s first token here vs 3.7 s through the Hermes host's agent
turn (desktop/hermes_host.py), Sonnet 5.5 0.74 s vs 2.6 s, Opus 5.5 1.27 s vs 1.25 s.
"""
import json
import logging
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.environ.get("Z0LIVE_HERMES_SOURCE", os.path.expanduser("~/.hermes/hermes-agent")))
import hermes_bootstrap  # noqa: E402,F401 - puts the installed Hermes environment on sys.path (as xai_secret.py)
from agent.auxiliary_client import call_llm  # noqa: E402

LOG = logging.getLogger("z0live.lanes")
PORT = int(os.environ.get("Z0LIVE_LANE_PORT", "8646"))
WARM = os.environ.get("Z0LIVE_LANE_WARM", "claude-subscription-directsdk-experimental:claude-haiku-5-5")


def ask(provider, model, system, user, max_tokens=300, timeout=60.0):
    t0, first, text = time.monotonic(), None, ""
    messages = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": user}]
    for chunk in call_llm(provider=provider, model=model, messages=messages, max_tokens=max_tokens,
                          stream=True, timeout=timeout):
        choices = getattr(chunk, "choices", None)
        piece = (choices[0].delta.content if choices else None) or ""
        if piece and first is None:
            first = time.monotonic() - t0
        text += piece
    return {"text": " ".join(text.split()), "first_s": round(first or 0, 3), "total_s": round(time.monotonic() - t0, 3)}


class Handler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/v1/ask":
            return self.reply(404, {"error": "not found"})
        try:
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)))
            out = ask(str(body["provider"]), str(body["model"]), str(body.get("system") or ""), str(body["user"]),
                      int(body.get("max_tokens") or 300), float(body.get("timeout") or 60))
            LOG.info("lane %s first=%.2fs total=%.2fs", body["model"], out["first_s"], out["total_s"])
            self.reply(200, out)
        except Exception as exc:  # noqa: BLE001 - a lane failure is a non-answer, never a crash
            LOG.warning("lane failed: %s: %s", type(exc).__name__, str(exc)[:200])
            self.reply(502, {"error": f"{type(exc).__name__}: {str(exc)[:200]}"})

    def reply(self, status, payload):
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, format, *args):  # noqa: A002 - quiet; lane_server logs its own lines
        pass


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    for spec in filter(None, WARM.split(",")):  # pay the cold start before the first call
        provider, model = spec.split(":", 1)
        try:
            LOG.info("warm %s %s", model, ask(provider, model, "", "Reply with just: ready", 5, 60))
        except Exception as exc:  # noqa: BLE001
            LOG.warning("warm %s failed: %s", model, type(exc).__name__)
    ThreadingHTTPServer(("127.0.0.1", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
