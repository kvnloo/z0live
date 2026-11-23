"""Turn the per-call voice traces written by mobile_live.py into latency/behaviour numbers.

usage: trace_report.py [TRACE.jsonl | DIR ...] [--json OUT] [--turns]
Default input: the whole trace store (HERMES_MOBILE_VOICE_TRACE_DIR or the mobile_live default).

Per user turn (provider `turn.done` role=user):
  eos_to_commit   end of user sound (mic energy) -> provider commits the user turn
  commit_to_audio provider commit -> first assistant audio frame back from the provider
  eos_to_audio    end of user sound -> first assistant audio  (headline "felt" latency, the desktop host-side)
Per delegation: start -> first lane answer, start -> Hermes answer, answers dropped after hang-up.
Barge-ins: user sound starting while assistant audio plays, and how long the assistant kept talking.
Times are measured on the desktop host; phone<->the desktop host network and phone playout buffers are NOT included.
"""
import json
import os
import statistics
import sys
from pathlib import Path

STORE = Path(os.environ.get("HERMES_MOBILE_VOICE_TRACE_DIR",
                            str(Path.home() / ".local/share/z0live/voice-traces")))


def load(path):
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass  # a call killed mid-write leaves one partial line
    return rows


def spans(rows, direction):
    """[(on, off)] sound spans; an unterminated span ends at the last event."""
    out, on = [], None
    for r in rows:
        if r["ev"] == "audio.on" and r.get("dir") == direction:
            on = r["t"]
        elif r["ev"] == "audio.off" and r.get("dir") == direction and on is not None:
            out.append((on, r.get("at", r["t"])))
            on = None
    if on is not None:
        out.append((on, rows[-1]["t"]))
    return out


def analyze(path):
    rows = load(path)
    if not rows:
        return None
    mic, spk = spans(rows, "input"), spans(rows, "output")
    rx = [r for r in rows if r["ev"] == "rx" and r.get("src") == "provider"]
    turns = []
    for r in rx:
        turn = (r.get("event") or {}).get("turn") or {}
        if r.get("type") != "turn.done":
            continue
        commit = r["t"]
        if turn.get("role") == "user":
            ended = [off for on, off in mic if off <= commit + 0.05]
            eos = ended[-1] if ended else None
            audio = next((on for on, _ in spk if on >= (eos if eos is not None else commit) - 0.05), None)
            turns.append({"t": commit, "role": "user", "text": (turn.get("transcript") or "")[:300],
                          "eos_to_commit": round(commit - eos, 3) if eos is not None else None,
                          "commit_to_audio": round(audio - commit, 3) if audio is not None and audio >= commit else None,
                          "eos_to_audio": round(audio - eos, 3) if audio is not None and eos is not None else None})
        elif turn.get("role") == "assistant":
            turns.append({"t": commit, "role": "assistant", "text": (turn.get("transcript") or "")[:300]})
    dels = {}
    for r in rows:
        if r["ev"].startswith("delegation."):
            d = dels.setdefault(r.get("did"), {"did": r.get("did")})
            kind = r["ev"].split(".", 1)[1]
            if kind == "start":
                d.update(t=r["t"], request=r.get("request", "")[:300])
            elif kind == "lane":
                d.update(lane_s=r.get("s"), lane=bool(r.get("text")))
            elif kind == "done":
                d.update(hermes_s=r.get("s"), closed_before_answer=r.get("closed"))
            elif kind == "failed":
                d.update(hermes_s=r.get("s"), failed=r.get("error"))
            elif kind == "dropped_after_close":
                d["dropped"] = True
        elif r["ev"] == "fast_lane.answer":
            dels.setdefault(r.get("did"), {"did": r.get("did")}).update(
                fast_lane_s=round(r["t"] - dels.get(r.get("did"), {}).get("t", r["t"]), 3),
                fast_lane_unhelpful=r.get("unhelpful"))
    barge = []
    for on, _ in mic:
        talking = next(((a, b) for a, b in spk if a < on < b), None)
        if talking:
            barge.append({"t": on, "assistant_kept_talking_s": round(talking[1] - on, 3)})
    first = lambda ev: next((r["t"] for r in rows if r["ev"] == ev), None)  # noqa: E731
    usage = [r.get("event") for r in rx if r.get("type") == "session.usage.updated"]
    closed = next((r for r in rows if r["ev"] == "call.closed"), {})
    return {"trace": str(path), "rev": rows[0].get("rev"), "profile": rows[0].get("profile"),
            "setup_s": next((r.get("setup_s") for r in rows if r["ev"] == "call.negotiated"), None),
            "provider_started_s": next((r["t"] for r in rx if r.get("type") == "session.started"), None),
            "first_assistant_audio_s": spk[0][0] if spk else None, "first_mic_sound_s": mic[0][0] if mic else None,
            "seconds": closed.get("seconds", rows[-1]["t"]), "close_reason": closed.get("reason", "unclosed"),
            "turns": turns, "delegations": list(dels.values()), "barge_ins": barge,
            "errors": [r.get("event") for r in rx if r.get("type") == "error"],
            "usage_last": usage[-1] if usage else None, "first_event": first("call.created")}


def pct(values, q):
    values = sorted(v for v in values if v is not None)
    if not values:
        return None
    return round(values[min(len(values) - 1, int(q * len(values)))], 3)


def summarize(calls):
    user = [t for c in calls for t in c["turns"] if t["role"] == "user"]
    dels = [d for c in calls for d in c["delegations"]]
    out: dict = {"calls": len(calls), "user_turns": len(user), "delegations": len(dels)}
    for key in ("eos_to_commit", "commit_to_audio", "eos_to_audio"):
        vals = [t[key] for t in user if t.get(key) is not None]
        out[key] = {"n": len(vals), "p50": pct(vals, .5), "p90": pct(vals, .9),
                    "mean": round(statistics.mean(vals), 3) if vals else None}
    for key in ("lane_s", "fast_lane_s", "hermes_s"):
        vals = [d[key] for d in dels if d.get(key) is not None]
        out["delegation_" + key] = {"n": len(vals), "p50": pct(vals, .5), "p90": pct(vals, .9)}
    out["delegations_dropped_after_hangup"] = sum(1 for d in dels if d.get("dropped"))
    out["delegations_failed"] = sum(1 for d in dels if d.get("failed"))
    out["barge_ins"] = sum(len(c["barge_ins"]) for c in calls)
    out["setup_s"] = {"p50": pct([c["setup_s"] for c in calls], .5), "p90": pct([c["setup_s"] for c in calls], .9)}
    return out


def main(argv):
    out = argv[argv.index("--json") + 1] if "--json" in argv else None
    args = [a for i, a in enumerate(argv) if not a.startswith("--") and (i == 0 or argv[i - 1] != "--json")]
    paths = []
    for a in args or [str(STORE)]:
        p = Path(a)
        paths += sorted(p.rglob("*.jsonl")) if p.is_dir() else [p]
    calls = [c for c in map(analyze, paths) if c]
    if not calls:
        print("no traces under", args or STORE)
        return 1
    for c in calls:
        user = [t for t in c["turns"] if t["role"] == "user"]
        lat = [t["eos_to_audio"] for t in user if t.get("eos_to_audio") is not None]
        print(f'{Path(c["trace"]).name}  {c["seconds"]}s  {c["close_reason"]}  setup={c["setup_s"]}s  '
              f'user_turns={len(user)}  eos->audio p50={pct(lat, .5)} max={max(lat) if lat else None}  '
              f'delegations={len(c["delegations"])}  barge_ins={len(c["barge_ins"])}  errors={len(c["errors"])}')
        if "--turns" in argv:
            for t in c["turns"]:
                extra = "" if t["role"] != "user" else (f' eos->commit={t["eos_to_commit"]} '
                                                         f'commit->audio={t["commit_to_audio"]} eos->audio={t["eos_to_audio"]}')
                print(f'   {t["t"]:7.2f} {t["role"]:9s}{extra}  {t["text"][:90]!r}')
            for d in c["delegations"]:
                print("   delegation", json.dumps(d, ensure_ascii=False)[:300])
    summary = summarize(calls)
    print(json.dumps(summary, indent=1))
    if out:
        Path(out).write_text(json.dumps({"summary": summary, "calls": calls}, indent=1, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
