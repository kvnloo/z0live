"""C1-ctx-compress-r13: attribute key v2 for the cue-gated supersession (frozen before scoring).

  LLAMA_SERVER=.../llama-server python ctx_compress_r13_eval.py --set main --gguf m.gguf --blocks B,A,B,A,O ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r13_eval.py --set r13  --gguf m.gguf --blocks B,A,B,A,O,R ...
  LLAMA_SERVER=.../llama-server python ctx_compress_r13_eval.py --set r12  --gguf m.gguf --blocks B,A,B,A ...
  python ctx_compress_r13_eval.py --set r13 --dry
  python ctx_compress_r13_eval.py --presence
  python ctx_compress_r13_eval.py --precheck

ONE production change: brief_compress._attribute is (slot subject, the first two non-FILLER predicate words, a digit
word ending the key as VALUE) = attribute key v2 (r11: the head word alone). Runs the frozen ctx_compress_r10_eval.main()
(which runs the frozen r2 harness: transport, blocks, deadline, JSON layout) with:
  A = bench/brief_compress_r4.py (r4 byte copy, baseline), B = brief_compress.py (production r13),
  R = bench/brief_compress_r11.py (r11 byte copy, report-only rival), O = full brief (report-only).
Sets: main (r2 suite), recency (r6), r8, r10 (r10e.SETS, unchanged), r12 (ctx_compress_r12_eval.r12_suite, the r12
held-out set, now dev), and NEW:
  --set r13  r13_suite(): 3 NEW scenarios (marina, hotel, festival; stable brief and call turns are the r2 scenarios'
             own, as r8 / r10 / r12) with topic noise and, per scenario, ten probes:
               (a2) SAMEHEAD  2 pairs: same predicate head word, different second word, different attribute (older
                              "my <s> is <head> <w1> ... X", newer "my <s> is now <head> <w2> ... new ... Y"); r11
                              merges them (heads coincide), v2 keys differ. a2x_old a2y_old gated, *_new report-only.
               (a3) LIMIT     same head AND same second word, different attribute: the v2 keys coincide, so r13 is
                              predicted to drop the older fact (known v2 limit). a3_old, a3_new report-only.
               (e)  SAME      2 genuine cued updates, value restated with different words after it or a label noun
                              before it ("is X." / "is changed to X ..." -> (VALUE,); "is number X" / "is now number Y"
                              -> (number, VALUE)): stale fix must hold. e1 e2 gated.
               (e3) LIMIT     genuine cued update with the same head and a different second word ("is on the north
                              jetty, tag X" / "is now on the south jetty, tag Y"): v2 keys differ, so r13 is predicted
                              to keep the stale value (known v2 limit, r11 fixes it). Report-only.
               (f)  UNIQ      one old unique fact, never superseded. Gated.
             Every older fact is shorter than every noise exchange and every newer one longer (as r8 / r10 / r12).
             Scored by ctx_compress_r6_eval.score_recency (unchanged).
presence(): for every recency / r8 / r10 / r12 / r13 probe, whether its planted value (probe.current) is a substring
of the A and B compressed briefs. Deterministic, no model.
precheck(): r4 / r11 / r13 on every brief of every set at 2810: planted values each brief holds, r13 == r4 on main,
the stale-fix and no-false-merge checks per item (the STOP rule of the r13 PREREG), and the v2 / r11 keys of every
r12 and r13 statement.
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
import brief_compress_r11  # noqa: E402
import ctx_compress_r2_eval as r2  # noqa: E402
import ctx_compress_r8_eval as r8  # noqa: E402
import ctx_compress_r10_eval as r10e  # noqa: E402
import ctx_compress_r12_eval as r12e  # noqa: E402
from context_eval import Probe  # noqa: E402

BUDGET = r10e.BUDGET
HARNESS = "ctx-compress-r13"
R2_SUITE = r10e.R2_SUITE
COMPRESS = {"A": lambda b: brief_compress_r4.compress_brief(b, BUDGET),
            "B": lambda b: brief_compress.compress_brief(b, BUDGET),
            "R": lambda b: brief_compress_r11.compress_brief(b, BUDGET),
            "O": lambda b: b}
PRECHECK = {"r4": brief_compress_r4, "r11": brief_compress_r11, "r13": brief_compress}
R13_NAMES = ["r13-marina", "r13-hotel", "r13-festival"]
PROBE_ORDER = ("a2x_old", "a2y_old", "e1", "e2", "f", "a3_old", "e3", "a2x_new", "a2y_new", "a3_new")
OLD_TARGET, NEW_TARGET = r12e.OLD_TARGET, r8.NEW_TARGET  # noise exchanges sit between (test_ctx_compress_r13 checks)
PRESENCE_ARMS = ("A", "B")
PROBE_SETS = ("recency", "r8", "r10", "r12", "r13")
# Per set: gated stale-update probes (current must be present and every superseded value absent in r13's brief) and
# gated old-fact probes (planted value present in r13's brief wherever r4 keeps it). Report-only items are not here.
STALE_KEYS = {"recency": ("bench", "badge"), "r8": ("a1", "a2"), "r10": ("a1", "a2"), "r12": ("e1", "e2"),
              "r13": ("e1", "e2")}
OLD_KEYS = {"recency": ("glaze",), "r8": ("c_old", "d"), "r10": ("e1_old", "e2_old", "d"),
            "r12": ("a_old", "b_old", "c_old", "d_old", "f"), "r13": ("a2x_old", "a2y_old", "f")}
EXISTING = ("recency", "r8", "r10", "r12")  # a precheck failure on these STOPs the experiment (PREREG)

# per pair: (ack label, older statement, newer statement, older question, newer question)
TOPICS = (
    {"topic": "marina", "noun": "Dock", "items": ("buoys", "ropes", "fender", "cleat", "rudder", "tiller", "anchor"),
     "note": " I wrote it on the board at the harbour office, next to the tide table, so the staff know.",
     "a2x": ("Boat", "My boat is moored at the east pontoon, the berth tag is {}.",
             "My boat is now moored for the new winter season, the storage ref is {}.",
             "What is the east pontoon berth tag code for my boat?", "What is the winter storage ref code for my boat?"),
     "a2y": ("Dinghy", "My dinghy is stored in the boat shed, the rack tag is {}.",
             "My dinghy is now stored on the new trailer park, the trailer plate is {}.",
             "What is the boat shed rack tag code for my dinghy?", "What is the trailer plate code for my dinghy?"),
     "a3": ("Paddleboard", "My paddleboard is booked for the harbour tour, the tour ref is {}.",
            "My paddleboard is now booked for the new repair slot, the repair ticket is {}.",
            "What is the harbour tour ref code for my paddleboard?",
            "What is the repair ticket code for my paddleboard?"),
     "e": (("mooring fee ref", "My mooring fee ref is {}.",
            "My mooring fee ref is changed to {} from this month, the office said.", "What is my mooring fee ref now?"),
           ("fuel card", "My fuel card is number {}.", "My fuel card is now number {}, the last one expired.",
            "What is my fuel card number now?")),
     "e3": ("tender", "My tender is on the north jetty, the jetty tag is {}.",
            "My tender is now on the south jetty, the jetty tag is {}.", "What is the jetty tag code for my tender now?"),
     "f": "harbour gate fob code"},
    {"topic": "hotel", "noun": "Room", "items": ("pillow", "duvet", "minibar", "robe", "slipper", "lamp", "blind"),
     "note": " I wrote it on the card at the front desk, next to the breakfast menu, so the staff know.",
     "a2x": ("Suitcase", "My suitcase is left at the luggage room, the claim tag is {}.",
             "My suitcase is now left with the new porter for the airport shuttle, the shuttle ticket is {}.",
             "What is the luggage room claim tag code for my suitcase?",
             "What is the airport shuttle ticket code for my suitcase?"),
     "a2y": ("Rental car", "My rental car is parked in the basement garage, the bay ticket is {}.",
             "My rental car is now parked for the new valet service, the valet tag is {}.",
             "What is the basement garage bay ticket code for my rental car?",
             "What is the valet tag code for my rental car?"),
     "a3": ("Room key", "My room key is linked with the spa booking, the spa ref is {}.",
            "My room key is now linked with the new breakfast plan, the plan code is {}.",
            "What is the spa booking ref code for my room key?", "What is the breakfast plan code for my room key?"),
     "e": (("wifi code", "My wifi code is {}.", "My wifi code is {} now, reception reset the router.",
            "What is my wifi code now?"),
           ("room", "My room is number {}.", "My room is changed to number {} after the leak, the manager said.",
            "What is my room number now?")),
     "e3": ("bike", "My bike is in the front rack, the rack tag is {}.",
            "My bike is now in the back rack, the rack tag is {}.", "What is the rack tag code for my bike now?"),
     "f": "minibar card code"},
    {"topic": "festival", "noun": "Stage", "items": ("flags", "lights", "banner", "speaker", "drums", "screen", "tent"),
     "note": " I wrote it on the sheet at the info point, next to the stage map, so the stewards know.",
     "a2x": ("Camper van", "My camper van is parked by the north gate, the parking pass is {}.",
             "My camper van is now parked for the new early exit, the exit pass is {}.",
             "What is the north gate parking pass code for my camper van?",
             "What is the early exit pass code for my camper van?"),
     "a2y": ("Wristband", "My wristband is linked to the weekend ticket, the ticket ref is {}.",
             "My wristband is now linked with the new bar tab, the tab code is {}.",
             "What is the weekend ticket ref code for my wristband?", "What is the bar tab code for my wristband?"),
     "a3": ("Friend group", "My friend group is registered for the main stage area, the group ref is {}.",
            "My friend group is now registered for the new pottery workshop, the workshop code is {}.",
            "What is the main stage area group ref code for my friend group?",
            "What is the pottery workshop code for my friend group?"),
     "e": (("locker code", "My locker code is {}.", "My locker code is {} now, the stewards reset them.",
            "What is my locker code now?"),
           ("shuttle seat", "My shuttle seat is number {}.", "My shuttle seat is switched to number {} for the late bus.",
            "What is my shuttle seat number now?")),
     "e3": ("flag", "My flag is on the left pole, the pole tag is {}.",
            "My flag is now on the right pole, the pole tag is {}.", "What is the pole tag code for my flag now?"),
     "f": "first aid badge code"},
)
PAIRS = ("a2x", "a2y", "a3")


def codes(i):
    w = lambda name, base: f"{name}-{base + i}"  # noqa: E731
    # code families unused by the stable briefs and by every earlier set (r6 / r8 / r10 / r12), test-checked
    return {"a2x_old": w("Vega", 22), "a2x_new": w("Rigel", 53), "a2y_old": w("Deneb", 16), "a2y_new": w("Altair", 64),
            "a3_old": w("Sirius", 27), "a3_new": w("Castor", 58), "e1_old": w("Lyra", 13), "e1_new": w("Orion", 46),
            "e2_old": w("Spica", 31), "e2_new": w("Antares", 68), "e3_old": w("Pollux", 24), "e3_new": w("Mira", 75),
            "f": w("Polaris", 87)}


def r13_facts(i):
    t, c = TOPICS[i], codes(i)
    note, fit, ack = t["note"], r8._fit, r8._ack
    (e1s, e1o, e1u, e1q), (e2s, e2o, e2u, e2q) = t["e"]
    e3s, e3o, e3u, e3q = t["e3"]

    def old(p):
        lab, txt = t[p][0], t[p][1]
        return fit(txt.format(c[f"{p}_old"]) + note, ack(lab, c[f"{p}_old"]), OLD_TARGET)

    def new(p):
        lab, txt = t[p][0], t[p][2]
        return fit(txt.format(c[f"{p}_new"]) + note, ack(lab, c[f"{p}_new"]), NEW_TARGET)

    def upd(s, txt, v, target):
        return fit(txt.format(v) + note, ack(s.capitalize(), v), target)

    old_facts = (old("a2x"), upd(e1s, e1o, c["e1_old"], OLD_TARGET), old("a3"),
                 upd(e3s, e3o, c["e3_old"], OLD_TARGET),
                 fit(f"My {t['f']} is {c['f']}." + note, ack(t["f"].capitalize(), c["f"]), OLD_TARGET),
                 old("a2y"), upd(e2s, e2o, c["e2_old"], OLD_TARGET))
    new_facts = (new("a2x"), upd(e1s, e1u, c["e1_new"], NEW_TARGET), new("a3"), upd(e3s, e3u, c["e3_new"], NEW_TARGET),
                 new("a2y"), upd(e2s, e2u, c["e2_new"], NEW_TARGET))
    code = " Please give just the code."
    by_key = {"e1": Probe("e1", e1q + code, c["e1_new"], (c["e1_old"],)),
              "e2": Probe("e2", e2q + code, c["e2_new"], (c["e2_old"],)),
              "e3": Probe("e3", e3q + code, c["e3_new"], (c["e3_old"],)),
              "f": Probe("f", f"What is my {t['f']}?" + code, c["f"])}
    for p in PAIRS:
        by_key[f"{p}_old"] = Probe(f"{p}_old", t[p][3] + code, c[f"{p}_old"])
        by_key[f"{p}_new"] = Probe(f"{p}_new", t[p][4] + code, c[f"{p}_new"])
    return sum(old_facts, ()), sum(new_facts, ()), tuple(by_key[k] for k in PROBE_ORDER)


def r13_suite():
    out = []
    for i, s in enumerate(R2_SUITE()):
        old, new, probes = r13_facts(i)
        chat = old + r8.noise(TOPICS[i], 2 * r8.NOISE_PAIRS) + new
        out.append(dataclasses.replace(s, name=R13_NAMES[i], chat=chat, probes=probes))
    return tuple(out)


def statements(i):
    """Every r13 fact statement of scenario i, by role: the asker text the supersession rule reads."""
    t, c = TOPICS[i], codes(i)
    out = {}
    for p in PAIRS:
        out[f"{p}_old"] = t[p][1].format(c[f"{p}_old"])
        out[f"{p}_new"] = t[p][2].format(c[f"{p}_new"])
    for k, (s, o, u, _) in zip(("e1", "e2"), t["e"]):
        out[f"{k}_old"] = o.format(c[f"{k}_old"])
        out[f"{k}_new"] = u.format(c[f"{k}_new"])
    out["e3_old"] = t["e3"][1].format(c["e3_old"])
    out["e3_new"] = t["e3"][2].format(c["e3_new"])
    out["f"] = f"My {t['f']} is {c['f']}."
    return out


def keys(texts):
    """v2 attribute (r13), r11 attribute and cue flag of every statement; None = no slot."""
    out = {}
    for role, txt in texts.items():
        ex = [f"user: {txt}"]
        v2, h = brief_compress._attribute(ex), brief_compress_r11._attribute(ex)
        out[role] = {"v2": list(v2) if v2 else None, "r11": list(h) if h else None, "cue": brief_compress._cue(ex)}
    return out


SETS = {**r10e.SETS, "r12": r12e.r12_suite, "r13": r13_suite}


class _arms:
    """r10e.dry / install read the module globals r10e.COMPRESS / r10e.SETS; set them for the call and restore."""
    def __enter__(self):
        self.saved = r10e.COMPRESS, dict(r10e.SETS)
        r10e.COMPRESS = COMPRESS
        r10e.SETS.update({"r12": r12e.r12_suite, "r13": r13_suite})

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
    """Deterministic, no model. Per brief: sizes, planted values held by r4 / r11 / r13; checks: main r13 == r4 byte
    for byte; per gated stale item r13 holds the current value and no superseded one; per gated old-fact item r13
    holds the value wherever r4 does. `stop` lists every failed check on an EXISTING set (PREREG STOP rule);
    `new_set_failures` the same on r13 (scored, not a stop)."""
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
                row["r13_eq_r4"] = comp["r13"] == comp["r4"]
                if not row["r13_eq_r4"]:
                    fails.append(f"main/{s.name}: r13 != r4")
            for k in STALE_KEYS.get(which, ()):
                p = next(q for q in s.probes if q.key == k)
                if p.current not in comp["r13"] or any(v in comp["r13"] for v in p.superseded):
                    fails.append(f"{which}/{s.name}/{k}: stale fix lost")
            for k in OLD_KEYS.get(which, ()):
                p = next(q for q in s.probes if q.key == k)
                if p.current in comp["r4"] and p.current not in comp["r13"]:
                    fails.append(f"{which}/{s.name}/{k}: old fact dropped")
            row["fails"] = fails
            (new_fail if which == "r13" else stop).extend(fails)
            if which in ("r12", "r13"):
                row["keys"] = keys((r12e if which == "r12" else sys.modules[__name__]).statements(i))
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
    ap = argparse.ArgumentParser()
    ap.add_argument("--set", choices=tuple(SETS), required=True)
    _, rest = ap.parse_known_args(argv)
    if "--meta" in rest:
        k = argv.index("--meta") + 1
        argv[k] = json.dumps({**json.loads(argv[k]), "r13_harness": HARNESS})
    # process-wide for the run: r10e.install binds r2.COMPRESS = r10e.COMPRESS and r2.suite = r10e.SETS[set]
    r10e.COMPRESS = COMPRESS
    r10e.SETS.update({"r12": r12e.r12_suite, "r13": r13_suite})
    return r10e.main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
