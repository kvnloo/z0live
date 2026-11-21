"""Replay a recorded call through the real adapter and score it against the Dessa contract.

A fixture is the provider side of one call (`rx` events) plus what each delegation cost: Hermes's work time and
answer, and each lane's time and answer. `replay` feeds the events to a real `mobile_live.Call` on the recorded
clock. Hermes is a fake dashboard chat behind the real `chat_continuity.ask_chat` client, over a real WebSocket;
the lanes are fakes behind `tiers.race`. The voice's own replies are the recorded ones, so the replay checks what
the adapter sends and when, not what the voice would have said to it.

    replay.py fixture TRACE.jsonl OUT.jsonl   # cut a fixture from a call trace (stays local: it has transcripts)
    replay.py run FIXTURE.jsonl ...           # print the contract report per call

The fake chat follows the dashboard's busy rule (display.busy_input_mode=interrupt): a prompt sent while a turn
runs redirects that turn (receipt status "redirected"), and the turn ends with one message.complete that every
socket listening on the chat receives. A redirected turn ends at max(its end, the new prompt + its own work).
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, web

FINAL = '"Agent Final Message":'
_did = contextvars.ContextVar("replay_did", default=None)


def fixture(trace: Path) -> list[dict]:
    """Provider events, mute toggles and delegation costs from one trace. Hermes's work time excludes the wait
    behind an earlier delegation of the same call (the per-call lock in the api path)."""
    rows = []
    for line in trace.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    out, started, free_at = [], {}, 0.0
    for r in rows:
        ev = r.get("ev")
        if ev == "rx" and (r.get("src") == "provider" or str(r.get("type", "")).startswith("session.input_audio")):
            event = r["event"]
            if r.get("type") == "session.context.appended":
                continue
            out.append({"t": r["t"], "rx": event, "src": r.get("src") or "mobile"})
        elif ev == "delegation.start":
            started[r["did"]] = r["t"]
        elif ev in ("delegation.done", "delegation.failed") and r.get("did") in started:
            began = max(started[r["did"]], free_at)
            free_at = r["t"]
            out.append({"t": r["t"], "cost": "hermes", "did": r["did"], "work": round(r["t"] - began, 3),
                        "answer": r.get("answer") or ""})
        elif ev == "lanes.tier":
            out.append({"t": r["t"], "cost": "lane", "did": r["did"], "tier": r["tier"], "s": r.get("s"),
                        "text": r.get("text") or ""})
    return out


@dataclass
class Turn:
    ends: float
    answer: str
    sockets: list = field(default_factory=list)


class FakeChat:
    """The dashboard's /api/ws for one chat, on the replay clock."""

    def __init__(self, replay):
        self.replay, self.turn, self.sockets, self.runner = replay, None, set(), None

    async def start(self):
        app = web.Application()
        app.router.add_get("/api/ws", self.ws)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        return f"ws://127.0.0.1:{site._server.sockets[0].getsockname()[1]}/api/ws"

    async def ws(self, request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.sockets.add(ws)
        try:
            async for msg in ws:
                frame = json.loads(msg.data)
                rid, method, params = frame["id"], frame["method"], frame.get("params") or {}
                if method == "session.resume":
                    await ws.send_json({"id": rid, "result": {"session_id": params["session_id"]}})
                elif method == "prompt.submit":
                    # The handler runs in the server's task: find the delegation by the text it sent.
                    did = next((d for d, text in reversed(self.replay.requests) if text == params["text"]), None)
                    self.replay.submitted.append((self.replay.now, did, params["text"]))
                    work, answer = self.replay.hermes.get(did, (5.0, f"answer for {did}"))
                    if self.turn is None:
                        self.turn = Turn(self.replay.now + work, answer)
                        status = "streaming"
                    else:  # busy: redirect the running turn (the dashboard's default busy_input_mode)
                        self.turn.ends = max(self.turn.ends, self.replay.now + work)
                        self.turn.answer = answer
                        status = "redirected"
                    await ws.send_json({"id": rid, "result": {"status": status}})
        finally:
            self.sockets.discard(ws)
        return ws

    async def tick(self, now):
        if self.turn and self.turn.ends <= now:
            turn, self.turn = self.turn, None
            frame = {"method": "event", "params": {"type": "message.complete", "session_id": "chat-1",
                                                   "payload": {"text": turn.answer}}}
            for ws in list(self.sockets):
                await ws.send_json(frame)

    async def close(self):
        await self.runner.cleanup()


class Replay:
    def __init__(self, rows: list[dict]):
        self.rows = sorted(rows, key=lambda r: r["t"])
        self.now = 0.0
        self.hermes = {r["did"]: (r["work"], r["answer"]) for r in self.rows if r.get("cost") == "hermes"}
        self.lanes = {}
        for r in self.rows:
            if r.get("cost") == "lane":
                self.lanes.setdefault(r["did"], {})[r["tier"]] = (r["s"] or 0.0, r["text"])
        self.submitted: list[tuple[float, str, str]] = []  # (t, did, text) sent to Hermes
        self.sent: list[tuple[float, dict]] = []  # events sent to the voice provider
        self.started: dict[str, float] = {}
        self.requests: list[tuple[str, str]] = []  # (did, request) as host_delegate received them
        self.waiting: list[tuple[float, asyncio.Future, object]] = []

    def later(self, seconds, value):
        future = asyncio.get_running_loop().create_future()
        self.waiting.append((self.now + seconds, future, value))
        return future

    async def pump(self, n=20):
        for _ in range(n):
            await asyncio.sleep(0)

    async def advance(self, t, chat):
        while True:
            due = sorted((w for w in self.waiting if w[0] <= t), key=lambda w: w[0])
            turn_end = chat.turn.ends if chat.turn and chat.turn.ends <= t else None
            if not due and turn_end is None:
                break
            at = min([w[0] for w in due] + ([turn_end] if turn_end is not None else []))
            self.now = at
            for w in [w for w in due if w[0] == at]:
                self.waiting.remove(w)
                if not w[1].done():
                    w[1].set_result(w[2])
            await chat.tick(at)
            await self.pump()
            await asyncio.sleep(0.005)  # socket round trips
            await self.pump()
        self.now = t

    async def run(self):
        import mobile_live as live
        import tiers

        chat = FakeChat(self)
        url = await chat.start()
        replay = self

        async def race(http, name, bindings, context, request):
            did = _did.get()
            s, text = replay.lanes.get(did, {}).get(name, (None, ""))
            if s is None:
                return "", None, None, {}
            return await replay.later(s, (text, name, s, {}))

        original = live.Call.host_delegate

        async def host_delegate(call, did, request):
            _did.set(did)
            self.started[did] = self.now
            self.requests.append((did, request))
            await original(call, did, request)

        cfg = {"fast": [{"provider": "p", "model": "f"}], "medium": [{"provider": "p", "model": "m"}],
               "slow": [{"provider": "p", "model": "s"}]}
        with tempfile.TemporaryDirectory() as traces, \
                patch.object(live, "TRACE_DIR", Path(traces)), patch.object(live, "TIERS_ON", True), \
                patch.object(live.chat_continuity, "WS", url), patch.object(tiers, "race", race), \
                patch.object(tiers, "load", return_value=cfg), patch.object(live.Call, "host_delegate", host_delegate), \
                patch.object(live.guest_voice, "configured", return_value=None):
            http = ClientSession()
            call = live.Call(live.Manager(http), profile="")
            call.relay, call.chat, call.heard_user = None, "chat-1", True
            call.subscription.ready.set()
            call.send = lambda event, to_provider=False, **_: to_provider and self.sent.append((self.now, event))
            for row in self.rows:
                await self.advance(row["t"], chat)
                if "rx" in row:
                    call.message(json.dumps(row["rx"]), row["src"] == "provider")
                    await self.pump()
            await self.advance(self.rows[-1]["t"] + 900, chat)
            call.closed = True
            for task in list(call.tasks):
                task.cancel()
            await asyncio.gather(*call.tasks, return_exceptions=True)
            await http.close()
            await chat.close()
        return report(self)


def words(text):
    return re.findall(r"[a-z0-9']+", text.lower())


def report(replay: Replay) -> dict:
    """The adapter's side of the Dessa contract, from what it sent."""
    rx = [(r["t"], r["rx"]) for r in replay.rows if "rx" in r]
    user_turns = [(t, (e.get("turn") or {}).get("transcript") or "") for t, e in rx
                  if e.get("type") == "turn.done" and (e.get("turn") or {}).get("role") == "user"]
    replies, open_at = [], None  # (created, done) of the voice's replies
    for t, e in rx:
        if (e.get("turn") or {}).get("role") == "assistant":
            if e.get("type") == "turn.created":
                open_at = t
            elif e.get("type") == "turn.done" and open_at is not None:
                replies.append((open_at, t))
                open_at = None
    fragments = []
    for t, did, text in replay.submitted:
        # The caller's whole utterance: the user turn that finishes within 2 s of the request.
        whole = next((u for ut, u in user_turns if 0 <= ut - replay.started.get(did, t) <= 2.0), "")
        if len(words(whole)) > len(words(text)) + 2 and set(words(text)) <= set(words(whole)):
            fragments.append({"did": did, "sent": text[:120], "said": whole[:200]})
    finals = {}
    for t, e in replay.sent:
        text = "".join(c.get("text", "") for c in e.get("content", []))
        if text.startswith(FINAL):
            finals.setdefault(" ".join(text[len(FINAL):].split())[:80], []).append((t, e.get("delegation_item_id")))
    duplicates = {k: len(v) for k, v in finals.items() if len(v) > 1}
    mid_reply = [t for v in finals.values() for t, _ in v if any(a < t < b for a, b in replies)]
    per_did = {}
    for v in finals.values():
        for t, did in v:
            per_did[did] = per_did.get(did, 0) + 1
    # Unanswered: no final reached the voice after the request started (a request folded into a running turn is
    # answered by that turn's one final).
    sends = sorted(t for v in finals.values() for t, _ in v)
    unanswered = [did for did, began in replay.started.items() if not any(t >= began for t in sends)]
    return {"delegations": len(replay.started), "fragments": fragments, "finals": len(sends),
            "duplicate_finals": duplicates, "finals_mid_reply": len(mid_reply),
            "max_finals_per_delegation": max(per_did.values(), default=0), "unanswered": unanswered}


def main(argv):
    if argv[:1] == ["fixture"] and len(argv) == 3:
        rows = fixture(Path(argv[1]))
        Path(argv[2]).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        os.chmod(argv[2], 0o600)
        return 0
    if argv[:1] == ["run"] and len(argv) > 1:
        for path in argv[1:]:
            rows = [json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()]
            print(Path(path).name, json.dumps(asyncio.run(Replay(rows).run()), ensure_ascii=False))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
