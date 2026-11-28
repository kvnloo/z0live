"""C1-ctx-compress-r14: label tie-break on attribute key v2 for the cue-gated supersession (frozen before scoring).

  LLAMA_SERVER=.../llama-server python ctx_compress_r14_eval.py --set main --gguf m.gguf --blocks B,A,B,A,O ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r14_eval.py --set r14  --gguf m.gguf --blocks B,A,B,A,O,R ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r14_eval.py --set r13  --gguf m.gguf --blocks B,A,B,A ...
  python ctx_compress_r14_eval.py --set r14 --dry
  python ctx_compress_r14_eval.py --presence
  python ctx_compress_r14_eval.py --precheck

ONE production change: brief_compress._attribute uses the value's label when the predicate has one: (slot subject,
head word, LABEL, the 1-2 non-FILLER words right before the value in its clause) = label tie-break; without a label it
is r13's attribute key v2 (slot subject + first two non-FILLER predicate words). Runs the frozen
ctx_compress_r10_eval.main() (which runs the frozen r2 harness: transport, blocks, deadline, JSON layout) with:
  A = bench/brief_compress_r4.py (r4 byte copy, baseline), B = brief_compress.py (production r14),
  R = bench/brief_compress_r13.py (r13 byte copy, report-only rival), O = full brief (report-only).
The recorded value-shape rival (V: head word + digit/letter shape of the value) is evaluated offline only (precheck
and presence), not as a model block.
Sets: main (r2 suite), recency (r6), r8, r10 (r10e.SETS, unchanged), r12 (r12 held-out set, dev), r13 (r13 held-out
set, now dev), and NEW:
  --set r14  r14_suite(): 3 NEW scenarios (airport, ski, theatre; stable brief and call turns are the r2 scenarios'
             own, as r8 / r10 / r12 / r13) with topic noise and, per scenario, nine probes:
               (a3) LIMIT-V2   2 pairs: same head word AND same second word, different attribute, different value
                               label (older "my <s> is <h> <w> ..., the <L1> is X", newer "my <s> is now <h> <w> the
                               new ..., the <L2> is Y"); r13 merges them (v2 keys coincide), r14 keeps both.
                               a3x_old a3y_old gated (PRIMARY), *_new report-only (budget).
               (e3) LIMIT-V2   2 genuine cued updates, same head word, different second word, same value label
                               ("is in the upper hall, the locker tag is X" / "is now in the lower hall, the locker
                               tag is Y"); r13 keeps the stale value (v2 keys differ), r14 supersedes. e3x e3y gated
                               (PRIMARY).
               (e)  SAME       2 genuine cued updates, value restated ("is X." / "is changed to Y ...", key (VALUE,))
                               or label noun before it ("is number X" / "is now number Y"). e1 e2 gated.
               (f)  UNIQ       one old unique fact, never superseded. Gated.
             Every older fact is shorter than every noise exchange and every newer one longer (as r8 - r13).
             Scored by ctx_compress_r6_eval.score_recency (unchanged).
presence(): for every recency / r8 / r10 / r12 / r13 / r14 probe, whether its planted value (probe.current) is a
substring of the A and B compressed briefs (and, report-only, the offline V brief). Deterministic, no model.
precheck(): r4 / r13 / r14 / V on every brief of every set at 2810: planted values each brief holds, r14 == r4 on main,
r14 == r13 on recency / r8 / r10 / r12, the stale-fix and no-false-merge checks per item (the STOP rule of the r14
PREREG), and the r14 / r13 keys of every r13 and r14 statement.
Synthetic text only.
"""
import argparse
import dataclasses
import importlib.util
import json
import re
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
import ctx_compress_r12_eval as r12e  # noqa: E402
import ctx_compress_r13_eval as r13e  # noqa: E402
from context_eval import Probe  # noqa: E402

BUDGET = r10e.BUDGET
HARNESS = "ctx-compress-r14"
R2_SUITE = r10e.R2_SUITE


def _vshape_module():
    """Offline value-shape rival V (report-only, no model block): r13's code with the attribute (slot subject, r11 head
    word, SHAPE of the first digit word: letter runs -> a, digit runs -> 9, other characters kept), v2 key when there is
    no digit word. Loaded from the r13 byte copy under its own module name, so production and r13 are untouched."""
    spec = importlib.util.spec_from_file_location("brief_compress_vshape", HERE / "brief_compress_r13.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    v2 = m._attribute

    def attribute(exchange):
        a = v2(exchange)
        if a is None:
            return None
        line = exchange[0]
        lab = m._label(line) or m._label(line, m.SPEAKER_ANY_CASE)
        text = line[len(lab) + 2:] if lab else line
        val = next((w for w in m.WORD.findall(text[m.SLOT.search(text).end():]) if any(c.isdigit() for c in w)), None)
        if val is None:
            return a
        return (a[0], a[1], "<shape>", re.sub(r"[0-9]+", "9", re.sub(r"[A-Za-z]+", "a", val)))

    m._attribute = attribute
    return m


brief_compress_vshape = _vshape_module()
COMPRESS = {"A": lambda b: brief_compress_r4.compress_brief(b, BUDGET),
            "B": lambda b: brief_compress.compress_brief(b, BUDGET),
            "R": lambda b: brief_compress_r13.compress_brief(b, BUDGET),
            "O": lambda b: b}
OFFLINE = {"V": lambda b: brief_compress_vshape.compress_brief(b, BUDGET)}  # report-only, never a model block
PRECHECK = {"r4": brief_compress_r4, "r13": brief_compress_r13, "r14": brief_compress, "V": brief_compress_vshape}
R14_NAMES = ["r14-airport", "r14-ski", "r14-theatre"]
PROBE_ORDER = ("a3x_old", "a3y_old", "e3x", "e3y", "e1", "e2", "f", "a3x_new", "a3y_new")
OLD_TARGET, NEW_TARGET = 300, r8.NEW_TARGET  # noise exchanges sit between (test_ctx_compress_r14 checks)
PRESENCE_ARMS = ("A", "B")
PROBE_SETS = ("recency", "r8", "r10", "r12", "r13", "r14")
# Per set: gated stale-update probes (current must be present and every superseded value absent in r14's brief) and
# gated old-fact probes (planted value present in r14's brief wherever r4 keeps it). Report-only items are not here.
STALE_KEYS = {"recency": ("bench", "badge"), "r8": ("a1", "a2"), "r10": ("a1", "a2"), "r12": ("e1", "e2"),
              "r13": ("e1", "e2"), "r14": ("e3x", "e3y", "e1", "e2")}
OLD_KEYS = {"recency": ("glaze",), "r8": ("c_old", "d"), "r10": ("e1_old", "e2_old", "d"),
            "r12": ("a_old", "b_old", "c_old", "d_old", "f"), "r13": ("a2x_old", "a2y_old", "f"),
            "r14": ("a3x_old", "a3y_old", "f")}
EXISTING = ("recency", "r8", "r10", "r12", "r13")  # a precheck failure on these STOPs the experiment (PREREG)
SAME_AS_R13 = ("recency", "r8", "r10", "r12")  # r14 == r13 byte for byte (no statement there changes group)

# per pair: (ack label, older statement, newer statement, older question, newer question)
# per update: (ack label, older statement, newer statement, question)
TOPICS = (
    {"topic": "airport", "noun": "Gate", "items": ("trolley", "kiosk", "carousel", "escalator", "lounge", "monitor",
                                                  "turnstile"),
     "note": " It is on the slip in my passport wallet.",
     "a3x": ("Bag", "My bag is checked through to the hub, the bag tag is {}.",
             "My bag is now checked through to the new lost desk, the claim ref is {}.",
             "What is the bag tag code for my bag to the hub?", "What is the lost desk claim ref code for my bag?"),
     "a3y": ("Ticket", "My ticket is linked with the boarding pass, the boarding pass is {}.",
             "My ticket is now linked with the new lounge pass, the lounge pass is {}.",
             "What is the boarding pass code for my ticket?", "What is the lounge pass code for my ticket?"),
     "e3x": ("Coat", "My coat is at the west cloakroom, the hook tag is {}.",
             "My coat is now at the east cloakroom, the hook tag is {}.", "What is the hook tag code for my coat now?"),
     "e3y": ("Pram", "My pram is with the gate desk, the pram tag is {}.",
             "My pram is now with the cabin crew, the pram tag is {}.", "What is the pram tag code for my pram now?"),
     "e": (("parking code", "My parking code is {}.", "My parking code is changed to {} for the long stay.",
            "What is my parking code now?"),
           ("seat", "My seat is number {}.", "My seat is now number {}, the crew swapped it.",
            "What is my seat number now?")),
     "f": "lounge voucher code"},
    {"topic": "ski", "noun": "Slope", "items": ("goggles", "gloves", "helmet", "bindings", "poles", "boots", "wax"),
     "note": " It is on the slip in my jacket pocket.",
     "a3x": ("Skis", "My skis are booked for the race day, the race bib is {}.",
             "My skis are now booked for the new wax service, the wax ticket is {}.",
             "What is the race bib code for my skis?", "What is the wax service ticket code for my skis?"),
     "a3y": ("Lift pass", "My lift pass is loaded with the day ride, the ride ref is {}.",
             "My lift pass is now loaded with the new meal plan, the meal code is {}.",
             "What is the day ride ref code for my lift pass?", "What is the meal plan code for my lift pass?"),
     "e3x": ("Boots", "My boots are in the upper hall, the locker tag is {}.",
             "My boots are now in the lower hall, the locker tag is {}.",
             "What is the locker tag code for my boots now?"),
     "e3y": ("Sledge", "My sledge is by the top station, the sledge tag is {}.",
             "My sledge is now by the base station, the sledge tag is {}.",
             "What is the sledge tag code for my sledge now?"),
     "e": (("chalet code", "My chalet code is {}.", "My chalet code is {} now, the host reset the door.",
            "What is my chalet code now?"),
           ("class", "My class is number {}.", "My class is switched to number {} for the week.",
            "What is my class number now?")),
     "f": "first aid card code"},
    {"topic": "theatre", "noun": "Scene", "items": ("curtain", "spotlight", "prop", "costume", "script", "wig",
                                                   "backdrop"),
     "note": " It is on the slip in my programme.",
     "a3x": ("Umbrella", "My umbrella is left at the box office, the box slip is {}.",
             "My umbrella is now left at the new bar, the bar token is {}.",
             "What is the box office slip code for my umbrella?", "What is the bar token code for my umbrella?"),
     "a3y": ("Booking", "My booking is paired with the dinner deal, the dinner ref is {}.",
             "My booking is now paired with the new tour, the tour code is {}.",
             "What is the dinner deal ref code for my booking?", "What is the tour code for my booking?"),
     "e3x": ("Scarf", "My scarf is in the stalls cloakroom, the peg tag is {}.",
             "My scarf is now in the circle cloakroom, the peg tag is {}.",
             "What is the peg tag code for my scarf now?"),
     "e3y": ("Bike", "My bike is on the side rail, the rail tag is {}.",
             "My bike is now on the rear rail, the rail tag is {}.", "What is the rail tag code for my bike now?"),
     "e": (("interval drinks code", "My interval drinks code is {}.",
            "My interval drinks code is {} now, the bar reset it.", "What is my interval drinks code now?"),
           ("row", "My row is number {}.", "My row is changed to number {} after the swap.",
            "What is my row number now?")),
     "f": "cast meet pass code"},
)
PAIRS = ("a3x", "a3y")
UPDATES = ("e3x", "e3y")


def codes(i):
    w = lambda name, base: f"{name}-{base + i}"  # noqa: E731
    # code families unused by the stable briefs and by every earlier set (r6 / r8 / r10 / r12 / r13), test-checked
    return {"a3x_old": w("Cobalt", 23), "a3x_new": w("Nickel", 54), "a3y_old": w("Zinc", 17), "a3y_new": w("Argon", 65),
            "e3x_old": w("Neon", 28), "e3x_new": w("Xenon", 59), "e3y_old": w("Boron", 14), "e3y_new": w("Radon", 47),
            "e1_old": w("Helium", 32), "e1_new": w("Carbon", 69), "e2_old": w("Iodine", 25),
            "e2_new": w("Sodium", 76), "f": w("Osmium", 88)}


def r14_facts(i):
    t, c = TOPICS[i], codes(i)
    note, fit, ack = t["note"], r8._fit, r8._ack
    (e1s, e1o, e1u, e1q), (e2s, e2o, e2u, e2q) = t["e"]

    def old(p):
        return fit(t[p][1].format(c[f"{p}_old"]) + note, ack(t[p][0], c[f"{p}_old"]), OLD_TARGET)

    def new(p):
        return fit(t[p][2].format(c[f"{p}_new"]) + note, ack(t[p][0], c[f"{p}_new"]), NEW_TARGET)

    def upd(s, txt, v, target):
        return fit(txt.format(v) + note, ack(s.capitalize(), v), target)

    old_facts = (old("a3x"), upd(e1s, e1o, c["e1_old"], OLD_TARGET), old("e3x"),
                 fit(f"My {t['f']} is {c['f']}." + note, ack(t["f"].capitalize(), c["f"]), OLD_TARGET),
                 old("a3y"), old("e3y"), upd(e2s, e2o, c["e2_old"], OLD_TARGET))
    new_facts = (new("a3x"), upd(e1s, e1u, c["e1_new"], NEW_TARGET), new("e3x"), new("a3y"), new("e3y"),
                 upd(e2s, e2u, c["e2_new"], NEW_TARGET))
    code = " Please give just the code."
    by_key = {"e1": Probe("e1", e1q + code, c["e1_new"], (c["e1_old"],)),
              "e2": Probe("e2", e2q + code, c["e2_new"], (c["e2_old"],)),
              "f": Probe("f", f"What is my {t['f']}?" + code, c["f"])}
    for p in UPDATES:
        by_key[p] = Probe(p, t[p][3] + code, c[f"{p}_new"], (c[f"{p}_old"],))
    for p in PAIRS:
        by_key[f"{p}_old"] = Probe(f"{p}_old", t[p][3] + code, c[f"{p}_old"])
        by_key[f"{p}_new"] = Probe(f"{p}_new", t[p][4] + code, c[f"{p}_new"])
    return sum(old_facts, ()), sum(new_facts, ()), tuple(by_key[k] for k in PROBE_ORDER)


def r14_suite():
    out = []
    for i, s in enumerate(R2_SUITE()):
        old, new, probes = r14_facts(i)
        chat = old + r8.noise(TOPICS[i], 2 * r8.NOISE_PAIRS) + new
        out.append(dataclasses.replace(s, name=R14_NAMES[i], chat=chat, probes=probes))
    return tuple(out)


def statements(i):
    """Every r14 fact statement of scenario i, by role: the asker text the supersession rule reads."""
    t, c = TOPICS[i], codes(i)
    out = {}
    for p in PAIRS + UPDATES:
        out[f"{p}_old"] = t[p][1].format(c[f"{p}_old"])
        out[f"{p}_new"] = t[p][2].format(c[f"{p}_new"])
    for k, (s, o, u, _) in zip(("e1", "e2"), t["e"]):
        out[f"{k}_old"] = o.format(c[f"{k}_old"])
        out[f"{k}_new"] = u.format(c[f"{k}_new"])
    out["f"] = f"My {t['f']} is {c['f']}."
    return out


def keys(texts):
    """r14 attribute, r13 (v2) attribute, offline V attribute and cue flag of every statement; None = no slot."""
    out = {}
    for role, txt in texts.items():
        ex = [f"user: {txt}"]
        a, v2, vs = (m._attribute(ex) for m in (brief_compress, brief_compress_r13, brief_compress_vshape))
        out[role] = {"r14": list(a) if a else None, "v2": list(v2) if v2 else None, "V": list(vs) if vs else None,
                     "cue": brief_compress._cue(ex)}
    return out


SETS = {**r10e.SETS, "r12": r12e.r12_suite, "r13": r13e.r13_suite, "r14": r14_suite}
_EXTRA = {"r12": r12e.r12_suite, "r13": r13e.r13_suite, "r14": r14_suite}


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
    comps = {**COMPRESS, **OFFLINE}
    for which in PROBE_SETS:
        for s in SETS[which]():
            brief = r2.chat_brief(s)
            comp = {a: comps[a](brief) for a in arms}
            for p in s.probes:
                out[f"{s.name}/{p.key}"] = {a: p.current in comp[a] for a in arms}
    return dict(sorted(out.items()))


def precheck():
    """Deterministic, no model. Per brief: sizes, planted values held by r4 / r13 / r14 / V; checks: main r14 == r4
    byte for byte; recency / r8 / r10 / r12 r14 == r13 byte for byte; per gated stale item r14 holds the current value
    and no superseded one; per gated old-fact item r14 holds the value wherever r4 does. `stop` lists every failed
    check on an EXISTING set (PREREG STOP rule); `new_set_failures` the same on r14 (scored, not a stop)."""
    out, stop, new_fail = {"briefs": {}, "stop": [], "new_set_failures": []}, [], []
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
            if which in SAME_AS_R13:
                row["r14_eq_r13"] = comp["r14"] == comp["r13"]
                if not row["r14_eq_r13"]:
                    fails.append(f"{which}/{s.name}: r14 != r13")
            for k in STALE_KEYS.get(which, ()):
                p = next(q for q in s.probes if q.key == k)
                if p.current not in comp["r14"] or any(v in comp["r14"] for v in p.superseded):
                    fails.append(f"{which}/{s.name}/{k}: stale fix lost")
            for k in OLD_KEYS.get(which, ()):
                p = next(q for q in s.probes if q.key == k)
                if p.current in comp["r4"] and p.current not in comp["r14"]:
                    fails.append(f"{which}/{s.name}/{k}: old fact dropped")
            row["fails"] = fails
            (new_fail if which == "r14" else stop).extend(fails)
            if which in ("r13", "r14"):
                row["keys"] = keys((r13e if which == "r13" else sys.modules[__name__]).statements(i))
            out["briefs"][f"{which}/{s.name}"] = row
    out["stop"], out["new_set_failures"] = stop, new_fail
    return out


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv == ["--precheck"]:
        print(json.dumps(precheck(), indent=1))
        return 0
    if argv == ["--presence"]:
        print(json.dumps(presence(), indent=1))
        return 0
    if argv == ["--presence-v"]:  # report-only: the offline V brief too
        print(json.dumps(presence(PRESENCE_ARMS + ("V",)), indent=1))
        return 0
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=tuple(SETS), required=True)
    _, rest = ap.parse_known_args(argv)
    if "--meta" in rest:
        k = argv.index("--meta") + 1
        argv[k] = json.dumps({**json.loads(argv[k]), "r14_harness": HARNESS})
    # process-wide for the run: r10e.install binds r2.COMPRESS = r10e.COMPRESS and r2.suite = r10e.SETS[set]
    r10e.COMPRESS = COMPRESS
    r10e.SETS.update(_EXTRA)
    return r10e.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
