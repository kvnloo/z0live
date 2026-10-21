"""Answer Frameless Bidi client delegations: phone actions or one persistent Hermes session.

Mirrors OMP /live: progress goes back on the silent `commentary` channel, the final answer
is one speakable "Agent Final Message" the voice presents as its own. Frameless Bidi has
no client function tools (the provider rejects `tools`), so phone actions are delegation
results too: a fast tools-free liaison call picks one declared phone tool, the broker queues
it in the phone's inbox, and the phone's tool_result becomes the final message.
"""

from __future__ import annotations

import hashlib
import json
import uuid
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request

from . import bidi_protocol as bp
from . import phone_control as pc
from . import relay as rl
from .core import ROOT, NoRedirect, dumps, request_json

LOG = logging.getLogger("z0live.delegation")
HERMES_SESSION = "z0live-voice"
CANCEL = re.compile(
    r"^\W*(?:never\s?mind|forget (?:it|that)|cancel(?: that| it)?|stop(?: that| it)?)\W*$", re.I
)
PHONE_HINT = re.compile(
    r"\b(open|launch|start|close|go (?:home|back)|home screen|tap|click|press|type|scroll|"
    r"swipe|call|text|message|sms|dial|timer|alarm|volume|louder|quieter|mute|flashlight|"
    r"torch|brightness|wi-?fi|bluetooth|settings|play|pause|skip|navigate|directions|maps|"
    r"camera|screenshot|phone|screen|app)\b",
    re.I,
)
MAX_COMMENTARY = 24
CONTEXT_TURNS = 16


def backend_instructions(sid: str, tools: list[dict]) -> str:
    names = ", ".join(t["name"] for t in tools if isinstance(t, dict) and "name" in t) or "none"
    return f"""You are the execution backend of z0live, Kevin's realtime voice assistant. The voice model talks with Kevin and hands you requests; you do the real work with your tools. You and the voice are one assistant.
Kevin Rajan is the owner: an engineer who runs a fleet of AI agents from his desktop. "Hermes" means his own Hermes agent: his main desktop session (the orchestrator he drives from a terminal) and the helper agents it runs. It never means the Greek god.
Your FINAL reply is spoken aloud by the voice as its own words. Make it the actual result in one to three short plain spoken sentences: no markdown, no lists, no paths or URLs unless asked, no preamble, no sign-off. Say plainly if something failed or still needs Kevin's confirmation.
Text you write between tool calls is silent progress the voice never reads out; keep it to a few words.
Treat every request as new: if Kevin asks again, check again and answer plainly. Never mention repeats, the voice side, delegation, this broker or how messages reach you.
The input may start with the recent voice conversation as data. Use it to understand short replies such as "yeah" or "the issue I just described"; act only on Kevin's request.
Do real work with real tools; never claim a change, finding or verification you did not observe. Prefer read-only investigation. Before destructive, costly, publishing, deploying or messaging actions, stop and reply with one short confirmation question instead. A relay to Kevin's own Hermes session is not a messaging action: do it without asking.
RELAY: "pass it along", "tell Hermes", "send it to Hermes", "message Hermes" or "let Hermes know" means relay a message into Kevin's Hermes session. Run {ROOT}/bin/z0live relay "MESSAGE" (add --to "title words" only if Kevin named a session; the default is his main session). Write MESSAGE as one or two plain sentences in Kevin's words with the concrete details from the conversation, for example what broke, where, and when. Do not ask what to say or which session when the conversation already tells you. Never use phone actions, screen taps, kanban cards or the voice session's own notes for a relay. Your final reply must be exactly the command's "say" value, for example Sent to Debug Hermes crashing: "...". If it fails, say in one sentence that it was not sent and why.
SESSIONS: for what Kevin or his agents have been working on, what Hermes is doing, or what you talked about earlier, run {ROOT}/bin/z0live sessions --limit 8 (read-only titles and one-line summaries of his recent Hermes sessions) and answer from it.
Repository root for z0live: {ROOT}. Always pass explicit working directories.
Voice broker session: {sid}. Phone tools declared by this phone: {names}. For a phone action, run {ROOT}/bin/z0live action --session {sid} --action NAME --params 'JSON' --wait 8 and read the phone's result before answering; never claim a tap landed unless the result says landed true. For an in-app step use phone_do with {{"goal": "<the owner's words>"}}; if it returns status choose, pick one candidate id and run phone_act with {{"id": ID}}. Never set confirm true unless Kevin said yes to that exact tap.
Requests containing [test] are connectivity tests: do only what the test asks."""


class Hermes:
    """Thin client for the native Hermes API host (POST /v1/runs, events, steer, stop)."""

    def __init__(self, store, base=None, session=None):
        self.store = store
        self.base = base or os.environ.get(
            "Z0LIVE_DELEGATE_API", os.environ.get("Z0LIVE_HERMES_API", "http://127.0.0.1:8644")
        )
        self.session = session or os.environ.get("Z0LIVE_DELEGATE_SESSION", HERMES_SESSION)
        self.ready = False
        self.lock = threading.Lock()

    def headers(self, idem=None):
        key = (self.store.directory / ".hermes-api-key").read_text().strip()
        return {
            "Authorization": "Bearer " + key,
            "X-Hermes-Session-Key": self.session,
            **({"Idempotency-Key": idem} if idem else {}),
        }

    def api(self, path, body=None, idem=None, timeout=10):
        return request_json(self.base + path, body, self.headers(idem), timeout=timeout)

    def binding(self):
        model = os.environ.get("Z0LIVE_DELEGATE_MODEL")
        provider = os.environ.get("Z0LIVE_DELEGATE_PROVIDER")
        if model and provider:
            return {"model": model, "provider": provider}
        return {"model": "claude-opus-5-5", "provider": "anthropic"}

    def ensure_session(self):
        with self.lock:
            if self.ready:
                return
            try:
                self.api("/api/sessions/" + self.session)
            except RuntimeError as exc:
                if "HTTP 404" not in str(exc):
                    raise
                self.api("/api/sessions", {
                    "id": self.session, "title": self.session.replace("-", " "),
                    "source": "api_server",
                    **self.binding(), "require_model_lock": True,
                })
            self.ready = True

    def start(self, text, instructions, idem):
        self.ensure_session()
        run = self.api("/v1/runs", {
            "session_id": self.session, "input": text, "instructions": instructions,
            **self.binding(),
        }, idem=idem)
        if not isinstance(run.get("run_id"), str):
            raise RuntimeError("Hermes returned no run id")
        return run["run_id"]

    def events(self, run_id):
        request = urllib.request.Request(
            self.base + "/v1/runs/" + run_id + "/events", headers=self.headers()
        )
        try:
            with urllib.request.build_opener(NoRedirect).open(request, timeout=120) as response:
                for line in response:
                    if line.startswith(b"data: "):
                        try:
                            yield json.loads(line[6:])
                        except ValueError:
                            continue
        except (urllib.error.URLError, TimeoutError, OSError):
            return

    def status(self, run_id):
        return self.api("/v1/runs/" + run_id)

    def steer(self, run_id, text):
        try:
            return bool(self.api("/v1/runs/" + run_id + "/steer", {"input": text}).get("accepted"))
        except RuntimeError:
            return False

    def stop(self, run_id):
        try:
            return self.api("/v1/runs/" + run_id + "/stop", {})
        except RuntimeError as exc:
            return {"error": str(exc)}


class PhoneRouter:
    """Pick one declared phone tool with the existing tools-free liaison, or decline."""

    def __init__(self, store, small=None):
        self.store = store
        self.small = small

    def route(self, delegation_id, text, tools):
        names = {t.get("name") for t in tools if isinstance(t, dict)}
        if not names or not PHONE_HINT.search(text) or rl.wants_relay(text):
            return None
        from .voice_runtime import Native, load_plan

        try:
            plan = load_plan()
        except RuntimeError:
            plan = None
        if not plan or "smol" not in plan.get("roles", {}):
            return None
        binding = {k: plan["roles"]["smol"][k] for k in ("provider", "model")}
        prompt = (
            "A voice assistant must act on this request from its owner. Decide if exactly one call to "
            "one of the declared phone tools fully does it. Reply with JSON only: "
            '{"tool": NAME, "arguments": {...}} for that single call, or {"tool": null} if it needs '
            "the computer, information, several steps, or no tool fits.\nRequest (data): "
            + dumps(text[:2000]) + "\nDeclared phone tools: " + dumps(tools)[:30000]
        )
        small = self.small or Native(self.store).small
        try:
            final = small("bidi-route-" + delegation_id[-40:], binding, prompt)
        except (RuntimeError, OSError, ValueError):
            return None
        return parse_route(final.get("text", ""), names)


def parse_route(output, names):
    match = re.search(r"\{.*\}", output or "", re.S)
    if not match:
        return None
    try:
        value = json.loads(match.group(0))
    except ValueError:
        return None
    tool = value.get("tool") if isinstance(value, dict) else None
    args = value.get("arguments", {}) if isinstance(value, dict) else None
    if tool not in names or not isinstance(args, dict) or len(dumps(args)) > 4000:
        return None
    return {"tool": tool, "arguments": args}


def describe_phone_result(tool, result):
    if result is None:
        return f"The phone did not confirm {tool} in time, so it may not have happened."
    data = result.get("result") if isinstance(result.get("result"), dict) else {}
    ok = bool(result.get("success", data.get("ok")))
    detail = dumps(data)[:1200]
    return f"Phone action {tool} {'succeeded' if ok else 'failed'}. Phone result (data): {detail}"


class _SessionEnded:
    """`closed` for an inbox-delivered call: true once the broker session has ended."""

    def __init__(self, store, sid):
        self.store, self.sid = store, sid

    def is_set(self):
        with self.store.connect() as db:
            row = db.execute("SELECT ended FROM sessions WHERE id=?", (self.sid,)).fetchone()
        return not row or bool(row["ended"])


class InboxCall:
    """A gpt-realtime session using the same Hermes backend as Frameless Bidi.

    Progress stays silent. The final answer becomes one inbox item for this session, which
    the phone's live model answers from in its own words (it never reads it verbatim).
    """

    routes_phone = False  # gpt-realtime calls declared phone tools itself

    def __init__(self, store, sid):
        self.store, self.sid, self.call_id = store, sid, "realtime"
        self.closed = _SessionEnded(store, sid)

    def append(self, delegation_id, text, channel=None) -> bool:
        if channel == "commentary":
            return True
        if text.startswith(bp.FINAL_LABEL):
            text = text[len(bp.FINAL_LABEL):].strip()
        self.store.publish(
            text[:12000], params={"delegation_id": delegation_id}, sid=self.sid,
            source="run:" + delegation_id,
        )
        return True


class Delegator:
    def __init__(self, store, hermes=None, router=None, phone_timeout=10.0, escalator=None):
        self.store = store
        self.hermes = hermes or Hermes(store)
        self.router = router or PhoneRouter(store)
        self.escalator = escalator or pc.Escalator()
        self.confirms = pc.Confirmations()
        self.phone_timeout = phone_timeout
        self.lock = threading.Lock()
        self.active: dict[str, dict] = {}  # sid -> {"run_id", "delegation_id"}
        self.waiters: dict[int, dict] = {}
        namespace = hashlib.sha256(str(store.directory.resolve()).encode()).hexdigest()[:12]
        self.idem_prefix = "z0live-bidi-" + namespace + "-"
        with store.connect() as db:
            db.execute(
                """CREATE TABLE IF NOT EXISTS live_delegations(id TEXT PRIMARY KEY, sid TEXT NOT NULL,
                call_id TEXT NOT NULL, text TEXT NOT NULL, route TEXT, run_id TEXT, status TEXT NOT NULL,
                created REAL NOT NULL, updated REAL NOT NULL, marks TEXT NOT NULL DEFAULT '{}',
                final TEXT)"""
            )

    # -- bookkeeping -------------------------------------------------------------
    def save(self, did, **fields):
        fields["updated"] = time.time()
        if "marks" in fields:
            fields["marks"] = dumps(fields["marks"])
        sets = ",".join(k + "=?" for k in fields)
        with self.store.connect() as db:
            db.execute(f"UPDATE live_delegations SET {sets} WHERE id=?", (*fields.values(), did))

    def trace(self, sid):
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT * FROM live_delegations WHERE sid=? ORDER BY created", (sid,)
            ).fetchall()
        return [{**dict(r), "marks": json.loads(r["marks"])} for r in rows]

    # -- events from LiveCall ------------------------------------------------------
    def delegate(self, call, did, text):
        now = time.time()
        with self.store.connect() as db:
            cur = db.execute(
                "INSERT OR IGNORE INTO live_delegations(id,sid,call_id,text,status,created,updated,marks) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (did, call.sid, call.call_id, text[:16000], "received", now, now,
                 dumps({"created": now})),
            )
        if not cur.rowcount:
            return  # duplicate delivery of the same delegation item
        threading.Thread(target=self.handle, args=(call, did, text), daemon=True,
                         name="delegation-" + did[-8:]).start()

    def user_turn(self, call, transcript):
        if CANCEL.match(transcript):
            self.cancel(call.sid)

    def cancel(self, sid) -> bool:
        with self.lock:
            active = self.active.pop(sid, None)
        if not active:
            return False
        self.hermes.stop(active["run_id"])
        self.save(active["delegation_id"], status="cancelled")
        LOG.info("cancelled run=%s on owner request", active["run_id"])
        return True

    def ask(self, sid, question, call_id=None) -> dict:
        """gpt-realtime work tool: start (or steer) the session's Hermes work, answer via inbox."""
        if not isinstance(question, str) or not question.strip() or len(question) > 16000:
            raise ValueError("question must be nonempty text of at most 16000 characters")
        if call_id is not None and (not isinstance(call_id, str) or len(call_id) > 200):
            raise ValueError("invalid call_id")
        key = sid + ":" + (call_id or uuid.uuid4().hex)
        did = "rt-" + hashlib.sha256(key.encode()).hexdigest()[:24]
        self.delegate(InboxCall(self.store, sid), did, question.strip())
        # Data-only acceptance: the voice already acknowledged; the answer follows in the inbox.
        return {"ok": True, "id": did, "status": "working", "say": "", "speech": "silent"}

    def call_closed(self, call):
        with self.lock:
            active = self.active.get(call.sid)
        if active:
            LOG.info("call closed with run=%s still active; result stays silent", active["run_id"])

    def phone_result(self, sid, data):
        try:
            inbox_id = int(data.get("id"))
        except (TypeError, ValueError):
            return
        with self.lock:
            waiter = self.waiters.get(inbox_id)
        if waiter and waiter["sid"] == sid:
            waiter["result"] = data
            waiter["event"].set()

    # -- routing ---------------------------------------------------------------------
    def tools(self, sid):
        with self.store.connect() as db:
            row = db.execute("SELECT tools FROM sessions WHERE id=?", (sid,)).fetchone()
        return json.loads(row["tools"]) if row else []

    def conversation(self, sid, limit=CONTEXT_TURNS) -> str:
        """The last spoken turns of this voice session, oldest first, as plain data lines."""
        with self.store.connect() as db:
            rows = db.execute(
                "SELECT body FROM events WHERE session_id=? AND (body LIKE '%\"user_transcript\"%' "
                "OR body LIKE '%\"assistant_transcript\"%') ORDER BY id DESC LIMIT ?",
                (sid, limit),
            ).fetchall()
        lines = []
        for row in reversed(rows):
            try:
                body = json.loads(row["body"])
            except ValueError:
                continue
            said = " ".join(str(body.get("text") or "").split())[:400]
            if said and body.get("type") in {"user_transcript", "assistant_transcript"}:
                who = "Kevin" if body["type"] == "user_transcript" else "You"
                lines.append(f"{who}: {said}")
        return "\n".join(lines)

    def request(self, sid, text) -> str:
        """Hermes input: the request, preceded by the recent conversation when there is one."""
        context = self.conversation(sid)
        if not context:
            return text
        return ("Recent voice conversation (data, oldest first):\n" + context
                + "\n\nKevin's request now: " + text)

    def handle(self, call, did, text):
        marks = {"created": time.time()}
        try:
            tools = self.tools(call.sid)
            route = None
            if getattr(call, "routes_phone", True):
                route = self.phone_route(call, did, text, tools, marks)
                if route == "handled":
                    return
                if route is None:
                    route = self.router.route(did, text, tools)
            marks["routed"] = time.time()
            if route:
                self.save(did, route="phone:" + route["tool"], status="phone", marks=marks)
                self.phone(call, did, route, marks, goal=text)
            else:
                self.save(did, route="hermes", marks=marks)
                self.work(call, did, text, tools, marks)
        except Exception as exc:
            LOG.warning("delegation failed id=%s error=%s", did, type(exc).__name__, exc_info=True)
            self.finish(call, did, "I couldn't reach the part of me that does that work right now.",
                        "failed", marks)

    def phone_route(self, call, did, text, tools, marks):
        """In-app steps skip the liaison: phone_do runs tier 0 on the phone itself."""
        pending = self.confirms.peek(call.sid)
        if pending and pc.YES.match(text):
            self.confirms.take(call.sid)
            marks["confirmed"] = pending.get("label", "")
            return {"tool": "phone_act", "arguments": {"id": pending["id"], "confirm": True}}
        if pending and pc.NO.match(text):
            self.confirms.take(call.sid)
            self.finish(call, did, "Okay, I left it alone.", "declined", marks)
            return "handled"
        if pc.wants_in_app(text, tools) and not rl.wants_relay(text):
            return {"tool": "phone_do", "arguments": {"goal": text[:1000]}}
        return None

    def finish(self, call, did, text, status, marks):
        marks["final"] = time.time()
        sent = False
        if not call.closed.is_set():
            sent = call.append(did, bp.final_message(text))
        self.save(did, status=status if sent else status + "_unspoken", final=text[:16000],
                  marks=marks)

    def phone_call(self, sid, did, tool, arguments):
        """Queue one declared phone tool in the inbox and wait for the phone's tool_result."""
        waiter = {"sid": sid, "event": threading.Event(), "result": None}
        # One inbox item per step: the source is unique per (delegation, tool).
        source = "bidi-phone:" + did + ("" if tool != "phone_act" else ":act")
        inbox_id = self.store.publish("", tool, arguments, sid=sid, source=source)
        with self.lock:
            self.waiters[inbox_id] = waiter
        try:
            waiter["event"].wait(self.phone_timeout)
        finally:
            with self.lock:
                self.waiters.pop(inbox_id, None)
        return waiter["result"]

    def phone(self, call, did, route, marks, goal=""):
        tool, started = route["tool"], time.time()
        if tool == "phone_do":
            self.escalator.warm()
        result = self.phone_call(call.sid, did, tool, route["arguments"])
        marks["phone_ms"] = round((time.time() - started) * 1000, 1)
        inner = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else {}
        if tool == "phone_do" and inner.get("status") == "choose":
            cands = pc.candidates_of(inner)
            decision, trail = self.escalator.decide(goal or route["arguments"].get("goal", ""), cands)
            marks["tier0"] = inner.get("decision")
            marks["tiers"] = trail
            if decision is None:
                names = [c["label"] for c in cands[:3]]
                ask = ("I'm not sure which you mean: " + ", ".join(names[:-1]) + " or " + names[-1] + "?") if len(names) > 1 \
                    else "I couldn't find that on the screen."
                self.finish(call, did, ask, "ask_owner", marks)
                return
            marks["decided"] = decision
            started = time.time()
            tool, result = "phone_act", self.phone_call(call.sid, did, "phone_act", {"id": decision["id"]})
            marks["act_ms"] = round((time.time() - started) * 1000, 1)
            inner = result.get("result") if isinstance(result, dict) and isinstance(result.get("result"), dict) else {}
        elif tool == "phone_do":
            marks["tier0"] = inner.get("decision")
        if inner.get("status") == "confirm_required" and isinstance(inner.get("target"), dict):
            self.confirms.hold(call.sid, inner["target"])
        if tool in {"phone_do", "phone_act", "phone_navigation"}:
            text = pc.speak(tool, inner if result else None)
        else:
            text = describe_phone_result(tool, result)
        status = "completed" if result else "unconfirmed"
        if inner.get("status") == "done" and not inner.get("landed"):
            status = "not_landed"
        self.finish(call, did, text, status, marks)

    def work(self, call, did, text, tools, marks):
        with self.lock:
            active = self.active.get(call.sid)
        if active and self.hermes.steer(active["run_id"], text):
            with self.lock:
                # The newest delegation receives the combined final answer.
                if self.active.get(call.sid) is active:
                    superseded = active["delegation_id"]
                    active["delegation_id"] = did
                else:
                    superseded = None
            if superseded:
                self.save(superseded, status="merged")
            marks["steered"] = time.time()
            self.save(did, run_id=active["run_id"], status="steered", marks=marks)
            return
        run_id = self.hermes.start(self.request(call.sid, text),
                                   backend_instructions(call.sid, tools),
                                   self.idem_prefix + did[-200:])
        marks["run_started"] = time.time()
        active = {"run_id": run_id, "delegation_id": did}
        with self.lock:
            self.active[call.sid] = active
        self.save(did, run_id=run_id, status="running", marks=marks)
        sent, last = 0, ""
        for event in self.hermes.events(run_id):
            name = event.get("event", "")
            note = ""
            if name in {"message.interim", "assistant.commentary"}:
                note = str(event.get("text") or "").strip()
            elif name == "tool.started":
                note = ("Using " + str(event.get("tool") or "a tool") + ": "
                        + str(event.get("preview") or "")[:160]).strip()
            if note and note != last and sent < MAX_COMMENTARY and not call.closed.is_set():
                last = note
                sent += 1
                marks.setdefault("first_commentary", time.time())
                call.append(active["delegation_id"], note[:600], "commentary")
            if name.startswith("run.") and name not in {"run.started", "run.queued"}:
                break
        run = self.await_terminal(run_id)
        with self.lock:
            still_active = self.active.get(call.sid) is active
            if still_active:
                self.active.pop(call.sid, None)
        if not still_active:
            return  # cancelled by the owner: never speak a stale result
        status = run.get("status")
        output = run.get("output") or run.get("final_response") or ""
        if not isinstance(output, str):
            output = dumps(output)
        if status == "completed" and output.strip():
            self.finish(call, active["delegation_id"], output.strip(), "completed", marks)
        else:
            self.finish(call, active["delegation_id"],
                        f"That work ended as {status or 'unknown'} without a result.",
                        status or "failed", marks)

    def await_terminal(self, run_id, deadline=3600.0):
        end = time.monotonic() + deadline
        while time.monotonic() < end:
            try:
                run = self.hermes.status(run_id)
            except RuntimeError:
                run = {}
            if run.get("status") in {"completed", "failed", "cancelled", "interrupted"}:
                return run
            time.sleep(0.4)
        return {"status": "timeout"}
