"""C1-ctx-sem-r7 JUDGE-CAPACITY ORACLE (not scored). The ONLY change vs the sem-r5 old-prompt arm is the model: the collision
judge is gemma-4-E4B-it Q4_K_M instead of gemma-4-E2B-it Q4_K_M. Prompt = sem-r2 JUDGE_PROMPT, greedy temp 0 seed 42,
max 4 tokens (sem2._post), raw reply parse (sem2.parse_judge), same llama-server flags (lanespeed.serve), same 198 pairs.
Two passes (p1, p2; p2 is the A/A control). Per-request wall time is recorded. Pairs are split by --shard i/n
(deterministic: pair index % n == i) only if one job cannot finish; the default is one shard.

  LLAMA_SERVER=.../llama-server python ctx_compress_sem_r7_oracle.py --gguf m.gguf --out oracle.json [--shard 0/1]
  python ctx_compress_sem_r7_oracle.py --summarize oracle.json [more shard files]   (no model: recompute the summary)

Counts and stop rule are frozen in ctx-compress-sem-r7-oracle-PREREG.md. Synthetic text only.
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import brief_compress_sem2 as sem2  # noqa: E402

PAIRS = HERE / "ctx_compress_sem_r5_pairs.json"            # the r5 file, unchanged (same sha256)
BASE = HERE / "ctx_compress_sem_r7_e2b_old.json"           # E2B old-prompt verdicts on all 198 pairs (sem-r5 old1 = old2 = sem-r6 p1)
DEV_SETS = ("main", "recency", "r8", "r12", "r13", "r14")
BLIND_SETS = ("r15", "v2")


def judge_one(url, older, newer):
    t0 = time.monotonic()
    reply = sem2._post(url, sem2.JUDGE_PROMPT.format(old=sem2.norm_statement(older), new=sem2.norm_statement(newer)), 4, 120.0, None)
    return {"reply": reply, "verdict": sem2.parse_judge(reply), "wall_s": round(time.monotonic() - t0, 4)}


def pct(xs, q):
    xs = sorted(xs)
    if not xs:
        return None
    k = (len(xs) - 1) * q
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def summarize(pairs, rows, base):
    """pairs: pair dicts; rows: {id: {"p1": {...}, "p2": {...}}}; base: {id: E2B old verdict}. Pure function."""
    v = lambda p, a: rows[p["id"]][a]["verdict"]  # noqa: E731
    e2 = lambda p: base[p["id"]]  # noqa: E731
    clear = lambda p: p["gold"] is not None and not p["ambiguous"]  # noqa: E731
    out = {"n_pairs": len(pairs)}
    blind = [p for p in pairs if p["set"] in BLIND_SETS]
    ups_all = [p for p in pairs if clear(p) and p["gold"] == "replace"]
    sts_all = [p for p in pairs if clear(p) and p["gold"] == "also"]
    ups_b = [p for p in blind if clear(p) and p["gold"] == "replace"]
    noise = [p for p in pairs if p["class"] == "noise"]
    out["blind_clear_false_also_e2b"] = [p["id"] for p in ups_b if p["sem3"] == "also"]
    out["fixed"] = [p["id"] for p in ups_b if p["sem3"] == "also" and v(p, "p1") == "replace"]
    out["new_false_replace_blind"] = [p["id"] for p in sts_all if p["set"] in BLIND_SETS and v(p, "p1") == "replace" and e2(p) != "replace"]
    out["new_false_replace_dev"] = [p["id"] for p in sts_all if p["set"] in DEV_SETS and v(p, "p1") == "replace" and e2(p) != "replace"]
    out["still_true_replace_e4b"] = [p["id"] for p in sts_all if v(p, "p1") == "replace"]
    out["still_true_replace_e2b"] = [p["id"] for p in sts_all if e2(p) == "replace"]
    out["noise_n"] = len(noise)
    out["noise_replace_e4b"] = [p["id"] for p in noise if v(p, "p1") == "replace"]
    out["noise_replace_e2b"] = [p["id"] for p in noise if e2(p) == "replace"]
    out["updates_replace_e4b"] = sum(v(p, "p1") == "replace" for p in ups_all)
    out["updates_replace_e2b"] = sum(e2(p) == "replace" for p in ups_all)
    out["update_n"], out["still_true_n"] = len(ups_all), len(sts_all)
    out["replace_by_set_e4b"] = {s: sum(v(p, "p1") == "replace" for p in pairs if p["set"] == s) for s in DEV_SETS + BLIND_SETS}
    out["replace_by_set_e2b"] = {s: sum(e2(p) == "replace" for p in pairs if p["set"] == s) for s in DEV_SETS + BLIND_SETS}
    out["ambiguous"] = [{"id": p["id"], "e2b": e2(p), "e4b": v(p, "p1")} for p in pairs if p["ambiguous"]]
    out["targets"] = [{"id": p["id"], "set": p["set"], "scenario": p.get("scenario"), "e4b_reply": rows[p["id"]]["p1"]["reply"],
                       "e4b": v(p, "p1")} for p in ups_b if p["sem3"] == "also"]
    out["aa_verdict_flips"] = [p["id"] for p in pairs if v(p, "p1") != v(p, "p2")]
    out["aa_reply_flips"] = [p["id"] for p in pairs if rows[p["id"]]["p1"]["reply"] != rows[p["id"]]["p2"]["reply"]]
    out["unparsable_e4b"] = [p["id"] for p in pairs if v(p, "p1") is None]
    out["e4b_vs_e2b_changed"] = [p["id"] for p in pairs if v(p, "p1") != e2(p)]
    w = [rows[p["id"]][a]["wall_s"] for p in pairs for a in ("p1", "p2") if "wall_s" in rows[p["id"]][a]]
    out["wall_s"] = {"n": len(w), "p50": pct(w, 0.5), "p95": pct(w, 0.95), "mean": (sum(w) / len(w)) if w else None}
    ok = (len(out["fixed"]) >= 1 and not out["new_false_replace_blind"]
          and len(out["noise_replace_e4b"]) <= len(out["noise_replace_e2b"]) and not out["aa_verdict_flips"])
    out["proceed_to_scored_run"] = bool(ok)
    out["outcome"] = "PASS(oracle)" if ok else "NULL(oracle-stop)"
    out["stop_rule"] = ("PASS iff blind clear false-ALSO fixed >= 1 AND blind new false-REPLACE == 0 AND noise REPLACE (122) "
                        "<= E2B old noise REPLACE AND A/A verdict flips == 0")
    return out


def load_rows(paths):
    rows = {}
    for pth in paths:
        d = json.loads(Path(pth).read_text())
        for k, r in d["rows"].items():
            rows[int(k)] = r
    return rows


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    pairs = json.loads(PAIRS.read_text(encoding="utf-8"))["pairs"]
    base = {int(k): x for k, x in json.loads(BASE.read_text())["verdicts"].items()}
    if argv[:1] == ["--check-verdict"]:      # CI: oracle summary must equal the independent verdict script
        s, w = json.loads(Path(argv[1]).read_text()), json.loads(Path(argv[2]).read_text())
        for k in ("fixed", "new_false_replace_blind", "aa_verdict_flips", "noise_replace_e4b", "noise_replace_e2b", "outcome"):
            assert s[k] == w[k], (k, s[k], w[k])
        print("summary == independent verdict:", w["outcome"])
        return 0
    if argv[:1] == ["--summarize"]:
        print(json.dumps(summarize(pairs, load_rows(argv[1:]), base), indent=1))
        return 0
    gguf = argv[argv.index("--gguf") + 1]
    out_path = argv[argv.index("--out") + 1]
    shard = argv[argv.index("--shard") + 1] if "--shard" in argv else "0/1"
    si, sn = (int(x) for x in shard.split("/"))
    mine = [p for i, p in enumerate(pairs) if i % sn == si]
    server = os.environ["LLAMA_SERVER"]
    import lanespeed
    url = f"http://127.0.0.1:{lanespeed.PORT}"
    log = open(out_path + ".server.log", "w")
    proc, cmd = lanespeed.serve(server, gguf, 512, log)
    rows, t0 = {}, time.monotonic()
    try:
        try:
            lanespeed.ask("You are a helpful assistant.", "Say ok.")   # warm-up, not scored
        except Exception:  # noqa: BLE001
            pass
        for arm in ("p1", "p2"):
            for p in mine:
                rows.setdefault(p["id"], {})[arm] = judge_one(url, p["older"], p["newer"])
    finally:
        lanespeed.stop(proc)
    res = {"harness": "ctx-compress-sem-r7-oracle", "shard": shard, "cmd": cmd, "wall_s": round(time.monotonic() - t0, 1),
           "gguf_sha256": lanespeed.sha256(gguf),
           "meta": json.loads(argv[argv.index("--meta") + 1]) if "--meta" in argv else {}, "rows": rows}
    if sn == 1:
        res["summary"] = summarize(pairs, rows, base)
        print(json.dumps(res["summary"], indent=1))
    Path(out_path).write_text(json.dumps(res, indent=1), encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
