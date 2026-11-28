"""C1-ctx-sem-r3: sem-r2 without the static reservation + a SOFT budget rule (trimmed superseding line in the older
line's slot), scored on the blind r15 set AND the blind v2 set (bench/ctx_blind_v2.json).

  LLAMA_SERVER=.../llama-server python ctx_compress_sem_r3_eval.py --set v2  --gguf m.gguf --blocks B,A,B,A ...
  LLAMA_SERVER=.../llama-server python ctx_compress_sem_r3_eval.py --set r15 --gguf m.gguf --blocks B,A,B,A ...
  python ctx_compress_sem_r3_eval.py --set v2 --dry
  python ctx_compress_sem_r3_eval.py --presence
  python ctx_compress_sem_r3_eval.py --groups      (v2 probe index groups per scenario, for the workflow)

Arm B = brief_compress_sem3.compress_brief_sem3. Everything else is the frozen sem-r2 / r15 harness (sets, probes,
scorer, transport, blocks, arrival-then-brief timing):
  A = bench/brief_compress_r4.py (r4 byte copy), B = sem-r3 (live key / judge calls against the block's own
  llama-server, same gguf), O = full brief (main only, report-only).
New set "v2": per blind v2 scenario i the stable brief and call turns of r2 scenario i % 3 (as r15 uses r2 scenario
i), the first statement of every fact (padded to r15's OLD_TARGET), the r8 topic noise, then every later statement
(padded to r15's NEW_TARGET with r15's NEW_NOTE). Probes from the data file, ask + r15's CODE_ASK; scored by
ctx_compress_r6_eval.score_recency (unchanged; whole-code match).
Report-only ablations per B block (no extra model call, presence only): B-nojudge (every collision REPLACE, sem-r3
selection) and B-sem2 (sem-r2's selection with its reservation on the same cache = what sem-r2 would have emitted).
Presence here is a whole-code match (v2 has codes that are prefixes of other codes). Synthetic text only.
"""
import dataclasses
import json
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import brief_compress_r4  # noqa: E402
import brief_compress_sem2 as sem2  # noqa: E402
import brief_compress_sem3 as sem3  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r8_eval as r8  # noqa: E402
import ctx_compress_r15_eval as r15e  # noqa: E402
import lanespeed  # noqa: E402
from context_eval import Probe  # noqa: E402

BUDGET = r15e.BUDGET
HARNESS = "ctx-compress-sem-r3"
URL = f"http://127.0.0.1:{lanespeed.PORT}"
BLIND2 = json.loads((HERE / "ctx_blind_v2.json").read_text(encoding="utf-8"))
V2_NAMES = [s["name"] for s in BLIND2["scenarios"]]
V2_GROUPS = tuple((tuple(range(0, (len(s["probes"]) + 1) // 2)),
                   tuple(range((len(s["probes"]) + 1) // 2, len(s["probes"])))) for s in BLIND2["scenarios"])
BLIND_SETS = ("r15", "v2")
PROBE_SETS = r15e.PROBE_SETS + ("v2",)

_STATE = {"arm": None, "cache": None}
_MEMO = {}


def has(value, text):
    """Whole-code presence (the scorer's own boundary rule): value not inside a longer code."""
    return re.search(r"(?<![\w-])" + re.escape(value.casefold()) + r"(?![\w-])", text.casefold()) is not None


def v2_facts(i):
    sc = BLIND2["scenarios"][i]

    def ex(f, note, target):
        return r8._fit(f["text"] + note, r8._ack(r15e.ACK, f["value"]), target)

    old = sum((ex(f, "", r15e.OLD_TARGET) for f in sc["old"]), ())
    new = sum((ex(f, r15e.NEW_NOTE, r15e.NEW_TARGET) for f in sc["new"]), ())
    probes = tuple(Probe(p["key"], p["ask"] + r15e.CODE_ASK, p["current"], tuple(p["superseded"]))
                   for p in sc["probes"])
    return old, new, probes


def v2_suite():
    base = r15e.R2_SUITE()
    out = []
    for i, sc in enumerate(BLIND2["scenarios"]):
        s = base[i % len(base)]
        old, new, probes = v2_facts(i)
        chat = old + r8.noise(sc["noise"], 2 * r8.NOISE_PAIRS) + new
        out.append(dataclasses.replace(s, name=sc["name"], chat=chat, probes=probes))
    return tuple(out)


r15e.SETS["v2"] = v2_suite
r15e._EXTRA["v2"] = v2_suite


def b_pass(brief, cache, judge):
    """ARRIVAL + BRIEF for one brief (sem-r2's b_pass with the sem-r3 selection); ablations in info['ablation']."""
    arrive, earlier = [], []
    for st in sem2.statements(brief, BUDGET):   # ARRIVAL: one user statement at a time, in conversation order
        rec = cache.arrive(st, earlier)
        earlier.append(st)
        if rec["key_s"] is not None or rec["judge_s"] is not None:
            arrive.append(rec)
    t0 = time.monotonic()
    out, info = sem3.compress_brief_sem3_info(brief, BUDGET, cache, judge)   # BRIEF: warm cache
    info["brief_wall_s"] = round(time.monotonic() - t0, 3)
    info["arrivals"] = arrive
    info["arrival_walls"] = [round((r["key_s"] or 0) + (r["judge_s"] or 0), 3) for r in arrive]
    info["cold_total_s"] = round(sum(info["arrival_walls"]), 3)
    info["judge_in_path_s"] = round(sum(r["judge_s"] or 0 for r in arrive), 3)
    info["cache_errors"] = list(cache.errors)
    always = sem2.KeyCache(judge=lambda o, n: "replace")   # B-nojudge: same keys, every collision is REPLACE
    always.keys = dict(cache.keys)
    nojudge, _ = sem3.compress_brief_sem3_info(brief, BUDGET, always)
    as_sem2, _ = sem2.compress_brief_sem2_info(brief, BUDGET, cache, None)   # B-sem2: reservation, same verdicts
    info["ablation"] = {"nojudge": nojudge, "sem2": as_sem2}
    return out, info


def _compress_b(brief):
    if _STATE["arm"] != "B":
        return sem3.compress_brief_sem3(brief, BUDGET)   # no cache: r4 byte for byte
    if brief not in _MEMO:
        log = []
        _MEMO[brief] = b_pass(brief, _STATE["cache"], sem2.llm_judge(URL, log=log))
        _MEMO[brief][1]["judge_log"] = log
    return _MEMO[brief][0]


COMPRESS = {"A": lambda b: brief_compress_r4.compress_brief(b, BUDGET), "B": _compress_b, "O": lambda b: b}
_ORIG_RUN_BLOCK = r2.run_block
B_FIELDS = ("candidates", "key_misses", "judge_misses", "collisions", "judges", "replaced", "reserved", "trimmed",
            "untrimmed", "fallback", "wall_s", "brief_wall_s", "arrivals", "arrival_walls", "cold_total_s",
            "judge_in_path_s", "cache_errors", "keys")


def _warm():
    try:
        lanespeed.ask("You are a helpful assistant.", "Say ok.")
    except Exception:  # noqa: BLE001 - a failed warm-up is not scored
        pass


def make_run_block(orig):
    def run_block(scenarios, arm, t_start, deadline_s):
        _MEMO.clear()
        _STATE["arm"] = arm if arm == "B" else None
        _STATE["cache"] = sem2.KeyCache(sem2.llm_key1(URL), sem2.llm_judge(URL)) if arm == "B" else None
        try:
            _warm()
            if arm == "B":   # the passes for every scenario of the block run here, timed, before any probe
                for s in scenarios:
                    COMPRESS[arm](r2.chat_brief(s))
            rows, stopped = orig(scenarios, arm, t_start, deadline_s)
            if arm == "B":
                brief = {s.name: r2.chat_brief(s) for s in scenarios}
                cur = {(s.name, p.key): p.current for s in scenarios for p in s.probes}
                for r in rows:
                    out, info = _MEMO[brief[r["scenario"]]]
                    c = cur[(r["scenario"], r["key"])]
                    r["in_brief"] = None if c is None else has(c, out)
                    r["sem"] = {k: info[k] for k in B_FIELDS}
                    r["in_brief_ablation"] = {k: (None if c is None else has(c, v))
                                              for k, v in info["ablation"].items()}
            return rows, stopped
        finally:
            _STATE["arm"] = None
            _STATE["cache"] = None
    return run_block


def presence():
    """Offline presence (A = r4; B without a cache is r4 too; B's real presence comes from the rows). Frozen r15
    presence for the existing sets; whole-code presence for v2."""
    saved = r15e.COMPRESS
    r15e.COMPRESS = COMPRESS
    try:
        out = r15e.presence()
    finally:
        r15e.COMPRESS = saved
    for s in v2_suite():
        brief = r2.chat_brief(s)
        comp = {a: COMPRESS[a](brief) for a in ("A", "B")}
        for p in s.probes:
            out[f"{s.name}/{p.key}"] = {a: has(p.current, comp[a]) for a in ("A", "B")}
    return dict(sorted(out.items()))


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    r15e.COMPRESS = COMPRESS
    if argv == ["--presence"]:
        print(json.dumps(presence(), indent=1))
        return 0
    if argv == ["--groups"]:
        print(json.dumps({"v2": V2_GROUPS, "names": V2_NAMES}))
        return 0
    if len(argv) == 3 and argv[0] == "--group":   # --group <v2 scenario> <vx|vy> -> "0,1,2,3" (workflow)
        print(",".join(str(k) for k in V2_GROUPS[int(argv[1])][{"vx": 0, "vy": 1}[argv[2]]]))
        return 0
    r2.run_block = make_run_block(_ORIG_RUN_BLOCK)
    if "--meta" in argv:
        k = argv.index("--meta") + 1
        argv[k] = json.dumps({**json.loads(argv[k]), "sem_harness": HARNESS})
    return r15e.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
