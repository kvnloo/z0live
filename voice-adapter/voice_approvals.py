"""N230: a Hermes approval prompt reaches the call; Kevin approves it by saying his pass-phrase.

Hermes already ships the request side: an API host's ``POST /v1/runs`` emits an ``approval.request`` event (request id,
redacted command, description, choices) on ``GET /v1/runs/{id}/events`` and parks the run until
``POST /v1/runs/{id}/approval`` answers that exact request id, or until the profile's ``approvals.timeout`` denies it.
This module is the call side only:

- ``RunsClient``: start a run on a z0live Hermes host, read its events, answer one request.
- ``spoken_prompt``: the line the voice says while a request is pending (the command Hermes already redacted).
- ``load_phrase`` / ``matches``: the pass-phrase check. The phrase lives in a profile ``.env`` (secret-intake writes
  ``Z0LIVE_APPROVAL_PHRASE``), never in git, traces or logs. Fail closed: no phrase set, an empty answer or any
  other words deny. Silence leaves the request alone, so Hermes' own ``approvals.timeout`` default decides.
- ``decide``: the one choice for a heard answer: ``once`` on a match, ``deny`` otherwise. Never ``session``/``always``.

Nothing here can raise an approval or widen one: Hermes decides which commands need approval, and an answer only
settles the request id it was raised for.
"""
from __future__ import annotations

import hmac
import json
import os
import re
import unicodedata
import urllib.error
import urllib.request
from pathlib import Path

PHRASE_ENV = "Z0LIVE_APPROVAL_PHRASE"
MIN_PHRASE_CHARS = 8  # shorter is too easy to say by accident (or for a guest to guess): treated as unset
MAX_HEARD_CHARS = 400
MAX_SPOKEN_COMMAND = 220
APPROVED = "Approved once. Hermes is running it."
DENIED = "Denied. That wasn't the pass-phrase, so Hermes won't run it."
NO_PHRASE = "Denied: no approval pass-phrase is set up, so I can't approve anything by voice."
SETTLED = "That approval was already settled, so nothing changed."


def normalize(text: str) -> str:
    """Lowercase ASCII words only: ASR adds punctuation, capitals and accents a spoken phrase never had."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def _read_env_key(path: Path, name: str) -> str:
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return ""
    for line in lines:
        line = line.strip()
        if line.startswith("export "):
            line = line[7:].lstrip()
        key, sep, value = line.partition("=")
        if sep and key.strip() == name:
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            return value
    return ""


def load_phrase(profile: str = "tek", home: Path | None = None) -> str:
    """The normalized pass-phrase, or "" when none is set (callers then deny). Read at each request, so a phrase set
    or rotated through secret-intake takes effect on the next prompt without a restart. Order: the process env, then
    the caller profile's .env (where secret-intake saves it)."""
    raw = os.environ.get(PHRASE_ENV, "")
    if not raw.strip() and re.fullmatch(r"[A-Za-z0-9_-]+", profile or ""):
        base = home or Path.home()
        raw = _read_env_key(base / ".hermes/profiles" / profile / ".env", PHRASE_ENV)
    phrase = normalize(raw)
    return phrase if len(phrase) >= MIN_PHRASE_CHARS else ""


def matches(heard: str, phrase: str) -> bool:
    """True when the heard words contain the phrase as one contiguous word run ("approve, blue harbor lantern" counts).
    Constant-time per window; an empty phrase or answer never matches."""
    if not phrase or not heard or len(heard) > MAX_HEARD_CHARS:
        return False
    words, want = normalize(heard).split(), phrase.split()
    n, hit = len(want), False
    for i in range(max(0, len(words) - n + 1)):
        hit |= hmac.compare_digest(" ".join(words[i:i + n]).encode(), phrase.encode())
    return hit


def decide(heard: str, phrase: str) -> str:
    """'once' for the pass-phrase, 'deny' for anything else (no phrase set included)."""
    return "once" if matches(heard, phrase) else "deny"


def mask(text: str, phrase: str) -> str:
    """The heard text with the phrase cut out, for traces. Only the length is kept when it matched."""
    return "[approval answer: pass-phrase]" if matches(text, phrase) else "[approval answer: %d chars]" % len(text or "")


def spoken_prompt(event: dict, profile: str) -> str:
    """What the voice says while a request waits. The command is Hermes' own redacted display text."""
    command = " ".join(str(event.get("command") or "").split())
    if len(command) > MAX_SPOKEN_COMMAND:
        command = command[:MAX_SPOKEN_COMMAND].rsplit(" ", 1)[0] + " ..."
    why = " ".join(str(event.get("description") or "").split())[:120]
    what = f"run {command}" if command else "take an action it flagged"
    return (f"Approval needed. {profile} wants to {what}" + (f" ({why})" if why else "") +
            ". Say your pass-phrase to approve it once. Anything else denies it.")


class RunsClient:
    """A z0live Hermes host's Runs API (loopback only; the bearer is the host key file)."""

    def __init__(self, port: int, key: str, timeout: float = 30.0):
        self.base, self.key, self.timeout = f"http://127.0.0.1:{int(port)}", key, timeout

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
        return urllib.request.urlopen(req, timeout=timeout or self.timeout)  # noqa: S310 - loopback only

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
        if not run_id:
            raise RuntimeError("host returned no run id")
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

    def respond(self, run_id: str, request_id: str, choice: str, generation=None) -> dict:
        """Answer exactly one request. Only 'once' or 'deny' ever leave the call."""
        if choice not in ("once", "deny") or not request_id:
            raise ValueError("a voice answer is once or deny, for one request id")
        body = {"request_id": request_id, "choice": choice}
        if generation is not None:
            body["execution_generation"] = generation
        return self._json("POST", f"/v1/runs/{run_id}/approval", body)

    def status(self, run_id: str) -> dict:
        return self._json("GET", f"/v1/runs/{run_id}")


def event_kind(event: dict) -> str:
    return str(event.get("event") or event.get("type") or "")


def request_id(event: dict) -> str:
    return str(event.get("request_id") or event.get("prompt_id") or "")


def run(client: RunsClient, text: str, session: str, instructions: str, on_request, idle_timeout: float = 900.0):
    """Start one run and follow it to its end. ``on_request(run_id, event)`` is called (on this thread) for each
    ``approval.request``, each ``approval.responded`` and a synthetic ``approval.cleared`` (the run stopped waiting
    without an answer from the call: Hermes' timeout, or another surface). It must not block: the call answers later
    through ``client.respond``. Returns (run_id, output, failed, approvals seen on the stream)."""
    run_id = client.start(text, session, instructions)
    output, failed, approvals, waiting = "", "", [], False
    for event in client.events(run_id, idle_timeout):
        kind = event_kind(event)
        if kind == "approval.request":
            waiting = True
            approvals.append({"request_id": request_id(event), "command": str(event.get("command") or "")[:300]})
            on_request(run_id, event)
        elif kind == "approval.responded":
            waiting = False
            approvals.append({"request_id": request_id(event), "responded": str(event.get("choice") or "")})
            on_request(run_id, event)
        elif kind == "run.completed":
            output = str(event.get("output") or "")
            break
        elif kind in ("run.failed", "run.cancelled", "run.interrupted"):
            failed = str(event.get("error") or kind)
            break
        elif kind == "tool.completed" and waiting:
            try:  # a parallel tool can finish while the request still waits: ask the run itself
                still = client.status(run_id).get("status") == "waiting_for_approval"
            except RuntimeError:
                still = False
            if not still:
                waiting = False
                on_request(run_id, {"event": "approval.cleared"})
    if waiting:
        on_request(run_id, {"event": "approval.cleared"})
    if not output and not failed:
        try:
            st = client.status(run_id)
            output = str(st.get("output") or "")
            failed = "" if output else str(st.get("error") or st.get("status") or "no output")
        except RuntimeError as exc:
            failed = str(exc)
    return run_id, output, failed, approvals
