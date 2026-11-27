"""C1-ctx-compress-r9: explicit-update-cue supersession (r9) vs r4 and r7 (frozen before scoring).

  LLAMA_SERVER=.../llama-server python ctx_compress_r9_eval.py --set main    --gguf m.gguf --blocks B,A,B,A,O ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r9_eval.py --set recency --gguf m.gguf --blocks B,A,B,A,O,R ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r9_eval.py --set r8      --gguf m.gguf --blocks B,A,B,A,O,R ...
  python ctx_compress_r9_eval.py --set r8 --dry

Runs the frozen ctx_compress_r2_eval.main() (transport, blocks, deadline, JSON layout unchanged) with arms:
  A = bench/brief_compress_r4.py (r4 byte copy, baseline)
  B = brief_compress.py (production r9: same-slot supersession only on an explicit update cue)
  R = bench/brief_compress_r7.py (r7 byte copy, report-only rival)
  O = full brief (report-only reference)
All via tiers.snapshot (6000 cap), budget 2810, same system prompt and call turns. Sets (all unchanged, reused):
  --set main     the r2 suite (workshop-1..3, 18 planted), scored by context_eval.score
  --set recency  the r6 recency suite (recency-1..3; reused unchanged by r7), scored by ctx_compress_r6_eval.score_recency
  --set r8       the r8 held-out suite (r8-allotment/-cycling/-choir; categories a b c d), score_recency
Synthetic text only.
"""
import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import brief_compress  # noqa: E402
import brief_compress_r4  # noqa: E402
import brief_compress_r7  # noqa: E402
import context_eval  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r6_eval as r6  # noqa: E402
import ctx_compress_r8_eval as r8  # noqa: E402
import tiers  # noqa: E402

BUDGET = r2.BUDGET
R2_SUITE = r8.R2_SUITE  # the unmodified r2 suite, bound at r8 import
SCORE = context_eval.score  # the unmodified main-set scorer
COMPRESS = {"A": lambda b: brief_compress_r4.compress_brief(b, BUDGET),
            "B": lambda b: brief_compress.compress_brief(b, BUDGET),
            "R": lambda b: brief_compress_r7.compress_brief(b, BUDGET),
            "O": lambda b: b}
SETS = {"main": R2_SUITE, "recency": r6.recency_suite, "r8": r8.r8_suite}


def dry(scenarios):
    """Per arm: brief size and, per probe, what the context holds (r8.predict: CURRENT / STALE / BOTH / ABSENT;
    probes with no planted value -> UNPLANTED). Not a score."""
    out = []
    for s in scenarios:
        brief = r2.chat_brief(s)
        row = {"scenario": s.name, "brief_chars": len(brief)}
        for arm in COMPRESS:  # r9 arms directly, never r2.COMPRESS, so the dry result does not depend on install()
            ctx = tiers.snapshot(COMPRESS[arm](brief), context_eval.exchange(s.call))
            row[arm] = {"brief_chars": len(COMPRESS[arm](brief)),
                        "system_chars": len(tiers.SYSTEM.format(tier="fast") + "\n\n" + ctx),
                        "items": {p.key: ("UNPLANTED" if p.current is None else
                                          r8.predict(p.current in ctx, any(v in ctx for v in p.superseded)))
                                  for p in s.probes}}
        out.append(row)
    return out


def install(which):
    r2.COMPRESS = COMPRESS
    r2.dry = dry
    r2.suite = SETS[which]
    r2.context_eval.score = SCORE if which == "main" else r6.score_recency


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=tuple(SETS), required=True)
    o, rest = ap.parse_known_args(argv)
    install(o.set)
    if "--meta" in rest:
        k = rest.index("--meta") + 1
        rest[k] = json.dumps({**json.loads(rest[k]), "r9_set": o.set, "r9_harness": "ctx-compress-r9"})
    sys.argv = [str(HERE / "ctx_compress_r2_eval.py")] + rest
    return r2.main()


if __name__ == "__main__":
    raise SystemExit(main())
