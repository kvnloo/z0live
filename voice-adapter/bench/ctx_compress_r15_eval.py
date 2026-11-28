"""C1-ctx-compress-r15: BLIND held-out test of the r14 rule (harness-only round; frozen before scoring).

  LLAMA_SERVER=.../llama-server python ctx_compress_r15_eval.py --set main --gguf m.gguf --blocks B,A,B,A,O ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r15_eval.py --set r15  --gguf m.gguf --blocks B,A,B,A,O,R ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r15_eval.py --set r14  --gguf m.gguf --blocks B,A,B,A ...
  python ctx_compress_r15_eval.py --set r15 --dry
  python ctx_compress_r15_eval.py --presence
  python ctx_compress_r15_eval.py --precheck

No production change: brief_compress.py is r14's byte for byte (label tie-break on attribute key v2). The question is
whether r14 holds against r4 on a set whose TEXT WAS WRITTEN BLIND TO THE RULE: a free hosted LLM got only a neutral
spec (a casual chat about a vet clinic / a car rental / a pottery class in which the user states personal facts with
short codes, some genuinely updated in the user's own words, two similar-sounding facts that are both true, free-form
facts, one fact said once) and never saw the compressor, the rule or an earlier set. The experimenter copied the user
fact sentences verbatim into bench/ctx_compress_r15_blind.json and added only probes, gold, categories, the topic
noise and the padding (test_ctx_compress_r15 checks the verbatim copy locally against the sha256-pinned raw text).

Runs the frozen ctx_compress_r10_eval.main() (which runs the frozen r2 harness) with the r14 arms:
  A = bench/brief_compress_r4.py (baseline), B = brief_compress.py (r14), R = bench/brief_compress_r13.py (r13,
  report-only rival), O = full brief (report-only).
Sets: main (r2), recency, r8, r12, r13, r14 (dev / re-run, unchanged suites) and NEW:
  --set r15  r15_suite(): per blind scenario (stable brief and call turns are the r2 scenarios' own, as r8 - r14)
             the first statement of every fact in blind order (padded to OLD_TARGET), the r8 topic noise, then every
             later statement (updates and the second fact of each similar pair, blind order, padded to NEW_TARGET).
             Probes per scenario from the data file: u1-u3 genuine updates (UPD_SAME / UPD_RENAMED, gated STALE),
             p_old (older fact of a still-true pair, gated LOSS), s1.. (STATIC, gated recall), p_new (report-only).
             Scored by ctx_compress_r6_eval.score_recency (unchanged).
Synthetic text only.
"""
import argparse
import dataclasses
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(HERE.parent))
import brief_compress  # noqa: E402
import brief_compress_r4  # noqa: E402
import brief_compress_r13  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r8_eval as r8  # noqa: E402
import ctx_compress_r10_eval as r10e  # noqa: E402
import ctx_compress_r14_eval as r14e  # noqa: E402
from context_eval import Probe  # noqa: E402

BUDGET = r10e.BUDGET
HARNESS = "ctx-compress-r15"
R2_SUITE = r10e.R2_SUITE
BLIND = json.loads((HERE / "ctx_compress_r15_blind.json").read_text())
R15_NAMES = [s["name"] for s in BLIND["scenarios"]]
# probe index groups per scenario = one abab job each (<= 5 probes x 6 blocks per job, as r14's hx)
R15_GROUPS = tuple((tuple(range(0, (len(s["probes"]) + 1) // 2)),
                    tuple(range((len(s["probes"]) + 1) // 2, len(s["probes"])))) for s in BLIND["scenarios"])
# 9 older facts must fit the 2810 brief with the stable header (car); noise exchanges sit between (test-checked)
OLD_TARGET, NEW_TARGET = 260, r8.NEW_TARGET
ACK = "Code"  # neutral acknowledgement label (assistant line; the rule reads the asker line only)
# padding before r8.PAD on the later statements only, so every later exchange is longer than every noise exchange
# (the blind sentences are short; r8 - r14 used a topic note for this). No slot, cue or digit (test-checked).
NEW_NOTE = " I am writing it down here as well so that both of us can look it up again whenever we need it."
CODE_ASK = " Please give just the code."
COMPRESS = r14e.COMPRESS
PRECHECK = {"r4": brief_compress_r4, "r13": brief_compress_r13, "r14": brief_compress}
PRESENCE_ARMS = ("A", "B")
PROBE_SETS = ("recency", "r8", "r12", "r13", "r14", "r15")
EXISTING = ("recency", "r8", "r12", "r13", "r14")  # a precheck failure on these STOPs the experiment (PREREG)
STALE_KEYS = {w: r14e.STALE_KEYS[w] for w in EXISTING}
OLD_KEYS = {w: r14e.OLD_KEYS[w] for w in EXISTING}


def r15_facts(i):
    sc = BLIND["scenarios"][i]

    def ex(f, note, target):
        return r8._fit(f["text"] + note, r8._ack(ACK, f["value"]), target)

    old = sum((ex(f, "", OLD_TARGET) for f in sc["old"]), ())
    new = sum((ex(f, NEW_NOTE, NEW_TARGET) for f in sc["new"]), ())
    probes = tuple(Probe(p["key"], p["ask"] + CODE_ASK, p["current"], tuple(p["superseded"])) for p in sc["probes"])
    return old, new, probes


def r15_suite():
    out = []
    for i, s in enumerate(R2_SUITE()):
        old, new, probes = r15_facts(i)
        chat = old + r8.noise(BLIND["scenarios"][i]["noise"], 2 * r8.NOISE_PAIRS) + new
        out.append(dataclasses.replace(s, name=R15_NAMES[i], chat=chat, probes=probes))
    return tuple(out)


def statements(i):
    """Every blind fact statement of scenario i, by its value: the asker text the supersession rule reads."""
    sc = BLIND["scenarios"][i]
    return {f["value"]: f["text"] for f in sc["old"] + sc["new"]}


def keys(texts):
    """r14 attribute, r13 (v2) attribute and cue flag of every statement; None = no 'my <subject> is' slot."""
    out = {}
    for role, txt in texts.items():
        ex = [f"user: {txt}"]
        a, v2 = (m._attribute(ex) for m in (brief_compress, brief_compress_r13))
        out[role] = {"r14": list(a) if a else None, "v2": list(v2) if v2 else None, "cue": brief_compress._cue(ex)}
    return out


SETS = {**r14e.SETS, "r15": r15_suite}
_EXTRA = {**r14e._EXTRA, "r15": r15_suite}


class _arms:
    """r10e.dry / install read the module globals r10e.COMPRESS / r10e.SETS; set them for the call and restore."""
    def __enter__(self):
        self.saved = r10e.COMPRESS, dict(r10e.SETS)
        r10e.COMPRESS = COMPRESS
        r10e.SETS.update(_EXTRA)

    def __exit__(self, *exc):
        r10e.COMPRESS = self.saved[0]
        r10e.SETS.clear()
        r10e.SETS.update(self.saved[1])


def dry(scenarios):
    with _arms():
        return r10e.dry(scenarios)


def presence(arms=PRESENCE_ARMS):
    """{"<scenario>/<key>": {arm: bool}} over every probe of PROBE_SETS: planted value is a substring of the arm's
    compressed brief."""
    out = {}
    for which in PROBE_SETS:
        for s in SETS[which]():
            brief = r2.chat_brief(s)
            comp = {a: COMPRESS[a](brief) for a in arms}
            for p in s.probes:
                out[f"{s.name}/{p.key}"] = {a: p.current in comp[a] for a in arms}
    return dict(sorted(out.items()))


def precheck():
    """Deterministic, no model. Per brief: sizes, planted values held by r4 / r13 / r14; checks: main r14 == r4 byte
    for byte; per gated dev stale item r14 holds the current value and no superseded one; per gated dev old-fact item
    r14 holds the value wherever r4 does. `stop` lists every failed check on an EXISTING set (PREREG STOP rule). On r15
    (scored, never a stop) it records per probe what each arm holds, whether B == A, and the keys of every statement."""
    out, stop = {"briefs": {}, "stop": []}, []
    for which in ("main",) + PROBE_SETS:
        for i, s in enumerate(SETS[which]()):
            brief = r2.chat_brief(s)
            comp = {n: m.compress_brief(brief, BUDGET) for n, m in PRECHECK.items()}
            row = {"full_chars": len(brief), "chars": {n: len(c) for n, c in comp.items()}, "probes": {}}
            for p in s.probes:
                if p.current is None:
                    continue
                vals = {"current": p.current, **{f"old{j}": v for j, v in enumerate(p.superseded)}}
                row["probes"][p.key] = {role: {"value": v, **{n: v in c for n, c in comp.items()}}
                                        for role, v in vals.items()}
            fails = []
            if which == "main":
                row["r14_eq_r4"] = comp["r14"] == comp["r4"]
                if not row["r14_eq_r4"]:
                    fails.append(f"main/{s.name}: r14 != r4")
            for k in STALE_KEYS.get(which, ()):
                p = next(q for q in s.probes if q.key == k)
                if p.current not in comp["r14"] or any(v in comp["r14"] for v in p.superseded):
                    fails.append(f"{which}/{s.name}/{k}: stale fix lost")
            for k in OLD_KEYS.get(which, ()):
                p = next(q for q in s.probes if q.key == k)
                if p.current in comp["r4"] and p.current not in comp["r14"]:
                    fails.append(f"{which}/{s.name}/{k}: old fact dropped")
            row["fails"] = fails
            stop.extend(fails)
            if which == "r15":
                row["r14_eq_r4"] = comp["r14"] == comp["r4"]
                row["r14_eq_r13"] = comp["r14"] == comp["r13"]
                row["keys"] = keys(statements(i))
            out["briefs"][f"{which}/{s.name}"] = row
    out["stop"] = stop
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--precheck"]:
        print(json.dumps(precheck(), indent=1))
        return 0
    if argv == ["--presence"]:
        print(json.dumps(presence(), indent=1))
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=tuple(SETS), required=True)
    _, rest = ap.parse_known_args(argv)
    if "--meta" in rest:
        k = argv.index("--meta") + 1
        argv[k] = json.dumps({**json.loads(argv[k]), "r15_harness": HARNESS})
    # process-wide for the run: r10e.install binds r2.COMPRESS = r10e.COMPRESS and r2.suite = r10e.SETS[set]
    r10e.COMPRESS = COMPRESS
    r10e.SETS.update(_EXTRA)
    return r10e.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
