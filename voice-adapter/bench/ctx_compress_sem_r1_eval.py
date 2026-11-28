"""C1-ctx-sem-r1: semantic (model-keyed) supersession in compress_brief on the BLIND r15 set (frozen before scoring).

  LLAMA_SERVER=.../llama-server python ctx_compress_sem_r1_eval.py --set main --gguf m.gguf --blocks B,A,B,A,O ...
  LLAMA_SERVER=.../llama-server python ctx_compress_sem_r1_eval.py --set r15  --gguf m.gguf --blocks B,A,B,A,O,R ...
  LLAMA_SERVER=.../llama-server python ctx_compress_sem_r1_eval.py --set r14  --gguf m.gguf --blocks B,A,B,A ...
  python ctx_compress_sem_r1_eval.py --set r15 --dry
  python ctx_compress_sem_r1_eval.py --presence
  LLAMA_SERVER=... python ctx_compress_sem_r1_eval.py --precheck --gguf m.gguf   (local model precheck, see PREREG)

ONE change: arm B = brief_compress_sem.compress_brief_sem (r4 order + one model pass that keys every value-bearing
user statement; statements sharing a key keep only the newest). Everything else is r15's frozen harness (sets, probes,
scorer, transport, blocks), re-used unchanged:
  A = bench/brief_compress_r4.py (r4 byte copy, baseline), B = sem-r1 (live model pass against the block's own
  llama-server, same gguf), R = brief_compress.py (r14, report-only), O = full brief (report-only).
The pass runs inside run_block, before the first scored probe of each scenario, against the block's fresh server
(after one unscored 1-token warm-up request, for every arm alike). Outside a block (dry, presence, precheck without
a server) B has no keyer and is r4 byte for byte. Per scored row of a B block the harness records `sem` (the pass
telemetry of the row's scenario: candidates, grouped, fallback, wall_s, server timings, keys) and `in_brief` (the
planted value is a substring of the compressed brief that block really used); the verdict takes B's presence from it.
Synthetic text only.
"""
import json
import os
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import brief_compress  # noqa: E402
import brief_compress_r4  # noqa: E402
import brief_compress_sem as sem  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r10_eval as r10e  # noqa: E402
import ctx_compress_r15_eval as r15e  # noqa: E402
import lanespeed  # noqa: E402

BUDGET = r15e.BUDGET
HARNESS = "ctx-compress-sem-r1"
URL = f"http://127.0.0.1:{lanespeed.PORT}"
SET_NAMES = tuple(r15e.SETS)
PROBE_SETS = r15e.PROBE_SETS

_STATE = {"keyer": None, "log": {}}   # keyer is set only inside a model block (run_block below)
_MEMO = {}


def _compress_sem(brief):
    """B: sem-r1 with the live keyer when inside a block, else r4 byte for byte. One pass per distinct brief per block."""
    keyer = _STATE["keyer"]
    if keyer is None:
        return sem.compress_brief_sem(brief, BUDGET)
    if brief not in _MEMO:
        server_log = {}
        out, info = sem.compress_brief_sem_info(brief, BUDGET, sem.llm_keyer(URL, log=server_log))
        _MEMO[brief] = (out, {**info, "server": server_log.get("timings"), "reply": server_log.get("reply")})
    return _MEMO[brief][0]


COMPRESS = {"A": lambda b: brief_compress_r4.compress_brief(b, BUDGET),
            "B": _compress_sem,
            "R": lambda b: brief_compress.compress_brief(b, BUDGET),
            "O": lambda b: b}
PRECHECK = {"r4": brief_compress_r4, "r14": brief_compress}
_ORIG_RUN_BLOCK = r2.run_block   # r15e.presence / dry / main read the module global r15e.COMPRESS: set it only in main / presence


def _warm():
    try:
        lanespeed.ask("You are a helpful assistant.", "Say ok.")
    except Exception:  # noqa: BLE001 - a failed warm-up is not scored
        pass


def make_run_block(orig):
    def run_block(scenarios, arm, t_start, deadline_s):
        _MEMO.clear()
        _STATE["keyer"] = sem.llm_keyer(URL) if arm == "B" else None
        try:
            _warm()
            if arm == "B":   # the pass for every scenario of the block runs here, timed, before any probe
                for s in scenarios:
                    COMPRESS["B"](r2.chat_brief(s))
            rows, stopped = orig(scenarios, arm, t_start, deadline_s)
            if arm == "B":
                brief = {s.name: r2.chat_brief(s) for s in scenarios}
                cur = {(s.name, p.key): p.current for s in scenarios for p in s.probes}
                for r in rows:
                    out, info = _MEMO[brief[r["scenario"]]]
                    r["sem"] = {k: info[k] for k in ("candidates", "grouped", "fallback", "wall_s", "server", "keys")}
                    r["in_brief"] = None if cur[(r["scenario"], r["key"])] is None else cur[(r["scenario"], r["key"])] in out
            return rows, stopped
        finally:
            _STATE["keyer"] = None
    return run_block


def presence():
    """Offline presence: A is r4; B without a keyer is r4 too, so B's real presence comes from the rows (in_brief)."""
    saved = r15e.COMPRESS
    r15e.COMPRESS = COMPRESS
    try:
        return r15e.presence()
    finally:
        r15e.COMPRESS = saved


def precheck_model(gguf, server, sets=("main",) + PROBE_SETS):
    """Local model precheck (not scored): start one llama-server, run the semantic pass on every brief of every set, and
    record per brief what B groups, which planted values A and B hold, and for every probe whether B drops a value
    that r4 holds (false merge) or holds a superseded one."""
    ub = 512
    with open(os.devnull, "w") as log:
        proc, _ = lanespeed.serve(server, gguf, ub, log)
    out = {"briefs": {}, "stop": []}
    try:
        _STATE["keyer"] = sem.llm_keyer(URL)
        _warm()
        for which in sets:
            for s in r15e.SETS[which]():
                brief = r2.chat_brief(s)
                a = COMPRESS["A"](brief)
                b = COMPRESS["B"](brief)
                info = _MEMO[brief][1]
                row = {"chars": {"A": len(a), "B": len(b)}, "B_eq_A": a == b,
                       "sem": {k: info[k] for k in ("candidates", "grouped", "fallback", "wall_s", "server", "keys", "reply")},
                       "probes": {}}
                for p in s.probes:
                    if p.current is None:
                        continue
                    row["probes"][p.key] = {"current": [p.current in a, p.current in b],
                                            "superseded": [[v in a, v in b] for v in p.superseded]}
                out["briefs"][f"{which}/{s.name}"] = row
    finally:
        _STATE["keyer"] = None
        lanespeed.stop(proc)
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    r15e.COMPRESS = COMPRESS   # process-wide for this script run (r15e.main -> r10e.main read it)
    if argv == ["--presence"]:
        print(json.dumps(presence(), indent=1))
        return 0
    if "--precheck" in argv:
        k = argv.index("--gguf") + 1
        res = precheck_model(Path(argv[k]), os.environ["LLAMA_SERVER"])
        print(json.dumps(res, indent=1))
        return 0
    # r15e.main -> r10e.main -> r2.main: the arms come from r15e.COMPRESS, the harness meta gets r15_harness (kept) + ours
    r2.run_block = make_run_block(_ORIG_RUN_BLOCK)
    if "--meta" in argv:
        k = argv.index("--meta") + 1
        argv[k] = json.dumps({**json.loads(argv[k]), "sem_harness": HARNESS})
    return r15e.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
