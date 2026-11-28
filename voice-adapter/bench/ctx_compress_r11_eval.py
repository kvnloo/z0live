"""C1-ctx-compress-r11: attribute-aware supersession (r11) vs r4, r9 rival (frozen before scoring).

  LLAMA_SERVER=.../llama-server python ctx_compress_r11_eval.py --set main    --gguf m.gguf --blocks B,A,B,A,O ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r11_eval.py --set r10     --gguf m.gguf --blocks B,A,B,A,O,R ...
  python ctx_compress_r11_eval.py --set r10 --dry
  python ctx_compress_r11_eval.py --set r10 --presence
  python ctx_compress_r11_eval.py --precheck

Harness only. Runs the frozen ctx_compress_r10_eval (sets main / recency / r8 / r10, transport, blocks, JSON layout,
presence() unchanged; meta still carries r10_set) with the r11 arms:
  A = bench/brief_compress_r4.py (r4 byte copy, baseline), B = brief_compress.py (production r11),
  R = bench/brief_compress_r9.py (r9 byte copy, report-only rival), O = full brief (report-only).
--precheck: deterministic, no model. For every main / recency / r8 / r10 scenario, which planted values (probe.current
and probe.superseded) are substrings of the r4, r9 and r11 compressed briefs at BUDGET, plus brief sizes.
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
import brief_compress_r9  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r10_eval as r10e  # noqa: E402

BUDGET = r10e.BUDGET
COMPRESS = {"A": lambda b: brief_compress_r4.compress_brief(b, BUDGET),
            "B": lambda b: brief_compress.compress_brief(b, BUDGET),
            "R": lambda b: brief_compress_r9.compress_brief(b, BUDGET),
            "O": lambda b: b}
PRECHECK_ARMS = {"r4": "A", "r9": "R", "r11": "B"}


class _arms:
    """r10e.dry / presence / install read the module global r10e.COMPRESS; set it to the r11 arms for the call and
    restore it after, so importing this module never changes the r10 harness."""
    def __enter__(self):
        self.saved = r10e.COMPRESS
        r10e.COMPRESS = COMPRESS

    def __exit__(self, *exc):
        r10e.COMPRESS = self.saved


def presence(arms=r10e.PRESENCE_ARMS):
    """r10 presence() (frozen) over the r11 arms: A = r4, B = r11."""
    with _arms():
        return r10e.presence(arms)


def dry(scenarios):
    with _arms():
        return r10e.dry(scenarios)


def precheck():
    out = {}
    for which in ("main", "recency", "r8", "r10"):
        for s in r10e.SETS[which]():
            brief = r2.chat_brief(s)
            comp = {name: COMPRESS[arm](brief) for name, arm in PRECHECK_ARMS.items()}
            row = {"full_chars": len(brief), "chars": {n: len(c) for n, c in comp.items()}, "probes": {}}
            for p in s.probes:
                if p.current is None:
                    continue
                vals = {"current": p.current, **{f"old{i}": v for i, v in enumerate(p.superseded)}}
                row["probes"][p.key] = {role: {"value": v, **{n: v in c for n, c in comp.items()}}
                                        for role, v in vals.items()}
            out[f"{which}/{s.name}"] = row
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--precheck"]:
        print(json.dumps(precheck(), indent=1))
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=tuple(r10e.SETS), required=True)
    _, rest = ap.parse_known_args(argv)
    if "--meta" in rest:
        k = argv.index("--meta") + 1
        argv[k] = json.dumps({**json.loads(argv[k]), "r11_harness": "ctx-compress-r11"})
    r10e.COMPRESS = COMPRESS  # process-wide for the run (install() binds r2.COMPRESS = r10e.COMPRESS; dry reads it)
    return r10e.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
