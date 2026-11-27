"""The request ledger: what Kevin asked for, who promised it, what proves it started, and what came of it.

One append-only JSON-lines file, ~/.local/share/z0live/ledger/ledger.jsonl (the directory comes from the env var
Z0LIVE_LEDGER, read on every call so a test can point it at a temp dir). Every writer (the voice adapter, Hermes
background agents, the TV conductor, kanban jobs) appends events; readers fold them into items. Stdlib only, loadable
by path like lane_layout.py.

    open_item(call, text, kind, owner) -> id      kind: action | question | research; owner: voice|background|conductor|job
    claim(id, by, note)                           "I will do X": a promise, no proof yet
    receipt(id, by, kind, ref, detail="")         proof it started: kind in pid|kanban|cron|commit|file|lane, ref = the id
    result(id, by, text, ok=True, refs=[], final=True)   finished output (a lane card, an answer, an artifact path)
    state(id, s, by, why="")                      s in open|running|done|blocked|failed|dropped
    items(call=None, open_only=False) -> [dict]   folded view, newest first
    brief(call, max_chars=4000) -> str            what the next agent must read: open promises, receipts, results

The rule the whole loop rests on: nobody tells Kevin work "started" or "done" unless the item has a receipt or a result.
A claim with no receipt after NOT_STARTED_SECONDS is `unstarted` in the folded view (and "NOT STARTED" in brief()); that
is derived, never a stored state. For an action only pid|kanban|cron|commit|file receipts are proof: a `lane` receipt
says an answerer was launched, not that the action happened.

Events: {"t": wall clock, "op": open|claim|receipt|result|state, "id": ..., "by": ..., ...}. A write is one os.write on an
O_APPEND fd (atomic for lines this size) under a short flock; readers take no lock and skip a partial or garbled line,
so a stuck writer can never stall a voice event loop.

CLI (for a background agent that must record proof; prints the id it wrote against):
    python3 ledger.py receipt ID --by NAME --kind kanban --ref t_1a2b [--detail TEXT]
    python3 ledger.py claim|result|state|open|items|brief ...   (see --help)
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import sys
import tempfile
import time
from pathlib import Path

KINDS = ("action", "question", "research")
OWNERS = ("voice", "background", "conductor", "job")
RECEIPT_KINDS = ("pid", "kanban", "cron", "commit", "file", "lane")
PROOF_KINDS = ("pid", "kanban", "cron", "commit", "file")  # what proves an ACTION started
STATES = ("open", "running", "done", "blocked", "failed", "dropped")
TERMINAL = ("done", "failed", "dropped")
NOT_STARTED_SECONDS = 120.0
TEXT_CAP = 4000
RESULT_CAP = 8000


def directory() -> Path:
    """Z0LIVE_LEDGER, else the real ledger; under `python -m unittest` / pytest with the variable unset, a per-run temp
    dir, so a test that builds a Call can never write its made-up requests into the ledger the TV reads."""
    if os.environ.get("Z0LIVE_LEDGER"):
        return Path(os.environ["Z0LIVE_LEDGER"])
    argv0 = str(sys.argv[0])
    if any(w in argv0 for w in ("unittest", "pytest")) or os.path.basename(argv0).startswith("test_"):
        return Path(tempfile.gettempdir()) / f"z0live-test-ledger-{os.getpid()}"
    return Path.home() / ".local/share/z0live/ledger"


def path() -> Path:
    return directory() / "ledger.jsonl"


def _append(event: dict) -> None:
    event = {"t": time.time(), **event}
    line = (json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n").encode()
    directory().mkdir(parents=True, exist_ok=True)
    fd = os.open(path(), os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        locked = False
        for _ in range(20):  # at most ~0.2 s: a stuck writer must not stall the caller's loop
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
                break
            except OSError:
                time.sleep(0.01)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                line = b"\n" + line  # a writer died mid-line: do not glue this event onto its remains
            os.write(fd, line)  # one write on an O_APPEND fd: atomic even if the lock was not taken
        finally:
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _clip(text, cap: int) -> str:
    return " ".join(str(text or "").split())[:cap] if cap <= TEXT_CAP else str(text or "")[:cap]


def open_item(call: str, text: str, kind: str = "question", owner: str = "voice") -> str:
    if kind not in KINDS:
        raise ValueError(f"kind must be one of {KINDS}")
    if owner not in OWNERS:
        raise ValueError(f"owner must be one of {OWNERS}")
    item = f"r{int(time.time() * 1000):x}{secrets.token_hex(4)}"
    _append({"op": "open", "id": item, "call": str(call or ""), "text": _clip(text, TEXT_CAP), "kind": kind, "owner": owner})
    return item


def claim(item: str, by: str, note: str = "") -> None:
    _append({"op": "claim", "id": item, "by": str(by), "note": _clip(note, TEXT_CAP)})


def receipt(item: str, by: str, kind: str, ref: str, detail: str = "") -> None:
    if kind not in RECEIPT_KINDS:
        raise ValueError(f"receipt kind must be one of {RECEIPT_KINDS}")
    if not str(ref or "").strip():
        raise ValueError("a receipt needs a ref (the pid, kanban id, cron id, sha, path or lane id)")
    _append({"op": "receipt", "id": item, "by": str(by), "kind": kind, "ref": str(ref)[:500], "detail": _clip(detail, 1000)})


def result(item: str, by: str, text: str, ok: bool = True, refs=(), final: bool = True) -> None:
    """final=False records output that does not finish the item (a lane's research note on an action, a failed lane
    when the answer may still come): it shows in brief() but leaves the item's state alone."""
    _append({"op": "result", "id": item, "by": str(by), "text": _clip(text, RESULT_CAP), "ok": bool(ok),
             "refs": [str(r)[:500] for r in refs or ()], "final": bool(final)})


def state(item: str, s: str, by: str, why: str = "") -> None:
    if s not in STATES:
        raise ValueError(f"state must be one of {STATES}")
    _append({"op": "state", "id": item, "s": s, "by": str(by), "why": _clip(why, 1000)})


_CACHE: dict = {}


def _read() -> list[dict]:
    """Every event, cached on the file's (path, mtime, size): the voice reads the ledger several times a request and
    the file only grows."""
    try:
        st = path().stat()
        key = (str(path()), st.st_mtime_ns, st.st_size)
        if _CACHE.get("key") == key:
            return _CACHE["events"]
        raw = path().read_bytes()
    except OSError:
        return []
    events = _parse(raw)
    _CACHE.update(key=key, events=events, folded=None)
    return events


def _parse(raw: bytes) -> list[dict]:
    events = []
    for line in raw.split(b"\n"):
        try:
            event = json.loads(line)
        except ValueError:
            continue  # blank, partial or garbled: skip it
        if isinstance(event, dict) and event.get("id") and event.get("op"):
            events.append(event)
    return events


def _fold(events: list[dict]) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in events:
        op, item = e["op"], e["id"]
        if op == "open":
            out[item] = {"id": item, "call": e.get("call", ""), "text": e.get("text", ""), "kind": e.get("kind", "question"),
                         "owner": e.get("owner", "voice"), "state": "open", "opened": e["t"], "updated": e["t"],
                         "claims": [], "receipts": [], "results": [], "history": []}
            continue
        it = out.get(item)
        if it is None:
            continue  # an event for an item this file never opened
        it["updated"] = max(it["updated"], e["t"])
        if op == "claim":
            it["claims"].append({"by": e.get("by", ""), "note": e.get("note", ""), "t": e["t"]})
        elif op == "receipt":
            it["receipts"].append({"by": e.get("by", ""), "kind": e.get("kind", ""), "ref": e.get("ref", ""),
                                   "detail": e.get("detail", ""), "t": e["t"]})
            if it["state"] == "open":
                it["state"] = "running"
        elif op == "result":
            it["results"].append({"by": e.get("by", ""), "text": e.get("text", ""), "ok": e.get("ok", True),
                                  "refs": e.get("refs", []), "t": e["t"]})
            if it["state"] != "dropped" and e.get("final", True):
                it["state"] = "done" if e.get("ok", True) else "failed"
        elif op == "state" and e.get("s") in STATES:
            it["state"] = e["s"]
            it["history"].append({"s": e["s"], "by": e.get("by", ""), "why": e.get("why", ""), "t": e["t"]})
    return out


def _derive(it: dict, now: float) -> dict:
    kinds = PROOF_KINDS if it["kind"] == "action" else RECEIPT_KINDS
    it["proven"] = any(r["kind"] in kinds for r in it["receipts"])
    first = it["claims"][0]["t"] if it["claims"] else None
    open_ = it["state"] not in TERMINAL and it["state"] != "blocked"  # blocked says why it did not start
    it["unstarted"] = bool(first is not None and not it["proven"] and open_ and now - first >= NOT_STARTED_SECONDS)
    it["claimed_not_started"] = bool(it["claims"] and not it["proven"] and open_)
    return it


def _folded() -> dict[str, dict]:
    events = _read()
    if _CACHE.get("folded") is None or _CACHE.get("events") is not events:
        _CACHE["folded"] = _fold(events)
    return _CACHE["folded"]


def items(call: str | None = None, open_only: bool = False, now: float | None = None) -> list[dict]:
    """The folded view, newest first. `now` is for a replay that runs on its own clock."""
    now = time.time() if now is None else now
    out = [_derive(dict(it), now) for it in _folded().values() if call is None or it["call"] == call]
    if open_only:
        out = [it for it in out if it["state"] not in TERMINAL]
    return sorted(out, key=lambda it: it["opened"], reverse=True)


def get(item: str, now: float | None = None) -> dict | None:
    folded = _folded().get(item)
    return _derive(dict(folded), time.time() if now is None else now) if folded else None


def _line(it: dict, now: float, full: bool) -> str:
    age = int(max(0, now - it["opened"]))
    if it["state"] in TERMINAL or it["state"] == "blocked":
        head = it["state"].upper()
    elif it["unstarted"]:
        head = "PROMISED, NOT STARTED (no proof after %ds)" % NOT_STARTED_SECONDS
    elif it["claimed_not_started"]:
        head = "promised, no proof yet"
    else:
        head = it["state"]
    out = [f"- [{it['id']}] {it['kind']}, {head}, {age}s ago: {it['text'][:300]}"]
    for c in it["claims"][-2:]:
        out.append(f"    promised by {c['by']}: {c['note'][:200]}")
    for r in it["receipts"][-3:]:
        out.append(f"    proof {r['kind']} {r['ref']}" + (f" ({r['detail'][:100]})" if r["detail"] else ""))
    for r in it["results"][-(2 if full else 1):]:
        out.append(f"    {'result' if r['ok'] else 'FAILED'} by {r['by']}: {r['text'][:600 if full else 300]}")
    for h in it["history"][-1:]:
        if h["why"]:
            out.append(f"    {h['s']}: {h['why'][:200]}")
    return "\n".join(out)


BRIEF_HEAD = ("Request ledger for this call. It is the record of what Kevin asked, what was promised, and what proves "
              "it. Never tell him work started, is running or is done unless an item below shows proof (a proof line) "
              "or a result. Do not repeat answers already given below; do not make a promise a new item would have to "
              "keep unless you start the work and record its proof.")


def brief(call: str, max_chars: int = 4000, now: float | None = None, current: str | None = None) -> str:
    """Open promises first (with their proof), then the newest results, newest first within each, cut to max_chars by
    dropping the oldest. Empty when the call has no items. `current` marks the item being answered now."""
    now = time.time() if now is None else now
    its = items(call, now=now)
    if not its:
        return ""
    live = [it for it in its if it["state"] not in TERMINAL]
    done = [it for it in its if it["state"] in TERMINAL]
    lines = []
    for it in live + done:
        line = _line(it, now, full=it["state"] in TERMINAL)
        lines.append(line.replace("- [", "- (this request) [", 1) if it["id"] == current else line)
    out, size = [], len(BRIEF_HEAD) + 1
    for line in lines:
        if size + len(line) + 1 > max_chars:
            continue  # drop what does not fit; later (older) shorter lines may still
        out.append(line)
        size += len(line) + 1
    dropped = len(lines) - len(out)
    return BRIEF_HEAD + "\n" + "\n".join(out) + (f"\n(+{dropped} older items not shown)" if dropped else "")


def main(argv: list[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("open"); p.add_argument("--call", required=True); p.add_argument("--kind", default="action")
    p.add_argument("--owner", default="job"); p.add_argument("text")
    p = sub.add_parser("claim"); p.add_argument("id"); p.add_argument("--by", required=True); p.add_argument("note", nargs="?", default="")
    p = sub.add_parser("receipt"); p.add_argument("id"); p.add_argument("--by", required=True)
    p.add_argument("--kind", required=True, choices=RECEIPT_KINDS); p.add_argument("--ref", required=True)
    p.add_argument("--detail", default="")
    p = sub.add_parser("result"); p.add_argument("id"); p.add_argument("--by", required=True); p.add_argument("--failed", action="store_true")
    p.add_argument("--ref", action="append", default=[]); p.add_argument("text")
    p = sub.add_parser("state"); p.add_argument("id"); p.add_argument("s", choices=STATES); p.add_argument("--by", required=True)
    p.add_argument("--why", default="")
    p = sub.add_parser("items"); p.add_argument("--call"); p.add_argument("--open", action="store_true")
    p = sub.add_parser("brief"); p.add_argument("call")
    a = ap.parse_args(argv)
    if a.cmd == "open":
        print(open_item(a.call, a.text, a.kind, a.owner))
    elif a.cmd == "claim":
        claim(a.id, a.by, a.note)
    elif a.cmd == "receipt":
        receipt(a.id, a.by, a.kind, a.ref, a.detail)
    elif a.cmd == "result":
        result(a.id, a.by, a.text, not a.failed, a.ref)
    elif a.cmd == "state":
        state(a.id, a.s, a.by, a.why)
    elif a.cmd == "items":
        print(json.dumps(items(a.call, a.open), indent=1))
    elif a.cmd == "brief":
        print(brief(a.call))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
