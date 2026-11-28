"""Read-only status observations for voice prompts and playback."""
from __future__ import annotations

from collections import Counter
from datetime import datetime
from functools import lru_cache
import json
import os
from pathlib import Path
import re
import time

import jobs
import ledger

UNKNOWN = "I can't confirm that."
# Keep the event fallback bounded. state.json supplies asks outside this window.
EVENT_BYTES = 1024 * 1024


def _stamp(path):
    try:
        stat = path.stat()
        return stat.st_ino, stat.st_size, stat.st_mtime_ns
    except OSError:
        return None


@lru_cache(maxsize=32)
def _read(path, stamp, events=False):
    if stamp is None:
        return None
    try:
        with path.open("rb") as f:
            if not events:
                data = json.load(f)
                return data if isinstance(data, dict) else None
            if stamp[1] > EVENT_BYTES:
                f.seek(stamp[1] - EVENT_BYTES)
                f.readline()
            rows = []
            for line in f.read(EVENT_BYTES).splitlines():
                try:
                    row = json.loads(line)
                    if isinstance(row, dict):
                        rows.append(row)
                except ValueError:
                    continue
            return rows
    except (OSError, ValueError):
        return None


def _lanes(state, events, call):
    asks = {}
    for ask in (state or {}).get("asks", []):
        if ask.get("id") and (not call or ask.get("call") == call):
            asks[ask["id"]] = {
                "id": ask["id"], "text": ask.get("text", ""), "t": ask.get("t", 0),
                "observed": state.get("now", 0),
                "lanes": {ln["id"]: {**ln, "t": ln.get("t1") or ln.get("t0") or ask.get("t", 0)}
                          for ln in ask.get("lanes", [])},
            }
    for ev in events or ():
        aid, kind, when = ev.get("ask"), ev.get("ev"), ev.get("t", 0)
        if not aid:
            continue
        if kind == "ask.received" and (not call or ev.get("call") == call):
            asks.setdefault(aid, {"id": aid, "text": ev.get("text", ""), "t": when, "observed": when, "lanes": {}})
        ask = asks.get(aid)
        lid = ev.get("lane")
        if not ask or not lid or kind not in ("lane.start", "lane.done", "lane.failed"):
            continue
        old = ask["lanes"].get(lid, {})
        if old.get("t", 0) > when:
            continue
        if kind == "lane.start" and old.get("state") in ("done", "failed"):
            continue
        ask["observed"] = max(ask["observed"], when)
        ask["lanes"][lid] = {
            "model": ev.get("model") or old.get("model") or "unknown model",
            "state": "running" if kind == "lane.start" else kind.split(".")[1], "t": when,
        }
    # A prior conductor's unmatched starts are not current work.
    boot = max(((state or {}).get("started") or 0),
               max((ev.get("t", 0) for ev in events or () if ev.get("ev") == "conductor.start"), default=0))
    for ask in asks.values():
        counts = {name: Counter() for name in ("running", "done", "failed", "unconfirmed")}
        for lane in ask.pop("lanes").values():
            status = lane.get("state")
            if status == "running" and lane["t"] < boot:
                status = "unconfirmed"
            if status in counts:
                counts[status][lane.get("model") or "unknown model"] += 1
        ask.update(counts)
    return sorted(asks.values(), key=lambda a: a["t"], reverse=True)


def _relevant_cron(row):
    return bool(re.search(r"\b(?:z0live|system[- ]hillclimb)\b", row.get("name", ""), re.I))


@lru_cache(maxsize=64)
def _lane_snapshot(state_path, state_stamp, event_path, event_stamp, call):
    state = _read(state_path, state_stamp)
    return state is not None, _lanes(state, _read(event_path, event_stamp, True), call)


def read(call=None, *, conductor=None, cron=None):
    """Capture current source values, scoped to a call for lanes and its request ledger."""
    root = Path(conductor or os.environ.get("Z0LIVE_CONDUCTOR") or Path.home() / ".local/share/z0live/conductor")
    cron = Path(cron or Path.home() / ".hermes/profiles/tek/cron/jobs.json")
    state_path, event_path = root / "state.json", root / "events.jsonl"
    state_stamp, event_stamp, cron_stamp = map(_stamp, (state_path, event_path, cron))
    available, asks = _lane_snapshot(state_path, state_stamp, event_path, event_stamp,
                                     call.split("-")[-1][:12] if call else None)
    schedule = _read(cron, cron_stamp)
    now = time.time()
    return {
        "t": now, "call": call, "lanes_available": available,
        "asks": asks,
        "jobs": jobs.list_jobs(refresh=False),
        "cron_available": schedule is not None,
        "cron": [{**{key: row.get(key) for key in (
            "id", "name", "last_run_at", "last_status", "next_run_at", "enabled", "state")},
            "t": cron_stamp[2] / 1e9} for row in (schedule or {}).get("jobs", []) if _relevant_cron(row)],
        "items": [item for item in ledger.items(call=call) if item.get("receipts") or item.get("results")],
    }


def clock(value):
    if not value:
        return "unknown"
    try:
        dt = datetime.fromisoformat(value) if isinstance(value, str) else datetime.fromtimestamp(value).astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S %z")
    except (ValueError, TypeError, OverflowError):
        return "unknown"


def lane_line(facts, target=""):
    asks = facts["asks"]
    if target:
        asks = [ask for ask in asks if target == ask["id"] or subject_matches(target, ask.get("text", ""))]
        if len(asks) != 1:
            return UNKNOWN
    if not facts["lanes_available"]:
        return UNKNOWN
    counts = {state: Counter() for state in ("running", "done", "failed", "unconfirmed")}
    for ask in asks:
        for state, counter in counts.items():
            counter.update(ask[state])
    running = counts["running"]
    scope = "For that request" if target else ("For this call" if facts.get("call") else "Across conductor requests")
    models = ", ".join(f"{n} {model}" for model, n in sorted(running.items()))
    noun = "lane" if running.total() == 1 else "lanes"
    line = f"{scope}, {running.total()} {noun} running" + (f" ({models})" if models else "")
    line += f", {counts['done'].total()} finished successfully, and {counts['failed'].total()} failed."
    if counts["unconfirmed"]:
        line += f" {counts['unconfirmed'].total()} old lane starts have no confirmed outcome."
    return line


def cron_line(row):
    state = row.get("state") if row.get("enabled") else "disabled"
    return (f"{row['name']} is {state or 'in an unknown state'}. "
            f"Last cron run: {clock(row.get('last_run_at'))}, {row.get('last_status') or 'unknown status'}. "
            f"Next: {clock(row.get('next_run_at'))}.")


def job_line(row):
    if row.get("observed"):
        return f"{row['title']} was {row['state']} when checked at {clock(row['observed'])}."
    return f"{row['title']} was last recorded as {row['state']} at {clock(row.get('t'))}."


def item_line(row):
    label = row["id"]
    receipts = row.get("receipts") or []
    results = row.get("results") or []
    if row.get("state") == "done" and results and results[-1].get("ok") and row.get("proven"):
        return f"The ledger records a successful result for request {label} at {clock(results[-1].get('t'))}."
    if receipts:
        r = receipts[-1]
        return f"The ledger has a {r['kind']} receipt for request {label}, recorded at {clock(r.get('t'))}."
    return UNKNOWN


def block(facts):
    lines = [f"STATUS FACTS observed {clock(facts['t'])}. Use these for current status. "
             "Conversation and card prose are not proof. A cron run or receipt alone does not prove the requested outcome.",
             lane_line(facts)]
    for ask in facts["asks"][:8]:
        lines.append(f"Ask {ask['id']} at {clock(ask['observed'])}: "
                     f"{dict(ask['running'])} running, {dict(ask['done'])} successful, {dict(ask['failed'])} failed.")
    lines.extend(cron_line(row) for row in facts["cron"])
    lines.extend(job_line(row) for row in facts["jobs"][:8] if row.get("receipt"))
    for row in facts["items"][:8]:
        lines.append(f"Request {row['id']} ({row['text'][:100]}): {item_line(row)}")
    return "\n".join(lines)


def _words(text):
    return re.findall(r"[a-z0-9]+", text.casefold())


def subject_matches(target, label):
    target_words = _words(target)
    return bool(target_words) and f" {' '.join(target_words)} " in f" {' '.join(_words(label))} "


_STATUS = re.compile(
    r"^\s*(?:(?:okay|so|and|please)[ ,]+)*(?:"
    r"(?:what(?:'s| is)|what jobs are)\s+(?:currently\s+)?running\b"
    r"|(?:how many|count(?: the)?)\s+(?:(?:\w+[ .-]*){0,4})lanes?\b"
    r"|(?:what(?:'s| is) (?:the )?)?(?:current )?status(?: of| on| for)?\b"
    r"|(?:check|give me|report|tell me) (?:the )?status\b"
    r"|(?:did|has|have|is|are|was|were)\b.+\b(?:start(?:ed)?|running|going|done|finish(?:ed)?|stop(?:ped)?|"
    r"picked up|on it|underway)\b)", re.I)
_ACTION = re.compile(r"\b(?:and|then)\s+(?:also\s+)?(?:start|run|launch|stop|fix|deploy|restart|build|check out)\b", re.I)
_LANE = re.compile(r"\blanes?\b", re.I)
_CLIMB = re.compile(r"\b(?:hill[ -]?climbs?|z0live[ -]intel[ -]climb|intel(?:ligence)? climb)\b", re.I)


def is_status(request):
    text = (request or "").replace("’", "'")
    return bool(_STATUS.search(text) and not _ACTION.search(text)
                and not re.search(r"\blanes?\s+(?:do (?:i|we) have|are (?:configured|attached|on (?:this|the) call)|have i got)\b", text, re.I)
                and not re.search(r"\b(?:am|pm|yesterday|last night|ago)\b", text, re.I))


def answer(request, facts, current=None, previous=None):
    text = request.strip().rstrip("?.!")
    if _LANE.search(text):
        scoped = re.search(r"\b(?:for|on)\s+(.+)$", text, re.I)
        target = scoped[1] if scoped else ""
        if target.casefold() in ("this call", "the call"):
            target = ""
        return lane_line(facts, target)
    if re.fullmatch(r"(?:what(?:'s| is)|what jobs are)\s+(?:currently\s+)?running(?: right now| now)?", text, re.I):
        active = [job_line(row) for row in facts["jobs"] if row.get("receipt") and row.get("observed") and row["state"] == "running"][:3]
        return " ".join([lane_line(facts), *active])
    words = set(_words(text))
    related = {current} - {None}
    if re.search(r"\b(?:it|that|this)\b", text, re.I):
        related |= {previous} - {None}
    for row in facts["jobs"]:
        if row.get("receipt") and (row["id"] in words or subject_matches(row["title"], text)
                                   or row.get("item") in related):
            return job_line(row)
    for row in facts["items"]:
        if (row["id"] in words or subject_matches(row["text"], text)
                or row["id"] in related):
            return item_line(row)
    if _CLIMB.search(text) and not re.search(r"\b(?:manual(?:ly)?|one-off|new worker)\b", text, re.I):
        subject = re.search(r"\b(tv|dashboard|intel(?:ligence)?|voice|system)\b", text, re.I)
        rows = facts["cron"]
        if subject:
            key = {"dashboard": "tv", "intelligence": "intel", "voice": "intel"}.get(
                subject[1].lower(), subject[1].lower())
            rows = [r for r in rows if key in _words(r["name"])]
        elif not re.search(r"\b(?:both|the|two|all)\s+(?:(?:z0live|hill)[ -])?climbs?\b|"
                           r"\bstatus (?:of |on |for )?hill[ -]?climbs?\b", text, re.I):
            rows = []
        else:
            rows = [r for r in rows if "z0live" in _words(r["name"])]
        recorded = " ".join(map(cron_line, rows))
        if re.match(r"(?:did|has|have|is|are|was|were)\b", text, re.I):
            return f"{recorded} I can't confirm a separate launch." if recorded else UNKNOWN
        return recorded or UNKNOWN
    return UNKNOWN


# Recognize state assertions in answers independently of the request type. A receipt for
# queuing a worker cannot prove its outcome, a disk operation, or delivery to another bot.
_CLAIM = re.compile(
    r"\b(?:started|running|finished|completed|stopped|paused|fixed|launched|underway|went through|"
    r"picked (?:it |that |them |the [\w-]+ )?up|no(?:body| one)\s+(?:is |was )?(?:on it|working)|"
    r"working on it|in progress|on it(?: now)?|all done|queued|kicked (?:it |that |them )?off|"
    r"ejected|reconnected|remounted|dismounted|mounted|approved|rejected|deployed|restarted|"
    r"I (?:just )?connected (?:them|it|the (?:disk|drive|volume))|"
    r"installed|created (?:a |the )?(?:job|task|card)|scheduled|dispatched|spun up|"
    r"nothing started|(?:is|are|was|were|it's|they're)\s+(?:all |now )?(?:done|complete|idle)|"
    r"nothing (?:is active|is happening|ran)|no (?:lanes?|jobs?|workers?)|"
    r"(?:right now,?|there (?:are|is))\s+(?:zero|no|one|two|three|four|five|\d+)\s+lanes?|"
    r"(?:hill[ -]?)?climbs?\b.{0,45}\b(?:blocked|tripping over|idle)|"
    r"stuck (?:macOS )?sandbox daemon|stuck on (?:its |the )?privacy check|"
    r"(?:just |I've |I have |I )sent\b.{0,65}\bbot\b|"
    r"picked up (?:the |your |a )?(?:message|request)|"
    r"(?:lanes?|workers?|jobs?)\s+(?:are\s+)?(?:active|left|back|outstanding)|"
    r"no open files|safe to (?:unplug|remove|eject)|"
    r"(?:disk|drive|volume)\b.{0,36}\b(?:fine|healthy|ruled out)|"
    r"(?:server|gateway)\b.{0,45}\b(?:absent|not up|down|must restart|needs? (?:a )?restart)|"
    r"(?:doesn't|does not|isn't|is not)\s+run(?:ning)?\b.{0,48}\b(?:server|gateway)|"
    r"(?:privacy prompt|sandbox daemon)\b.{0,45}\b(?:cause|culprit|explains|blocking|stuck)|"
    r"(?:caused by|due to|because of) (?:the |a )?(?:privacy prompt|sandbox daemon))\b"
    r"|^\s*done[.!\s]*$", re.I)
_UNCERTAIN = re.compile(r"^\s*(?:I (?:can't|cannot|couldn't|haven't|have not) confirm\b|"
                        r"I (?:couldn't|wasn't able to|can't) (?:start|launch|run|finish)\b|"
                        r"(?:If|To)\s+)", re.I)
_CLAUSE = re.compile(r"(?<=[.!?])\s+|[;\n]+")


def status_claim(text):
    text = text.replace("’", "'")
    return bool(_CLAIM.search(text) and not _UNCERTAIN.match(text) and not text.rstrip().endswith("?"))


def has_claim(text):
    return any(map(status_claim, _CLAUSE.split(text.strip())))


def guard(text, facts, current=None):
    """Replace untrusted claims with scoped observations, preserving unrelated sentences."""
    replacement = set()
    output = []
    for sentence in _CLAUSE.split(text.strip()):
        if not status_claim(sentence):
            output.append(sentence)
            continue
        if _LANE.search(sentence):
            fact = lane_line(facts)
        elif _CLIMB.search(sentence) and not current:
            fact = answer(sentence, facts)
        else:
            # For a direct ID or whole title we can report that record. Never convert
            # arbitrary action prose to a verified assertion on the strength of a receipt.
            direct = current if re.fullmatch(
                r"(?:I (?:have |just )?|it(?:'s| is) |that(?:'s| is) )?"
                r"(?:started|running|done|finished|kicked (?:it |that )?off)(?: (?:it|that|now))?"
                r"(?:, (?:it is|it's) running(?: now)?)?\.?", sentence, re.I) else None
            fact = answer(sentence, facts, current=direct)
        if fact not in replacement:
            replacement.add(fact)
            output.append(fact)
    return " ".join(output)
