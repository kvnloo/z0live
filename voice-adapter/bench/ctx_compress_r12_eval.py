"""C1-ctx-compress-r12: held-out transfer of the r11 attribute-aware supersession rule (frozen before scoring).

  LLAMA_SERVER=.../llama-server python ctx_compress_r12_eval.py --set main --gguf m.gguf --blocks B,A,B,A,O ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r12_eval.py --set r12  --gguf m.gguf --blocks B,A,B,A,O,R ...
  python ctx_compress_r12_eval.py --set r12 --dry
  python ctx_compress_r12_eval.py --presence
  python ctx_compress_r12_eval.py --precheck

Harness only; no production change (brief_compress.py is r11, byte-identical to PR #176 3dcf56c). Runs the frozen
ctx_compress_r10_eval.main() (which runs the frozen r2 harness: transport, blocks, deadline, JSON layout) with:
  A = bench/brief_compress_r4.py (r4 byte copy, baseline), B = brief_compress.py (production r11),
  R = bench/brief_compress_r7.py (r7 byte copy, report-only rival), O = full brief (report-only).
Sets: main (r2 suite, unchanged). NEW:
  --set r12  r12_suite(): 3 NEW scenarios (campsite, coworking space, leisure centre; stable brief and call turns are
             the r2 scenarios' own, as in r8 / r10) with topic noise and, per scenario, eleven probes:
               (a) CO     same predicate head word, different attribute (older "my <s> is <head> ... X", newer
                          "my <s> is now <head> ... new ... Y"); r11 heads COINCIDE, so r11 is predicted to drop the
                          older fact (a known limit of the lexical head). Probe a_old (gated), a_new (report-only).
               (b) PRON   pronoun / plural subject ("our tent is", "my kids are", "my parents are"), different
                          attribute, newer one with a cue. Probe b_old (gated), b_new (report-only).
               (c) MULTI  multi-word predicates ("is kept in ...", "is due back at ..."), different attribute,
                          different head words. Probe c_old (gated), c_new (report-only).
               (d) CUEVAL a cue word only inside the value text ("the New Barn gate", "the Switched On network",
                          "the Now Dry bin"), different attribute. Probe d_old (gated), d_new (report-only).
               (e) SAME   two genuine same-attribute updates carrying a cue, value restated (stale fix must hold).
                          Probes e1, e2 (gated).
               (f) UNIQ   one old unique fact, never superseded. Probe f (gated).
             Every older fact is shorter than every noise exchange and every newer one longer (as r8 / r10).
             Scored by ctx_compress_r6_eval.score_recency (unchanged).
presence(): for every r12 probe, whether its planted value (probe.current) is a substring of the A and B compressed
briefs. Deterministic, no model.
precheck(): r4 / r7 / r9 / r11 on every main and r12 brief at 2810: which planted values each brief holds, plus the
r11 attribute (slot subject, predicate head) of every r12 fact statement.
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
import brief_compress_r7  # noqa: E402
import brief_compress_r9  # noqa: E402
import context_eval  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r8_eval as r8  # noqa: E402
import ctx_compress_r10_eval as r10e  # noqa: E402
import tiers  # noqa: E402
from context_eval import Probe  # noqa: E402

BUDGET = r10e.BUDGET
HARNESS = "ctx-compress-r12"
R2_SUITE = r10e.R2_SUITE
COMPRESS = {"A": lambda b: brief_compress_r4.compress_brief(b, BUDGET),
            "B": lambda b: brief_compress.compress_brief(b, BUDGET),
            "R": lambda b: brief_compress_r7.compress_brief(b, BUDGET),
            "O": lambda b: b}
PRECHECK = {"r4": brief_compress_r4, "r7": brief_compress_r7, "r9": brief_compress_r9, "r11": brief_compress}
R12_NAMES = ["r12-campsite", "r12-coworking", "r12-leisure"]
FM_OLD_KEYS = ("a_old", "b_old", "c_old", "d_old")
FM_NEW_KEYS = ("a_new", "b_new", "c_new", "d_new")
SAME_KEYS = ("e1", "e2")
UNIQUE_KEYS = ("f",)
PROBE_ORDER = ("a_old", "b_old", "c_old", "d_old", "e1", "e2", "f", "a_new", "b_new", "c_new", "d_new")
OLD_TARGET, NEW_TARGET = 300, r8.NEW_TARGET  # noise exchanges sit between (test_ctx_compress_r12 checks)
PRESENCE_ARMS = ("A", "B")

# per pair: (subject label for the ack, older statement, newer statement, older question, newer question)
TOPICS = (
    {"topic": "campsite", "noun": "Camp", "items": ("lantern", "kettle", "tarp", "stove", "pegs", "canopy", "hammock"),
     "note": " I wrote it on the board by the site shop, next to the pitch map, so the wardens know.",
     "a": ("Van", "My van is booked for the ferry on Friday, the booking ref is {}.",
           "My van is now booked in at the new garage for its service, the job card is {}.",
           "What is the ferry booking ref for my van?", "What is the garage job card code for my van?"),
     "b": ("Tent", "Our tent is the green one tagged {}.",
           "Our tent is now pitched on the new terrace, the pitch tag is {}.",
           "What is the tag code on our green tent?", "What is the terrace pitch tag code for our tent?"),
     "c": ("Cool box", "My cool box is kept in the shower block fridge, the fridge tag is {}.",
           "My cool box is due back at the new hire desk tomorrow, the hire slip is {}.",
           "What is the shower block fridge tag code for my cool box?",
           "What is the hire desk slip code for my cool box?"),
     "d": ("Bike trailer", "My bike trailer is the orange one tagged {}.",
           "My bike trailer is parked by the New Barn gate, the parking tag is {}.",
           "What is the tag code on my orange bike trailer?",
           "What is the New Barn parking tag code for my bike trailer?"),
     "e": (("pitch number", "My pitch number is {} now, the warden moved us after the storm."),
           ("gate code", "My gate code is changed to {} from today, the warden said.")),
     "f": "hook-up card number"},
    {"topic": "coworking", "noun": "Desk", "items": ("monitor", "lamp", "chair", "dock", "plant", "sofa", "printer"),
     "note": " I wrote it on the pad at the front desk, next to the visitor book, so the hosts know.",
     "a": ("Locker", "My locker is on the second floor by the lifts, the locker tag is {}.",
           "My locker is now on a new keypad system, the keypad code is {}.",
           "What is the locker tag code for my locker on the second floor?",
           "What is the keypad code for my locker?"),
     "b": ("Kids", "My kids are both on the family pass, the pass code is {}.",
           "My kids are now in the new holiday club downstairs, the pickup code is {}.",
           "What is the family pass code for my kids?", "What is the holiday club pickup code for my kids?"),
     "c": ("Standing desk", "My standing desk is set to the tall preset, the preset tag is {}.",
           "My standing desk is booked out for the new team on Thursdays, the booking ref is {}.",
           "What is the preset tag code for my standing desk?",
           "What is the Thursday booking ref for my standing desk?"),
     "d": ("Laptop", "My laptop is the silver one tagged {}.",
           "My laptop is registered on the Switched On guest network, the device ID is {}.",
           "What is the tag code on my silver laptop?", "What is the guest network device ID for my laptop?"),
     "e": (("desk number", "My desk number is {} after the move, they replaced the floor plan."),
           ("mailbox number", "My mailbox number is now {} since the post room changed.")),
     "f": "printer PIN code"},
    {"topic": "leisure", "noun": "Pool", "items": ("lanes", "float", "towel", "goggles", "sauna", "flume", "jacuzzi"),
     "note": " I wrote it on the form at reception, next to the class timetable, so the staff know.",
     "a": ("Car", "My car is registered for the staff car park, the permit is {}.",
           "My car is now registered with the new charging bay app, the charger ID is {}.",
           "What is the staff car park permit code for my car?", "What is the charging bay charger ID for my car?"),
     "b": ("Parents", "My parents are members on the senior plan, the membership code is {}.",
           "My parents are now booked into the new aqua class, the class card is {}.",
           "What is the senior plan membership code for my parents?",
           "What is the aqua class card code for my parents?"),
     "c": ("Gym bag", "My gym bag is hanging on the peg rail, the peg tag is {}.",
           "My gym bag is waiting at the new lost property desk, the claim slip is {}.",
           "What is the peg tag code for my gym bag?", "What is the lost property claim slip code for my gym bag?"),
     "d": ("Towel", "My towel is the red one tagged {}.",
           "My towel is in the Now Dry laundry bin, the laundry ticket is {}.",
           "What is the tag code on my red towel?", "What is the laundry ticket code for my towel?"),
     "e": (("lane booking code", "Small change, my lane booking code is {} now, the system was updated."),
           ("class pass number", "My class pass number is changed to {}, reception swapped the cards.")),
     "f": "sauna token code"},
)
PAIRS = ("a", "b", "c", "d")


def codes(i):
    w = lambda name, base: f"{name}-{base + i}"  # noqa: E731
    # code families unused by the stable briefs and by every earlier set (r6 / r8 / r10)
    return {"a_old": w("Jade", 21), "a_new": w("Onyx", 52), "b_old": w("Topaz", 17), "b_new": w("Amber", 63),
            "c_old": w("Coral", 25), "c_new": w("Pearl", 56), "d_old": w("Ruby", 38), "d_new": w("Garnet", 71),
            "e1_old": w("Quartz", 14), "e1_new": w("Agate", 45), "e2_old": w("Beryl", 29), "e2_new": w("Jasper", 67),
            "f": w("Zircon", 84)}


def r12_facts(i):
    t, c = TOPICS[i], codes(i)
    note, fit, ack = t["note"], r8._fit, r8._ack
    (e1s, e1u), (e2s, e2u) = t["e"]

    def old(p):
        lab, txt = t[p][0], t[p][1]
        return fit(txt.format(c[f"{p}_old"]) + note, ack(lab, c[f"{p}_old"]), OLD_TARGET)

    def new(p):
        lab, txt = t[p][0], t[p][2]
        return fit(txt.format(c[f"{p}_new"]) + note, ack(lab, c[f"{p}_new"]), NEW_TARGET)

    def plain(s, v):
        return fit(f"My {s} is {v}." + note, ack(s.capitalize(), v), OLD_TARGET)

    old_facts = (old("a"), old("b"), plain(e1s, c["e1_old"]), old("c"), plain(t["f"], c["f"]), old("d"),
                 plain(e2s, c["e2_old"]))
    new_facts = (new("a"), fit(e1u.format(c["e1_new"]) + note, ack(e1s.capitalize(), c["e1_new"]), NEW_TARGET),
                 new("b"), new("c"), fit(e2u.format(c["e2_new"]) + note, ack(e2s.capitalize(), c["e2_new"]), NEW_TARGET),
                 new("d"))
    code = " Please give just the code."
    by_key = {"e1": Probe("e1", f"What is my {e1s} now?" + code, c["e1_new"], (c["e1_old"],)),
              "e2": Probe("e2", f"What is my {e2s} now?" + code, c["e2_new"], (c["e2_old"],)),
              "f": Probe("f", f"What is my {t['f']}?" + code, c["f"])}
    for p in PAIRS:
        by_key[f"{p}_old"] = Probe(f"{p}_old", t[p][3] + code, c[f"{p}_old"])
        by_key[f"{p}_new"] = Probe(f"{p}_new", t[p][4] + code, c[f"{p}_new"])
    return sum(old_facts, ()), sum(new_facts, ()), tuple(by_key[k] for k in PROBE_ORDER)


def r12_suite():
    out = []
    for i, s in enumerate(R2_SUITE()):
        old, new, probes = r12_facts(i)
        chat = old + r8.noise(TOPICS[i], 2 * r8.NOISE_PAIRS) + new
        out.append(dataclasses.replace(s, name=R12_NAMES[i], chat=chat, probes=probes))
    return tuple(out)


def statements(i):
    """Every r12 fact statement of scenario i, by role: the asker text the r11 rule reads."""
    t, c = TOPICS[i], codes(i)
    out = {}
    for p in PAIRS:
        out[f"{p}_old"] = t[p][1].format(c[f"{p}_old"])
        out[f"{p}_new"] = t[p][2].format(c[f"{p}_new"])
    for k, (s, upd) in zip(("e1", "e2"), t["e"]):
        out[f"{k}_old"] = f"My {s} is {c[f'{k}_old']}."
        out[f"{k}_new"] = upd.format(c[f"{k}_new"])
    out["f"] = f"My {t['f']} is {c['f']}."
    return out


def attributes(i):
    """r11 attribute (slot subject, predicate head) and cue flag of every statement; None = no slot."""
    out = {}
    for role, txt in statements(i).items():
        ex = [f"user: {txt}"]
        a = brief_compress._attribute(ex)
        out[role] = {"attribute": list(a) if a else None, "cue": brief_compress._cue(ex),
                     "r7_slot": brief_compress._slot(ex)}
    return out


SETS = {"main": r10e.SETS["main"], "r12": r12_suite}


class _arms:
    """r10e.dry / install read the module global r10e.COMPRESS / r10e.SETS; set them for the call and restore."""
    def __enter__(self):
        self.saved = r10e.COMPRESS, dict(r10e.SETS)
        r10e.COMPRESS = COMPRESS
        r10e.SETS["r12"] = r12_suite

    def __exit__(self, *exc):
        r10e.COMPRESS = self.saved[0]
        r10e.SETS.clear()
        r10e.SETS.update(self.saved[1])


def dry(scenarios):
    with _arms():
        return r10e.dry(scenarios)


def presence(arms=PRESENCE_ARMS):
    """{"<scenario>/<key>": {arm: bool}} over every r12 probe: planted value is a substring of the arm's brief."""
    out = {}
    for s in r12_suite():
        brief = r2.chat_brief(s)
        comp = {a: COMPRESS[a](brief) for a in arms}
        for p in s.probes:
            out[f"{s.name}/{p.key}"] = {a: p.current in comp[a] for a in arms}
    return dict(sorted(out.items()))


def precheck():
    out = {}
    for which in ("main", "r12"):
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
            if which == "r12":
                row["attributes"] = attributes(i)
            out[f"{which}/{s.name}"] = row
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
        argv[k] = json.dumps({**json.loads(argv[k]), "r12_harness": HARNESS})
    # process-wide for the run: r10e.install binds r2.COMPRESS = r10e.COMPRESS and r2.suite = r10e.SETS[set]
    r10e.COMPRESS = COMPRESS
    r10e.SETS["r12"] = r12_suite
    return r10e.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
