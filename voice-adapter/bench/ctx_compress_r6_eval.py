"""C1-ctx-compress-r6: r2 harness with r6 arms, plus a recency probe set (frozen before scoring).

  LLAMA_SERVER=.../llama-server python ctx_compress_r6_eval.py --set main    --gguf m.gguf --blocks B,A,B,A,O ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r6_eval.py --set recency --gguf m.gguf --blocks B,A,B,A,O ...
  python ctx_compress_r6_eval.py --set recency --dry

Runs the frozen ctx_compress_r2_eval.main() (transport, blocks, deadline, JSON layout unchanged) with:
  arms  A = bench/brief_compress_r4.py (byte copy of the r4/r5 production compressor, baseline)
        B = brief_compress.py (production, r6 tiered fact key)
        O = full brief (no compression), report-only reference
  --set main     the r2 suite (3 workshop scenarios x 8 probes, 18 planted), scored by context_eval.score (unchanged)
  --set recency  recency_suite(): the r2 scenarios with a chat noise cut (noise(18+2i,6) -> noise(10+2i,6), so the
                 full brief stays under tiers.snapshot's 6000 cap) and five planted fact exchanges per scenario:
                   two supersede pairs (bench, badge): the OLD value early in the chat, the NEW value as the newest
                   chat exchanges, NEW 2-9% longer than OLD (inside the 10% band), both inside the noise size range
                   edges (OLD shorter than every noise exchange, NEW longer), so r4's shortest-first order reaches
                   OLD first and NEW last;
                   one old unique fact (glaze), never superseded, about OLD's size (report-only control for r6's
                   mirror risk: newest-first inside the band can drop an old unique fact).
                 3 probes per scenario, scored by score_recency (below).
Synthetic text only.
"""
import argparse
import dataclasses
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import brief_compress  # noqa: E402
import brief_compress_r4  # noqa: E402
import context_eval  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import tiers  # noqa: E402
from context_eval import Probe, Turn  # noqa: E402
from ctx_compress_r5_gates import ABSTAIN  # noqa: E402

BUDGET = r2.BUDGET
COMPRESS = {"A": lambda b: brief_compress_r4.compress_brief(b, BUDGET),
            "B": lambda b: brief_compress.compress_brief(b, BUDGET),
            "O": lambda b: b}
SUPERSEDE_KEYS = ("bench", "badge")
UNIQUE_KEYS = ("glaze",)
RECENCY_NAMES = ["recency-1", "recency-2", "recency-3"]
ACK = ("Noted. I will keep that with your workshop notes so it is easy to find later, "
       "and I can repeat it whenever you ask while the display is being assembled.")
NOTE = (" I wrote it on the sign-in sheet by the front door, next to the big box of clay samples, so the staff know"
        " which one is mine for today.")


def _pair(user, reply):
    return (Turn("user", user), Turn("assistant", reply))


def recency_facts(i):
    bench_old, bench_new = f"Fern-{21 + i}", f"Moss-{48 + i}"
    badge_old, badge_new = f"Teal-{13 + i}", f"Plum-{64 + i}"
    glaze = f"Opal-{82 + i}"
    old = (_pair(f"My workshop bench number is {bench_old}." + NOTE, f"Bench {bench_old}, got it. " + ACK)
           + _pair(f"My badge colour code is {badge_old}." + NOTE, f"Badge code {badge_old}, got it. " + ACK)
           + _pair(f"My glaze batch code is {glaze}." + NOTE, f"Glaze batch {glaze}, got it. " + ACK))
    new = (_pair(f"Update: my workshop bench number is now {bench_new}, not the old one." + NOTE,
                 f"Bench {bench_new}, got it. " + ACK)
           + _pair(f"Update: my badge colour code is now {badge_new}, not the old one." + NOTE,
                   f"Badge code {badge_new}, got it. " + ACK))
    code = " Please give just the code."
    probes = (Probe("bench", "What is my workshop bench number now?" + code, bench_new, (bench_old,)),
              Probe("badge", "What is my badge colour code now?" + code, badge_new, (badge_old,)),
              Probe("glaze", "What is my glaze batch code?" + code, glaze))
    return old, new, probes


def recency_suite():
    out = []
    for i, s in enumerate(context_eval.suite()):
        c = s.chat
        old, new, probes = recency_facts(i)
        chat = c[:2] + old + context_eval.noise(6) + c[14:16] + context_eval.noise(10 + 2 * i, 6) + new
        out.append(dataclasses.replace(s, name=RECENCY_NAMES[i], chat=chat, probes=probes))
    return tuple(out)


CODE = re.compile(r"\b[A-Z][a-z]+-\d+\b")


def _has(value, reply):
    return re.search(r"(?<![\w-])" + re.escape(value.casefold()) + r"(?![\w-])", reply.casefold()) is not None


def score_recency(probe, reply):
    """CORRECT (newest only) / STALE (older only) / BOTH / ABSTAIN / INVENTED / OFF_FORMAT / ERROR.
    Unique probes (no superseded value): RECALL / ABSTAIN / INVENTED / OFF_FORMAT / ERROR."""
    if not reply:
        return "ERROR"
    new = _has(probe.current, reply)
    old = any(_has(v, reply) for v in probe.superseded)
    if new and old:
        return "BOTH"
    if new:
        return "RECALL" if not probe.superseded else "CORRECT"
    if old:
        return "STALE"
    if tiers.SKIP.search(reply) or ABSTAIN.search(reply):
        return "ABSTAIN"
    if CODE.search(reply):
        return "INVENTED"
    return "OFF_FORMAT"


def dry(scenarios):
    out = []
    for s in scenarios:
        brief = r2.chat_brief(s)
        row = {"scenario": s.name, "brief_chars": len(brief)}
        for arm in COMPRESS:
            ctx = r2.context(s, arm)
            row[arm] = {"brief_chars": len(COMPRESS[arm](brief)), "system_chars": len(r2.system(s, arm)),
                        "planted_present": [p.key for p in s.probes if p.current and p.current in ctx],
                        "stale_present": [p.key for p in s.probes if any(v in ctx for v in p.superseded)]}
        out.append(row)
    return out


def install(which):
    """Point the frozen r2 harness at the r6 arms (and, for recency, the recency suite and scorer)."""
    r2.COMPRESS = COMPRESS
    r2.dry = dry
    if which == "recency":
        r2.suite = recency_suite
        r2.context_eval.score = score_recency


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=("main", "recency"), required=True)
    o, rest = ap.parse_known_args(argv)
    install(o.set)
    if "--meta" in rest:
        k = rest.index("--meta") + 1
        rest[k] = json.dumps({**json.loads(rest[k]), "r6_set": o.set,
                              "r6_harness": "ctx-compress-r6"})
    sys.argv = [str(HERE / "ctx_compress_r2_eval.py")] + rest
    return r2.main()


if __name__ == "__main__":
    raise SystemExit(main())
