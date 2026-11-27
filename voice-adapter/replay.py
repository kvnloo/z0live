"""Replay a recorded call through the real adapter and score it against the Dessa contract.

A fixture is the provider side of one call (`rx` events) plus what each delegation cost: Hermes's work time and
answer, and each lane's time and answer. `replay` feeds the events to a real `mobile_live.Call` on the recorded
clock. Hermes is a fake dashboard chat behind the real `chat_continuity.ask_chat` client, over a real WebSocket;
the lanes are fakes behind `tiers.race`. The voice's own replies are the recorded ones, so the replay checks what
the adapter sends and when, not what the voice would have said to it.

A call with a second voice (`src: guest` rows, the GPT-Live guest's session) replays through the real GPTGuest
floor, on the recorded clock: `repeats` are answers sent again after the phone had already heard them, and `lost`
are answers it never heard that were never sent again, or that a voice said into a reply the floor had muted, or
whose rest the phone never heard, and the adapter never handed back, after a barge-in or the provider cut the reply
saying them (`cut`). A fixture keeps when the phone's output was loud, which is what makes a barge-in (mobile_live
GPTGuest.interrupt) a barge-in, and the guest Hermes's answers when the trace has them.
A fixture keeps the call's people count (`floor.people`),
so a call with people at the phone replays on their floor without the chat it opened from.

    replay.py fixture TRACE.jsonl OUT.jsonl   # cut a fixture from a call trace (stays local: it has transcripts)
    replay.py run FIXTURE.jsonl ...           # print the contract report per call

The fake chat follows the dashboard's busy rule (display.busy_input_mode=interrupt): a prompt sent while a turn
runs redirects that turn (receipt status "redirected"), and the turn ends with one message.complete that every
socket listening on the chat receives. A redirected turn ends at max(its end, the new prompt + its own work).
prompt.background answers {task_id} at once and sends background.complete {task_id, text} to the chat's sockets
after that request's own work. Call.sleep runs on the replay clock, so receipts fire when they would on a call.

A report's own_answer_s is request -> first final carrying that request's id (a lane answer or Hermes); held lists
requests with none (folded into another request's turn); receipt_s is request -> spoken receipt. promised lists the
voice's own hand-off lines that promised new work or said it was handled before that request's own answer, queued
notice or refusal reached it (mobile_live.promises_new_work), in the words of it that reached the phone: the replayed
speech is the recorded one, muted where the replayed floor mutes it. A line the floor unmuted before anything reached
the voice promised too when its muted start opened a promise (mobile_live.opens_promise) and the phone heard the rest. unasked lists caller turns that named Tek when Tek's own Hermes was not asked for them.
"""
from __future__ import annotations

import asyncio
import contextvars
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from unittest.mock import patch

from aiohttp import ClientSession, web

FINAL = '"Agent Final Message":'
# The caller addressing Tek by name, as the transcriber hears it (takes 9 and 14 heard "Tech"): the name opens the turn
# or follows a greeting, then a mark or a question word. Kept apart from guest_floor.Addressing on purpose.
TEK = re.compile(r"(?:^|(?:hey|hi|ok|okay)[\s,]+)\W*(?:tek|tech|teck|tec|teknium)\b(?:'s)?(?=\s*[,.!?:—]|\s+(?:what|why|how|can))",
                 re.I)
QUEUED = re.compile(r"queued as number \d+")  # the adapter's queued notice (mobile_live.QUEUED)
_did = contextvars.ContextVar("replay_did", default=None)


def fixture(trace: Path) -> list[dict]:
    """Provider events of both voices, mute toggles and delegation costs from one trace. Hermes's work time excludes
    the wait behind an earlier delegation of the same call (the per-call lock in the api path)."""
    rows = []
    for line in trace.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    out, started, free_at, guest_started = [], {}, 0.0, {}
    for r in rows:
        ev = r.get("ev")
        if ev in ("audio.on", "audio.off") and r.get("dir") in ("output", "input"):
            out.append({"t": r.get("at", r["t"]), r["dir"] + "_loud": ev == "audio.on"})
        elif ev == "guest.delegation.start":
            guest_started[r.get("did")] = r["t"]
        elif ev == "guest.delegation.done" and r.get("answer") and r.get("did") in guest_started:
            out.append({"t": r["t"], "cost": "guest", "work": round(r["t"] - guest_started[r["did"]], 3),
                        "answer": r["answer"]})
        if ev == "rx" and (r.get("src") == "provider" or str(r.get("type", "")).startswith("session.input_audio")):
            event = r["event"]
            if r.get("type") == "session.context.appended":
                continue
            out.append({"t": r["t"], "rx": event, "src": r.get("src") or "mobile"})
        elif ev == "guest.rx" and r.get("type") != "session.context.appended":
            out.append({"t": r["t"], "rx": r["event"], "src": "guest"})  # the second voice's own session
        elif ev == "delegation.start":
            started[r["did"]] = r["t"]
        elif ev in ("delegation.done", "delegation.failed") and r.get("did") in started:
            began = max(started[r["did"]], free_at)
            free_at = r["t"]
            out.append({"t": r["t"], "cost": "hermes", "did": r["did"], "work": round(r["t"] - began, 3),
                        "answer": r.get("answer") or ""})
        elif ev == "floor.people":
            out.append({"t": r["t"], "people": r["people"]})  # the call's people count (from the chat it opened on)
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
        self.side: list[tuple[float, str, str]] = []  # (ends, task_id, answer) of running background agents

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
                elif method == "prompt.background":
                    # A fresh side agent beside the chat's turn: {task_id} at once, then background.complete
                    # {task_id, text} on the chat's sockets once its own work is done (methods_prompt.py).
                    did = next((d for d, text in reversed(self.replay.requests) if text in params["text"]), None)
                    self.replay.submitted.append((self.replay.now, did, params["text"]))
                    self.replay.backgrounded.append(did)
                    work, answer = self.replay.hermes.get(did, (5.0, f"answer for {did}"))
                    task_id = f"bg_{len(self.replay.backgrounded)}"
                    self.side.append((self.replay.now + work, task_id, answer))
                    await ws.send_json({"id": rid, "result": {"task_id": task_id}})
        finally:
            self.sockets.discard(ws)
        return ws

    def next_due(self, t):
        ends = [e for e, _, _ in self.side] + ([self.turn.ends] if self.turn else [])
        return min((e for e in ends if e <= t), default=None)

    async def tick(self, now):
        for side in [s for s in self.side if s[0] <= now]:
            self.side.remove(side)
            frame = {"method": "event", "params": {"type": "background.complete", "session_id": "chat-1",
                                                   "payload": {"task_id": side[1], "text": side[2]}}}
            for ws in list(self.sockets):
                await ws.send_json(frame)
        if self.turn and self.turn.ends <= now:
            turn, self.turn = self.turn, None
            frame = {"method": "event", "params": {"type": "message.complete", "session_id": "chat-1",
                                                   "payload": {"text": turn.answer}}}
            for ws in list(self.sockets):
                await ws.send_json(frame)

    async def close(self):
        await self.runner.cleanup()


class Replay:
    def __init__(self, rows: list[dict], history: list | None = None, conductor: Path | None = None):
        self.rows = sorted(rows, key=lambda r: r["t"])
        # The TV conductor's state.json the call watches for lane cards: never the owner's own (a missing file is
        # no conductor). `conductor` rows ({"t", "conductor": state}) write it at their time, with the call's id.
        self.conductor = conductor
        self.cards: list[tuple[float, str, dict]] = []  # the adapter's lanes.card_* trace events
        people = max((r["people"] for r in self.rows if "people" in r), default=1)
        if history is None and people > 1:  # a fixture cut from a trace: the chat said how many share the phone
            history = [{"role": "assistant", "content": [{"type": "output_text",
                                                           "text": f"This call has {people} people at the desk."}]}]
        self.history = history  # the chat the call opened from, as the phone sends it (Call.start)
        self.now = 0.0
        self.hermes = {r["did"]: (r["work"], r["answer"]) for r in self.rows if r.get("cost") == "hermes"}
        self.guest_answers = [(r["work"], r["answer"]) for r in self.rows if r.get("cost") == "guest"]  # in order
        self.loud = False  # the phone's output is loud (the recorded audio.on/off spans)
        self.loud_at: list[float] = []  # when the adapter saw the output loud
        self.mic = False  # the caller's mic is loud (input spans: mic quiet ends an utterance)
        self.extra: list[dict] = []  # provider events fed mid-call (floor_sim's voices), judged with the rows
        self.call = None
        self.floor: list[tuple[float, str, dict]] = []  # the adapter's floor.* trace events
        self.lanes = {}
        began = {}
        for r in self.rows:
            if r.get("cost") == "lane":
                self.lanes.setdefault(r["did"], {})[r["tier"]] = (r["s"] or 0.0, r["text"])
            if r.get("cost") in ("hermes", "lane"):
                at = r["t"] - ((r["work"] if r["cost"] == "hermes" else r["s"]) or 0.0)
                began[r["did"]] = min(began.get(r["did"], at), at)
        # A delegation the adapter opened itself (people on the call) has a fresh id on each replay: the n-th one
        # stands for the n-th recorded one, with its lanes and Hermes answer (take 15 replayed "answer for ...").
        self.unclaimed = [did for did in sorted(began, key=began.get) if did.startswith("z0live-")]
        self.submitted: list[tuple[float, str, str]] = []  # (t, did, text) sent to Hermes
        self.sent: list[tuple[float, dict]] = []  # events sent to the voice provider
        self.sent_guest: list[tuple[float, dict]] = []  # events sent to the guest voice's session
        self.heard: list[tuple[float, str]] = []  # voice transcript the phone received (audible replies only)
        self.heard_from: list[str] = []  # the voice each of `heard` came from ("primary" or "guest")
        self.leg = "primary"  # the voice whose event the adapter is handling
        self.guest = any(r.get("src") == "guest" for r in self.rows)
        self.started: dict[str, float] = {}
        self.requests: list[tuple[str, str]] = []  # (did, request) as host_delegate received them
        self.waiting: list[tuple[float, asyncio.Future, object]] = []
        self.backgrounded: list[str] = []  # delegations Hermes ran as background agents beside the chat's turn
        self.guest_asked: list[tuple[float, str]] = []  # (t, request) the guest voice's own Hermes received

    def later(self, seconds, value):
        future = asyncio.get_running_loop().create_future()
        self.waiting.append((self.now + seconds, future, value))
        return future

    def call_later(self, seconds, callback):
        """loop.call_later on the replay clock (GPTGuest's flush timer)."""
        future = self.later(seconds, None)
        future.add_done_callback(lambda f: f.cancelled() or callback())
        return future  # .cancel() as on a TimerHandle

    async def pump(self, n=20):
        for _ in range(n):
            await asyncio.sleep(0)

    async def advance(self, t, chat):
        while True:
            self.waiting = [w for w in self.waiting if not w[1].cancelled()]
            due = sorted((w for w in self.waiting if w[0] <= t), key=lambda w: w[0])
            turn_end = chat.next_due(t)
            if not due and turn_end is None:
                break
            at = min([w[0] for w in due] + ([turn_end] if turn_end is not None else []))
            self.now = at
            self.keep_loud()
            for w in [w for w in due if w[0] == at]:
                self.waiting.remove(w)
                if not w[1].done():
                    w[1].set_result(w[2])
            await chat.tick(at)
            await self.pump()
            await asyncio.sleep(0.005)  # socket round trips
            await self.pump()
        self.now = t
        self.keep_loud()

    @property
    def events(self) -> list[dict]:
        return sorted(self.rows + self.extra, key=lambda r: r["t"]) if self.extra else self.rows

    def react(self, leg, event):
        """An event the adapter sent to a voice's session. The recorded voice never answers it (floor_sim's does)."""

    def after(self, row):
        """A row has been fed to the adapter."""

    def keep_loud(self):
        if self.loud and self.call:
            self.call.output_loud_at = self.now
            self.loud_at.append(self.now)  # the phone's output was loud (said_once: what the phone was hearing)
        if self.mic and self.call:
            self.call.input_loud_at = self.now

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
            if did not in self.hermes and did not in self.lanes and did.startswith("z0live-") and self.unclaimed:
                recorded = self.unclaimed.pop(0)
                if recorded in self.hermes:
                    self.hermes[did] = self.hermes[recorded]
                if recorded in self.lanes:
                    self.lanes[did] = self.lanes[recorded]
            _did.set(did)
            self.started[did] = self.now
            self.requests.append((did, request))
            await original(call, did, request)

        async def ask_guest_host(http, profile, session, request, *_, **__):
            replay.guest_asked.append((replay.now, request))
            recorded = replay.guest_answers[len(replay.guest_asked) - 1:][:1]
            work, answer = recorded[0] if recorded else (5.0, f"guest answer for {request[:40]}")
            return await replay.later(work, answer)

        trace = live.Trace.__call__
        guest_message = live.GPTGuest.message

        def from_guest(guest, raw):  # what the adapter sends while handling the guest's event is the guest's
            replay.leg = "guest"  # not a ContextVar: a timer set meanwhile (floor_sim's voices) would inherit it
            try:
                return guest_message(guest, raw)
            finally:
                replay.leg = "primary"

        def floor_trace(tr, ev, **data):
            if ev.startswith("floor."):
                replay.floor.append((replay.now, ev, data))
            if ev.startswith("lanes.card"):
                replay.cards.append((replay.now, ev, data))
            return trace(tr, ev, **data)

        class Clock:  # mobile_live's view of time is the recorded clock; the asyncio loop keeps real time
            def __getattr__(self, name):
                return getattr(time, name)

            def monotonic(self):
                return replay.now

        cfg = {"fast": [{"provider": "p", "model": "f"}], "medium": [{"provider": "p", "model": "m"}],
               "slow": [{"provider": "p", "model": "s"}]}
        guest = live.guest_voice.GuestConfig("gpt-live", "tek", "ember", "tek") if self.guest else None
        with tempfile.TemporaryDirectory() as traces, \
                patch.object(tiers, "CONDUCTOR_STATE", self.conductor or Path(traces) / "no-conductor" / "state.json"), \
                patch.object(live, "TRACE_DIR", Path(traces)), patch.object(live, "TIERS_ON", True), \
                patch.object(live.chat_continuity, "WS", url), patch.object(tiers, "race", race), \
                patch.object(tiers, "load", return_value=cfg), patch.object(live.Call, "host_delegate", host_delegate), \
                patch.object(live.guest_voice, "configured", return_value=guest), patch.object(live, "time", Clock()), \
                patch.object(live, "ask_guest_host", ask_guest_host), patch.object(live.Trace, "__call__", floor_trace), \
                patch.object(live.GPTGuest, "message", from_guest), \
                patch.object(live.GPTGuest, "later", lambda _, seconds, callback: self.call_later(seconds, callback)), \
                patch.object(live.Call, "sleep", lambda call, seconds: replay.later(seconds, None), create=True):
            http = ClientSession()
            call = self.call = live.Call(live.Manager(http), profile="")
            call.relay, call.chat, call.heard_user = None, "chat-1", True
            call.subscription.ready.set()
            if self.history is not None:
                call.read_history(self.history)
            def send(event, to_provider=False, **_):
                if to_provider:
                    self.sent.append((self.now, event))
                    self.react("primary", event)
                elif event.get("type") == "session.output_transcript.delta":
                    self.heard.append((self.now, event.get("delta") or ""))
                    self.heard_from.append(self.leg)

            call.send = send
            if isinstance(call.guest, live.GPTGuest):
                call.guest.send = lambda event: (self.sent_guest.append((self.now, event)), self.react("guest", event))
            for row in self.rows:
                await self.advance(row["t"], chat)
                if "output_loud" in row:
                    self.loud = row["output_loud"]
                    call.output_loud_at = self.now
                elif "input_loud" in row:
                    self.mic = row["input_loud"]
                    call.input_loud_at = self.now
                elif "rx" in row and row["src"] == "guest":
                    if call.guest:
                        call.guest.message(json.dumps(row["rx"]))
                    await self.pump()
                elif "rx" in row:
                    call.message(json.dumps(row["rx"]), row["src"] == "provider")
                    await self.pump()
                elif "conductor" in row and self.conductor:
                    state = json.loads(json.dumps(row["conductor"]).replace("{call}", live.lane_cards.call_key(call.id)))
                    self.conductor.write_text(json.dumps(state))
                self.after(row)
            await self.advance(self.rows[-1]["t"] + 900, chat)
            self.counts = dict(call.counts)
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
    rx = [(r["t"], r["rx"]) for r in replay.events if "rx" in r]
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
    import mobile_live
    receipt = getattr(mobile_live, "RECEIPT", None)
    finals, receipts, own, queued, answers = {}, {}, {}, {}, {}
    for t, e in replay.sent:
        text = "".join(c.get("text", "") for c in e.get("content", []))
        did = e.get("delegation_item_id")
        if text.startswith(FINAL) and receipt and receipt in text:
            receipts.setdefault(did, t)  # a receipt says the work runs; it is not an answer
            continue
        if text.startswith(FINAL) and QUEUED.search(text):
            queued.setdefault(did, t)  # a queued notice says the work has not started; it is not an answer
            continue
        if text.startswith(FINAL) and did in replay.started:
            own.setdefault(did, t)
            answers.setdefault(did, " ".join(text[len(FINAL):].split()))
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
    # Request -> first final the voice got after it (a lane answer or Hermes), in seconds.
    answer_s = sorted(round(min(t for t in sends if t >= began) - began, 3)
                      for began in replay.started.values() if any(t >= began for t in sends))
    repeats, lost = said_once(replay)
    # Own answer: the first final carrying this request's id. A request folded into another request's turn has none
    # ("held"): it hears that turn's answer whenever that turn ends.
    own_s = {did: round(own[did] - began, 3) for did, began in replay.started.items() if did in own}
    # Promised: the voice's own hand-off line for a request Hermes runs promised new work or said it was handled,
    # before that request's answer, queued notice or refusal reached the voice (takes 7 and 9).
    created = {}
    for t, e in rx:
        if e.get("type") == "delegation.created":
            created.setdefault((e.get("item") or {}).get("id"), t)
    # What each of the voice's replies said that reached the phone: a line the floor muted promised nothing to anyone
    # (take 16's "Okay, I'll take that" went into a muted reply).
    # A reply the floor unmuted is heard from there on, but it opened muted: take 19's hand-off ack said " I'll"
    # muted, and the phone heard "get the full picture". What it said by its unmute is what the heard rest promises.
    heard = [(t, x) for (t, x), by in zip(replay.heard, replay.heard_from) if by == "primary"]
    unmuted = {d.get("turn"): t for t, ev, d in replay.floor
               if ev == "floor.reply_unmuted" and d.get("speaker") == "primary"}
    handed = sorted(t for t, e in replay.sent
                    if "".join(c.get("text", "") for c in e.get("content", [])).startswith(FINAL))
    said, open_at, line = [], None, []
    for r in replay.events:
        e, turn_ = r.get("rx") or {}, (r.get("rx") or {}).get("turn") or {}
        if r.get("src") != "provider":
            continue
        if e.get("type") == "output_transcript.added":
            line.append((r["t"], (e.get("item") or {}).get("text") or ""))
        if turn_.get("role") != "assistant":
            continue
        if e.get("type") == "turn.created":
            open_at, line = r["t"], []
        elif e.get("type") == "turn.done":
            at = r["t"] if open_at is None else open_at
            said.append((at, [(t, x) for t, x in heard if at <= t <= r["t"]], line, unmuted.get(turn_.get("id"))))
            open_at, line = None, []

    def promises(heard_, line, unmuted_at, began):
        if mobile_live.promises_new_work("".join(x for _, x in heard_)):
            return True
        # Unmuted before anything reached the voice for this request, with a promise begun muted, and heard.
        answered = next((t for t in handed if t >= began), float("inf"))
        return (unmuted_at is not None and unmuted_at < answered
                and mobile_live.opens_promise("".join(x for t, x in line if t <= unmuted_at))
                and bool("".join(x for t, x in heard_ if t < answered).strip()))

    # A run the adapter started on its own (people on the call) has no delegation of the provider's: its start.
    promised = [{"did": did, "said": "".join(x for _, x in heard_)[:160]} for did, began in replay.started.items()
                for at, heard_, line, unmuted_at in said
                if created.get(did, began) - 1.5 <= at < own.get(did, float("inf"))
                and promises(heard_, line, unmuted_at, began)]
    # Unasked: the caller named Tek (as the transcriber hears it: "Hey, Tech, ...", takes 9 and 14) and Tek's own
    # Hermes was not asked between the turn's first words and 15 s after it closed.
    unasked, seen = [], {}
    for r in replay.events:
        e, turn_ = r.get("rx") or {}, (r.get("rx") or {}).get("turn") or {}
        key = (r.get("src"), turn_.get("id") or e.get("turn_id"))
        seen.setdefault(key, r["t"])  # a leg can close a turn 10 s after its words (take 9)
        text = turn_.get("transcript") or ""
        if e.get("type") == "turn.done" and turn_.get("role") == "user" and TEK.search(text):
            began = seen[key]
            if (not any(began <= at <= r["t"] + 15 for at, _ in replay.guest_asked)
                    and not any(abs(began - u["t"]) < 5 for u in unasked)):  # both legs hear the same words
                unasked.append({"t": began, "said": text[:120]})
    return {"delegations": len(replay.started), "answer_s": answer_s, "own_answer_s": own_s,
            "held": [did for did in replay.started if did not in own],
            "receipt_s": {did: round(t - replay.started[did], 3) for did, t in receipts.items() if did in replay.started},
            "queued_s": {did: round(t - replay.started[did], 3) for did, t in queued.items() if did in replay.started},
            "answers": answers,  # did -> the first answer the voice got for it (its own words, not a status line)
            "backgrounded": len(replay.backgrounded), "counts": getattr(replay, "counts", {}), "fragments": fragments, "finals": len(sends),
            "duplicate_finals": duplicates, "finals_mid_reply": len(mid_reply),
            "max_finals_per_delegation": max(per_did.values(), default=0), "unanswered": unanswered,
            "repeats": repeats, "lost": lost, "promised": promised, "unasked": unasked}


REACH_SECONDS = 5.0  # a request to a bot reaches its Hermes this soon after the caller's line ends (check 2)


def checks(replay: Replay) -> dict[str, list]:
    """Film call 1's five checks, judged on what the phone got (replay.heard) and on which Hermes runs started, for
    a fixture with `line` rows (bench/take_fixture.py). Each value lists what broke the check; [] passes.
      quiet    lines 0-1 and 5 (the people talking to each other) get no bot speech: nothing reaches the phone
               from line 0 until line 2, and after line 5 only the words of answers the caller asked for
      whole    every answer is heard in full, or its unheard rest is handed back (report lost)
      promised no hand-off line promises work before Hermes says what happened (report promised)
      reached  each line to a bot reaches that bot's Hermes within REACH_SECONDS of the line's end
      runs     no Hermes run for words nobody said to a bot: a run is what can write to a board"""
    from answer_delivery import RESUME_PREFIX, SAY_EXACTLY, content

    lines = [r for r in replay.events if "line" in r]
    by = {r["line"]: r for r in lines}
    report_ = report(replay)
    out = {"quiet": [], "whole": report_["lost"], "promised": report_["promised"], "reached": [], "runs": []}
    if 2 in by:
        early = "".join(text for t, text in replay.heard if by[0]["t"] <= t < by[2]["t"]).strip()
        if early:
            out["quiet"].append({"line": "0-1", "heard": early[:120]})
    if 5 in by:
        handed = content(RESUME_PREFIX + SAY_EXACTLY).union(*(content("".join(c.get("text", "") for c in e.get("content", [])))
                                                    for _, e in replay.sent + replay.sent_guest))
        # Each reply begun after line 5 starts: a draft to it says words of its own; a reply reading an answer the
        # caller asked for says that answer's words (a third or more of what it says) along with its own.
        opened, replies = {}, []
        for r in replay.events:
            e = r.get("rx") or {}
            if (e.get("turn") or {}).get("role") == "assistant" and e.get("type") in ("turn.created", "turn.done"):
                if e["type"] == "turn.created":
                    opened[r["src"]] = r["t"]
                elif r["src"] in opened:
                    replies.append((opened.pop(r["src"]), r["t"]))
        replies += [(t, float("inf")) for t in opened.values()]
        before = set().union(*(content(x) for t, x in replay.heard if t < by[5]["t"]))
        for a, b in replies:
            said = set().union(*(content(x) for t, x in replay.heard if a <= t <= b + 1.0)) - before
            if a >= by[5]["t"] and len(said - handed) > 2 and 3 * len(said & handed) < len(said):
                out["quiet"].append({"line": 5, "heard": sorted(said - handed)[:12]})
    runs = {"hermes": sorted(replay.started.values()), "tek": [t for t, _ in replay.guest_asked]}
    for line in lines:
        if line["to"] in runs and not any(line["t"] <= t <= line["end"] + REACH_SECONDS for t in runs[line["to"]]):
            out["reached"].append({"line": line["line"], "to": line["to"]})
    for bot, starts in runs.items():
        for t in starts:
            if not any(line["to"] == bot and line["t"] <= t <= line["end"] + 2 * REACH_SECONDS for line in lines):
                out["runs"].append({"bot": bot, "t": round(t, 3)})
    return out


def said_once(replay: Replay) -> tuple[list[dict], list[dict]]:
    """Per voice: answers handed again after the recorded voice had already said them (repeats), and answers it
    never said that were never handed again (lost). "Said" is answer_delivery's measure, on the transcript the phone
    received: enough of a piece's words that the call had not heard before it was sent."""
    from answer_delivery import MAX_RETELLS, RESUME_PREFIX, SAY_EXACTLY, anchors, content, needed, sentences, spoken
    import mobile_live

    repeats, lost = [], []
    speech = replay.heard  # what reached the phone from either voice (a muted reply never does)
    rx = [(r["t"], r["rx"], r["src"]) for r in replay.events if "rx" in r]
    for leg, sent in (("primary", replay.sent), ("guest", replay.sent_guest)):
        src = "guest" if leg == "guest" else "provider"
        # What this voice said that reached the phone: what its own answers are judged by (the ledger hears one leg).
        own = [x for x, by in zip(speech, replay.heard_from) if by == leg]
        replies = []  # (opened, done) of this voice's replies
        for t, e, s in rx:
            if s == src and (e.get("turn") or {}).get("role") == "assistant":
                if e.get("type") == "turn.created":
                    replies.append([t, float("inf")])
                elif e.get("type") == "turn.done" and replies:
                    replies[-1][1] = t

        def playing(t):  # the reply the phone was hearing at t: open, or its output not yet quiet (GPTGuest.playing)
            loud = max((at for at in replay.loud_at if at <= t), default=float("-inf"))
            return next((opened for opened, done in reversed(replies)
                         if opened <= t and t - max(done, loud) < mobile_live.LEVEL_OFF_SECONDS), None)

        # did -> [(t, text, late)]: late when handed while one of this voice's replies was open (answer_delivery.Piece).
        # The resume cue ("To finish what I was saying:") is the adapter's, not the answer's: the voice may drop it.
        # A hand-off is the delegation's the floor traced it for (`of`): the voice's id is None for the adapter's own.
        # A lane guess or status line (`lane` on floor.answer_sent) is superseded by Hermes's own answer: a reply that
        # ran its course never owes its sentences.
        handings: dict[str | None, list[tuple[float, str, bool]]] = {}
        lanes: set[tuple[str | None, float]] = set()
        owners = [(t, data["of"], bool(data.get("lane"))) for t, ev, data in replay.floor
                  if ev in ("floor.answer_sent", "floor.answer_resent") and data.get("speaker") == leg and "of" in data]
        last = None
        for t, e in sent:
            if e.get("channel") != "speakable":
                continue
            did, text = e.get("delegation_item_id"), "".join(c.get("text", "") for c in e.get("content", []))
            lane = False
            if text.startswith(FINAL) or last is None or last[1] != t:
                at = next((i for i, (ot, _, _) in enumerate(owners) if ot >= t - 1e-6), None)
                if at is not None and owners[at][0] <= t + 1e-6:
                    _, did, lane = owners.pop(at)
            elif last is not None:
                did = last[0]
            if text.startswith(FINAL) or last is None or last != (did, t):
                body = text[len(FINAL):] if text.startswith(FINAL) else text
                late = any(opened < t < done for opened, done in replies)
                body = body.strip().removeprefix(SAY_EXACTLY).removeprefix(RESUME_PREFIX)
                handings.setdefault(did, []).append((t, body, late))
                if lane:
                    lanes.add((did, t))
            else:  # the next 500-byte piece of the same hand-off
                at, prior, late = handings[did][-1]
                handings[did][-1] = (at, prior + text, late)
            last = (did, t)

        def said(words, since, until):
            heard = set().union(*(content(x) for t, x in speech if since <= t <= until))
            return not words or len(words & heard) >= needed(len(words))

        def heard_before(t):
            return set().union(*(content(x) for at, x in speech if at < t))

        def heard_between(since, until):
            return set().union(*(content(x) for at, x in speech if since <= at <= until))

        pieces_of: dict[str | None, list[dict]] = {}
        for did, items in handings.items():
            pieces = pieces_of[did] = []  # each distinct piece of this answer: first sent, its new words, handed again
            for t, text, late in items:
                words = content(text)
                again = [p for p in pieces if p["all"] <= words]  # a resend carries whole pieces
                if again and words <= set().union(*(p["all"] for p in pieces)):  # a resend of earlier pieces
                    if any(ledger_entry([(p["t"], p["text"], p["late"])], own, t).pieces[0].spoken for p in again):
                        repeats.append({"leg": leg, "did": did, "t": t})
                    for p in again:
                        p["resent"] = True
                else:
                    pieces.append({"t": t, "text": text, "late": late, "all": words, "words": words - heard_before(t),
                                   "resent": False})
        # What this leg's voice said (its recorded transcript), heard or not. A piece it said that the phone never
        # heard went into a muted reply: lost however often it was sent (take 12: line 3's answer was resent, read
        # into a reply the floor had muted, then dropped as unspoken). A resend the recording never had is judged
        # only by the phone: the recorded voice never had the chance to answer it.
        voice = [(t, (e.get("item") or {}).get("text", "")) for t, e, s in rx
                 if s == src and e.get("type") == "output_transcript.added"]

        def voiced(words, since):
            spoken = set().union(*(content(x) for t, x in voice if t >= since))
            return bool(words) and len(words & spoken) >= needed(len(words))

        # Playback stopped under a reply: the provider ended it because the caller started talking over it (take
        # 14), the adapter cut it (floor.barge_in), or the floor left the leg. What the phone had not heard by then of
        # each answer that reply was saying (answer_delivery's rest), or the whole of an answer handed into that reply
        # that it never began (take 15, line 3), is owed. This is judged from the phone's side, not from the adapter's
        # own floor.answer_cut, floor.answer_spoken (take 14 credited line 3 at 83.86 s and lost its tail at 93.95 s)
        # or floor.answer_resumed (take 15 resumed line 3 from its third paragraph).
        barged = talked_over(rx, src) + [  # the caller talked over the reply
            (playing(t), t) for t, ev, data in replay.floor if playing(t) is not None
            and ev == "floor.barge_in" and data.get("speaker") == leg and data.get("by") != "provider"]
        left = [(playing(t), t) for t, ev, data in replay.floor  # the floor moved off it
                if playing(t) is not None and ev == "floor.changed" and data.get("previous") == leg]
        stops = []  # a reply stops once: take 17's provider ended line 3's at 66.857, and floor.barge_in came at 67.22
        for opened, t in sorted(barged + left, key=lambda x: x[1]):
            if all(o != opened for o, _ in stops):
                stops.append((opened, t))
        # A reply that ran its course owes the paragraphs of an answer it began and left out, each naming something
        # it never said (answer_delivery.Entry.skipped). Take 16: the resumed line-3 rest was read as paragraph 2 and
        # a paraphrase of paragraph 3; paragraph 1 and card t_0a96e2de were never said, yet floor.answer_spoken
        # credited the answer as the reply began.
        # A reply the caller talked over, or the floor left before it ended, is only cut: it never reached what it
        # did not say. One whose last words were draining when the floor moved ran its course too (take 16: the
        # floor went to Tek at its turn.done, 121.166 s).
        early = {opened for opened, _ in barged} | {opened for opened, t in left
                                                    if any(o == opened and t < done for o, done in replies)}
        ends = sorted([(opened, t, True) for opened, t in stops]
                      + [(opened, done, False) for opened, done in replies if done < float("inf") and opened not in early],
                      key=lambda x: x[1])
        receipt = getattr(mobile_live, "RECEIPT", None)
        owed: dict[str | None, list[tuple[float, str]]] = {}
        retold: dict[str | None, int] = {}  # times each answer's sentences naming something were owed (Ledger.retold)
        for opened, t, cut in ends:
            # An answer was being said when the reply said two new words of it that no answer it said more of
            # covers (takes 7 and 13: an earlier answer shared its words with the one being said).
            fresh = heard_between(opened, t) - heard_before(opened)
            hits = {did: set().union(*(p["all"] for p in pieces)) & fresh for did, pieces in pieces_of.items()
                    if any(p["t"] < t for p in pieces)}
            for did, mine in hits.items():
                more = set().union(*(theirs for theirs in hits.values() if len(theirs) > len(mine)))
                items = [(at, x, late) for at, x, late in handings[did] if at < t]
                entry = ledger_entry(items, own, t, {at for d, at in lanes if d == did})
                if entry.started and len(mine - more) >= 2:
                    # A cut owes what follows the last sentence said, and, like a reply that ran its course, every
                    # sentence naming something it never said (take 17: "I won't start another one" said sentence 3
                    # of line 3's answer, and sentence 2, card t_0a96e2de, was jumped).
                    # Those it jumped, or skipped by running its course, are owed MAX_RETELLS times per answer: take
                    # 18's voice dropped card t_0a96e2de from the reply and again from the verbatim hand-back, so the
                    # reply to that hand-back owes it once more.
                    after = entry.rest() if cut else ""
                    named = [] if retold.get(did, 0) >= MAX_RETELLS else [
                        x for x in sentences(entry.skipped()) if x not in sentences(after)
                        and (cut or not retold.get(did) or entry.unheard(x))]  # retold: only what names
                    if named:
                        retold[did] = retold.get(did, 0) + 1
                    rest = " ".join([after] + named).strip()
                elif not cut:
                    continue  # never begun by a reply that ran its course: still pending, judged below
                else:  # handed into this reply and never begun: all of it (its latest words: a receipt is overtaken)
                    into = [x for at, x, late in items if late and opened <= at and not (receipt and receipt in x)
                            and content(x) - heard_before(at)]  # with no new words it has nothing left to hear
                    rest = into[-1] if into else ""
                if rest and not (receipt and rest in receipt):  # a status line's rest: the answer overtakes it
                    owed.setdefault(did, []).append((t, rest))
        for t, ev, data in sorted(replay.floor, key=lambda x: x[0]):
            if (data.get("speaker") == leg and ev == "floor.answer_dropped" and data.get("reason") == "new_request"
                    and data.get("did") in owed):  # the caller moved on: the adapter chose not to finish it
                owed[data["did"]] = [(at, rest) for at, rest in owed[data["did"]] if at > t]
        for did, rests in owed.items():
            # Each owed sentence must reach the phone, or be handed to the voice again (the recorded voice never had
            # that hand-back to say: a replay judges that hand-back by its own pieces).
            # A sentence that names something (anchors) is said only once its names are: other words of it can come
            # from elsewhere (take 17: "install", "card" and "phone" of the jumped sentence 2 were said by others).
            for t, rest in rests:
                later = [x for items in handings.values() for at, x, _ in items if at > t]
                heard, handed = heard_between(t, float("inf")), set().union(*map(content, later))
                voiced_after = spoken(" ".join(x for at, x in speech if at >= t))  # an id spelled across pieces
                handed_names = set().union(*map(spoken, later))

                def owes(s, own):
                    names = anchors(s)
                    if names:
                        return not all(a <= voiced_after for a in names) and not all(a <= handed_names for a in names)
                    return len(own & heard) < needed(len(own)) and len(own & handed) < needed(len(own))

                unsaid = [s for s in sentences(rest) if (own := content(s)) and owes(s, own)]
                if unsaid:
                    lost.append({"leg": leg, "did": did, "t": t, "cut": True, "unsaid": unsaid[0][:80]})
                    break
        owed = {did: rests for did, rests in owed.items() if rests}
        for did, items in handings.items():
            for i, p in enumerate(pieces_of[did]):
                if did in owed or said(p["words"], p["t"], float("inf")):  # a stopped answer is judged by its rest
                    continue
                if any(said(q["words"], q["t"], float("inf")) for q in pieces_of[did][i + 1:]):
                    continue  # a receipt the answer overtook (take 9's "Still working on that in Hermes")
                if not p["resent"] and not any(t >= p["t"] for t, _ in voice):
                    continue  # sent after the recorded voice's last words: the recording cannot say (floor_sim can)
                if not p["resent"]:
                    lost.append({"leg": leg, "did": did, "t": p["t"]})
                elif voiced(p["words"], p["t"]):
                    lost.append({"leg": leg, "did": did, "t": p["t"], "muted": True})
    return repeats, lost


def ledger_entry(items: list[tuple[float, str, bool]], speech: list[tuple[float, str]], t: float,
                 lanes: set[float] = frozenset()):
    """The adapter's ledger entry for this answer at a stop at t (answer_delivery.Entry), from what the phone heard
    since it was first handed over. `lanes`: when its lane guesses were handed."""
    from answer_delivery import Entry, Piece, content, spoken

    entry = Entry("", None, 0, [Piece(text, frozenset(content(text) - set().union(*(content(x) for t_, x in speech
                                                                                       if t_ < at))), at in lanes, late)
                                for at, text, late in items])
    if entry.pieces:
        entry.said = set().union(*(content(x) for at, x in speech if items[0][0] <= at <= t))
        entry.spoken = spoken(" ".join(x for at, x in speech if items[0][0] <= at <= t))
        for piece in entry.pieces:
            piece.hear(entry.said)
    return entry


def talked_over(rx: list, src: str) -> list[tuple[float, float]]:
    """(opened, done) of this voice's replies that the provider ended while the caller had started talking: a caller
    turn on the same session began inside the reply, on the provider's own clock (take 14, line 5 at 93.95)."""
    starts = [(e.get("turn") or {}).get("start_ms") for _, e, s in rx if s == src
              and e.get("type") in ("turn.created", "turn.done") and (e.get("turn") or {}).get("role") == "user"]
    out, opened = [], None
    for t, e, s in rx:
        turn = e.get("turn") or {}
        if s != src or turn.get("role") != "assistant":
            continue
        if e.get("type") == "turn.created":
            opened = t
        elif e.get("type") == "turn.done" and opened is not None:
            begin, end = turn.get("start_ms"), turn.get("end_ms")
            if begin is not None and end is not None and any(u is not None and begin < u < end for u in starts):
                out.append((opened, t))
            opened = None
    return out


def main(argv):
    if argv[:1] == ["fixture"] and len(argv) == 3:
        rows = fixture(Path(argv[1]))
        Path(argv[2]).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows))
        os.chmod(argv[2], 0o600)
        return 0
    if argv[:1] in (["run"], ["check"]) and len(argv) > 1:
        for path in argv[1:]:
            replayed = Replay([json.loads(line) for line in Path(path).read_text().splitlines() if line.strip()])
            result = asyncio.run(replayed.run())
            print(Path(path).name, json.dumps(checks(replayed) if argv[0] == "check" else result, ensure_ascii=False))
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
