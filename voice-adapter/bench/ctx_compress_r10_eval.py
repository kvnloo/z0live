"""C1-ctx-compress-r10: held-out cue-word false-merge check of the r9 rule (frozen before scoring).

  LLAMA_SERVER=.../llama-server python ctx_compress_r10_eval.py --set main    --gguf m.gguf --blocks B,A,B,A,O ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r10_eval.py --set recency --gguf m.gguf --blocks B,A,B,A,O,R ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r10_eval.py --set r8      --gguf m.gguf --blocks B,A,B,A,O,R ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r10_eval.py --set r10     --gguf m.gguf --blocks B,A,B,A,O,R ...
  python ctx_compress_r10_eval.py --set r10 --dry

Harness only; no production change (brief_compress.py is byte-identical to r9). Runs the frozen
ctx_compress_r2_eval.main() (transport, blocks, deadline, JSON layout unchanged) with the r9 arms:
  A = bench/brief_compress_r4.py (r4 byte copy, baseline), B = brief_compress.py (production r9),
  R = bench/brief_compress_r7.py (r7 byte copy, report-only rival), O = full brief (report-only).
Sets: main (r2 suite), recency (r6 suite), r8 (r8 suite): all unchanged, reused. NEW:
  --set r10  r10_suite(): 3 NEW scenarios (rowing club, climbing gym, community library; stable brief and call turns
             are the r2 scenarios' own, as in r8) with topic noise and, per scenario:
               (e) CFM    PRIMARY. Two cue-word false-merge pairs: an older fact "my <thing> is the <colour> one
                          tagged X" and a newer, DIFFERENT-attribute fact in the same lexical slot that carries an
                          r9 update cue ("my <thing> is now at ...", "... in the new ...", "was moved to ..."),
                          ticket Y. Both facts stay true. Probes: the older tag (e1_old, e2_old, gated) and the newer
                          ticket (e1_new, e2_new, report-only).
               (a) SAME   two true same-attribute updates with cues not yet used for (a) wording in r8 (instead,
                          updated, no longer, replaced, switched, update/changed); stale fix transfer, gated.
               (d) UNIQ   one old unique fact, never superseded.
             Every older fact is shorter than every noise exchange and every newer one longer (as r8), so r4's
             shortest-first order reaches the old facts first and the new ones last at the 2810 margin.
             7 probes per scenario, order a1 a2 d e1_old e2_old e1_new e2_new; scored by
             ctx_compress_r6_eval.score_recency (unchanged).
presence(): for every recency / r8 / r10 probe, whether its planted value (probe.current) is a substring of each
arm's compressed brief. Deterministic, no model. The r10 verdict gates INVENTED only over probes whose value is
present in the A or the B brief (r10 PREREG).
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
import context_eval  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r6_eval as r6  # noqa: E402
import ctx_compress_r8_eval as r8  # noqa: E402
import ctx_compress_r9_eval as r9  # noqa: E402
import tiers  # noqa: E402
from context_eval import Probe  # noqa: E402

BUDGET = r9.BUDGET
R2_SUITE = r9.R2_SUITE
SCORE = r9.SCORE
COMPRESS = r9.COMPRESS  # A r4, B production (r9), R r7, O full
R10_NAMES = ["r10-rowing", "r10-climbing", "r10-library"]
SAME_KEYS = ("a1", "a2")
CFM_OLD_KEYS = ("e1_old", "e2_old")
CFM_NEW_KEYS = ("e1_new", "e2_new")
UNIQUE_KEYS = ("d",)
PROBE_ORDER = ("a1", "a2", "d", "e1_old", "e2_old", "e1_new", "e2_new")
# exchange sizes (chars): r10 noise exchanges sit at 352-359 (shorter topic nouns than r8), so older facts are
# padded to <= 340 (r8: 352) and newer ones to <= 382 (as r8); test_ctx_compress_r10 checks old < noise < new
OLD_TARGET, NEW_TARGET = 340, r8.NEW_TARGET
PRESENCE_ARMS = ("A", "B")

TOPICS = (
    {"topic": "rowing", "noun": "Crew", "items": ("sculls", "blade", "rigger", "slide", "stern", "cox", "keel"),
     "note": " I wrote it on the whiteboard in the boathouse, next to the outing list, so the coaches know.",
     "a": (("boathouse locker number", "Quick note, my boathouse locker number is {} instead, after the refit."),
           ("oar set label", "Heads up, my oar set label is updated to {}, the coach relabelled them.")),
     "d": "erg profile code",
     "e": (("kayak", "My kayak is the yellow one tagged {}.",
            "My kayak is now at the repair bay by the slipway, the repair ticket on it is {}.",
            "What is the tag code on my yellow kayak?", "What is the repair ticket code for my kayak?"),
           ("lifejacket", "My lifejacket is the orange one tagged {}.",
            "My lifejacket is in the new drying room this week, the hook number for it is {}.",
            "What is the tag code on my orange lifejacket?", "What is the drying room hook number for my lifejacket?"))},
    {"topic": "climbing", "noun": "Wall", "items": ("crimp", "sloper", "pinch", "jug", "arete", "slab", "roof"),
     "note": " I wrote it on the clipboard at the front desk, next to the route sheet, so the setters know.",
     "a": (("gym membership number", "Small thing, my gym membership number is {} since they replaced the cards."),
           ("belay card code", "My belay card code is no longer the old one, it is {} after the recheck.")),
     "d": "chalk locker label",
     "e": (("harness", "My harness is the black one tagged {}.",
            "My harness is now hanging in the inspection cage, the cage slip for it is {}.",
            "What is the tag code on my black harness?", "What is the inspection cage slip code for my harness?"),
           ("rope", "My rope is the green one tagged {}.",
            "My rope was moved to the back store for the week, the store ticket for it is {}.",
            "What is the tag code on my green rope?", "What is the back store ticket code for my rope?"))},
    {"topic": "library", "noun": "Shelf", "items": ("atlas", "novel", "poetry", "journal", "index", "map", "folio"),
     "note": " I wrote it on the slip at the help desk, next to the returns trolley, so the staff know.",
     "a": (("library card number", "Update: my library card number is {}, the system changed over this month."),
           ("reading room desk", "My reading room desk is {} after they switched the layout around.")),
     "d": "book club shelf code",
     "e": (("umbrella", "My umbrella is the striped one tagged {}.",
            "My umbrella is now in the lost property box, the claim slip for it is {}.",
            "What is the tag code on my striped umbrella?", "What is the lost property claim slip code for my umbrella?"),
           ("tote bag", "My tote bag is the canvas one tagged {}.",
            "My tote bag is at the new cloakroom desk today, the cloakroom ticket is {}.",
            "What is the tag code on my canvas tote bag?", "What is the cloakroom ticket code for my tote bag?"))},
)


def codes(i):
    w = lambda name, base: f"{name}-{base + i}"  # noqa: E731
    # code families unused by the stable briefs (Cedar Slate Birch Elm Alder Mint) and by r6 / r8
    return {"a1_old": w("Poplar", 23), "a1_new": w("Spruce", 47), "a2_old": w("Maple", 15), "a2_new": w("Aspen", 66),
            "e1_old": w("Rowan", 33), "e1_new": w("Hazel", 58), "e2_old": w("Larch", 41), "e2_new": w("Holly", 74),
            "d": w("Linden", 86)}


def r10_facts(i):
    t, c = TOPICS[i], codes(i)
    note = t["note"]
    (a1s, a1u), (a2s, a2u) = t["a"]
    (e1s, e1o, e1n, e1oq, e1nq), (e2s, e2o, e2n, e2oq, e2nq) = t["e"]
    fit, ack = r8._fit, r8._ack
    old_facts = (
        fit(f"My {a1s} is {c['a1_old']}." + note, ack(a1s.capitalize(), c["a1_old"]), OLD_TARGET),
        fit(e1o.format(c["e1_old"]) + note, ack(e1s.capitalize() + " tag", c["e1_old"]), OLD_TARGET),
        fit(f"My {t['d']} is {c['d']}." + note, ack(t["d"].capitalize(), c["d"]), OLD_TARGET),
        fit(e2o.format(c["e2_old"]) + note, ack(e2s.capitalize() + " tag", c["e2_old"]), OLD_TARGET),
        fit(f"My {a2s} is {c['a2_old']}." + note, ack(a2s.capitalize(), c["a2_old"]), OLD_TARGET))
    new_facts = (
        fit(e1n.format(c["e1_new"]) + note, ack(e1s.capitalize() + " ticket", c["e1_new"]), NEW_TARGET),
        fit(a1u.format(c["a1_new"]) + note, ack(a1s.capitalize(), c["a1_new"]), NEW_TARGET),
        fit(e2n.format(c["e2_new"]) + note, ack(e2s.capitalize() + " ticket", c["e2_new"]), NEW_TARGET),
        fit(a2u.format(c["a2_new"]) + note, ack(a2s.capitalize(), c["a2_new"]), NEW_TARGET))
    code = " Please give just the code."
    by_key = {
        "a1": Probe("a1", f"What is my {a1s} now?" + code, c["a1_new"], (c["a1_old"],)),
        "a2": Probe("a2", f"What is my {a2s} now?" + code, c["a2_new"], (c["a2_old"],)),
        "d": Probe("d", f"What is my {t['d']}?" + code, c["d"]),
        "e1_old": Probe("e1_old", e1oq + code, c["e1_old"]),
        "e2_old": Probe("e2_old", e2oq + code, c["e2_old"]),
        "e1_new": Probe("e1_new", e1nq + code, c["e1_new"]),
        "e2_new": Probe("e2_new", e2nq + code, c["e2_new"])}
    return sum(old_facts, ()), sum(new_facts, ()), tuple(by_key[k] for k in PROBE_ORDER)


def r10_suite():
    out = []
    for i, s in enumerate(R2_SUITE()):
        old, new, probes = r10_facts(i)
        t = TOPICS[i]
        chat = old + r8.noise(t, 2 * r8.NOISE_PAIRS) + new
        out.append(dataclasses.replace(s, name=R10_NAMES[i], chat=chat, probes=probes))
    return tuple(out)


SETS = {**r9.SETS, "r10": r10_suite}
PROBE_SETS = ("recency", "r8", "r10")


def dry(scenarios):
    """Per arm: brief size and, per probe, what the context holds (r8.predict). Not a score."""
    out = []
    for s in scenarios:
        brief = r2.chat_brief(s)
        row = {"scenario": s.name, "brief_chars": len(brief)}
        for arm in COMPRESS:
            ctx = tiers.snapshot(COMPRESS[arm](brief), context_eval.exchange(s.call))
            row[arm] = {"brief_chars": len(COMPRESS[arm](brief)),
                        "system_chars": len(tiers.SYSTEM.format(tier="fast") + "\n\n" + ctx),
                        "items": {p.key: ("UNPLANTED" if p.current is None else
                                          r8.predict(p.current in ctx, any(v in ctx for v in p.superseded)))
                                  for p in s.probes}}
        out.append(row)
    return out


def presence(arms=PRESENCE_ARMS):
    """{"<scenario>/<key>": {arm: bool}}: planted value (probe.current) is a substring of the arm's compressed brief.
    Over every recency, r8 and r10 probe. Deterministic string check, computed from the frozen suites and arms."""
    out = {}
    for which in PROBE_SETS:
        for s in SETS[which]():
            brief = r2.chat_brief(s)
            comp = {a: COMPRESS[a](brief) for a in arms}
            for p in s.probes:
                out[f"{s.name}/{p.key}"] = {a: p.current in comp[a] for a in arms}
    return dict(sorted(out.items()))


def install(which):
    r2.COMPRESS = COMPRESS
    r2.dry = dry
    r2.suite = SETS[which]
    r2.context_eval.score = SCORE if which == "main" else r6.score_recency


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=tuple(SETS), required=True)
    ap.add_argument("--presence", action="store_true", help="print presence() as JSON and exit")
    o, rest = ap.parse_known_args(argv)
    if o.presence:
        print(json.dumps(presence(), indent=1))
        return 0
    install(o.set)
    if "--meta" in rest:
        k = rest.index("--meta") + 1
        rest[k] = json.dumps({**json.loads(rest[k]), "r10_set": o.set, "r10_harness": "ctx-compress-r10"})
    sys.argv = [str(HERE / "ctx_compress_r2_eval.py")] + rest
    return r2.main()


if __name__ == "__main__":
    raise SystemExit(main())
