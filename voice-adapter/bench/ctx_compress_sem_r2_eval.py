"""C1-ctx-sem-r2: cached per-statement keys + collision judge + static-displacement rule on the BLIND r15 set.

  LLAMA_SERVER=.../llama-server python ctx_compress_sem_r2_eval.py --set r15 --gguf m.gguf --blocks B,A,B,A,O,R ...
  python ctx_compress_sem_r2_eval.py --set r15 --dry
  python ctx_compress_sem_r2_eval.py --presence
  LLAMA_SERVER=... python ctx_compress_sem_r2_eval.py --precheck --gguf m.gguf [--sets main,r14,...]  (local, not scored)

Arm B = brief_compress_sem2.compress_brief_sem2 (mechanism bundle, see the PREREG). Everything else is the frozen
r15 / sem-r1 harness (sets, probes, scorer, transport, blocks):
  A = bench/brief_compress_r4.py (r4 byte copy, baseline), B = sem-r2 (live model calls against the block's own
  llama-server, same gguf), R = sem-r1 (brief_compress_sem.py, live batched pass, report-only), O = full brief.
In a B block, after one unscored warm-up request (every arm alike), for every scenario of the block:
  1. ARRIVAL (off the voice path, G8b): every candidate statement of the brief is keyed once, in conversation order,
     into a fresh per-block KeyCache (one request per new statement, timed per statement);
  2. BRIEF (G8a): compress_brief_sem2_info with the warm cache and the live judge, timed around the whole call
     (cache lookups + collision judges + selection).
Report-only ablations from the same block (no extra model call, presence only): B-nojudge (every collision treated as
REPLACE) and B-nodisp (the B judge verdicts, no displacement rule).
Per scored row of a B block: `sem` (pass telemetry) and `in_brief`; R rows: `sem1` and `in_brief`. Synthetic text only.
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import brief_compress_r4  # noqa: E402
import brief_compress_sem as sem1  # noqa: E402
import brief_compress_sem2 as sem2  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r15_eval as r15e  # noqa: E402
import lanespeed  # noqa: E402

BUDGET = r15e.BUDGET
HARNESS = "ctx-compress-sem-r2"
URL = f"http://127.0.0.1:{lanespeed.PORT}"
PROBE_SETS = r15e.PROBE_SETS

_STATE = {"arm": None, "cache": None}   # arm "B" / "R" only inside a model block (run_block below)
_MEMO = {}


def b_pass(brief, cache, judge):
    """ARRIVAL + BRIEF for one brief; returns (out, info) with ablation outputs in info['ablation']."""
    arrive, earlier = [], []
    for st in sem2.statements(brief, BUDGET):   # ARRIVAL: one user statement at a time, in conversation order
        rec = cache.arrive(st, earlier)
        earlier.append(st)
        if rec["key_s"] is not None or rec["judge_s"] is not None:
            arrive.append(rec)
    t0 = time.monotonic()
    out, info = sem2.compress_brief_sem2_info(brief, BUDGET, cache, judge)   # BRIEF: warm cache
    info["brief_wall_s"] = round(time.monotonic() - t0, 3)
    info["arrivals"] = arrive
    info["arrival_walls"] = [round((r["key_s"] or 0) + (r["judge_s"] or 0), 3) for r in arrive]
    info["cold_total_s"] = round(sum(info["arrival_walls"]), 3)
    info["judge_in_path_s"] = round(sum(r["judge_s"] or 0 for r in arrive), 3)
    info["cache_errors"] = list(cache.errors)
    always = sem2.KeyCache(judge=lambda o, n: "replace")   # B-nojudge: same keys, every collision is REPLACE
    always.keys = dict(cache.keys)
    nojudge, _ = sem2.compress_brief_sem2_info(brief, BUDGET, always)
    nodisp, _ = sem2.compress_brief_sem2_info(brief, BUDGET, cache, None, displacement_rule=False)
    info["ablation"] = {"nojudge": nojudge, "nodisp": nodisp}
    return out, info


def _compress_b(brief):
    if _STATE["arm"] != "B":
        return sem2.compress_brief_sem2(brief, BUDGET)   # no cache: r4 byte for byte
    if brief not in _MEMO:
        log = []
        _MEMO[brief] = b_pass(brief, _STATE["cache"], sem2.llm_judge(URL, log=log))   # log: brief-time misses only
        _MEMO[brief][1]["judge_log"] = log
    return _MEMO[brief][0]


def _compress_r(brief):
    if _STATE["arm"] != "R":
        return sem1.compress_brief_sem(brief, BUDGET)   # no keyer: r4 byte for byte
    if brief not in _MEMO:
        log = {}
        out, info = sem1.compress_brief_sem_info(brief, BUDGET, sem1.llm_keyer(URL, log=log))
        _MEMO[brief] = (out, {**info, "server": log.get("timings")})
    return _MEMO[brief][0]


COMPRESS = {"A": lambda b: brief_compress_r4.compress_brief(b, BUDGET), "B": _compress_b, "R": _compress_r,
            "O": lambda b: b}
_ORIG_RUN_BLOCK = r2.run_block
B_FIELDS = ("candidates", "key_misses", "judge_misses", "collisions", "judges", "replaced", "reserved", "fallback",
            "wall_s", "brief_wall_s", "arrivals", "arrival_walls", "cold_total_s", "judge_in_path_s", "cache_errors",
            "keys")


def _warm():
    try:
        lanespeed.ask("You are a helpful assistant.", "Say ok.")
    except Exception:  # noqa: BLE001 - a failed warm-up is not scored
        pass


def make_run_block(orig):
    def run_block(scenarios, arm, t_start, deadline_s):
        _MEMO.clear()
        _STATE["arm"] = arm if arm in ("B", "R") else None
        _STATE["cache"] = sem2.KeyCache(sem2.llm_key1(URL), sem2.llm_judge(URL)) if arm == "B" else None
        try:
            _warm()
            if arm in ("B", "R"):   # the passes for every scenario of the block run here, timed, before any probe
                for s in scenarios:
                    COMPRESS[arm](r2.chat_brief(s))
            rows, stopped = orig(scenarios, arm, t_start, deadline_s)
            if arm in ("B", "R"):
                brief = {s.name: r2.chat_brief(s) for s in scenarios}
                cur = {(s.name, p.key): p.current for s in scenarios for p in s.probes}
                for r in rows:
                    out, info = _MEMO[brief[r["scenario"]]]
                    c = cur[(r["scenario"], r["key"])]
                    r["in_brief"] = None if c is None else c in out
                    if arm == "B":
                        r["sem"] = {k: info[k] for k in B_FIELDS}
                        r["in_brief_ablation"] = {k: (None if c is None else c in v)
                                                  for k, v in info["ablation"].items()}
                    else:
                        r["sem1"] = {k: info[k] for k in ("candidates", "grouped", "fallback", "wall_s", "server")}
            return rows, stopped
        finally:
            _STATE["arm"] = None
            _STATE["cache"] = None
    return run_block


def presence():
    """Offline presence: A is r4; B without a cache is r4 too, so B's real presence comes from the rows (in_brief)."""
    saved = r15e.COMPRESS
    r15e.COMPRESS = COMPRESS
    try:
        return r15e.presence()
    finally:
        r15e.COMPRESS = saved


def precheck_model(gguf, server, sets=("main",) + PROBE_SETS):
    """Local model precheck (not scored): one llama-server; per brief the B pass (arrival + brief), what it drops,
    which planted values A / B / ablations hold, and the STOP check (a dev old fact r4 holds and B drops)."""
    with open(os.devnull, "w") as log:
        proc, _ = lanespeed.serve(server, gguf, 512, log)
    out = {"briefs": {}, "stop": []}
    try:
        _warm()
        for which in sets:
            for s in r15e.SETS[which]():
                jlog = []
                cache = sem2.KeyCache(sem2.llm_key1(URL), sem2.llm_judge(URL, log=jlog))
                brief = r2.chat_brief(s)
                a = COMPRESS["A"](brief)
                b, info = b_pass(brief, cache, sem2.llm_judge(URL, log=jlog))
                row = {"chars": {"A": len(a), "B": len(b)}, "B_eq_A": a == b,
                       "sem": {k: info[k] for k in B_FIELDS}, "judge_replies": [x["reply"] for x in jlog],
                       "probes": {}}
                for p in s.probes:
                    if p.current is None:
                        continue
                    row["probes"][p.key] = {
                        "current": {"A": p.current in a, "B": p.current in b,
                                    **{k: p.current in v for k, v in info["ablation"].items()}},
                        "superseded_in": {"A": any(v in a for v in p.superseded),
                                          "B": any(v in b for v in p.superseded)}}
                    if which != "r15" and p.key.endswith(("_old",)) and p.current in a and p.current not in b:
                        out["stop"].append(f"{which}/{s.name}/{p.key}")
                out["briefs"][f"{which}/{s.name}"] = row
    finally:
        lanespeed.stop(proc)
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    r15e.COMPRESS = COMPRESS
    if argv == ["--presence"]:
        print(json.dumps(presence(), indent=1))
        return 0
    if "--precheck" in argv:
        k = argv.index("--gguf") + 1
        sets = tuple(argv[argv.index("--sets") + 1].split(",")) if "--sets" in argv else ("main",) + PROBE_SETS
        print(json.dumps(precheck_model(Path(argv[k]), os.environ["LLAMA_SERVER"], sets), indent=1))
        return 0
    r2.run_block = make_run_block(_ORIG_RUN_BLOCK)
    if "--meta" in argv:
        k = argv.index("--meta") + 1
        argv[k] = json.dumps({**json.loads(argv[k]), "sem_harness": HARNESS})
    return r15e.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
