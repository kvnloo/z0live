"""Status questions answered from the live records, never from a snapshot or a model (N216, 11-28).

The 11-27 23:42 call audit (R2) found 13 false claims; most were status: "zero lanes running" while two ran, "both
hill climbs were stopped" when both had run that evening, "nobody picked it up" about a card a worker had claimed,
"five still running" after one had finished. They came from tool-free tiers reading an old snapshot, background
answers stating outcomes with no proof, and the voice rewording counts and times.

This module is the adapter's one source for status:
- topics(request): which records a status question needs (lanes, climbs, blockers, cards, jobs, latency); [] when the
  request is not a status question.
- read(topics, sources): read those records now (TV conductor state, kanban board, z0live job ledger, cron jobs,
  call rubric). Every fact keeps its id; the snapshot keeps the read time and the set of ids read.
- render(snapshot): the spoken answer, digits and clock times as read, the read time first.
- gate(text, snapshot): a sentence that says something is running, finished, stopped or not picked up stays only
  if it cites an id read in this snapshot and the record agrees; else it becomes CANT_CONFIRM (or the record's own
  line when the cited id says otherwise).
- reworded(source, spoken): numbers and clock times the voice changed when it spoke a status answer.

Stdlib only; every reader is injectable for tests (Sources).
"""
from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

CANT_CONFIRM = "I can't confirm that."
HOME = Path.home()

# Status topics. A request is a status question when it asks (question form) about one of these, not when it asks
# for work ("start the hill climb").
TOPIC = {
    "lanes": re.compile(r"\blanes?\b|\bwhat(?:'s| is| are)\s+(?:still\s+)?(?:running|going|active)\b"
                        r"|\b(?:anything|something|what)\s+(?:still\s+)?running\b|\bworkers?\b", re.I),
    "climbs": re.compile(r"\bhill\s*-?\s*climbs?\b|\bclimbs?\b|\bcron\b|\bscheduled\b", re.I),
    "blockers": re.compile(r"\bblock(?:ed|ers?|ing)\b|\bstuck\b|\bwaiting on\b", re.I),
    "cards": re.compile(r"\bcards?\b|\bkanban\b|\btasks?\b|\bt_[0-9a-f]{6,}\b|\bpicked (?:it |that )?up\b", re.I),
    "jobs": re.compile(r"\bjobs?\b", re.I),
    "latency": re.compile(r"\blatency\b|\bresponse times?\b|\breply times?\b|\bhow (?:fast|slow|long)\b"
                          r"|\bfirst words?\b|\bttfw\b|\bspeed\b", re.I),
}
QUESTION = re.compile(r"\?\s*$|^\W*(?:(?:and|so|okay|ok|well|hey|um|uh|bro|yo)\W+)*(?:what|which|how|when|who|where|why|is|are|was|were|"
                      r"did|does|do|has|have|any|anything|status|tell me|give me|show me|list|check)\b", re.I)
ACTION = re.compile(r"^\W*(?:(?:and|so|okay|ok|please|go ahead and)\W+)*(?:start|kick|run|launch|add|remove|drop|set|"
                    r"stop|kill|restart|deploy|fix|build|make|create|open|close|approve|reject|eject|install)\b", re.I)
LAYOUT = re.compile(r"\b(?:add|remove|drop|set)\b.*\blanes?\b|\blanes? (?:layout|setup)\b|\bhow many lanes (?:do i have|"
                    r"are attached|are on the call)\b", re.I)


def topics(request: str) -> list[str]:
    text = " ".join(str(request or "").split())
    if not text or ACTION.search(text) or LAYOUT.search(text) or not QUESTION.search(text):
        return []
    return [name for name, rx in TOPIC.items() if rx.search(text)]


def profile_home(profile: str = "") -> Path:
    return HOME / ".hermes" / "profiles" / profile if profile and profile != "default" else HOME / ".hermes"


def all_cron_files(root: Path | None = None) -> list:
    """Every profile's cron store: the default profile's and each <root>/profiles/<name>/cron/jobs.json."""
    root = root or HOME / ".hermes"
    return [p for p in [root / "cron" / "jobs.json", *sorted(root.glob("profiles/*/cron/jobs.json"))] if p.exists()]


def board_db_path(env=None, root: Path | None = None) -> Path:
    """The kanban board file `hermes kanban list` reads (hermes_cli.kanban_db.kanban_db_path): HERMES_KANBAN_DB pins
    it; else the board from HERMES_KANBAN_BOARD or <root>/kanban/current; `default` is <root>/kanban.db."""
    env, root = os.environ if env is None else env, root or HOME / ".hermes"
    if env.get("HERMES_KANBAN_DB", "").strip():
        return Path(env["HERMES_KANBAN_DB"].strip()).expanduser()
    slug = env.get("HERMES_KANBAN_BOARD", "").strip()
    if not slug:
        try:
            slug = (root / "kanban" / "current").readlink().name
        except OSError:
            slug = ""
    if not slug or slug == "default":
        return root / "kanban.db"
    return root / "kanban" / "boards" / slug / "kanban.db"


def kanban_db(path: Path | None = None) -> Callable[[], list[dict]]:
    """The board's cards (not archived), read straight from its SQLite file in read-only mode: a status question
    never writes the board (`hermes kanban list` recomputes ready cards first) and needs no CLI."""
    def run() -> list[dict]:
        import sqlite3
        db = path or board_db_path()
        con = sqlite3.connect(db.resolve().as_uri() + "?mode=ro", uri=True, timeout=5)
        try:
            con.row_factory = sqlite3.Row
            rows = con.execute("select id, title, assignee, status, started_at from tasks where status != 'archived' "
                               "order by created_at").fetchall()
        finally:
            con.close()
        return [dict(r) for r in rows]
    return run


def jobs_ledger() -> list[dict]:
    """Open z0live jobs (jobs.py), synced against their executors."""
    import jobs
    return jobs.list_jobs(open_only=True)


@dataclass
class Sources:
    conductor: Path = field(default_factory=lambda: Path(os.environ.get(
        "Z0LIVE_CONDUCTOR", str(HOME / ".local/share/z0live/conductor"))) / "state.json")
    cron: Path | list = field(default_factory=lambda: all_cron_files())
    rubric: Path = field(default_factory=lambda: HOME / ".local/share/z0live/rubric/history.jsonl")
    kanban: Callable[[], list[dict]] = field(default_factory=kanban_db)
    jobs: Callable[[], list[dict]] = jobs_ledger

    @classmethod
    def for_profile(cls, profile: str = "") -> "Sources":
        return cls()  # every profile's cron (climbs live in tek's and poteto's) and the shared board


def _clock(epoch: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(epoch))


def _iso_clock(value: str) -> str:
    """'2026-11-28T02:11:55.25-06:00' -> '02:11' (local wall time as the record wrote it)."""
    m = re.search(r"T(\d\d:\d\d)", str(value or ""))
    return m.group(1) if m else ""


CARD_ID = re.compile(r"\bt_[0-9a-f]{6,}\b")
CLIMB = re.compile(r"hill\s*-?\s*climb|\bclimb\b", re.I)


def read(want, sources: Sources | None = None, now: float | None = None, request: str = "") -> dict:
    """Read the records the topics need, now. Every reader failure is kept (errors) and said, never guessed over.
    The snapshot: read_at, topics, the records per topic, ids (every id read) and state ({id: status as read})."""
    src, now = sources or Sources(), now or time.time()
    want = set(want)
    snap = {"read_at": now, "topics": sorted(want), "ids": set(), "state": {}, "errors": {},
            "named": CARD_ID.findall(request or "")}

    def keep(rows, key="id", status="state"):
        for r in rows:
            snap["ids"].add(str(r.get(key)))
            snap["state"][str(r.get(key))] = str(r.get(status) or "")

    if "lanes" in want:
        try:
            st = json.loads(src.conductor.read_text())
            snap["lanes"] = [dict(ln, ask=a.get("id")) for a in st.get("asks", []) for ln in a.get("lanes", [])
                             if ln.get("state") == "running"]
            keep(snap["lanes"])
        except (OSError, ValueError, AttributeError) as exc:
            snap["errors"]["lanes"] = type(exc).__name__
    if want & {"lanes", "blockers", "cards"}:
        try:
            snap["cards"] = [c for c in src.kanban() if isinstance(c, dict) and c.get("id")]
            keep(snap["cards"], status="status")
        except Exception as exc:  # noqa: BLE001 - an unreadable board is said, not guessed
            snap["errors"]["cards"] = str(exc)[:120] or type(exc).__name__
    if "climbs" in want:
        try:
            jobs = []
            for path in ([src.cron] if isinstance(src.cron, Path) else list(src.cron)):
                jobs += json.loads(path.read_text()).get("jobs", [])
            snap["climbs"] = [j for j in jobs if isinstance(j, dict) and CLIMB.search(str(j.get("name") or ""))]
            keep(snap["climbs"], status="last_status")
        except (OSError, ValueError, AttributeError) as exc:
            snap["errors"]["climbs"] = type(exc).__name__
    if "jobs" in want:
        try:
            snap["jobs"] = [j for j in src.jobs() if isinstance(j, dict) and j.get("id")]
            keep(snap["jobs"])
        except Exception as exc:  # noqa: BLE001
            snap["errors"]["jobs"] = str(exc)[:120] or type(exc).__name__
    if "latency" in want:
        try:
            rows = [json.loads(line) for line in src.rubric.read_text().splitlines() if line.strip()]
            snap["latency"] = max(rows, key=lambda r: str(r.get("at") or "")) if rows else None
        except (OSError, ValueError) as exc:
            snap["errors"]["latency"] = type(exc).__name__
    return snap


# What a sentence claims about the state of work. NEGATIVE first: "hasn't started" also contains "started".
NEGATIVE = re.compile(r"\b(?:has(?:n't| not)|have(?:n't| not)|is(?:n't| not)|are(?:n't| not)|was(?:n't| not)|"
                      r"were(?:n't| not)|did(?:n't| not)|does(?:n't| not)|do(?:n't| not)|never|not)\s+(?:yet\s+|been\s+|even\s+)?(?:start|run|pick|"
                      r"launch|kick|go|finish|complet|done)\w*|\bnot (?:running|started)\b|\bnobody\b|\bno one\b|"
                      r"\bnothing (?:is |was )?(?:running|started|ran)\b|\bstopped\b|\bstalled\b|\bblocked by\b|"
                      r"\bno longer (?:running|scheduled|active)\b|\brunning anymore\b|\bnone (?:of them )?(?:is|are|"
                      r"was|were) (?:running|started|done|finished)\b|"
                      r"\b(?:zero|no|none of the)\s+(?:\w+\s+){0,2}?(?:lanes?|jobs?|cards?|workers?|climbs?|tasks?|"
                      r"agents?|runs?)\b", re.I)
FINISHED = re.compile(r"\b(?:finished|completed|done|ran|succeeded|failed|ended|returned)\b|\bago\b"
                      r"|\blast (?:run|check|checked|round|dispatch)\b", re.I)
# A measured time ("the first result takes 30 to 67 seconds"): stands only on the numbers of the rubric row read now.
MEASURE = re.compile(r"\b\d+(?:\.\d+)?\s*(?:to|-|–)?\s*(?:\d+(?:\.\d+)?\s*)?(?:s|secs?|seconds?|ms|minutes?)\b", re.I)
MEASURE_TOPIC = re.compile(r"\b(?:takes?|took|latency|response|reply|answer|result|first|p50|p90|median|average)\b",
                           re.I)
ALL = ["lanes", "cards", "climbs", "jobs", "latency"]
STARTED = re.compile(r"\b(?:started|kicked(?: it| that| them)? off|launched|picked (?:it |that |them )?up|claimed|"
                     r"dispatched|began|begun)\b", re.I)
RUNNING = re.compile(r"\b(?:running|underway|in progress|going|working on|still on|active)\b", re.I)
RUN_STATES = {"running", "claimed", "in_progress", "started"}
DONE_STATES = {"done", "ok", "completed", "success", "succeeded", "archived", "failed", "error"}


def _claim(sentence: str) -> str:
    """negative | finished | started | running | measure | '' (no claim about the state of work)."""
    for kind, rx in (("negative", NEGATIVE), ("finished", FINISHED), ("started", STARTED), ("running", RUNNING)):
        if rx.search(sentence):
            return kind
    if MEASURE.search(sentence) and MEASURE_TOPIC.search(sentence):
        return "measure"
    return ""


def has_claim(text: str) -> bool:
    return any(_claim(s) for s in re.split(r"(?<=[.!?])\s+", " ".join(str(text or "").split())))


def _measured(sentence: str, snap: dict) -> bool:
    """Every number in a measured-time sentence is one of the rubric row's numbers read in this snapshot."""
    row = snap.get("latency") or {}
    have = {float(v) for v in (row.get("aggregate") or {}).values() if isinstance(v, (int, float))}
    said = {float(n) for n in re.findall(r"\d+(?:\.\d+)?", CLOCK.sub(" ", sentence))}
    return bool(have) and said <= have


def _agrees(kind: str, state: str) -> bool:
    state = state.lower()
    if not state:  # an id proven elsewhere (a PROOF line) without a state read here: the proof stands
        return True
    if kind == "negative":
        return state not in RUN_STATES
    if kind == "finished":
        return state in DONE_STATES
    if kind == "started":
        return state in RUN_STATES | DONE_STATES
    return state in RUN_STATES  # running


def gate(text: str, snap: dict, extra_ids=()) -> tuple[str, list]:
    """Keep a sentence that claims work is running, finished, stopped or not picked up only when it cites an id read
    in this snapshot (or proven this turn: extra_ids) and that record agrees. A cited id whose record disagrees
    becomes the record's own line; a claim citing nothing becomes CANT_CONFIRM. Returns (text, dropped sentences)."""
    known, states = set(snap.get("ids") or ()) | set(extra_ids), snap.get("state") or {}
    clock = _clock(snap.get("read_at") or time.time())
    out, dropped = [], []
    for sentence in re.split(r"(?<=[.!?])\s+", " ".join(str(text or "").split())):
        if not sentence:
            continue
        kind = _claim(sentence)
        if not kind or (kind == "measure" and _measured(sentence, snap)):
            out.append(sentence)
            continue
        cited = [tok for tok in re.findall(r"[\w-]+", sentence) if tok in known]
        if not cited:
            dropped.append(sentence)
            line = CANT_CONFIRM
        else:
            wrong = [i for i in cited if not _agrees(kind, states.get(i, ""))]
            if not wrong:
                out.append(sentence)
                continue
            dropped.append(sentence)
            line = " ".join(f"{i} is {states[i]} as of {clock}." for i in dict.fromkeys(wrong))
        if not out or out[-1] != line:
            out.append(line)
    return " ".join(out), dropped


CLOCK = re.compile(r"\b(\d{1,2}):(\d{2})(?::\d{2})?(?:\s*([ap])\.?m\.?\b)?", re.I)
# A count: digits not glued to a name (gpt-6.2-sol, t_4740, 328934019fc6), optionally with a unit (120m, 340 s).
COUNT = re.compile(r"(?<![\w.-])(\d+(?:\.\d+)?)(?:[smh]\b|(?![\w-]|\.\d))", re.I)
WORDS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve thirteen "
                                    "fourteen fifteen sixteen seventeen eighteen nineteen twenty".split())}
WORDS.update({"thirty": 30, "forty": 40, "fifty": 50, "sixty": 60, "ninety": 90, "single": 1})


def _numbers(text: str) -> set:
    text = str(text or "")
    found = {("clock", int(h) % 12, int(m)) for h, m, _ in CLOCK.findall(text)}
    rest = CLOCK.sub(" ", text)
    found |= {("n", float(n)) for n in COUNT.findall(rest)}
    found |= {("n", float(WORDS[w])) for w in re.findall(r"[a-z]+", rest.lower()) if w in WORDS}
    return found


def reworded(source: str, spoken: str) -> list[str]:
    """Counts and clock times of a status answer the voice dropped or changed: every one in the source must be in
    what it said, and it may say no number the source lacks. [] when it kept them all."""
    want, said = _numbers(source), _numbers(spoken)
    raw = {("n", float(n)) for n in re.findall(r"\d+(?:\.\d+)?", CLOCK.sub(" ", str(source or "")))}
    fmt = (lambda x: f"{x[1]:02d}:{x[2]:02d}" if x[0] == "clock" else f"{x[1]:g}")
    return ([f"missing {fmt(x)}" for x in sorted(want - said)]
            + [f"changed to {fmt(x)}" for x in sorted(said - want - raw)])


def _active(j: dict) -> bool:
    return bool(j.get("enabled", True)) and j.get("state") != "paused"


def _climb_line(j: dict) -> str:
    bits = [str(j.get("schedule_display") or j.get("schedule") or "no schedule")]
    if not j.get("enabled", True) or j.get("state") == "paused":
        bits.append("paused")
    if j.get("last_run_at"):
        bits.append(f"last ran {_iso_clock(j['last_run_at'])}")
    if j.get("last_status"):
        bits.append(str(j["last_status"]))
    if j.get("next_run_at") and j.get("enabled", True):
        bits.append(f"next {_iso_clock(j['next_run_at'])}")
    return f"{j.get('name')} ({j.get('id')}): " + ", ".join(bits) + "."


def _plural(n: int, word: str) -> str:
    return f"{n} {word}{'' if n == 1 else 's'}"


def _card_list(cards, limit=4) -> str:
    return ", ".join(f"{c['id']} ({str(c.get('title') or '')[:60]})" for c in cards[:limit])


def _num(v) -> str:
    return f"{v:g}" if isinstance(v, float) else str(v)


def render(snap: dict) -> str:
    """The spoken status answer: read time first, then each record as read (digits, ids and clock times verbatim)."""
    want, errs = set(snap["topics"]), snap["errors"]
    parts = [f"As of {_clock(snap['read_at'])}:"]
    cards = snap.get("cards")
    if "lanes" in want:
        if "lanes" in snap:
            lanes = snap["lanes"]
            by = {}
            for ln in lanes:
                by[ln.get("model") or "?"] = by.get(ln.get("model") or "?", 0) + 1
            parts.append(f"{_plural(len(lanes), 'TV lane')} running"
                         + (" (" + ", ".join(f"{n} {m}" for m, n in by.items()) + ")." if by else "."))
        else:
            parts.append("I couldn't read the TV lanes just now.")
    if want & {"lanes", "cards", "blockers"} and cards is None:
        parts.append("I couldn't read the kanban board just now.")
    if cards is not None:
        named = snap.get("named") or []
        if named and "cards" in want:
            index = {c["id"]: c for c in cards}
            for cid in named:
                c = index.get(cid)
                parts.append(f"{cid} ({str(c.get('title') or '')[:60]}) is {c.get('status')}"
                             + (f", assigned to {c.get('assignee')}." if c.get("assignee") else ".")
                             if c else f"{cid} is not on the board.")
        elif want & {"lanes", "cards"}:
            running = [c for c in cards if c.get("status") == "running"]
            parts.append(f"{_plural(len(running), 'kanban card')} running"
                         + (f" ({', '.join(c['id'] for c in running[:4])})." if running else "."))
        if "cards" in want and not named:
            counts = {}
            for c in cards:
                counts[c.get("status")] = counts.get(c.get("status"), 0) + 1
            parts.append("On the board: " + ", ".join(f"{counts[s]} {s}" for s in
                                                      ("ready", "todo", "blocked", "review") if counts.get(s)) + "."
                         if any(counts.get(s) for s in ("ready", "todo", "blocked", "review")) else "")
        if "blockers" in want:
            blocked = [c for c in cards if c.get("status") == "blocked"]
            parts.append(f"{_plural(len(blocked), 'kanban card')} blocked"
                         + (f": {_card_list(blocked)}." if blocked else ".")
                         + (f" And {len(blocked) - 4} more." if len(blocked) > 4 else ""))
    if "climbs" in want:
        if "climbs" in snap:
            on = [j for j in snap["climbs"] if _active(j)]
            off = [j for j in snap["climbs"] if not _active(j)]
            parts.append(f"{_plural(len(on), 'hill climb')} scheduled" + (":" if on else "."))
            parts.extend(_climb_line(j) for j in on)
            if off:
                parts.append(f"{len(off)} paused: " + ", ".join(f"{j.get('name')} ({j.get('id')})" for j in off) + ".")
        else:
            parts.append("I couldn't read the cron list just now.")
    if "jobs" in want:
        if "jobs" in snap:
            js = snap["jobs"]
            parts.append(f"{_plural(len(js), 'z0live job')} open" + (":" if js else "."))
            parts.extend(f"{j['id']} ({str(j.get('title') or '')[:60]}) {j.get('state')}, {j.get('age_s')} s old."
                         for j in js[:4])
        else:
            parts.append("I couldn't read the z0live job ledger just now.")
    if "latency" in want:
        row = snap.get("latency")
        if row:
            a = row.get("aggregate") or {}
            parts.append(f"first words p50 {_num(a.get('ttfw_p50_s'))} s, p90 {_num(a.get('ttfw_p90_s'))} s; "
                         f"first answer to a delegated request p50 {_num(a.get('deleg_first_answer_p50_s'))} s, "
                         f"p90 {_num(a.get('deleg_first_answer_p90_s'))} s; {row.get('calls')} calls since "
                         f"{row.get('window_since')}, measured {_iso_clock(row.get('at'))}.")
        else:
            parts.append("I couldn't read the call measurements just now." if "latency" in errs
                         else "No call measurements are recorded yet.")
    return " ".join(p for p in parts if p)
