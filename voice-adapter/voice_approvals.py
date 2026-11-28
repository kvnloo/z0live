"""Follow a Hermes run and announce approval requests on the call."""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path

MAX_SPOKEN_COMMAND = 220


def enabled() -> bool:
    return os.environ.get("Z0LIVE_VOICE_APPROVALS", "").strip().lower() in ("1", "on", "true", "yes")


def spoken_prompt(event: dict, profile: str) -> str:
    """What the voice says while a request waits. The command is Hermes' own redacted display text."""
    command = " ".join(str(event.get("command") or "").split())
    if len(command) > MAX_SPOKEN_COMMAND:
        command = command[:MAX_SPOKEN_COMMAND].rsplit(" ", 1)[0] + " ..."
    why = " ".join(str(event.get("description") or "").split())[:120]
    what = f"run {command}" if command else "take an action it flagged"
    return (f"Approval needed. {profile} wants to {what}" + (f" ({why})" if why else "") +
            ". Review this request in an authenticated Hermes approval interface. Keep approval secrets off the call.")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class RunsClient:
    """A z0live Hermes host's Runs API (loopback only; the bearer is the host key file)."""

    def __init__(self, port: int, key: str, timeout: float = 30.0):
        self.base, self.key, self.timeout = f"http://127.0.0.1:{int(port)}", key, timeout
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    @classmethod
    def for_profile(cls, profile: str, port: int, home: Path | None = None) -> "RunsClient":
        key_file = (home or Path.home()) / ".config/z0live/guest-hosts" / f"{profile}.key"
        try:
            return cls(port, key_file.read_text().strip())
        except OSError:
            raise RuntimeError(f"no host key for {profile}") from None

    def _req(self, method: str, path: str, body: dict | None = None, timeout: float | None = None):
        headers = {"Authorization": "Bearer " + self.key}
        data = None
        if body is not None:
            headers["Content-Type"] = "application/json"
            data = json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        return self.opener.open(req, timeout=timeout or self.timeout)  # noqa: S310 - loopback only

    def _json(self, method: str, path: str, body: dict | None = None) -> dict:
        try:
            with self._req(method, path, body) as r:
                return json.loads(r.read().decode())
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"host returned HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise RuntimeError(f"host is not reachable ({type(exc).__name__})") from None
        except ValueError:
            raise RuntimeError("host returned non-JSON") from None

    def start(self, text: str, session: str, instructions: str = "") -> str:
        body = {"input": text, "session_id": session}
        if instructions:
            body["instructions"] = instructions
        run_id = str(self._json("POST", "/v1/runs", body).get("run_id") or "")
        if not re.fullmatch(r"[A-Za-z0-9_-]+", run_id):
            raise RuntimeError("host returned an invalid run id")
        return run_id

    def events(self, run_id: str, timeout: float):
        """Yield each SSE event (a dict) until the stream closes. ``timeout`` bounds a silent socket, not the run."""
        try:
            resp = self._req("GET", f"/v1/runs/{run_id}/events", timeout=timeout)
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"host returned HTTP {exc.code}") from None
        except (urllib.error.URLError, OSError, TimeoutError) as exc:
            raise RuntimeError(f"host is not reachable ({type(exc).__name__})") from None
        with resp:
            data: list[str] = []
            for raw in resp:
                line = raw.decode("utf-8", "replace").rstrip("\r\n")
                if line.startswith("data:"):
                    data.append(line[5:].lstrip())
                    continue
                if line or not data:
                    continue
                try:
                    event = json.loads("\n".join(data))
                except ValueError:
                    event = None
                data = []
                if isinstance(event, dict):
                    yield event

    def status(self, run_id: str) -> dict:
        return self._json("GET", f"/v1/runs/{run_id}")


def event_kind(event: dict) -> str:
    return str(event.get("event") or event.get("type") or "")


def request_id(event: dict) -> str:
    return str(event.get("request_id") or event.get("prompt_id") or "")


def run(client: RunsClient, text: str, session: str, instructions: str, on_request, idle_timeout: float = 900.0):
    """Follow one run and deliver approval events to a nonblocking observer."""
    run_id = client.start(text, session, instructions)
    output, failed = "", ""
    try:
        for event in client.events(run_id, idle_timeout):
            kind = event_kind(event)
            if kind in ("approval.request", "approval.responded"):
                on_request(run_id, event)
            elif kind == "run.completed":
                output = str(event.get("output") or "")
                break
            elif kind in ("run.failed", "run.cancelled", "run.interrupted"):
                failed = str(event.get("error") or kind)
                break
    finally:
        on_request(run_id, {"event": "approval.cleared"})
    if not output and not failed:
        try:
            st = client.status(run_id)
            output = str(st.get("output") or "")
            failed = "" if output else str(st.get("error") or st.get("status") or "no output")
        except RuntimeError as exc:
            failed = str(exc)
    return run_id, output, failed
