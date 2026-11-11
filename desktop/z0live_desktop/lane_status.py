"""Per-call lane status: what each helper lane is doing right now, for live-mode UIs (Glide's lane strip).

Read-only view over real lane work, never a guess. `install(fanout)` wraps the fan-out's model calls so
every lane job records start / finish / error with its task and topic; `status(fanout, sid)` joins that
with the call's lane config, the blackboard and local-server health:

  {"session_id", "revision", "epoch", "lanes": [{"id", "role", "model", "resolved", "route", "state",
    "researching", "error", "entry": {"id", "text", "confidence", "status", "kind"} | None}]}

state: dead (no local server, failed health probe, skipped by the fan-out, or its last job errored),
thinking (a job in flight), verified / corrected / answered (its newest current blackboard entry), else
idle. A lane with no recorded job and no entry is idle: nothing is invented.
"""
from __future__ import annotations

import json
import threading
import time
import urllib.request

from . import lanes as lanes_mod

HEALTH_TTL_S = 15.0
_health: dict[str, tuple[float, str | None]] = {}  # base url -> (checked at, error or None)
_health_lock = threading.Lock()


def _task(prompt: str) -> tuple[str, str]:
    """(task, topic) from a lane prompt: which fan-out job, and the words it is working on."""
    from . import lane_fanout as lf
    for prefix, task in ((getattr(lf, "PANSWER", None), "answer"), (getattr(lf, "VERIFY", None), "verify"),
                         (getattr(lf, "INTENT", None), "intent"), (getattr(lf, "ANSWER", None), "answer"),
                         (getattr(lf, "GATHER", None), "gather")):
        if prefix and prompt.startswith(prefix):
            rest = prompt[len(prefix):]
            break
    else:
        task, rest = "job", prompt[prompt.find("{"):] if "{" in prompt else ""
    try:
        data = json.loads(rest)
    except ValueError:
        data = {}
    topic = data.get("kevin_now") or data.get("question") or "" if isinstance(data, dict) else ""
    return task, " ".join(str(topic).split())[:160]


def _key(binding: dict, prompt: str) -> str:
    """Lane id: tier + configured model. Large lanes have no tier; their verify/answer prompts mark them."""
    from . import lane_fanout as lf
    large = any(p and prompt.startswith(p) for p in (getattr(lf, "PANSWER", None), getattr(lf, "VERIFY", None)))
    return f"{'large' if large else binding.get('tier', 'small')}:{binding.get('model')}"


class Tracker:
    def __init__(self):
        self.lock = threading.Lock()
        self.calls: dict[str, dict[str, dict]] = {}  # sid -> lane id -> activity

    def start(self, sid, key, task, topic):
        with self.lock:
            a = self.calls.setdefault(sid, {}).setdefault(key, {"inflight": 0})
            a.update(inflight=a["inflight"] + 1, task=task, topic=topic, started=time.time())

    def finish(self, sid, key, model=None, error=None, ms=None):
        with self.lock:
            a = self.calls.setdefault(sid, {}).setdefault(key, {"inflight": 1})
            a.update(inflight=max(0, a["inflight"] - 1), error=error, finished=time.time())
            if model:
                a["resolved"] = model
            if ms is not None:
                a["ms"] = ms

    def get(self, sid):
        with self.lock:
            return {k: dict(v) for k, v in self.calls.get(sid, {}).items()}


class _Recording:
    """Models proxy: same complete() signature, records each lane job on the tracker."""

    def __init__(self, models, tracker, sid):
        self._models, self._tracker, self._sid = models, tracker, sid
        self.plan = getattr(models, "plan", None)

    def __getattr__(self, name):
        return getattr(self._models, name)

    def complete(self, binding, prompt, *args, **kwargs):
        key = _key(binding, prompt)
        task, topic = _task(prompt)
        self._tracker.start(self._sid, key, task, topic)
        try:
            result = self._models.complete(binding, prompt, *args, **kwargs)
        except Exception as exc:
            self._tracker.finish(self._sid, key, error=type(exc).__name__)
            raise
        self._tracker.finish(self._sid, key, model=result.get("model"), ms=result.get("ms"))
        return result


def install(fanout):
    """Record every lane job of this fan-out. Idempotent."""
    if getattr(fanout, "tracker", None) is not None:
        return fanout.tracker
    tracker = Tracker()
    original = fanout._models
    fanout._models = lambda sid: _Recording(original(sid), tracker, sid)
    fanout.tracker = tracker
    return tracker


def _probe(base: str) -> str | None:
    now = time.monotonic()
    with _health_lock:
        hit = _health.get(base)
        if hit and now - hit[0] < HEALTH_TTL_S:
            return hit[1]
    try:
        with urllib.request.urlopen(base.rstrip("/") + "/health", timeout=1.5) as r:
            error = None if r.status == 200 else f"health {r.status}"
    except Exception as exc:  # noqa: BLE001 - any failure means the lane cannot answer
        error = type(exc).__name__
    with _health_lock:
        _health[base] = (now, error)
    return error


def _dead(binding, fanout, sid) -> str | None:
    skipped = (getattr(fanout, "dead", {}) or {}).get(sid) or {}
    if binding.get("model") in skipped:
        return str(skipped[binding["model"]])[:120] or "skipped"
    if binding.get("route") == "local":
        base = lanes_mod.local_base(binding["model"])
        if not base:
            return "no local server"
        return _probe(base)
    return None


def _bindings(cfg) -> list[tuple[str, dict]]:
    out = [(b.get("tier", "small"), b) for b in cfg["lanes"]["smol"]["models"]]
    out += [("large", b) for b in lanes_mod.large(cfg)]
    council = cfg["lanes"].get("council") or {}
    if council.get("enabled"):
        seats = (council.get("params") or {}).get("seats") or []
        out.append(("council", {"model": "+".join(map(str, seats)) or council.get("pattern", "council"),
                                "route": "council", "provider": "council"}))
    seen = set()  # a model bound twice under one role (e.g. router + racer) is one lane: activity is keyed by role:model
    return [(r, b) for r, b in out if (r, b.get("model")) not in seen and not seen.add((r, b.get("model")))]


def status(fanout, sid) -> dict:
    cfg = fanout.cfg(sid)
    activity = fanout.tracker.get(sid) if getattr(fanout, "tracker", None) else {}
    board = fanout.board.read(sid, limit=50)
    lanes = []
    for role, b in _bindings(cfg):
        key = f"{role}:{b.get('model')}"
        a = activity.get(key, {})
        resolved = a.get("resolved")
        names = {f"{role}:{resolved}", f"{role}:{b.get('model')}"} if resolved else {f"{role}:{b.get('model')}"}
        entry = next((e for e in reversed(board["entries"]) if e["lane"] in names), None)
        if entry is None and role == "large" and resolved:  # verify()/correct() write as the verifier
            entry = next((e for e in reversed(board["entries"])
                          if e["status"] in {"verified", "corrected"} and
                          any(s == "verified-by:" + n for s in e["sources"] for n in names)), None)
        error = _dead(b, fanout, sid) or (a.get("error") if not a.get("inflight") else None)
        if error:
            state = "dead"
        elif a.get("inflight"):
            state = "thinking"
        elif entry:
            state = {"verified": "verified", "corrected": "corrected"}.get(entry["status"], "answered")
        else:
            state = "idle"
        lanes.append({
            "id": key, "role": role, "model": b.get("model"), "resolved": resolved, "route": b.get("route"),
            "state": state, "error": error,
            "researching": f"{a['task']}: {a['topic']}" if a.get("task") and a.get("topic") else a.get("task"),
            "ms": a.get("ms"),
            "entry": {k: entry[k] for k in ("id", "text", "confidence", "status", "kind")} if entry else None,
        })
    return {"session_id": sid, "revision": cfg.get("revision"), "epoch": board["epoch"], "lanes": lanes,
            "at": time.time()}
