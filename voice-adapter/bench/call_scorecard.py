#!/usr/bin/env python3
"""Print aggregate JSON for real calls and days. See call_scorecard.md for definitions."""
import argparse
from collections import Counter, defaultdict, deque
from dataclasses import dataclass, field
from datetime import date, timedelta
import importlib.util
import json
import math
import os
from pathlib import Path
import re
import sys

DATA = Path.home() / ".local/share/z0live"
STATUS = {f"delegation.{kind}": kind for kind in ("receipt", "progress", "cap", "status")}
PATHS = {"delegation.hermes": "cli", "delegation.api": "api",
         "delegation.chat": "chat", "delegation.memory": "background",
         "delegation.background": "background"}
CLAIM = re.compile(
    r"\b(?:i|we)\s+(?:(?:have|just|already)\s+)*(?:started|launched|kicked off|"
    r"queued|finished|completed|deployed|shipped|created|restarted|set up)\b|"
    r"\b(?:started|launched|kicked off|queued|finished|completed|deployed|shipped|created|restarted)"
    r"\s+(?:the|a|an|both|it)\b|"
    r"\b(?:it|that|both|the (?:job|run|task|batch|build|hill climb))\s+"
    r"(?:is|are|has been|have been)\s+(?:done|complete|running|underway|started|finished|queued)\b"
)
COMPLETED = re.compile(r"\b(?:done|complete|finished|completed|deployed|shipped|created)\b", re.I)
FILLER = set("a an the it this that to i we you me please can could would will do did"
             " is are of for on and in my your just now go ahead again start get".split())


def rows(path):
    try:
        with path.open(encoding="utf-8", errors="replace") as stream:
            for line in stream:
                try:
                    row = json.loads(line)
                except ValueError:
                    row = None
                yield row if isinstance(row, dict) else {}
    except FileNotFoundError:
        return


def words(text):
    return re.findall(r"[a-z0-9]+", str(text or "").casefold().replace("’", "'"))


def similar(a, b):
    if not a or not b:
        return False
    if a == b:
        return True
    left, right = set(a) - FILLER, set(b) - FILLER
    common = left & right
    return len(common) >= 3 and len(common) / len(left | right) >= .65


def possible_repeat(a, b):
    left, right = [token for token in a if token not in FILLER], [token for token in b if token not in FILLER]
    shared = set(left) & set(right)
    return (len(shared) >= 4 and len(shared) / len(set(left) | set(right)) >= .2
            and bool(set(zip(left, left[1:])) & set(zip(right, right[1:]))))


def overlap(source, spoken):
    """Allow the voice to paraphrase while requiring distinctive shared words."""
    if not source or not spoken:
        return 0
    a, b = " ".join(source), " ".join(spoken)
    if f" {a} " in f" {b} ":
        return 1
    if len(source) >= 12 and f" {' '.join(source[:12])} " in f" {b} ":
        return .9
    common = (set(source) & set(spoken)) - FILLER
    return len(common) / max(1, len(set(source) - FILLER)) if len(common) >= 3 else 0


def claims(text):
    normalized = re.sub(r"\b(i|we)'ve\b", r"\1 have", text.casefold().replace("’", "'"))
    normalized = re.sub(r"\b(it|that)'s\b", r"\1 is", normalized)
    for sentence in re.split(r"[.!?;\n]", normalized):
        match = CLAIM.search(sentence)
        if sentence.strip() == "done":
            return match or re.search("done", sentence)
        if match and not re.search(r"\b(?:not|never|if|once|when|could|would)\b",
                                   sentence[:match.start()]):
            return match
    return None


@dataclass
class Score:
    seconds: float = 0
    counts: Counter = field(default_factory=Counter)
    statuses: Counter = field(default_factory=Counter)
    transcripts: Counter = field(default_factory=Counter)
    skipped: Counter = field(default_factory=Counter)
    lanes: Counter = field(default_factory=Counter)
    first: list = field(default_factory=list)
    paths: Counter = field(default_factory=Counter)
    latencies: dict = field(default_factory=lambda: defaultdict(list))

    def add(self, other):
        self.seconds += other.seconds
        for name in ("counts", "statuses", "transcripts", "skipped", "lanes", "paths"):
            getattr(self, name).update(getattr(other, name))
        self.first.extend(other.first)
        for path, values in other.latencies.items():
            self.latencies[path].extend(values)

    def render(self):
        def rate(n, d):
            return round(n / d, 3) if d else None

        def percentiles(values):
            ordered = sorted(values)
            return {"n": len(ordered), **{
                key: round(ordered[math.ceil(q * len(ordered)) - 1], 3) if ordered else None
                for key, q in (("p50", .5), ("p90", .9))}}

        return {
            "minutes": round(self.seconds / 60, 3),
            **{key: self.counts[key] for key in (
                "requests", "requests_without_observed_answer", "repeat_requests_10m",
                "possible_repeat_requests_10m", "caller_turns", "repeat_caller_turns_10m",
                "possible_repeat_caller_turns_10m",
                "tier_skips", "tier_attempts", "lane_cards_spoken", "lane_card_batches_spoken",
                "action_claims", "unproven_action_claims", "requests_with_unproven_claim",
                "spoken_action_claims", "unproven_spoken_action_claims",
                "reaction_repeat_flags", "invalid_trace_lines")},
            "first_spoken_answer_s": percentiles(self.first),
            "tier_skip_rate": rate(self.counts["tier_skips"], self.counts["tier_attempts"]),
            "hermes_paths": {path: {"requests": n, "terminal_s": percentiles(self.latencies[path])}
                             for path, n in sorted(self.paths.items())},
            "status": {kind: {"sent": self.statuses[kind], "transcript_matched": self.transcripts[kind],
                              "sent_per_min": rate(self.statuses[kind] * 60, self.seconds),
                              "spoken_per_min": rate(self.transcripts[kind] * 60, self.seconds)}
                       for kind in ("receipt", "progress", "cap", "status", "total", "deferred", "dropped")},
            "lane_cards_skipped": dict(sorted(self.skipped.items())),
            "lane_outcomes": {key: self.lanes[key] for key in ("evidence", "artifact_only", "died", "empty", "unfinished")},
        }


def lane_outcome(lane, event, root):
    if event["ev"] == "lane.done":
        return "evidence"  # The conductor validated a nonempty card summary.
    if event["ev"] == "lane.stopped":
        return "died"
    if event["ev"] == "lane.start":
        return "unfinished"
    if event.get("rc") not in (0, "0"):
        return "died"
    # A zero-exit job can emit prose or diagnostics but still have no usable card.
    # Older versions could omit a valid last-message file from captured stdout.
    for name in ("last.txt", "out.txt"):
        if not re.fullmatch(r"[\w-]+", lane):
            break
        try:
            with (root / lane / name).open("rb") as stream:
                stream.seek(max(0, os.fstat(stream.fileno()).st_size - 12000))
                tail = stream.read().decode(errors="replace")
        except OSError:
            continue
        for match in re.finditer(r"\{", tail):
            try:
                value, _ = json.JSONDecoder().raw_decode(tail[match.start():])
            except ValueError:
                continue
            if isinstance(value, dict) and value.get("summary"):
                return "artifact_only"
    return "empty"


class Conductor:
    def __init__(self, path):
        self.asks = defaultdict(dict)
        self.lanes = defaultdict(dict)
        self.starts = defaultdict(dict)
        for row in rows(path):
            ev, ask = row.get("ev"), row.get("ask")
            if ev == "ask.received" and row.get("call") and ask:
                self.asks[row["call"]][ask] = row
            elif ev in ("lane.start", "lane.done", "lane.failed", "lane.stopped") and ask and row.get("lane"):
                self.lanes[ask][row["lane"]] = row
                if ev == "lane.start":
                    self.starts[ask][row["lane"]] = row


def ledger_reader(path):
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("z0live_scorecard_ledger", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module.items


def ledger_proof(items, request, request_wall, claim_wall, completed):
    for item in items:
        if item.get("kind") != "action" or not similar(words(request), words(item.get("text"))):
            continue
        opened = item.get("opened", request_wall)
        if not isinstance(opened, (int, float)) or opened > claim_wall or opened < request_wall - 600:
            continue
        if completed:
            candidates = [result for result in item.get("results", []) if result.get("ok")]
        else:
            candidates = [
                proof for proof in item.get("receipts", [])
                if proof.get("ref") and proof.get("kind") in {"pid", "kanban", "cron", "commit", "file"}
            ]
        if any(isinstance(record.get("t"), (int, float)) and opened <= record["t"] <= claim_wall
               for record in candidates):
            return True
    return False


def delivered(rows_, turns, markers, aliases):
    """Yield attributed provider speech using speakable handoffs and turn transcripts."""
    used = set()
    for row in rows_:
        if row.get("ev") not in ("tx", "context.sent"):
            continue
        event = row.get("event") or row
        if event.get("channel") != "speakable":
            continue
        wire_id = event.get("delegation_item_id") or row.get("did")
        did = aliases.get(wire_id, wire_id)
        parts = event.get("texts") or [part.get("text", "") for part in event.get("content", [])]
        text = " ".join(parts).removeprefix('"Agent Final Message":').strip()
        if not text:
            continue
        sent = row["t"]
        key = (did, text, sent)
        if key in used:
            continue
        used.add(key)
        mark = next((kind for when, kind in reversed(markers.get(did, []))
                     if 0 <= sent - when <= .15), "answer")
        source = words(text)
        candidates = [(overlap(source, spoken), start, index)
                      for index, (start, end, spoken, _) in enumerate(turns)
                      if start >= sent - .35 and sent <= end and start <= sent + 45]
        strength, start, index = max(candidates, key=lambda x: (x[0], -x[1]), default=(0, 0, -1))
        if strength >= .48:
            yield did, mark, max(sent, start), index


def count_repeat(score, key, recent, wall, tokens):
    while recent and wall - recent[0][0] > 600:
        recent.popleft()
    prior = [other for previous, other in recent if 0 < wall - previous <= 600]
    if any(similar(tokens, other) for other in prior):
        score.counts[f"repeat_{key}_10m"] += 1
    elif any(possible_repeat(tokens, other) for other in prior):
        score.counts[f"possible_repeat_{key}_10m"] += 1
    recent.append((wall, tokens))


def analyze(path, conductor, lane_root, recent, recent_turns, read_ledger=None):
    stream = iter(rows(path))
    first = next(stream, {})
    if first.get("ev") != "call.created" or first.get("profile") != "tek":
        return None
    score = Score()
    call = path.stem.split("-", 1)[-1]
    call_key = call.rsplit("-", 1)[-1][:12]
    asks = conductor.asks.get(call_key, {})
    ledger_items = (read_ledger(call=call) + read_ledger(call=call_key)) if read_ledger else []
    call_wall = first.get("wall", 0)
    requests, markers, aliases, ask_dids = {}, defaultdict(list), {}, {}
    turns, creations, handoffs, answers, caller_turns = [], {}, [], [], []
    last = first["t"]
    for row in stream:
        if not row:
            score.counts["invalid_trace_lines"] += 1
            continue
        ev, did, t = row.get("ev"), row.get("did"), row.get("t", last)
        last = max(t, last)
        if ev == "call.closed":
            score.seconds = row.get("seconds", t)
        elif ev in ("delegation.start", "lanes.layout"):
            requests.setdefault(did, {"t": t, "wall": row.get("wall", call_wall + t),
                                      "text": row.get("request", "")})
        elif ev in PATHS and did in requests:
            requests[did]["path"] = PATHS[ev]
        elif ev in ("delegation.done", "delegation.failed") and did in requests:
            requests[did]["terminal"] = row.get("s", t - requests[did]["t"])
            if ev == "delegation.done":
                answers.append((did, row.get("answer", ""), row.get("wall", call_wall + t), "generated"))
        elif ev == "lanes.tier":
            errors = row.get("errors") or {}
            score.counts["tier_skips"] += sum(value == "SKIP" for value in errors.values())
            score.counts["tier_attempts"] += len(errors) + bool(row.get("text"))
        elif ev in STATUS:
            kind = STATUS[ev]
            markers[did].append((t, kind))
            score.statuses.update((kind, "total"))
        elif ev in ("delegation.status_deferred", "delegation.status_dropped"):
            score.statuses[ev.rsplit("_", 1)[-1]] += 1
        elif ev == "floor.answer_sent":
            aliases[did] = row.get("of") or did
        elif ev in ("lanes.card_spoken", "lanes.card_skipped"):
            lanes = row.get("lanes", [])
            if ev == "lanes.card_spoken":
                score.counts["lane_cards_spoken"] += len(lanes)
                score.counts["lane_card_batches_spoken"] += 1
            else:
                score.skipped[row.get("reason", "unknown")] += len(lanes)
            if did:
                for ask in [row.get("ask"), *(lane.rsplit("-", 1)[0] for lane in lanes if lane)]:
                    if ask:
                        ask_dids[ask] = did
        elif ev == "user.reaction" and row.get("repeat"):
            score.counts["reaction_repeat_flags"] += 1
        if ev in ("tx", "context.sent") and row.get("type") in (
                "delegation.context.append", "session.context.append"):
            handoffs.append(row)
        if ev == "rx" and row.get("type") in ("turn.created", "turn.done"):
            turn = (row.get("event") or {}).get("turn", {})
            if turn.get("role") == "user" and row["type"] == "turn.done" and turn.get("transcript"):
                caller_turns.append((t, words(turn["transcript"])))
            if turn.get("role") != "assistant":
                continue
            if row["type"] == "turn.created":
                creations[turn.get("id")] = t
            elif turn.get("transcript"):
                start = creations.pop(turn.get("id"), t)
                turns.append((start, t, words(turn["transcript"]), turn["transcript"]))

    score.seconds = score.seconds or last
    for ask, data in asks.items():
        ask_time = data.get("t", 0)
        if not call_wall <= ask_time <= call_wall + last + 2:
            continue
        outcomes = conductor.lanes.get(ask, {})
        for lane, event in outcomes.items():
            score.lanes[lane_outcome(lane, event, lane_root)] += 1
        if ask not in ask_dids:
            candidates = [(abs(req["wall"] - ask_time), did) for did, req in requests.items()
                          if abs(req["wall"] - ask_time) <= 5]
            if candidates:
                ask_dids[ask] = min(candidates)[1]

    spoken_times, spoken_statuses, attributed = {}, set(), {}
    for did, kind, when, turn in delivered(handoffs, turns, markers, aliases):
        if kind != "answer":
            spoken_statuses.add((turn, kind))
        if did in requests and kind == "answer":
            attributed[turn] = did
            spoken_times[did] = min(when, spoken_times.get(did, math.inf))
    for _, kind in spoken_statuses:
        score.transcripts.update((kind, "total"))
    score.counts["caller_turns"] = len(caller_turns)
    for t, tokens in caller_turns:
        count_repeat(score, "caller_turns", recent_turns, call_wall + t, tokens)
    for did, req in sorted(requests.items(), key=lambda pair: pair[1]["wall"]):
        score.counts["requests"] += 1
        nearby = [(abs(t - req["t"]), tokens) for t, tokens in caller_turns if abs(t - req["t"]) <= 1]
        tokens = min(nearby)[1] if nearby else words(req["text"])
        wall = req["wall"]
        count_repeat(score, "requests", recent, wall, tokens)
        route = req.get("path", "unobserved")
        score.paths[route] += 1
        if isinstance(req.get("terminal"), (int, float)):
            score.latencies[route].append(req["terminal"])
        if did in spoken_times:
            score.first.append(max(0, spoken_times[did] - req["t"]))
        else:
            score.counts["requests_without_observed_answer"] += 1

    unproven_requests = set()
    # Track generated and spoken claims separately so paraphrases do not inflate either count.
    for index, (start, end, _, text) in enumerate(turns):
        if claims(text):
            did = attributed.get(index)
            if did is None:
                eligible = [(req["t"], key) for key, req in requests.items()
                            if 0 <= start - req["t"] <= 30]
                did = max(eligible, default=(0, None))[1]
            answers.append((did, text, call_wall + end, "spoken"))
    seen = set()
    for did, answer, wall, source in answers:
        claim = claims(answer)
        if not claim:
            continue
        completed = bool(COMPLETED.search(claim.group()))
        key = (did, tuple(words(answer)), source)
        if key in seen:
            continue
        seen.add(key)
        score.counts["spoken_action_claims" if source == "spoken" else "action_claims"] += 1
        req = requests.get(did, {})
        lane_claim = re.search(r"\b(?:started|launched|queued|kicked off|finished|completed)"
                               r"\s+(?:[\w-]+\s+){0,3}lanes?\b", answer, re.I)
        lane_events = conductor.lanes if completed else conductor.starts
        ran_lane = lane_claim and any(
            ask_did == did and any(event.get("t", math.inf) <= wall
                                   and (not completed or event["ev"] == "lane.done")
                                   for event in lane_events[ask].values())
            for ask, ask_did in ask_dids.items())
        if not ran_lane and not ledger_proof(
                ledger_items, req.get("text"), req.get("wall", 0), wall, completed):
            counter = "unproven_spoken_action_claims" if source == "spoken" else "unproven_action_claims"
            score.counts[counter] += 1
            if did in requests:
                unproven_requests.add(did)
    score.counts["requests_with_unproven_claim"] = len(unproven_requests)
    return score


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("traces", nargs="*", type=Path, help="trace files or directories")
    parser.add_argument("--from", dest="start", type=date.fromisoformat, help="inclusive local trace date")
    parser.add_argument("--through", type=date.fromisoformat, help="inclusive local trace date")
    parser.add_argument("--conductor", type=Path, default=DATA / "conductor/events.jsonl")
    parser.add_argument("--lane-root", type=Path, default=Path.home() / "workspace/sandbox/tek/lanes")
    parser.add_argument("--ledger-module", type=Path, default=Path(__file__).resolve().parents[1] / "ledger.py")
    args = parser.parse_args()
    if args.start and args.through and args.start > args.through:
        parser.error("--from must be on or before --through")
    paths = set()
    for root in args.traces or [DATA / "voice-traces"]:
        if not root.exists():
            parser.error(f"trace path does not exist: {root}")
        paths.update(root.rglob("*.jsonl") if root.is_dir() else [root])
    conductor = Conductor(args.conductor)
    read_ledger = ledger_reader(args.ledger_module)
    recent, recent_turns, days = deque(), deque(), defaultdict(Score)
    count = 0
    day_calls = Counter()
    for path in sorted(paths, key=lambda p: (p.parent.name, p.name)):
        day = path.parent.name
        if (args.start and day < args.start.isoformat()) or (args.through and day > args.through.isoformat()):
            continue
        score = analyze(path, conductor, args.lane_root, recent, recent_turns, read_ledger)
        if score is not None:
            count += 1
            day_calls[day] += 1
            days[day].add(score)
            print(json.dumps({"scope": "call", "day": day, "call": path.name, **score.render()}))
    if args.start and args.through:
        day = args.start
        while day <= args.through:
            days.setdefault(day.isoformat(), Score())
            day += timedelta(days=1)
    for day, score in sorted(days.items()):
        print(json.dumps({"scope": "day", "day": day, "calls": day_calls[day], **score.render()}))
    return 0 if count else 1


if __name__ == "__main__":
    raise SystemExit(main())
